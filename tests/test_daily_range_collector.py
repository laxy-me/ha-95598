from scripts.fetchers.vue_daily_range import VueDailyRangeCollector


def test_normalize_daily_range_row_maps_95598_fields():
    row = VueDailyRangeCollector._normalize_row(
        {
            "date": "2026-05-01",
            "total_usage": "8.31",
            "valley_usage": "2.50",
            "flat_usage": "1.80",
            "peak_usage": "4.01",
            "tip_usage": "—",
        }
    )

    assert row == {
        "date": "2026-05-01",
        "total_usage": 8.31,
        "valley_usage": 2.5,
        "flat_usage": 1.8,
        "peak_usage": 4.01,
        "tip_usage": 0.0,
    }


def test_normalize_daily_range_row_skips_empty_date():
    assert VueDailyRangeCollector._normalize_row({"date": "", "total_usage": "1"}) is None
