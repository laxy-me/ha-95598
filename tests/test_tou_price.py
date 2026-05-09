import json

from scripts.support.tou_price import TimeOfUsePriceResolver


def test_resolve_tou_price_and_calculate_charge(tmp_path) -> None:
    config_path = tmp_path / "tou_price_config.json"
    config_path.write_text(
        json.dumps(
            {
                "versions": [
                    {
                        "version": "test_2025",
                        "validfrom": "2025-01-01",
                        "validuntil": "2025-12-31",
                        "season_rules": [
                            {
                                "name": "summer",
                                "months": [7, 8, 9],
                                "tiers": [
                                    {
                                        "up_to": 180,
                                        "rates": {
                                            "valley": 0.3,
                                            "flat": 0.5,
                                            "peak": 0.8,
                                            "tip": 1.0,
                                        }
                                    },
                                    {
                                        "up_to": None,
                                        "rates": {
                                            "valley": 0.4,
                                            "flat": 0.6,
                                            "peak": 0.9,
                                            "tip": 1.1,
                                        }
                                    }
                                ]
                            }
                        ],
                        "tip_rules": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    resolver = TimeOfUsePriceResolver(str(config_path))
    selection = resolver.get_selection_for_date("2025-08-18")

    assert selection is not None
    assert selection.version == "test_2025"
    assert selection.season_rule_name == "summer"
    assert selection.tiers[0]["rates"]["peak"] == 0.8

    daily_charge = resolver.calculate_daily_charge(
        "2025-08-18",
        valley_usage=1.0,
        flat_usage=2.0,
        peak_usage=3.0,
        tip_usage=4.0,
        month_usage_before=175,
    )
    assert daily_charge == 8.2


def test_resolve_tou_price_returns_none_when_no_version_matches(tmp_path) -> None:
    config_path = tmp_path / "tou_price_config.json"
    config_path.write_text(json.dumps({"versions": []}), encoding="utf-8")

    resolver = TimeOfUsePriceResolver(str(config_path))
    assert resolver.get_selection_for_date("2026-01-01") is None
    assert resolver.calculate_daily_charge("2026-01-01", 1, 1, 1, 1, month_usage_before=0) is None


def test_resolve_tou_price_uses_env_config_path(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "custom_tou_price.json"
    config_path.write_text(
        json.dumps(
            {
                "versions": [
                    {
                        "version": "custom_env",
                        "validfrom": "2026-01-01",
                        "validuntil": "2026-12-31",
                        "season_rules": [{"name": "all", "months": list(range(1, 13)), "tiers": []}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TOU_PRICE_CONFIG", str(config_path))

    resolver = TimeOfUsePriceResolver()

    assert resolver.config_path == config_path
    assert resolver.get_selection_for_date("2026-05-01").version == "custom_env"
