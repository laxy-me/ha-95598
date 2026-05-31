import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import schedule

from captcha_solver.replay import auto_replay_once
from scripts.state_registry import registry as state_registry


DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Serialize all fetch attempts — scheduled jobs share a single lock with
# the manual Ingress trigger so we never start a second selenium driver
# while one is mid-login.
_run_task_lock = threading.Lock()

# --- Settlement retry -------------------------------------------------------
# 95598 daily usage is D+1 and the settlement moment drifts (observed as late
# as ~11:00 the next day). The default job_times schedule mostly fires in the
# morning, so if yesterday settles after the morning run, the regular schedule
# won't pick it up until tomorrow. After each fetch, if the DB's settled daily
# frontier is still behind yesterday, arm a one-shot retry later today; it
# self-cancels and stops re-arming once caught up or past the cutoff hour.
SETTLEMENT_RETRY_TAG = "settlement-retry"
SETTLEMENT_RETRY_DELAY_MINUTES = int(os.getenv("SETTLEMENT_RETRY_DELAY_MINUTES", "90"))
SETTLEMENT_RETRY_CUTOFF_HOUR = int(os.getenv("SETTLEMENT_RETRY_CUTOFF_HOUR", "22"))
# schedule is not thread-safe; the manual Ingress trigger runs in a daemon
# thread and also reaches the re-arm path, so serialize schedule mutations.
# RLock (not Lock): run_pending() holds this while executing a due job, and
# that job's run_task re-enters via _maybe_schedule_settlement_retry on the
# same thread — a plain Lock would self-deadlock there.
_schedule_lock = threading.RLock()


def schedule_jobs(fetcher, updater, job_start_time: str, job_times: int, retry_times_limit: int, republish_interval_minutes: int) -> None:
    base_time = datetime.strptime(job_start_time, "%H:%M")

    for index in range(job_times):
        random_delay_minutes = random.randint(-10, 10)
        final_time = base_time + timedelta(hours=(24 / job_times) * index) + timedelta(minutes=random_delay_minutes)
        run_time_str = final_time.strftime("%H:%M")
        logging.info("Scheduled job will run at %s every day", run_time_str)
        schedule.every().day.at(run_time_str).do(run_task, fetcher, retry_times_limit)

    if republish_interval_minutes > 0:
        logging.info("Cached data will be republished every %s minutes", republish_interval_minutes)
        schedule.every(republish_interval_minutes).minutes.do(updater.republish)
    else:
        logging.info("Periodic cache republish is disabled.")


def run_task(data_fetcher, retry_times_limit: int):
    if not _run_task_lock.acquire(blocking=False):
        logging.info("Skip fetch — another fetch task is already running.")
        return
    state_registry.set_state(state_registry.RUNNING)
    try:
        for retry_times in range(1, retry_times_limit + 1):
            try:
                data_fetcher.fetch()
                # Push the freshly read data into HA's long-term
                # statistics so the energy dashboard's per-day chart
                # picks up any newly arrived daily rows on the right
                # date. INSERT OR REPLACE on (statistic_id, start_ts)
                # also reclaims any rows HA's auto-recorder wrote with
                # sum=0 since the last fetch — that was the root cause
                # of the negative-bar dashboard render.
                try:
                    from scripts import statistics_backfill
                    result = statistics_backfill.run_backfill(clear_first=False)
                    if not result.get("success"):
                        logging.warning(
                            "Post-fetch statistics backfill returned non-success: %s",
                            result,
                        )
                except Exception as exc:
                    logging.warning("Post-fetch statistics backfill raised: %s", exc)
                # If yesterday still hasn't settled, arm a delayed retry so it
                # lands today instead of waiting for tomorrow's morning job.
                _maybe_schedule_settlement_retry(data_fetcher, retry_times_limit)
                return
            except Exception as exc:
                logging.error(
                    "state-refresh task failed, reason is [%s], %s retry times left.",
                    exc,
                    retry_times_limit - retry_times,
                )
    finally:
        # Revert UI to idle only if a successful login didn't already
        # bump state to LOGGED_IN.
        snap = state_registry.snapshot()
        if snap["state"] != state_registry.LOGGED_IN:
            state_registry.set_state(state_registry.IDLE)
        auto_replay_once(DATA_DIR)
        _run_task_lock.release()


def _maybe_schedule_settlement_retry(data_fetcher, retry_times_limit: int) -> None:
    """Arm a one-shot retry later today if the last fetch left us behind on
    the daily series (yesterday not settled yet) and we're before the cutoff
    hour. Idempotent: clears any pending retry before arming a fresh one."""
    if not getattr(data_fetcher, "behind_on_daily", False):
        with _schedule_lock:
            schedule.clear(SETTLEMENT_RETRY_TAG)
        return
    if datetime.now().hour >= SETTLEMENT_RETRY_CUTOFF_HOUR:
        logging.info(
            "Daily series still behind but past cutoff hour %s; leaving it for tomorrow's job.",
            SETTLEMENT_RETRY_CUTOFF_HOUR,
        )
        with _schedule_lock:
            schedule.clear(SETTLEMENT_RETRY_TAG)
        return
    with _schedule_lock:
        schedule.clear(SETTLEMENT_RETRY_TAG)
        schedule.every(SETTLEMENT_RETRY_DELAY_MINUTES).minutes.do(
            _run_settlement_retry, data_fetcher, retry_times_limit
        ).tag(SETTLEMENT_RETRY_TAG)
    logging.info(
        "Daily series behind — armed a settlement retry in %s minutes.",
        SETTLEMENT_RETRY_DELAY_MINUTES,
    )


def _run_settlement_retry(data_fetcher, retry_times_limit: int):
    """One-shot retry body. run_task re-arms the next retry (or clears it once
    caught up) via _maybe_schedule_settlement_retry, so this cancels itself."""
    logging.info("Settlement retry firing — re-fetching to pick up late-settled daily data.")
    run_task(data_fetcher, retry_times_limit)
    return schedule.CancelJob


def trigger_manual_fetch(data_fetcher, retry_times_limit: int) -> bool:
    """Spawn a fetch in a worker thread. Returns False if one is already
    running (the lock acquisition in run_task will fast-reject)."""
    if _run_task_lock.locked():
        logging.info("Manual fetch trigger ignored — a fetch is already in progress.")
        return False
    logging.info("Manual fetch triggered from Ingress UI.")
    threading.Thread(
        target=run_task,
        args=(data_fetcher, retry_times_limit),
        daemon=True,
        name="manual-fetch",
    ).start()
    return True


def run_forever() -> None:
    while True:
        # Guard against a daemon-thread fetch mutating schedule.jobs (arming
        # or clearing a settlement retry) while we iterate it here.
        with _schedule_lock:
            schedule.run_pending()
        time.sleep(1)
