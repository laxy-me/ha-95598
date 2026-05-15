"""Background thread that pings 95598 every N minutes using the
persisted session cookies, in hope that 95598 uses a sliding TTL
and the touch will postpone the next forced re-login.

Empirically 95598's session dies ~1.5 hours after creation. We
don't know whether the TTL is sliding (extended on each use) or
absolute (fixed from issue time). If sliding, this thread keeps
the session alive indefinitely. If absolute, it's a cheap no-op
and the next scheduled fetch handles the relogin as usual.

Set ``SESSION_KEEPALIVE_SECONDS`` env to 0 (or negative) to disable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests


SESSION_FILE = Path("/app/data/ha_95598_session.json")
HEARTBEAT_URL = "https://95598.cn/osgweb/userAcc"

DEFAULT_INTERVAL_SECONDS = 30 * 60  # 30 minutes


def _load_cookies(jar: requests.cookies.RequestsCookieJar) -> bool:
    if not SESSION_FILE.exists():
        return False
    try:
        payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    cookies = payload.get("cookies") or []
    if not cookies:
        return False
    jar.clear()
    for cookie in cookies:
        try:
            jar.set(
                name=cookie["name"],
                value=cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
                secure=cookie.get("secure", False),
            )
        except Exception:
            continue
    return True


def _save_cookies_back(session: requests.Session) -> None:
    if not SESSION_FILE.exists():
        return
    try:
        payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    existing_by_name = {c.get("name"): c for c in (payload.get("cookies") or [])}
    for c in session.cookies:
        existing_by_name[c.name] = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "expiry": c.expires,
        }
    payload["cookies"] = list(existing_by_name.values())
    try:
        SESSION_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.debug("Session keepalive: failed to write cookies back: %s", exc)


def _heartbeat_once() -> tuple[bool, int]:
    """Returns (alive, status_code). alive=True if session OK."""
    jar = requests.cookies.RequestsCookieJar()
    if not _load_cookies(jar):
        return False, 0
    session = requests.Session()
    session.cookies = jar
    session.headers.update(
        {
            "User-Agent": os.getenv(
                "BROWSER_USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36",
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://95598.cn/osgweb/index",
        }
    )
    try:
        r = session.get(HEARTBEAT_URL, timeout=15, allow_redirects=False)
    except Exception as exc:
        logging.debug("Session keepalive: heartbeat request failed: %s", exc)
        return False, -1
    if r.status_code == 200:
        _save_cookies_back(session)
        return True, r.status_code
    return False, r.status_code


def start(interval_seconds: Optional[int] = None) -> None:
    raw = (
        interval_seconds
        if interval_seconds is not None
        else os.getenv("SESSION_KEEPALIVE_SECONDS")
    )
    try:
        interval = int(raw) if raw is not None else DEFAULT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_SECONDS
    if interval <= 0:
        logging.info(
            "Session keepalive disabled (SESSION_KEEPALIVE_SECONDS=%s).", raw,
        )
        return

    def _loop():
        # Wait one interval before first ping so we don't pile on top
        # of startup login.
        time.sleep(interval)
        while True:
            try:
                alive, status = _heartbeat_once()
                if alive:
                    logging.info("Session keepalive: 95598 heartbeat OK")
                else:
                    logging.info(
                        "Session keepalive: 95598 heartbeat returned %s "
                        "(session may have expired; next scheduled fetch "
                        "will re-login).",
                        status,
                    )
            except Exception as exc:
                logging.warning("Session keepalive crashed once: %s", exc)
            time.sleep(interval)

    thread = threading.Thread(
        target=_loop, name="session-keepalive", daemon=True,
    )
    thread.start()
    logging.info("Session keepalive thread started (interval=%ss).", interval)
