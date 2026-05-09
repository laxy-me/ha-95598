from datetime import datetime

from scripts.support.cache_store import CacheStore


def test_cache_store_round_trip(tmp_path) -> None:
    cache_file = tmp_path / "ha_95598_cache.json"
    store = CacheStore(cache_file)

    store.save_partial_data("user1", balance=1.23, last_daily_date="2026-04-30")
    store.update_progress_stage("user1", "complete", fetch_date="2026-04-30")

    assert store.get_cached_user_data("user1")["balance"] == 1.23
    assert store.get_progress("user1")["stage"] == "complete"
    assert store.is_progress_complete(store.get_progress("user1")) is True


def test_cache_store_skip_startup_fetch_only_when_complete_and_current(tmp_path) -> None:
    cache_file = tmp_path / "ha_95598_cache.json"
    store = CacheStore(cache_file)

    assert store.should_skip_startup_fetch() is False

    today = datetime.now().strftime("%Y-%m-%d")
    store.save(
        {
            "user1": {
                "data": {"balance": 1.23, "last_daily_date": today},
                "progress": {"stage": "complete", "fetch_date": today},
            }
        }
    )
    assert store.should_skip_startup_fetch() is True
