import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from captcha_solver.tencent import TencentCaptchaHandler
from scripts.const import BALANCE_URL, LOGIN_URL
from scripts.support.credentials import LoginCredential, mask_account
from scripts.support.error_watcher import ErrorWatcher
from scripts.support.notifier import build_notifier
from scripts.support.session_manager import SessionManager


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"


class LoginManager:
    """Owns all 95598 login, session restore, and login fallback behavior."""

    def __init__(
        self,
        credentials: list[LoginCredential],
        session_manager: SessionManager,
        driver_wait_time: int,
        qr_wait_count: int,
        qr_wait_interval: int,
        qr_refresh_limit: int,
        trace_dir: Callable[[], Path],
        log_page_state: Callable,
        step_sleep: Callable,
        click_button: Callable,
    ) -> None:
        self._credentials = credentials
        self._credential_index = 0
        self._account = credentials[0].account
        self._password = credentials[0].password
        # 95598 caps password logins per account per UTC day. RK001
        # ("网络连接超时") is the error message it returns when that
        # cap is hit. Once we see it in a session, skip further
        # password attempts and go straight to QR until the next
        # process start (or the next day's quota reset).
        self._password_login_locked: set[str] = set()
        self.session_manager = session_manager
        self.driver_wait_time = driver_wait_time
        self.qr_wait_count = qr_wait_count
        self.qr_wait_interval = qr_wait_interval
        self.qr_refresh_limit = qr_refresh_limit
        self._trace_dir = trace_dir
        self._log_page_state = log_page_state
        self._step_sleep = step_sleep
        self._click_button = click_button
        self.notifier = build_notifier()
        self.login_method = "unknown"
        self.tencent_captcha = TencentCaptchaHandler(
            trace_dir=self._trace_dir,
            log_page_state=self._log_page_state,
            step_sleep=self._step_sleep,
            confirm_login_success=self._confirm_login_success,
        )

    def restore_or_login(self, driver) -> str:
        self._set_login_method("unknown")
        if self.session_manager.restore(driver):
            self._set_login_method("restored-session")
            logging.info("Skip interactive login because a valid session was restored.")
        elif self._login_with_credential_rotation(driver):
            if self.login_method == "unknown":
                self._set_login_method("password")
        else:
            raise RuntimeError("login unsuccessed")

        self.log_login_success(driver)
        return self.login_method

    def log_login_success(self, driver) -> None:
        logging.info("Login success via %s on %s", self.login_method, LOGIN_URL)
        if driver is not None:
            self.session_manager.save(driver)

    def clear_session(self) -> None:
        self.session_manager.clear()

    def save_session(self, driver) -> None:
        self.session_manager.save(driver)

    def _activate_credential(self, index: int) -> LoginCredential:
        self._credential_index = index % len(self._credentials)
        credential = self._credentials[self._credential_index]
        self._account = credential.account
        self._password = credential.password
        return credential

    def _set_login_method(self, method: str) -> None:
        self.login_method = method

    def _confirm_login_success(self, driver) -> bool:
        try:
            current_url = driver.current_url or ""
            if current_url.startswith(LOGIN_URL):
                return False
            if SessionManager.is_session_usable(driver):
                return True

            driver.get(BALANCE_URL)
            WebDriverWait(driver, min(self.driver_wait_time, 10)).until(
                lambda d: not (d.current_url or "").startswith(LOGIN_URL)
            )
            return SessionManager.is_session_usable(driver)
        except Exception as exc:
            logging.debug("Failed to confirm login success after redirect: %s", exc)
            return False

    # 95598 daily-cap message. Match keywords rather than the full
    # localized string to tolerate punctuation / wording drift.
    _DAILY_LIMIT_MARKERS = ("RK001", "网络连接超时", "登录次数", "超出限制", "请明日")

    def _classify_error(self, error: str) -> str:
        if not error:
            return ""
        if any(marker in error for marker in self._DAILY_LIMIT_MARKERS):
            return "limit_exceeded"
        return "error"

    def _wait_for_post_password_login_state(self, driver, timeout: int = 12) -> str:
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: self._confirm_login_success(d)
                or bool(self._get_error_message(d, "//div[@class='errmsg-tip']//span"))
                or self.tencent_captcha.has_captcha(d)
            )
        except Exception:
            pass

        if self._confirm_login_success(driver):
            return "success"
        # 95598 preloads the Tencent captcha widget DOM into the page
        # *before* the user submits anything, so `has_captcha` is True
        # whether or not a real captcha was actually issued. Check the
        # explicit error tip first — if 95598 surfaced an errmsg (e.g.
        # RK001 daily limit) we must not mistake the preloaded widget
        # for a real captcha challenge.
        error_msg = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
        if error_msg:
            return self._classify_error(error_msg)
        if self.tencent_captcha.has_captcha(driver):
            return "captcha"
        return "unknown"

    def _login_with_credential_rotation(self, driver, phone_code: bool = False) -> bool:
        total_credentials = len(self._credentials)
        skipped_all = True
        for attempt in range(total_credentials):
            credential = self._activate_credential(
                self._credential_index if attempt == 0 else self._credential_index + 1
            )
            if credential.account in self._password_login_locked:
                logging.info(
                    "Skip password login for credential %s — daily password-login cap "
                    "already hit this session (RK001). Will try fallback only.",
                    credential.label,
                )
                continue
            skipped_all = False
            logging.info(
                "Try interactive login with credential [%s/%s]: %s",
                attempt + 1,
                total_credentials,
                credential.label,
            )
            if self.login(driver, phone_code=phone_code, allow_fallback=False):
                return True
            logging.info("Login credential %s did not complete password login.", credential.label)

        if skipped_all:
            logging.info("All credentials are locked out of password login. Using fallback directly.")
        else:
            logging.info("All configured login credentials failed password login. Switch to configured fallback.")
        return self._fallback_login(driver)

    @ErrorWatcher.watch
    def login(self, driver, phone_code=False, allow_fallback: bool = True) -> bool:
        try:
            driver.get(LOGIN_URL)
            self._log_page_state(driver, "after_open_login_url")
            WebDriverWait(driver, self.driver_wait_time * 3).until(
                EC.visibility_of_element_located((By.CLASS_NAME, "user"))
            )
        except Exception:
            logging.debug("Login failed, open URL: %s failed.", LOGIN_URL)
        logging.info("Open LOGIN_URL:%s.\r", LOGIN_URL)
        self._step_sleep(driver, "login_page_load")

        driver.implicitly_wait(0)
        try:
            WebDriverWait(driver, 10).until(EC.invisibility_of_element_located((By.CLASS_NAME, "el-loading-mask")))
        finally:
            driver.implicitly_wait(self.driver_wait_time)

        element = WebDriverWait(driver, self.driver_wait_time).until(
            EC.presence_of_element_located((By.CLASS_NAME, "user"))
        )
        driver.execute_script("arguments[0].click();", element)
        logging.info("find_element 'user'.\r")
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[2]/span')
        self._step_sleep(driver, "after_switch_to_password_tab")

        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]')
        logging.info("Click the Agree option.\r")
        self._step_sleep(driver, "after_click_agree")
        if phone_code:
            self._set_login_method("phone-code")
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[3]/span')
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            input_elements[2].send_keys(self._account)
            logging.info("input_elements account : %s\r", mask_account(self._account))
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[1]/div[2]/div[2]/div/a')
            code = input("Input your phone verification code: ")
            input_elements[3].send_keys(code)
            logging.info("input_elements verification code: %s.\r", code)
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[2]/div/button/span')
            self._step_sleep(driver, "after_submit_phone_code_login")
            logging.info("Click login button.\r")
            return True

        if self._password is not None and len(self._password) > 0:
            self._set_login_method("password")
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            input_elements[0].send_keys(self._account)
            logging.info("input_elements account : %s\r", mask_account(self._account))
            input_elements[1].send_keys(self._password)
            logging.info("input_elements password : ********\r")

            self._click_button(driver, By.CLASS_NAME, "el-button.el-button--primary")
            self._step_sleep(driver, "after_submit_password_login")
            logging.info("Click login button.\r")
            post_login_state = self._wait_for_post_password_login_state(driver)
            logging.info("Post password-login state: %s", post_login_state)
            if post_login_state == "captcha":
                captcha_info = self.tencent_captcha.get_info(driver)
                logging.info(
                    "Tencent captcha widget detected after password submit, mode=%s, prompt=%s.",
                    captcha_info.get("mode"),
                    captcha_info.get("prompt", ""),
                )
                self.tencent_captcha.capture_state(driver, "after_submit_password_login_tencent_captcha")
                if captcha_info.get("mode") == "point_click" and self.tencent_captcha.solve_point_click_captcha(driver):
                    return True
                if not allow_fallback:
                    return False
                logging.info("Tencent captcha local solver did not complete login. Switch to QR-code login fallback.")
                return self._fallback_login(driver)
            if self._confirm_login_success(driver):
                return True
            if post_login_state in ("error", "limit_exceeded"):
                error_message = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                if post_login_state == "limit_exceeded":
                    self._password_login_locked.add(self._account)
                    logging.warning(
                        "95598 hit daily password-login cap for %s: %s. "
                        "Locking out password login this session — using QR-code fallback "
                        "(QR logins do not consume the password quota).",
                        mask_account(self._account),
                        error_message or "<empty>",
                    )
                else:
                    logging.info(
                        "Password login returned a page error without a usable session: %s. Switch to QR-code login fallback.",
                        error_message or "<empty>",
                    )
                self._log_page_state(driver, "after_submit_password_login_error")
                self._save_tencent_presence(driver)
                if self.tencent_captcha.is_captcha_actually_displayed(driver):
                    self.tencent_captcha.capture_state(driver, "after_submit_password_login_error_tencent_captcha")
                if not allow_fallback:
                    return False
                return self._fallback_login(driver)
            if post_login_state == "unknown":
                logging.info("Password login result is still unknown after waiting. Capture page state before QR-code fallback.")
                self._log_page_state(driver, "after_submit_password_login_unknown")
            logging.info("Tencent captcha was not detected after password submit. Switch to QR-code login fallback.")
        if not allow_fallback:
            return False
        return self._fallback_login(driver)

    def _save_tencent_presence(self, driver) -> None:
        try:
            presence = self.tencent_captcha.get_presence_snapshot(driver)
            presence_path = self._trace_dir() / "after_submit_password_login_error.tencent_presence.json.txt"
            presence_path.write_text(json.dumps(presence, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.info("Saved Tencent presence snapshot to %s", presence_path)
        except Exception as exc:
            logging.info("Failed to save Tencent presence snapshot: %s", exc)

    def _get_error_message(self, driver, path) -> Optional[str]:
        driver.implicitly_wait(0)
        try:
            element = driver.find_element(By.XPATH, path)
            return element.text
        except Exception:
            return None
        finally:
            driver.implicitly_wait(self.driver_wait_time)

    def _fallback_login(self, driver) -> bool:
        fallback = os.getenv("LOGIN_FALLBACK")
        if fallback == "qrcode":
            self._set_login_method("qrcode")
            return self._qr_login(driver)
        return False

    def _qr_login(self, driver) -> bool:
        logging.info("qrcode login start")
        element = WebDriverWait(driver, self.driver_wait_time).until(
            EC.presence_of_element_located((By.CLASS_NAME, "qr_code"))
        )
        driver.execute_script("arguments[0].click();", element)
        logging.info("switch to qrcode mode")

        self._step_sleep(driver, "after_switch_to_qrcode_mode")

        qr_code_path = DATA_DIR / "login_qr_code.png"
        for refresh_index in range(self.qr_refresh_limit + 1):
            qr_element = WebDriverWait(driver, self.driver_wait_time).until(
                EC.visibility_of_element_located((By.XPATH, "//div[@class='sweepCodePic']//img"))
            )
            logging.info("find imgLogin element")

            img_src = qr_element.get_attribute("src")
            if img_src.startswith("data:image"):
                base64_data = img_src.split(",")[1]
                img_screenshot = base64.b64decode(base64_data)
            else:
                logging.info("qrcode img src not base64")
                img_screenshot = qr_element.screenshot_as_png

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(qr_code_path, "wb") as file:
                file.write(img_screenshot)
                logging.info("save qrcode to %s", qr_code_path)

            # Mirror the QR into the public Home Assistant www dir so it
            # can be opened in a browser via the configured public URL.
            public_path = os.getenv("QR_CODE_PUBLIC_PATH") or ""
            if public_path:
                try:
                    Path(public_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(public_path).write_bytes(img_screenshot)
                except Exception as exc:
                    logging.warning("Failed to mirror QR to %s: %s", public_path, exc)
            public_url = (os.getenv("QR_CODE_PUBLIC_URL") or "").strip()
            if public_url:
                logging.info("Open QR in browser: %s", public_url)

            if self.notifier.send_qr_code(img_screenshot):
                logging.info("QRCode notification sent successfully.")
            else:
                logging.info("Please scan the local QR code file at %s", qr_code_path)

            should_refresh = False
            for index in range(1, self.qr_wait_count + 1):
                logging.info(
                    "qrcode check login wait[%s] count[%s] refresh[%s/%s]",
                    self.qr_wait_interval,
                    index,
                    refresh_index,
                    self.qr_refresh_limit,
                )
                time.sleep(self.qr_wait_interval)
                if driver.current_url != LOGIN_URL:
                    self._set_login_method("qrcode")
                    logging.info("Login success via qrcode.")
                    return True

                error = self._get_error_message(driver, "//div[@class='sweepCodePic']//div[@class='erwBg']//p")
                if error is None:
                    continue

                logging.error("qrcode login error[%s]", error)
                if "二维码失效" in error and refresh_index < self.qr_refresh_limit:
                    logging.info(
                        "QR code expired, refreshing QR code and retrying [%s/%s].",
                        refresh_index + 1,
                        self.qr_refresh_limit,
                    )
                    try:
                        driver.execute_script("arguments[0].click();", qr_element)
                        time.sleep(1)
                    except Exception as exc:
                        logging.warning("Failed to click expired QR code for refresh: %s", exc)
                    should_refresh = True
                    break

                if self.tencent_captcha.is_captcha_actually_displayed(driver):
                    captcha_info = self.tencent_captcha.get_info(driver)
                    logging.info(
                        "Tencent captcha still visible during QR fallback, mode=%s, prompt=%s",
                        captcha_info.get("mode"),
                        captcha_info.get("prompt", ""),
                    )
                    self.tencent_captcha.capture_state(driver, "qrcode_login_error_tencent_captcha")
                return False

            if should_refresh:
                continue

            if refresh_index < self.qr_refresh_limit:
                logging.warning(
                    "qrcode Login timeout, refreshing QR code and retrying [%s/%s].",
                    refresh_index + 1,
                    self.qr_refresh_limit,
                )
                try:
                    driver.execute_script("arguments[0].click();", qr_element)
                    time.sleep(1)
                except Exception as exc:
                    logging.warning("Failed to refresh QR code after timeout: %s", exc)
                continue

            logging.warning("qrcode Login timeout")
            break

        if self.tencent_captcha.is_captcha_actually_displayed(driver):
            captcha_info = self.tencent_captcha.get_info(driver)
            logging.info(
                "Tencent captcha still visible after QR timeout, mode=%s, prompt=%s",
                captcha_info.get("mode"),
                captcha_info.get("prompt", ""),
            )
            self.tencent_captcha.capture_state(driver, "qrcode_login_timeout_tencent_captcha")

        return False
