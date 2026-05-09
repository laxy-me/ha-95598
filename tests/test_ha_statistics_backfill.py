from datetime import date
from zoneinfo import ZoneInfo

from scripts.tools.backfill_ha_energy_statistics import DailyEnergyRow, build_daily_boundary_points


def test_build_daily_boundary_points_uses_previous_day_cumulative_sum():
    rows = [
        DailyEnergyRow(day=date(2026, 1, 1), usage=1.5, charge=0.7),
        DailyEnergyRow(day=date(2026, 1, 2), usage=2.0, charge=0.9),
    ]

    points = build_daily_boundary_points(rows, ZoneInfo("Asia/Shanghai"), "usage")

    assert [point.sum for point in points] == [0.0, 1.5, 3.5]
    assert [point.state for point in points] == [0.0, 1.5, 3.5]
