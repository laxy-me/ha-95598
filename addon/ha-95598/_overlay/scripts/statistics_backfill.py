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
from datetime import datetime, timedelta
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


def _build_snapshot_series(
    monthly_rows: list[tuple[str, float, float]],
) -> tuple[list[tuple[datetime, float, float]], list[tuple[datetime, float, float]]]:
    """Return (usage_points, charge_points) — each a sorted list of
    (datetime, state, sum). state == sum (both equal cumulative through
    that month's end). A zero anchor is prepended at the end of the
    month before the earliest monthly_rows entry."""
    if not monthly_rows:
        return [], []
    anchor_dt = _month_end_dt(_previous_month_key(monthly_rows[0][0]))
    usage = [(anchor_dt, 0.0, 0.0)]
    charge = [(anchor_dt, 0.0, 0.0)]
    cum_u = 0.0
    cum_c = 0.0
    for month_key, total_u, total_c in monthly_rows:
        cum_u += total_u
        cum_c += total_c
        end = _month_end_dt(month_key)
        usage.append((end, round(cum_u, 2), round(cum_u, 2)))
        charge.append((end, round(cum_c, 2), round(cum_c, 2)))
    return usage, charge


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


def run_backfill() -> dict:
    """Public entrypoint. Reads monthly_usage, computes month-end
    snapshots, skips any that HA already has, and imports the rest for
    both the usage and charge sensors."""
    logging.info("Statistics backfill: starting")
    user_id = _resolve_user_id()
    if not user_id:
        return {"success": False, "error": "no monthly_usage rows in fork SQLite"}

    monthly_rows = _load_monthly_snapshots(user_id)
    if not monthly_rows:
        return {"success": False, "error": "monthly_usage empty"}

    usage_points, charge_points = _build_snapshot_series(monthly_rows)
    span_start = usage_points[0][0] - timedelta(hours=1)
    span_end = usage_points[-1][0] + timedelta(hours=1)

    ws = _ws_connect()
    try:
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
            "Statistics backfill: usage imported %d (existing %d), charge imported %d (existing %d)",
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
