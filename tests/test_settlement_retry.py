"""Tests for the settlement-aware daily retry (v0.1.37).

95598 daily usage settles D+1 and can settle late in the day; a morning
fetch that completes before settlement must re-fetch later the same day
instead of leaving yesterday's row for tomorrow.
"""
import os
from datetime import datetime, timedelta

import pytest
import schedule

import scripts.support.job_scheduler as js
from scripts.data_fetcher import DataFetcher
from scripts.support.db import SqliteDB


# --- _daily_series_behind: real temp DB, the three "not yet settled" states --

def _make_db(tmp_path) -> SqliteDB:
    os.environ["DB_NAME"] = str(tmp_path / "test_homeassistant.db")
    db = SqliteDB()
    assert db.connect_user_db("u1") is True
    return db


def _insert(db: SqliteDB, date: str, usage: float) -> None:
    assert db.insert_daily_data(
        {
            "date": date,
            "total_usage": usage,
            "total_charge": None,
            "valley_usage": 0.0,
            "flat_usage": 0.0,
            "peak_usage": 0.0,
            "tip_usage": 0.0,
        }
    ) is True


def _fetcher_with_db(db: SqliteDB) -> DataFetcher:
    # DataFetcher.__init__ spins up selenium/login wiring; we only need the
    # DB-backed helper, so build a bare instance and inject the db.
    fetcher = DataFetcher.__new__(DataFetcher)
    fetcher.db = db
    return fetcher


def _d(days_ago: int) -> str:
    return (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_behind_false_when_frontier_reached_yesterday(tmp_path) -> None:
    db = _make_db(tmp_path)
    _insert(db, _d(2), 18.0)
    _insert(db, _d(1), 17.0)  # yesterday settled
    db.close_connect()
    assert _fetcher_with_db(db)._daily_series_behind("u1") is False


def test_behind_true_when_yesterday_absent_state_a(tmp_path) -> None:
    db = _make_db(tmp_path)
    _insert(db, _d(2), 18.0)  # only day-before; yesterday's row not there yet
    db.close_connect()
    assert _fetcher_with_db(db)._daily_series_behind("u1") is True


def test_behind_true_when_yesterday_is_zero_placeholder_state_b(tmp_path) -> None:
    db = _make_db(tmp_path)
    _insert(db, _d(2), 18.0)
    _insert(db, _d(1), 0.0)  # placeholder: present but unsettled
    db.close_connect()
    # get_latest_completed_daily filters total_usage>0, so frontier is still
    # day-before -> behind.
    assert _fetcher_with_db(db)._daily_series_behind("u1") is True


def test_behind_true_when_db_empty(tmp_path) -> None:
    db = _make_db(tmp_path)
    db.close_connect()
    assert _fetcher_with_db(db)._daily_series_behind("u1") is True


def test_behind_false_when_db_disabled() -> None:
    fetcher = DataFetcher.__new__(DataFetcher)
    fetcher.db = None
    assert fetcher._daily_series_behind("u1") is False


# --- _maybe_schedule_settlement_retry: arm / skip-past-cutoff / clear --------

class _StubFetcher:
    def __init__(self, behind: bool) -> None:
        self.behind_on_daily = behind


class _FakeDateTime:
    fixed = datetime(2026, 5, 31, 10, 0, 0)

    @classmethod
    def now(cls):
        return cls.fixed


@pytest.fixture(autouse=True)
def _clear_schedule():
    schedule.clear()
    yield
    schedule.clear()


def _tagged():
    return [j for j in schedule.jobs if js.SETTLEMENT_RETRY_TAG in j.tags]


def test_retry_armed_when_behind_before_cutoff(monkeypatch) -> None:
    _FakeDateTime.fixed = datetime(2026, 5, 31, 10, 0, 0)
    monkeypatch.setattr(js, "datetime", _FakeDateTime)
    js._maybe_schedule_settlement_retry(_StubFetcher(behind=True), retry_times_limit=3)
    assert len(_tagged()) == 1


def test_retry_not_armed_when_caught_up_and_clears_stale(monkeypatch) -> None:
    _FakeDateTime.fixed = datetime(2026, 5, 31, 10, 0, 0)
    monkeypatch.setattr(js, "datetime", _FakeDateTime)
    # pre-seed a stale retry; a caught-up fetch must clear it.
    schedule.every(90).minutes.do(lambda: None).tag(js.SETTLEMENT_RETRY_TAG)
    assert len(_tagged()) == 1
    js._maybe_schedule_settlement_retry(_StubFetcher(behind=False), retry_times_limit=3)
    assert len(_tagged()) == 0


def test_retry_not_armed_past_cutoff(monkeypatch) -> None:
    _FakeDateTime.fixed = datetime(2026, 5, 31, 23, 0, 0)  # past cutoff (22)
    monkeypatch.setattr(js, "datetime", _FakeDateTime)
    js._maybe_schedule_settlement_retry(_StubFetcher(behind=True), retry_times_limit=3)
    assert len(_tagged()) == 0
