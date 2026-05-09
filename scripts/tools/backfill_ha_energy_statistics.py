import argparse
import shutil
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_SOURCE_DB = Path("data/homeassistant.db")
DEFAULT_HA_DB = Path("/home/pi/ha/config/home-assistant_v2.db")


@dataclass(frozen=True)
class DailyEnergyRow:
    day: date
    usage: float
    charge: float


@dataclass(frozen=True)
class StatisticPoint:
    start_ts: float
    state: float
    sum: float


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill Home Assistant recorder long-term statistics from ha-95598 daily data."
    )
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="ha-95598 SQLite database path.")
    parser.add_argument("--ha-db", default=str(DEFAULT_HA_DB), help="Home Assistant recorder SQLite database path.")
    parser.add_argument("--user-id", required=True, help="95598 user id stored in ha-95598 SQLite.")
    parser.add_argument(
        "--usage-statistic-id",
        required=True,
        help="Home Assistant statistic_id for total electricity usage.",
    )
    parser.add_argument(
        "--charge-statistic-id",
        required=True,
        help="Home Assistant statistic_id for total electricity cost.",
    )
    parser.add_argument("--timezone", default="Asia/Shanghai", help="Timezone used by Home Assistant.")
    parser.add_argument("--apply", action="store_true", help="Actually write Home Assistant DB.")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a DB backup before writing.")
    return parser.parse_args()


def load_daily_rows(source_db: Path, user_id: str) -> list[DailyEnergyRow]:
    conn = sqlite3.connect(source_db)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date, total_usage, COALESCE(total_charge, 0)
            FROM daily_usage
            WHERE user_id = ?
            ORDER BY date
            """,
            (user_id,),
        )
        rows = [
            DailyEnergyRow(
                day=datetime.strptime(row[0], "%Y-%m-%d").date(),
                usage=round(float(row[1] or 0), 2),
                charge=round(float(row[2] or 0), 2),
            )
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(f"No daily rows found in {source_db} for user_id={user_id}")
    return rows


def build_daily_boundary_points(rows: list[DailyEnergyRow], tz: ZoneInfo, value_attr: str) -> list[StatisticPoint]:
    points = []
    cumulative = 0.0
    current = rows[0].day
    end = rows[-1].day + timedelta(days=1)
    by_day = {row.day: float(getattr(row, value_attr)) for row in rows}

    while current <= end:
        points.append(
            StatisticPoint(
                start_ts=local_midnight_ts(current, tz),
                state=round(cumulative, 2),
                sum=round(cumulative, 2),
            )
        )
        cumulative += by_day.get(current, 0.0)
        current += timedelta(days=1)
    return points


def local_midnight_ts(day: date, tz: ZoneInfo) -> float:
    return datetime.combine(day, time.min, tzinfo=tz).timestamp()


def get_metadata_id(conn: sqlite3.Connection, statistic_id: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM statistics_meta WHERE statistic_id = ?", (statistic_id,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"statistics_meta not found: {statistic_id}")
        return int(row[0])
    finally:
        cursor.close()


def backup_db(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak.{timestamp}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def upsert_statistics_points(conn: sqlite3.Connection, metadata_id: int, points: list[StatisticPoint]) -> None:
    conn.executemany(
        """
        INSERT INTO statistics (
            created, created_ts, metadata_id, start, start_ts,
            mean, mean_weight, min, max, last_reset, last_reset_ts, state, sum
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(metadata_id, start_ts) DO UPDATE SET
            state = excluded.state,
            sum = excluded.sum
        """,
        [
            (
                None,
                point.start_ts,
                metadata_id,
                None,
                point.start_ts,
                point.state,
                point.sum,
            )
            for point in points
        ],
    )


def update_existing_statistics_rows(
    conn: sqlite3.Connection,
    table: str,
    metadata_id: int,
    points: list[StatisticPoint],
) -> int:
    first_ts = points[0].start_ts
    last_point = points[-1]
    point_timestamps = [point.start_ts for point in points]
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            SELECT id, start_ts
            FROM {table}
            WHERE metadata_id = ? AND start_ts >= ? AND start_ts <= ?
            ORDER BY start_ts
            """,
            (metadata_id, first_ts, last_point.start_ts),
        )
        rows = cursor.fetchall()
        updates = []
        for row_id, start_ts in rows:
            ts = float(start_ts)
            point_index = bisect_right(point_timestamps, ts) - 1
            if point_index < 0:
                continue
            point = points[point_index] if point_index < len(points) else last_point
            updates.append((point.state, point.sum, row_id))
        cursor.executemany(f"UPDATE {table} SET state = ?, sum = ? WHERE id = ?", updates)
        return len(updates)
    finally:
        cursor.close()


def normalize_sum_to_state(conn: sqlite3.Connection, metadata_id: int) -> dict[str, int]:
    results = {}
    for table in ("statistics", "statistics_short_term"):
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                UPDATE {table}
                SET sum = state
                WHERE metadata_id = ?
                  AND state IS NOT NULL
                  AND (sum IS NULL OR ABS(sum - state) > 0.000001)
                """,
                (metadata_id,),
            )
            results[table] = cursor.rowcount
        finally:
            cursor.close()
    return results


def backfill_one_statistic(conn: sqlite3.Connection, statistic_id: str, points: list[StatisticPoint]) -> dict:
    metadata_id = get_metadata_id(conn, statistic_id)
    upsert_statistics_points(conn, metadata_id, points)
    updated_statistics = update_existing_statistics_rows(conn, "statistics", metadata_id, points)
    updated_short_term = update_existing_statistics_rows(conn, "statistics_short_term", metadata_id, points)
    return {
        "statistic_id": statistic_id,
        "metadata_id": metadata_id,
        "boundary_points": len(points),
        "updated_statistics_rows": updated_statistics,
        "updated_short_term_rows": updated_short_term,
        "first": points[0].sum,
        "last": points[-1].sum,
    }


def main():
    args = parse_args()
    source_db = Path(args.source_db)
    ha_db = Path(args.ha_db)
    tz = ZoneInfo(args.timezone)

    daily_rows = load_daily_rows(source_db, args.user_id)
    usage_points = build_daily_boundary_points(daily_rows, tz, "usage")
    charge_points = build_daily_boundary_points(daily_rows, tz, "charge")

    print(
        f"source rows={len(daily_rows)} range={daily_rows[0].day}~{daily_rows[-1].day} "
        f"usage_total={usage_points[-1].sum:.2f} charge_total={charge_points[-1].sum:.2f}"
    )
    if not args.apply:
        print("dry-run only; pass --apply to write Home Assistant statistics")
        return

    if not args.no_backup:
        print(f"backup={backup_db(ha_db)}")

    conn = sqlite3.connect(ha_db, timeout=30)
    try:
        usage_result = backfill_one_statistic(conn, args.usage_statistic_id, usage_points)
        charge_result = backfill_one_statistic(conn, args.charge_statistic_id, charge_points)
        usage_result["normalized"] = normalize_sum_to_state(conn, usage_result["metadata_id"])
        charge_result["normalized"] = normalize_sum_to_state(conn, charge_result["metadata_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"usage={usage_result}")
    print(f"charge={charge_result}")


if __name__ == "__main__":
    main()
