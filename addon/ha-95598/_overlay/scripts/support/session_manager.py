import json
import logging
from datetime import datetime
from pathlib import Path

from selenium.webdriver.common.by import By

from scripts.const import HOME_URL, LOGIN_URL


class SessionManager:
    """Persist and restore 95598 browser session state."""

    def __init__(self, session_file: Path, can_use_session, log_page_state, step_sleep):
        self.session_file = session_file
        self._can_use_session = can_use_session
        self._log_page_state = log_page_state
        self._step_sleep = step_sleep

    def clear(self) -> None:
        try:
            if self.session_file.exists():
                self.session_file.unlink()
                logging.info("Removed expired session file %s", self.session_file)
        except Exception as exc:
            logging.warning("Failed to remove session file %s: %s", self.session_file, exc)

    def save(self, driver) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            storage_state = driver.execute_script(
                """
                const dump = (storage) => {
                  const out = {};
                  for (let i = 0; i < storage.length; i++) {
                    const key = storage.key(i);
                    out[key] = storage.getItem(key);
                  }
                  return out;
                };
                return {
                  local_storage: dump(window.localStorage),
                  session_storage: dump(window.sessionStorage)
                };
                """
            )
            payload = {
                "saved_at": datetime.now().isoformat(),
                "current_url": driver.current_url,
                "cookies": driver.get_cookies(),
                "storage": storage_state,
            }
            self.session_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logging.info("Saved login session to %s", self.session_file)
        except Exception as exc:
            logging.warning("Failed to persist login session: %s", exc)

    @staticmethod
    def is_session_usable(driver) -> bool:
        try:
            current_url = driver.current_url or ""
            if current_url.startswith(LOGIN_URL):
                return False
            page_text = ""
            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                page_text = ""
            if (
                "页面停留时间过长" in page_text
                or "请重试" in page_text
                or "您的登录已过期" in page_text
                or "登录已过期" in page_text
                or "重新登录" in page_text
            ):
                return False
            if "登录" in page_text and "注册" in page_text and "退出登录" not in page_text:
                return False
            markers = [
                (By.CLASS_NAME, "el-dropdown"),
                (By.XPATH, "//span[contains(text(),'退出登录') or contains(text(),'我的')]"),
                (By.XPATH, "//div[contains(@class,'userNumber')]"),
            ]
            for by, value in markers:
                if driver.find_elements(by, value):
                    return True
        except Exception:
            return False
        return False

    def restore(self, driver) -> bool:
        if not self.session_file.exists():
            return False

        try:
            payload = json.loads(self.session_file.read_text(encoding="utf-8"))
            cookies = payload.get("cookies") or []
            storage = payload.get("storage") or {}
            if not cookies:
                logging.info("Session file %s has no cookies, skip restore.", self.session_file)
                return False

            driver.get("https://95598.cn/")
            for cookie in cookies:
                restored_cookie = dict(cookie)
                same_site = restored_cookie.get("sameSite")
                if same_site not in {"Strict", "Lax", "None"}:
                    restored_cookie.pop("sameSite", None)
                try:
                    driver.add_cookie(restored_cookie)
                except Exception as exc:
                    logging.debug("Skip one cookie during session restore: %s", exc)

            try:
                local_storage = storage.get("local_storage") or {}
                session_storage = storage.get("session_storage") or {}
                driver.execute_script(
                    """
                    const localData = arguments[0] || {};
                    const sessionData = arguments[1] || {};
                    Object.entries(localData).forEach(([key, value]) => window.localStorage.setItem(key, value));
                    Object.entries(sessionData).forEach(([key, value]) => window.sessionStorage.setItem(key, value));
                    """,
                    local_storage,
                    session_storage,
                )
            except Exception as exc:
                logging.debug("Failed to restore storage state: %s", exc)

            driver.get(HOME_URL)
            self._log_page_state(driver, "after_restore_session_open_home")
            self._step_sleep(driver, "after_restore_session_open_home")
            if self._can_use_session(driver):
                logging.info("Reused persisted login session successfully.")
                return True

            logging.info("Persisted session is no longer valid, will relogin.")
            self.clear()
            return False
        except Exception as exc:
            logging.warning("Failed to restore persisted session: %s", exc)
            self.clear()
            return False
