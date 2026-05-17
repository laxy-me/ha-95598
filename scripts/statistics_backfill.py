"""Idempotent backfill of HA long-term statistics for the two total
cumulative sensors (electricity usage + charge).

HA's energy dashboard slices "this month / this year" by diffing the
sum column of the recorder's long-term statistics table. Until the
add-on was first installed, HA had no statistics for our sensors, so
the dashboard's year-view sees only the slice from install time
onward and underreports.

This module asks HA (via the WebSocket recorder API) which hour-aligned
start_ts entries already exist for each sensor, then for every month
in our SQLite ``monthly_usage`` table that doesn't already have an
entry at its month-end 23:00 boundary, ``import_statistics`` is called
with a (state, sum) snapshot equal to the running cumulative through
end-of-that-month. A zero anchor is also placed at the end of the month
before our earliest data so the year-view's "year start" lookup lands
on a real value.

Idempotent: re-running won't write the same start_ts twice — already
present snapshots are skipped, untouched.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import websocket  # python3 websocket-client


DB_PATH = Path("/app/data/homeassistant.db")
TZ = ZoneInfo("Asia/Shanghai")

USAGE_SENSOR = "sensor.95598_8711_zong_yong_dian_liang"
CHARGE_SENSOR = "sensor.95598_8711_zong_dian_fei"


def _ws_connect() -> websocket.WebSocket:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN not set; cannot reach HA core WS")
    ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=15)
    ws.recv()  # auth_required hello
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth = json.loads(ws.recv())
    if auth.get("type") != "auth_ok":
        raise RuntimeError(f"HA WS auth failed: {auth}")
    return ws


def _ws_call(ws: websocket.WebSocket, msg_id: int, payload: dict) -> dict:
    ws.send(json.dumps(dict(payload, id=msg_id)))
    while True:
        resp = json.loads(ws.recv())
        if resp.get("id") == msg_id:
            return resp


def _load_monthly_snapshots(user_id: str) -> list[tuple[str, float, float]]:
    """Return [(month_key, total_usage, total_charge), ...] sorted by month."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT month, total_usage, COALESCE(total_charge, 0) "
            "FROM monthly_usage WHERE user_id = ? ORDER BY month",
            (user_id,),
        )
        return [
            (row[0], float(row[1] or 0), float(row[2] or 0))
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def _load_daily_snapshots(user_id: str) -> list[tuple[str, float, float]]:
    """Return [(date_str, total_usage, total_charge), ...] sorted ascending."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT date, total_usage, COALESCE(total_charge, 0) "
            "FROM daily_usage WHERE user_id = ? ORDER BY date",
            (user_id,),
        )
        return [
            (row[0], float(row[1] or 0), float(row[2] or 0))
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def _contiguous_current_month_dailies(
    daily_rows: list[tuple[str, float, float]],
    current_month: str,
) -> list[tuple[date, float, float]]:
    """Return day rows for ``current_month`` only if they form a
    contiguous run starting at ``YYYY-MM-01``. Otherwise return []."""
    current = []
    expected_first = f"{current_month}-01"
    for date_str, day_u, day_c in daily_rows:
        if not date_str.startswith(current_month + "-"):
            continue
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        current.append((d, day_u, day_c))
    if not current:
        return []
    current.sort(key=lambda r: r[0])
    if current[0][0].isoformat() != expected_first:
        return []
    cleaned = [current[0]]
    for entry in current[1:]:
        if (entry[0] - cleaned[-1][0]).days != 1:
            break
        cleaned.append(entry)
    return cleaned


def _resolve_user_id() -> Optional[str]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM monthly_usage LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _month_end_dt(month_key: str) -> datetime:
    """23:00 (local) of the last calendar day of the given YYYY-MM."""
    year, month = (int(part) for part in month_key.split("-"))
    if month == 12:
        first_of_next = datetime(year + 1, 1, 1, tzinfo=TZ)
    else:
        first_of_next = datetime(year, month + 1, 1, tzinfo=TZ)
    last_day = first_of_next - timedelta(days=1)
    return last_day.replace(hour=23, minute=0, second=0, microsecond=0)


def _previous_month_key(month_key: str) -> str:
    year, month = (int(part) for part in month_key.split("-"))
    month -= 1
    if month == 0:
        month = 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def _build_full_series(
    monthly_rows: list[tuple[str, float, float]],
    daily_rows: list[tuple[str, float, float]],
) -> tuple[
    list[tuple[datetime, float, float]],
    list[tuple[datetime, float, float]],
    dict,
]:
    """Build the usage + charge statistics series and a small diag dict.

    Series content (state == sum at every point):

      1. zero anchor at the end of the month before the earliest
         month in ``monthly_rows``;
      2. one row at 23:00 (local) of each completed month's last day,
         with cumulative through that month (drawn from monthly_rows);
      3. for the *current* month, if ``daily_rows`` contains a
         contiguous run starting at YYYY-MM-01, one row at 23:00 of
         each such day with cumulative = (cumulative through end of
         previous month) + (daily sum up to and including that day).

    The current month's *month-end anchor* is intentionally omitted —
    its monthly_usage row keeps growing through the live month, so a
    fixed snapshot at YYYY-MM-31 23:00 would either land in the future
    (and collide with HA's auto-record) or lock in a stale state.
    """
    diag = {
        "current_month_daily_imported": 0,
        "current_month_daily_skipped_reason": None,
    }
    if not monthly_rows:
        return [], [], diag

    current_month = datetime.now(TZ).strftime("%Y-%m")
    completed = [row for row in monthly_rows if row[0] < current_month]
    if not completed:
        diag["current_month_daily_skipped_reason"] = "no completed months yet"
        return [], [], diag

    anchor_dt = _month_end_dt(_previous_month_key(completed[0][0]))
    usage: list[tuple[datetime, float, float]] = [(anchor_dt, 0.0, 0.0)]
    charge: list[tuple[datetime, float, float]] = [(anchor_dt, 0.0, 0.0)]

    cum_u = 0.0
    cum_c = 0.0
    cum_through: dict[str, tuple[float, float]] = {}
    for month_key, total_u, total_c in monthly_rows:
        cum_u += total_u
        cum_c += total_c
        cum_through[month_key] = (cum_u, cum_c)
        if month_key < current_month:
            end = _month_end_dt(month_key)
            usage.append((end, round(cum_u, 2), round(cum_u, 2)))
            charge.append((end, round(cum_c, 2), round(cum_c, 2)))

    # Current-month daily fill (only when contiguous from day 1).
    prev_month = _previous_month_key(current_month)
    start_cum = cum_through.get(prev_month, (0.0, 0.0))
    dailies = _contiguous_current_month_dailies(daily_rows, current_month)
    if not dailies:
        diag["current_month_daily_skipped_reason"] = (
            "no contiguous daily run starting at the 1st of the current month"
        )
    else:
        day_cum_u = start_cum[0]
        day_cum_c = start_cum[1]
        for d, day_u, day_c in dailies:
            day_cum_u += day_u
            day_cum_c += day_c
            end_dt = datetime.combine(d, time(23, 0), tzinfo=TZ)
            usage.append((end_dt, round(day_cum_u, 2), round(day_cum_u, 2)))
            charge.append((end_dt, round(day_cum_c, 2), round(day_cum_c, 2)))
        diag["current_month_daily_imported"] = len(dailies)
    return usage, charge, diag


def _existing_starts(
    ws: websocket.WebSocket,
    msg_id: int,
    statistic_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> set[int]:
    """Set of unix-epoch second-aligned start_ts already present in HA
    statistics within [start_dt, end_dt]."""
    resp = _ws_call(
        ws,
        msg_id,
        {
            "type": "recorder/statistics_during_period",
            "statistic_ids": [statistic_id],
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "period": "hour",
        },
    )
    if not resp.get("success"):
        raise RuntimeError(f"statistics_during_period failed: {resp}")
    rows = resp.get("result", {}).get(statistic_id, []) or []
    out: set[int] = set()
    for row in rows:
        # HA returns start as ISO8601 string. Normalize to unix int seconds.
        try:
            start_value = row.get("start")
            if isinstance(start_value, (int, float)):
                ts = int(start_value // 1000) if start_value > 10**12 else int(start_value)
            else:
                ts = int(datetime.fromisoformat(str(start_value)).timestamp())
            out.add(ts)
        except Exception:
            continue
    return out


def _import_points(
    ws: websocket.WebSocket,
    msg_id: int,
    statistic_id: str,
    unit: str,
    points: list[tuple[datetime, float, float]],
) -> dict:
    if not points:
        return {"success": True, "result": None, "skipped": True}
    return _ws_call(
        ws,
        msg_id,
        {
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "name": None,
                "source": "recorder",
                "statistic_id": statistic_id,
                "unit_of_measurement": unit,
            },
            "stats": [
                {"start": dt.isoformat(), "state": state, "sum": total}
                for dt, state, total in points
            ],
        },
    )


def _current_hour_dt(ts: Optional[float] = None) -> datetime:
    """The current hour rounded down (HA statistics are hour-aligned)."""
    base = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=TZ)
    return base.replace(minute=0, second=0, microsecond=0)


def _current_cumulative(
    monthly_rows: list[tuple[str, float, float]],
    daily_rows: list[tuple[str, float, float]],
) -> tuple[float, float]:
    """Cumulative usage / charge through the most recent day we have
    data for (= SUM of completed months + sum of contiguous dailies
    in the current month). When daily is non-contiguous or missing,
    falls back to the cumulative through the last monthly row."""
    if not monthly_rows:
        return 0.0, 0.0
    current_month = datetime.now(TZ).strftime("%Y-%m")
    cum_u = 0.0
    cum_c = 0.0
    cum_through: dict[str, tuple[float, float]] = {}
    for month_key, total_u, total_c in monthly_rows:
        cum_u += total_u
        cum_c += total_c
        cum_through[month_key] = (cum_u, cum_c)
    prev_month = _previous_month_key(current_month)
    start_cum = cum_through.get(prev_month, (0.0, 0.0))
    dailies = _contiguous_current_month_dailies(daily_rows, current_month)
    if dailies:
        day_cum_u = start_cum[0]
        day_cum_c = start_cum[1]
        for _, day_u, day_c in dailies:
            day_cum_u += day_u
            day_cum_c += day_c
        return round(day_cum_u, 2), round(day_cum_c, 2)
    return round(cum_u, 2), round(cum_c, 2)


def push_current_statistics() -> dict:
    """Idempotently overwrite a single hour-aligned statistics row
    (= current hour) for both cumulative sensors with fork's
    authoritative cumulative state.

    Why: HA's auto-recorder occasionally writes ``sum = 0`` for our
    total_increasing sensors at hour boundaries — its in-memory
    cumulative tracker re-initializes on add-on / HA restart and
    loses the baseline established by import_statistics. The
    energy dashboard's per-day delta then reads as a huge negative.

    Scheduling this at every hour ":30" (after HA's :00~:05 hourly
    write window) re-establishes the correct sum so HA's next
    record naturally continues from fork's value.

    Idempotent: re-running at the same hour overwrites the same
    start_ts with the same sum (a no-op for the data)."""
    logging.info("Statistics hourly push: starting")
    user_id = _resolve_user_id()
    if not user_id:
        return {"success": False, "error": "no monthly_usage rows"}
    monthly_rows = _load_monthly_snapshots(user_id)
    if not monthly_rows:
        return {"success": False, "error": "monthly_usage empty"}
    daily_rows = _load_daily_snapshots(user_id)
    cum_u, cum_c = _current_cumulative(monthly_rows, daily_rows)
    hour = _current_hour_dt()

    ws = _ws_connect()
    try:
        u = _import_points(ws, 30, USAGE_SENSOR, "kWh",
                           [(hour, cum_u, cum_u)])
        if not u.get("success", False):
            return {"success": False, "step": "import_usage", "error": u}
        c = _import_points(ws, 31, CHARGE_SENSOR, "CNY",
                           [(hour, cum_c, cum_c)])
        if not c.get("success", False):
            return {"success": False, "step": "import_charge", "error": c}
        logging.info(
            "Statistics hourly push: usage=%.2f kWh, charge=%.2f CNY at %s",
            cum_u, cum_c, hour.isoformat(),
        )
        return {
            "success": True,
            "hour": hour.isoformat(),
            "usage_state": cum_u,
            "charge_state": cum_c,
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass


def run_backfill(*, clear_first: bool = True) -> dict:
    """Public entrypoint. Reads monthly_usage, computes month-end
    snapshots through the previous month (the current month is left
    to HA's auto-recorder), optionally clears any existing statistics
    for the two cumulative sensors (to drop pollution from the early
    last_reset_value_template bug), then imports the snapshots.

    Idempotent in the sense that re-running with the same fork data
    produces the same final state in HA. ``clear_first=True`` is
    needed in practice because the add-on's first 24 hours wrote
    sum-column values that drift away from state; without clearing,
    energy-dashboard slicing across the install boundary mis-reports.
    """
    logging.info("Statistics backfill: starting (clear_first=%s)", clear_first)
    user_id = _resolve_user_id()
    if not user_id:
        return {"success": False, "error": "no monthly_usage rows in fork SQLite"}

    monthly_rows = _load_monthly_snapshots(user_id)
    if not monthly_rows:
        return {"success": False, "error": "monthly_usage empty"}
    daily_rows = _load_daily_snapshots(user_id)

    usage_points, charge_points, diag = _build_full_series(monthly_rows, daily_rows)
    if not usage_points:
        return {
            "success": False,
            "error": "no completed months yet — nothing to backfill",
        }

    ws = _ws_connect()
    try:
        cleared = False
        existing_usage: set[int] = set()
        existing_charge: set[int] = set()
        if clear_first:
            clear_resp = _ws_call(
                ws,
                5,
                {
                    "type": "recorder/clear_statistics",
                    "statistic_ids": [USAGE_SENSOR, CHARGE_SENSOR],
                },
            )
            if not clear_resp.get("success", False):
                return {
                    "success": False,
                    "step": "clear_statistics",
                    "error": clear_resp,
                }
            cleared = True
            logging.info("Statistics backfill: cleared existing rows for both sensors")
        else:
            span_start = usage_points[0][0] - timedelta(hours=1)
            span_end = usage_points[-1][0] + timedelta(hours=1)
            existing_usage = _existing_starts(ws, 10, USAGE_SENSOR, span_start, span_end)
            existing_charge = _existing_starts(ws, 11, CHARGE_SENSOR, span_start, span_end)

        usage_to_import = [
            p for p in usage_points if int(p[0].timestamp()) not in existing_usage
        ]
        charge_to_import = [
            p for p in charge_points if int(p[0].timestamp()) not in existing_charge
        ]

        usage_resp = _import_points(ws, 20, USAGE_SENSOR, "kWh", usage_to_import)
        if not usage_resp.get("success", False):
            return {
                "success": False,
                "step": "import_usage",
                "error": usage_resp,
            }
        charge_resp = _import_points(ws, 21, CHARGE_SENSOR, "CNY", charge_to_import)
        if not charge_resp.get("success", False):
            return {
                "success": False,
                "step": "import_charge",
                "error": charge_resp,
            }

        summary = {
            "success": True,
            "user_id": user_id,
            "monthly_rows": len(monthly_rows),
            "daily_rows": len(daily_rows),
            "current_month_daily_imported": diag["current_month_daily_imported"],
            "current_month_daily_skipped_reason": diag[
                "current_month_daily_skipped_reason"
            ],
            "cleared_first": cleared,
            "usage": {
                "candidates": len(usage_points),
                "existing": len(existing_usage),
                "imported": len(usage_to_import),
                "points": [
                    {"start": dt.isoformat(), "state": state}
                    for dt, state, _ in usage_to_import
                ],
            },
            "charge": {
                "candidates": len(charge_points),
                "existing": len(existing_charge),
                "imported": len(charge_to_import),
                "points": [
                    {"start": dt.isoformat(), "state": state}
                    for dt, state, _ in charge_to_import
                ],
            },
        }
        logging.info(
            "Statistics backfill: cleared=%s usage imported %d (existing %d), charge imported %d (existing %d)",
            cleared,
            summary["usage"]["imported"],
            summary["usage"]["existing"],
            summary["charge"]["imported"],
            summary["charge"]["existing"],
        )
        return summary
    finally:
        try:
            ws.close()
        except Exception:
            pass
