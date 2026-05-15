import logging
import os
import re
import time
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from scripts.fetchers.vue_daily_range import VueDailyRangeCollector
from scripts.fetchers.vue_state import (
    normalize_balance,
    normalize_bill_detail,
    normalize_usage,
    selected_vue_data,
)
from scripts.pages.usage_page import UsagePage
from scripts.sensor_updater import SensorUpdater
from scripts.support.browser_factory import create_chromium_driver
from scripts.support.data_persister import DataPersister
from scripts.support.error_watcher import ErrorWatcher
from typing import Optional
from scripts.support.page_tracer import PageTracer
from scripts.support.session_manager import SessionManager
from scripts.support.credentials import LoginCredential, mask_account
from scripts.support.login_manager import LoginManager
from scripts.support.ha95598_navigator import Ha95598Navigator

from scripts.const import BALANCE_URL, ELECTRIC_BILL_SUMMARY_URL

from pathlib import Path
from scripts.support.tou_price import TimeOfUsePriceResolver


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

class DataFetcher:

    def __init__(self, account: str, password: str, updater=None, credentials: Optional[list[LoginCredential]] = None):
        if 'PYTHON_IN_DOCKER' not in os.environ:
            import dotenv
            dotenv.load_dotenv(verbose=True)
        self.credentials = credentials or [
            LoginCredential(account=account, password=password, label=mask_account(account))
        ]
        self.updater = updater

        self.DRIVER_IMPLICITY_WAIT_TIME = int(os.getenv("DRIVER_IMPLICITY_WAIT_TIME", 60))
        self.RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))
        self.RETRY_WAIT_TIME_OFFSET_UNIT = int(os.getenv("RETRY_WAIT_TIME_OFFSET_UNIT", 5))
        self.IGNORE_USER_ID = [user_id.strip() for user_id in os.getenv("IGNORE_USER_ID", "").split(",") if user_id.strip()]
        self.QR_CODE_LOGIN_WAIT_COUNT = int(os.getenv("QR_CODE_LOGIN_WAIT_COUNT", 7))
        self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT = int(os.getenv("QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT", 10))
        self.QR_CODE_LOGIN_REFRESH_LIMIT = int(os.getenv("QR_CODE_LOGIN_REFRESH_LIMIT", 1))
        self.tou_price_resolver = TimeOfUsePriceResolver()
        self.page_tracer = PageTracer(DATA_DIR / "pages")
        self.session_manager = SessionManager(
            session_file=DATA_DIR / "ha_95598_session.json",
            can_use_session=self._can_use_session,
            log_page_state=self._log_page_state,
            step_sleep=self._step_sleep,
        )
        self.login_manager = LoginManager(
            credentials=self.credentials,
            session_manager=self.session_manager,
            driver_wait_time=self.DRIVER_IMPLICITY_WAIT_TIME,
            qr_wait_count=self.QR_CODE_LOGIN_WAIT_COUNT,
            qr_wait_interval=self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT,
            qr_refresh_limit=self.QR_CODE_LOGIN_REFRESH_LIMIT,
            trace_dir=self._trace_dir,
            log_page_state=self._log_page_state,
            step_sleep=self._step_sleep,
            click_button=self._click_button,
        )
        self.tencent_captcha = self.login_manager.tencent_captcha
        self.navigator = Ha95598Navigator(
            driver_wait_time=self.DRIVER_IMPLICITY_WAIT_TIME,
            login_manager=self.login_manager,
            tencent_captcha=self.tencent_captcha,
            log_page_state=self._log_page_state,
            step_sleep=self._step_sleep,
            click_button=self._click_button,
        )
        self.usage_page = UsagePage(
            navigator=self.navigator,
            log_page_state=self._log_page_state,
            step_sleep=self._step_sleep,
        )
        self._init_db()
        self.data_persister = DataPersister(self.db, self.tou_price_resolver)

    def _init_db(self):
        self.db_type = os.getenv("DB_TYPE", "sqlite").lower()
        if self.db_type == 'sqlite':
            from scripts.support.db import SqliteDB
            self.db = SqliteDB()
            logging.info("Using SQLite database to store data.")
        else:
            self.db = None
            if self.db_type not in ('none', ''):
                logging.warning("Unsupported DB_TYPE=%s, database storage disabled.", self.db_type)
            logging.info("No database will be used to store data.")

    def _trace_dir(self) -> Path:
        return self.page_tracer.ensure_trace_dir()

    def _resolve_trace_label(self, label: Optional[str] = None) -> str:
        return self.page_tracer.resolve_label(label, caller_depth=3)

    def _log_page_state(self, driver, label: Optional[str] = None) -> None:
        self.page_tracer.log_page_state(driver, label)

    def _can_use_session(self, driver) -> bool:
        return SessionManager.is_session_usable(driver)

    def _progress_date(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _is_progress_current(self, progress: dict) -> bool:
        return (progress or {}).get("fetch_date") == self._progress_date()

    def _has_completed_stage(self, progress: dict, stage: str) -> bool:
        stage_order = {
            "none": 0,
            "balance": 1,
            "yearly": 2,
            "monthly": 3,
            "daily": 4,
            "tou": 5,
            "persist": 6,
            "tou_backfill": 7,
            "billing": 8,
            "complete": 9,
        }
        current_stage = (progress or {}).get("stage", "none")
        return stage_order.get(current_stage, 0) >= stage_order.get(stage, 0)

    def _step_sleep(self, driver, label: Optional[str] = None, multiplier: int = 1) -> None:
        step_label = self._resolve_trace_label(label)
        seconds = self.RETRY_WAIT_TIME_OFFSET_UNIT * multiplier
        logging.info("Sleep %ss for step [%s]", seconds, step_label)
        time.sleep(seconds)

    def _click_button(self, driver, button_search_type, button_search_key):
        """Click an element only after it becomes clickable."""
        self._log_page_state(driver, f"before_click_{button_search_key}")
        click_element = driver.find_element(button_search_type, button_search_key)
        WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.element_to_be_clickable(click_element))
        driver.execute_script("arguments[0].click();", click_element)

    def _get_webdriver(self):
        return create_chromium_driver(self.DRIVER_IMPLICITY_WAIT_TIME)

    def create_webdriver(self):
        return self._get_webdriver()

    def step_sleep(self, driver, label: Optional[str] = None, multiplier: int = 1) -> None:
        self._step_sleep(driver, label, multiplier)

    def log_page_state(self, driver, label: Optional[str] = None) -> None:
        self._log_page_state(driver, label)

    def fetch(self):

        """main logic here"""

        driver = self._get_webdriver()
        ErrorWatcher.instance().set_driver(driver)
        updater = self.updater or SensorUpdater()

        try:
            self._step_sleep(driver, "after_webdriver_init")
            logging.info("Webdriver initialized.")
            self.login_manager.restore_or_login(driver)

            self._step_sleep(driver, "after_login_success")
            logging.info(f"Try to get the userid list")
            user_id_list = self.navigator.get_user_ids(driver)
            logging.info(f"Here are a total of {len(user_id_list)} userids, which are {user_id_list} among which {self.IGNORE_USER_ID} will be ignored.")
            self._step_sleep(driver, "after_get_user_ids")

            for userid_index, user_id in enumerate(user_id_list):
                try:
                    if user_id in self.IGNORE_USER_ID:
                        logging.info(f"The user ID {user_id} will be ignored in user_id_list")
                        continue

                    progress = updater.get_progress(user_id)
                    should_open_balance_page = not (
                        self._is_progress_current(progress)
                        and self._has_completed_stage(progress, "balance")
                    )

                    if should_open_balance_page:
                        driver.get(BALANCE_URL)
                        self._log_page_state(driver, "after_open_balance_url")
                        self._step_sleep(driver, "after_open_balance_url")
                        current_userid = self.navigator.ensure_target_userid(driver, userid_index, expected_user_id=user_id)
                        self._step_sleep(driver, f"after_choose_balance_user_{userid_index}")
                        if current_userid in self.IGNORE_USER_ID:
                            logging.info(f"The user ID {current_userid} will be ignored in user_id_list")
                            continue
                    else:
                        logging.info(
                            "Skip opening balance page for %s because today's progress already passed balance stage.",
                            user_id,
                        )

                    (
                        balance,
                        last_daily_date,
                        last_daily_usage,
                        last_daily_charge,
                        yearly_charge,
                        yearly_usage,
                        month_charge,
                        month_usage,
                        valley_usage,
                        flat_usage,
                        peak_usage,
                        tip_usage,
                    ) = self._get_all_data(driver, user_id, userid_index, updater)
                    updater.update_one_userid(
                        user_id=user_id,
                        balance=balance,
                        last_daily_date=last_daily_date,
                        last_daily_usage=last_daily_usage,
                        yearly_charge=yearly_charge,
                        yearly_usage=yearly_usage,
                        month_charge=month_charge,
                        month_usage=month_usage,
                        last_daily_charge=last_daily_charge,
                        valley_usage=valley_usage,
                        flat_usage=flat_usage,
                        peak_usage=peak_usage,
                        tip_usage=tip_usage,
                    )

                    self._step_sleep(driver, f"after_update_user_state_{user_id}")
                except Exception as e:
                    if userid_index != len(user_id_list) - 1:
                        logging.info(f"The current user {user_id} data fetching failed {e}, the next user data will be fetched.")
                    else:
                        logging.info(f"The user {user_id} data fetching failed, {e}")
                        logging.info("Webdriver will quit after processing the user list.")
                    continue
        except Exception as e:
            logging.error(
                f"Webdriver quit abnormly, reason: {e}. {self.RETRY_TIMES_LIMIT} retry times left.")
            raise
        finally:
            updater.close()
            driver.quit()

    def _open_bill_summary_page(self, driver):
        driver.get(ELECTRIC_BILL_SUMMARY_URL)
        self._log_page_state(driver, "after_open_bill_summary_url")
        self._step_sleep(driver, "after_open_bill_summary_url")
        WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.visibility_of_element_located((By.XPATH, "//div[contains(@class,'billContent_bill')]"))
        )

    def _get_bill_available_years(self, driver):
        years = []
        year_nodes = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.visibility_of_all_elements_located(
                (By.XPATH, "//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_year')]/span")
            )
        )
        for option in year_nodes:
            try:
                years.append(int(option.text.strip().replace("年", "")))
            except (TypeError, ValueError):
                continue
        return sorted(set(years), reverse=True)

    def _select_bill_year(self, driver, target_year: int) -> bool:
        target_year = int(target_year)
        try:
            active_year = driver.find_element(
                By.XPATH,
                "//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_sleectYear')]/span",
            ).text.strip()
            if active_year == f"{target_year}年":
                return True

            option = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        f"//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_year')]/span[normalize-space()='{target_year}年']",
                    )
                )
            )
            driver.execute_script("arguments[0].click();", option)
            self._step_sleep(driver, f"after_select_bill_year_{target_year}")
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                lambda d: d.find_element(
                    By.XPATH,
                    "//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_sleectYear')]/span",
                ).text.strip()
                == f"{target_year}年"
            )
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.visibility_of_element_located((By.XPATH, "//div[contains(@class,'billContent_bill')]"))
            )
            return True
        except Exception as exc:
            logging.warning("Failed to switch bill year to %s: %s", target_year, exc)
            return False

    def _expand_bill_summary(self, driver):
        for _ in range(6):
            buttons = driver.find_elements(By.XPATH, "//div[contains(@class,'content_button')]//*[contains(text(),'查看更多')]")
            if not buttons:
                return
            previous_count = len(driver.find_elements(By.XPATH, "//div[contains(@class,'billContent_bill')]"))
            driver.execute_script("arguments[0].click();", buttons[0])
            self._step_sleep(driver, "after_expand_bill_summary")
            current_count = len(driver.find_elements(By.XPATH, "//div[contains(@class,'billContent_bill')]"))
            if current_count <= previous_count:
                return

    def _parse_bill_month_key(self, bill_time_text: str):
        match = re.search(r"(\d{4})/(\d{2})/\d{2}", str(bill_time_text).strip())
        if not match:
            return None
        return f"{match.group(1)}-{match.group(2)}"

    def _get_visible_bill_month_keys(self, driver):
        month_keys = []
        month_nodes = driver.find_elements(By.XPATH, "//div[contains(@class,'bill_time')]/span[1]")
        for node in month_nodes:
            month_key = self._parse_bill_month_key(node.text)
            if month_key:
                month_keys.append(month_key)
        return month_keys

    def _row_has_nonzero_tou(self, row):
        if not row:
            return False
        return any(float(row.get(field, 0.0) or 0.0) > 0 for field in ("valley_usage", "flat_usage", "peak_usage", "tip_usage"))

    def _monthly_tou_needs_sync(self, month_key: str) -> bool:
        existing = self.db.get_period_row("monthly_usage", "month", month_key)
        return not self._row_has_nonzero_tou(existing)

    def _open_bill_detail_by_index(self, driver, bill_index: int):
        month_rows = driver.find_elements(By.XPATH, "//div[contains(@class,'billList_content')]")
        if bill_index >= len(month_rows):
            return False
        arrow = month_rows[bill_index].find_element(By.XPATH, ".//img[contains(@class,'back_right')]")
        driver.execute_script("arguments[0].click();", arrow)
        self._step_sleep(driver, f"after_open_bill_detail_{bill_index}")
        WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.visibility_of_element_located((By.XPATH, "//div[contains(@class,'billInfo_cycle')]"))
        )
        return True

    def _parse_monthly_bill_detail(self, driver):
        try:
            detail = normalize_bill_detail(selected_vue_data(driver))
            if detail.get("month"):
                return {
                    "month": detail.get("month"),
                    "total_usage": detail.get("usage"),
                    "total_charge": detail.get("charge"),
                    "valley_usage": detail.get("valley_usage") or 0.0,
                    "flat_usage": detail.get("flat_usage") or 0.0,
                    "peak_usage": detail.get("peak_usage") or 0.0,
                    "tip_usage": detail.get("tip_usage") or 0.0,
                }
        except Exception as exc:
            logging.debug("Failed to parse monthly bill detail from Vue state, fallback to DOM: %s", exc)

        try:
            cycle_text = driver.find_element(By.XPATH, "//div[contains(@class,'billInfo_cycle')]").text
            month_key = self._parse_bill_month_key(cycle_text)
            if month_key is None:
                return None

            total_usage = None
            try:
                total_usage_text = driver.find_element(
                    By.XPATH,
                    "//span[contains(text(),'正向有功(总)')]/ancestor::div[contains(@class,'item_item')][1]/span[contains(@class,'thisReadPq')]",
                ).text
                total_usage = float(total_usage_text.strip())
            except Exception:
                total_usage = None

            tou_values = {
                "valley_usage": 0.0,
                "flat_usage": 0.0,
                "peak_usage": 0.0,
                "tip_usage": 0.0,
            }
            tou_items = driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'wrap_pvQtyJm')]//div[contains(@class,'right_top')]//div[contains(@class,'top_item')]",
            )
            for item in tou_items:
                label = item.find_element(By.XPATH, ".//span[contains(@class,'name')]").text.strip()
                value_text = item.find_element(By.XPATH, ".//div[contains(@class,'item_right')]/span").text.strip()
                value = float(value_text or 0)
                if "低谷" in label or "谷" in label:
                    tou_values["valley_usage"] = value
                elif "平" in label:
                    tou_values["flat_usage"] = value
                elif "峰" in label:
                    tou_values["peak_usage"] = value
                elif "尖" in label:
                    tou_values["tip_usage"] = value

            if total_usage is None:
                total_usage = round(sum(tou_values.values()), 2)

            total_charge = None
            matched_charge_count = 0
            charge_items = driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'wrap_electricChargeJm')]//div[contains(@class,'prcGroup_amtGroup')]//div[contains(@class,'amt_item')]",
            )
            for item in charge_items:
                spans = item.find_elements(By.XPATH, "./span")
                if len(spans) < 4:
                    continue
                label = spans[0].text.strip()
                amount_text = spans[3].text.strip()
                amount = float(amount_text or 0)
                if "峰" in label or "平" in label or "谷" in label or "尖" in label:
                    total_charge = (total_charge or 0.0) + amount
                    matched_charge_count += 1
            if matched_charge_count:
                total_charge = round(total_charge or 0.0, 2)

            return {
                "month": month_key,
                "total_usage": total_usage,
                "total_charge": total_charge,
                **tou_values,
            }
        except Exception as exc:
            logging.warning("Failed to parse monthly bill detail: %s", exc)
            return None

    def _sync_monthly_bill_tou(self, driver, user_id: str):
        if self.db is None:
            return [], False
        if not self.db.connect_user_db(user_id):
            return [], False
        rows = []
        touched_years = set()
        verified = False
        try:
            self._open_bill_summary_page(driver)
            available_years = self._get_bill_available_years(driver)
            current_year = datetime.now().year
            current_month = datetime.now().month
            target_years = [year for year in available_years if year == current_year]
            if current_month <= 2 and (current_year - 1) in available_years:
                target_years.append(current_year - 1)

            for target_year in target_years:
                if not self._select_bill_year(driver, target_year):
                    continue
                verified = True

                existing_year = self.db.get_period_row("yearly_usage", "year", str(target_year))
                needs_deep_sync = not self._row_has_nonzero_tou(existing_year)

                visible_months = self._get_visible_bill_month_keys(driver)
                pending_months = [month_key for month_key in visible_months if self._monthly_tou_needs_sync(month_key)]

                if needs_deep_sync:
                    self._expand_bill_summary(driver)
                    visible_months = self._get_visible_bill_month_keys(driver)
                    pending_months = [month_key for month_key in visible_months if self._monthly_tou_needs_sync(month_key)]

                if not pending_months:
                    logging.info("Monthly bill TOU is already complete for visible months in %s.", target_year)
                    continue

                for bill_index, month_key in enumerate(visible_months):
                    if month_key not in pending_months:
                        continue
                    if not self._open_bill_detail_by_index(driver, bill_index):
                        continue
                    detail = self._parse_monthly_bill_detail(driver)
                    if detail is not None:
                        rows.append(detail)
                    driver.back()
                    self._step_sleep(driver, f"after_return_bill_summary_{target_year}_{bill_index}")
                    WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                        EC.visibility_of_element_located((By.XPATH, "//div[contains(@class,'billContent_bill')]"))
                    )
                    if needs_deep_sync:
                        self._expand_bill_summary(driver)

            for row in rows:
                existing = self.db.get_period_row("monthly_usage", "month", row["month"]) or {}
                self.db.insert_monthly_data(
                    {
                        "month": row["month"],
                        "total_usage": row.get("total_usage") if row.get("total_usage") is not None else existing.get("total_usage", 0.0),
                        "total_charge": row.get("total_charge") if row.get("total_charge") is not None else existing.get("total_charge"),
                        "valley_usage": row.get("valley_usage", 0.0),
                        "flat_usage": row.get("flat_usage", 0.0),
                        "peak_usage": row.get("peak_usage", 0.0),
                        "tip_usage": row.get("tip_usage", 0.0),
                    }
                )
                touched_years.add(row["month"][:4])

            for year in sorted(touched_years):
                self.db.sync_yearly_from_monthly(year)
        finally:
            self.db.close_connect()

        return rows, verified

    def _select_usage_year(self, driver, target_year: int) -> bool:
        target_year = int(target_year)
        input_xpath = '//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input'
        try:
            year_input = driver.find_element(By.XPATH, input_xpath)
            current_value = (year_input.get_attribute("value") or "").strip()
            if current_value == str(target_year):
                return True

            self._click_button(driver, By.XPATH, input_xpath)
            self._step_sleep(driver, f"after_open_usage_year_selector_{target_year}")
            option = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.element_to_be_clickable((By.XPATH, f"//span[text() = '{target_year}']"))
            )
            driver.execute_script("arguments[0].click();", option)
            self._step_sleep(driver, f"after_select_usage_year_{target_year}")
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                lambda d: (d.find_element(By.XPATH, input_xpath).get_attribute("value") or "").strip() == str(target_year)
            )
            return True
        except Exception as exc:
            logging.warning("Failed to switch usage year to %s: %s", target_year, exc)
            return False

    def _get_all_data(self, driver, user_id, userid_index, updater: SensorUpdater):
        progress = updater.get_progress(user_id)
        cached = updater.get_cached_user_data(user_id)
        if not self._is_progress_current(progress):
            updater.update_progress_stage(user_id, "none", fetch_date=self._progress_date())
            progress = updater.get_progress(user_id)
            cached = updater.get_cached_user_data(user_id)

        balance = cached.get("balance")
        if self._has_completed_stage(progress, "balance"):
            logging.info("Skip balance fetch for %s because today's progress already exists.", user_id)
        else:
            balance = self._get_electric_balance(driver)
            if (balance is None):
                logging.error(f"Get electricity charge balance for {user_id} failed, Pass.")
            else:
                logging.info(
                    f"Get electricity charge balance for {user_id} successfully, balance is {balance} CNY.")
                updater.save_partial_data(user_id, balance=balance)
                updater.update_progress_stage(user_id, "balance", fetch_date=self._progress_date())
                progress = updater.get_progress(user_id)
        #time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # swithc to electricity usage page
        self.usage_page.open_for_user(driver, user_id, userid_index)
        # get data for each user id
        yearly_usage = cached.get("yearly_usage")
        yearly_charge = cached.get("yearly_charge")
        if self._has_completed_stage(progress, "yearly"):
            logging.info("Skip yearly fetch for %s because today's progress already exists.", user_id)
        else:
            yearly_usage, yearly_charge = self._get_yearly_data(driver)

            if yearly_usage is None:
                logging.error(f"Get year power usage for {user_id} failed, pass")
            else:
                logging.info(
                    f"Get year power usage for {user_id} successfully, usage is {yearly_usage} kwh")
            if yearly_charge is None:
                logging.error(f"Get year power charge for {user_id} failed, pass")
            else:
                logging.info(
                    f"Get year power charge for {user_id} successfully, yealrly charge is {yearly_charge} CNY")
            updater.save_partial_data(user_id, yearly_usage=yearly_usage, yearly_charge=yearly_charge)
            updater.update_progress_stage(user_id, "yearly", fetch_date=self._progress_date())
            progress = updater.get_progress(user_id)

        # 按月获取数据
        month = None
        month_usage = None
        month_charge = None
        if self._has_completed_stage(progress, "monthly"):
            logging.info("Skip monthly fetch for %s because today's progress already exists.", user_id)
            if cached.get("month_usage") is not None:
                month_usage = [cached.get("month_usage")]
            if cached.get("month_charge") is not None:
                month_charge = [cached.get("month_charge")]
        else:
            month, month_usage, month_charge = self._get_month_usage(driver)
            if month is None:
                logging.error(f"Get month power usage for {user_id} failed, pass")
            else:
                for m in range(len(month)):
                    logging.info(f"Get month power charge for {user_id} successfully, {month[m]} usage is {month_usage[m]} KWh, charge is {month_charge[m]} CNY.")
                updater.save_partial_data(
                    user_id,
                    month_usage=month_usage[-1] if month_usage else None,
                    month_charge=month_charge[-1] if month_charge else None,
                )
                updater.update_progress_stage(user_id, "monthly", fetch_date=self._progress_date())
                progress = updater.get_progress(user_id)
        # get yesterday usage
        last_daily_date = cached.get("last_daily_date")
        last_daily_usage = cached.get("last_daily_usage")
        if self._has_completed_stage(progress, "daily"):
            logging.info("Skip daily fetch for %s because today's progress already exists.", user_id)
        else:
            last_daily_date, last_daily_usage = self._get_yesterday_usage(driver)
            if last_daily_usage is None:
                logging.error(f"Get daily power consumption for {user_id} failed, pass")
            else:
                logging.info(
                    f"Get daily power consumption for {user_id} successfully, , {last_daily_date} usage is {last_daily_usage} kwh.")
                updater.save_partial_data(
                    user_id,
                    last_daily_date=last_daily_date,
                    last_daily_usage=last_daily_usage,
                )
                updater.update_progress_stage(user_id, "daily", fetch_date=self._progress_date())
                progress = updater.get_progress(user_id)
        valley_usage = cached.get("valley_usage")
        flat_usage = cached.get("flat_usage")
        peak_usage = cached.get("peak_usage")
        tip_usage = cached.get("tip_usage")
        daily_tou_map = {}
        if self._has_completed_stage(progress, "tou"):
            logging.info("Skip TOU fetch for %s because today's progress already exists.", user_id)
        else:
            daily_tou_map = self._get_recent_daily_usage_breakdown_map(driver, limit_days=7)
            latest_tou = daily_tou_map.get(last_daily_date) if last_daily_date else None
            if latest_tou:
                valley_usage = latest_tou.get("valley_usage")
                flat_usage = latest_tou.get("flat_usage")
                peak_usage = latest_tou.get("peak_usage")
                tip_usage = latest_tou.get("tip_usage")
            else:
                valley_usage, flat_usage, peak_usage, tip_usage = self._get_latest_daily_usage_breakdown(driver)
                if last_daily_date and any(value is not None for value in (valley_usage, flat_usage, peak_usage, tip_usage)):
                    daily_tou_map[last_daily_date] = {
                        "valley_usage": valley_usage or 0.0,
                        "flat_usage": flat_usage or 0.0,
                        "peak_usage": peak_usage or 0.0,
                        "tip_usage": tip_usage or 0.0,
                    }

            if daily_tou_map or any(value is not None for value in (valley_usage, flat_usage, peak_usage, tip_usage)):
                logging.info(
                    f"Get recent time-of-use power usage for {user_id} successfully, latest valley={valley_usage} KWh, flat={flat_usage} KWh, peak={peak_usage} KWh, tip={tip_usage} KWh, days={len(daily_tou_map)}."
                )
                updater.save_partial_data(
                    user_id,
                    valley_usage=valley_usage,
                    flat_usage=flat_usage,
                    peak_usage=peak_usage,
                    tip_usage=tip_usage,
                )
                updater.update_progress_stage(user_id, "tou", fetch_date=self._progress_date())
                progress = updater.get_progress(user_id)
            else:
                logging.error(f"Get latest time-of-use power usage for {user_id} failed, pass")

        last_daily_charge = None

        # 新增储存用电量
        if self.db is not None:
            # 将数据存储到数据库
            logging.info(f"db is {self.db_type}, we will store the data to the database.")
            # 按天获取数据 7天/30天
            date, usages = self._get_daily_usage_data(driver)
            last_daily_charge = self._save_user_data(
                user_id,
                last_daily_date,
                last_daily_usage,
                last_daily_charge,
                date,
                usages,
                month,
                month_usage,
                month_charge,
                yearly_charge,
                yearly_usage,
                valley_usage,
                flat_usage,
                peak_usage,
                tip_usage,
                daily_tou_map,
            )
            updater.save_partial_data(user_id, last_daily_charge=last_daily_charge)
            updater.update_progress_stage(user_id, "persist", fetch_date=self._progress_date())
            progress = updater.get_progress(user_id)

            # Backfill TOU breakdown for days that have aged out of the
            # "recent 7-day" window. The 95598 daily Vue state only
            # exposes thisPPq / thisVPq for the most recent 7 days; older
            # rows in our DB carry peak/valley=0. Pull TOU for the prior
            # range via the date-range Vue query so dashboards (and the
            # monthly bill TOU calculator) see correct numbers.
            if not self._has_completed_stage(progress, "tou_backfill"):
                try:
                    self._backfill_historic_daily_tou(driver, user_id, days=self._get_daily_usage_window_days())
                    updater.update_progress_stage(user_id, "tou_backfill", fetch_date=self._progress_date())
                    progress = updater.get_progress(user_id)
                except Exception as exc:
                    logging.warning(
                        "Historic daily TOU backfill failed for %s (continuing): %s",
                        user_id, exc,
                    )
        else:
            logging.info("db is None, we will not store the data to the database.")

        if self.db is not None:
            if self._has_completed_stage(progress, "billing"):
                logging.info("Skip monthly billing TOU fetch for %s because today's progress already exists.", user_id)
            else:
                bill_rows, bill_verified = self._sync_monthly_bill_tou(driver, user_id)
                if bill_rows:
                    months = ", ".join(row["month"] for row in bill_rows)
                    logging.info("Synced monthly bill TOU for %s: %s", user_id, months)
                elif bill_verified:
                    logging.info("Monthly bill TOU check completed for %s with no new data.", user_id)
                else:
                    logging.warning("Monthly bill TOU check did not complete for %s", user_id)
                if bill_verified:
                    updater.update_progress_stage(user_id, "billing", fetch_date=self._progress_date())
                    progress = updater.get_progress(user_id)

        if self.db is None or self._has_completed_stage(progress, "billing"):
            updater.update_progress_stage(user_id, "complete", fetch_date=self._progress_date())

        if month_charge:
            month_charge = month_charge[-1]
        else:
            month_charge = None
        if month_usage:
            month_usage = month_usage[-1]
        else:
            month_usage = None

        return (
            balance,
            last_daily_date,
            last_daily_usage,
            last_daily_charge,
            yearly_charge,
            yearly_usage,
            month_charge,
            month_usage,
            valley_usage,
            flat_usage,
            peak_usage,
            tip_usage,
        )

    def _get_electric_balance(self, driver):
        try:
            balance = normalize_balance(selected_vue_data(driver)).get("balance")
            if balance is not None:
                logging.info("Read electricity balance from Vue state: %s CNY", balance)
                return balance
        except Exception as exc:
            logging.debug("Failed to read balance from Vue state, fallback to DOM: %s", exc)

        try:
            try:
                # 定位是否有"应交金额"标题（确认是后缴费账户）
                title_text = driver.find_element(By.XPATH, "//p[contains(@class, 'balance_title') and contains(text(), '应交金额')]").text
                if "应交金额" in title_text:
                    # 后缴费账户：需要查找"账户余额"，而不是"应交金额"
                    # 查找包含"账户余额"的balance_title元素，然后获取其内部的金额
                    balance_content = driver.find_element(By.XPATH, "//p[contains(@class, 'balance_title') and contains(text(), '账户余额')]")
                    # 提取数字部分
                    balance_text = re.sub(r'[^\d.]', '', balance_content.text)
                    if balance_text:
                        return float(balance_text)
            except Exception as e:
                # 后缴费账户解析失败，继续尝试预缴费账户逻辑
                pass

            # 2. 预缴费账户的"账户余额"（原逻辑）
            balance_text = driver.find_element(By.CLASS_NAME, "cff8").text
            balance = balance_text.replace("元", "")
            if "欠费" in balance_text:
                return -float(balance)
            else:
                return float(balance)
        except Exception as e:
            logging.error(f"Failed to get balance: {e}")
            return None

    def _get_yearly_data(self, driver, target_year=None):

        try:
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            self._step_sleep(driver, "after_open_yearly_tab")
            desired_year = target_year
            if desired_year is None and datetime.now().month == 1:
                desired_year = datetime.now().year - 1
            if desired_year is not None and not self._select_usage_year(driver, desired_year):
                return None, None
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.visibility_of_element_located((By.XPATH, "//*[@id='pane-first']//ul[contains(@class,'total')]"))
            )
            usage_data = normalize_usage(selected_vue_data(driver))
            if usage_data.get("yearly_usage") is not None:
                logging.info(
                    "Read yearly usage data from Vue state: usage=%s, charge=%s",
                    usage_data.get("yearly_usage"),
                    usage_data.get("yearly_charge"),
                )
                return usage_data.get("yearly_usage"), usage_data.get("yearly_charge")
        except Exception as e:
            logging.error(f"The yearly data get failed : {e}")
            return None, None

        # get data
        try:
            yearly_usage = driver.find_element(By.XPATH, "//*[@id='pane-first']//ul[contains(@class,'total')]/li[1]/span").text
        except Exception as e:
            logging.error(f"The yearly_usage data get failed : {e}")
            yearly_usage = None

        try:
            yearly_charge = driver.find_element(By.XPATH, "//*[@id='pane-first']//ul[contains(@class,'total')]/li[2]/span").text
        except Exception as e:
            logging.error(f"The yearly_charge data get failed : {e}")
            yearly_charge = None

        return yearly_usage, yearly_charge

    def _backfill_historic_daily_tou(self, driver, user_id: str, days: int = 7) -> None:
        """Use the date-range Vue query to refresh TOU breakdown for the
        last ``days`` days (matches the user's daily_usage_window_days
        option). Only fires for dates that DON'T have any TOU breakdown
        in the DB yet (peak+valley+flat+tip == 0) — already-settled
        historic days are immutable and don't need to be re-queried.
        Persists each row via ``insert_daily_data`` which COALESCEs
        the TOU fields, so existing values are only overwritten when
        the range query gives a real number.
        """
        if self.db is None:
            return
        from datetime import datetime, timedelta
        today = datetime.now().date()
        window_start_date = today - timedelta(days=days)
        # today's row is updated by the main daily fetch step, skip here.
        today_str = today.isoformat()

        from scripts.support.db import SqliteDB
        db = SqliteDB()
        if not db.connect_user_db(user_id):
            return
        try:
            # First-init / month-not-covered guard: if the window
            # doesn't reach the first of the current month AND we have
            # no daily row for that day yet, extend the backfill
            # window back to the 1st so we pull a full month-to-date
            # peak/valley breakdown even when the user picked
            # daily_usage_window_days=7. Only widens once per month.
            month_start = today.replace(day=1)
            if window_start_date > month_start:
                cur = db.connect.cursor()
                cur.execute(
                    f"SELECT 1 FROM {db.DAILY_TABLE} WHERE user_id = ? AND date = ?",
                    (db.user_id, month_start.isoformat()),
                )
                if cur.fetchone() is None:
                    logging.info(
                        "TOU backfill: extending window to month start %s "
                        "(daily_usage_window_days=%s left current month uncovered).",
                        month_start.isoformat(), days,
                    )
                    window_start_date = month_start
                cur.close()
            window_start = window_start_date.isoformat()

            cursor = db.connect.cursor()
            cursor.execute(
                f"""
                SELECT date FROM {db.DAILY_TABLE}
                WHERE user_id = ? AND date >= ? AND date < ?
                  AND (COALESCE(peak_usage, 0) + COALESCE(valley_usage, 0)
                       + COALESCE(flat_usage, 0) + COALESCE(tip_usage, 0)) = 0
                ORDER BY date
                """,
                (db.user_id, window_start, today_str),
            )
            unsettled = [r[0] for r in cursor.fetchall()]
            # Also include month-to-date dates that aren't in DB yet —
            # range collector will return them and we'll insert fresh.
            if window_start_date <= month_start:
                d = month_start
                while d < today:
                    iso = d.isoformat()
                    cur2 = db.connect.cursor()
                    cur2.execute(
                        f"SELECT 1 FROM {db.DAILY_TABLE} WHERE user_id = ? AND date = ?",
                        (db.user_id, iso),
                    )
                    if cur2.fetchone() is None and iso not in unsettled:
                        unsettled.append(iso)
                    cur2.close()
                    d += timedelta(days=1)
                unsettled.sort()
            cursor.close()

            if not unsettled:
                logging.info(
                    "Historic TOU backfill: all daily rows in last %s days have TOU breakdown, skipping range fetch.",
                    days,
                )
                # Even when no Vue query is needed, recompute local
                # daily charges so any rate change since the previous
                # fetch is applied to historic rows.
                db.close_connect()
                self._recompute_daily_charges(user_id, window_start, today_str)
                return

            range_start = unsettled[0]
            range_end = unsettled[-1]
            logging.info(
                "Historic TOU backfill: %s dates missing TOU; querying range %s..%s",
                len(unsettled), range_start, range_end,
            )

            collector = VueDailyRangeCollector(
                click_button=self._click_button,
                step_sleep=self._step_sleep,
                log_page_state=self._log_page_state,
            )
            rows = collector.collect(driver, range_start, range_end)
            if not rows:
                logging.info("Historic TOU backfill: no rows returned for %s..%s", range_start, range_end)
                return
            persisted = 0
            for row in rows:
                date = row.get("date")
                if not date:
                    continue
                total_usage = (
                    float(row.get("valley_usage") or 0)
                    + float(row.get("flat_usage") or 0)
                    + float(row.get("peak_usage") or 0)
                    + float(row.get("tip_usage") or 0)
                )
                # Recompute total_charge using the active TOU rates so it
                # reflects the current single-tier/env-override settings
                # rather than whatever rate was active when this row was
                # first persisted.
                month_before = db.get_month_total_usage_before(date)
                charge = self.tou_price_resolver.calculate_daily_charge(
                    date,
                    row.get("valley_usage"),
                    row.get("flat_usage"),
                    row.get("peak_usage"),
                    row.get("tip_usage"),
                    month_before,
                )
                ok = db.insert_daily_data(
                    {
                        "date": date,
                        "total_usage": total_usage or float(row.get("total_usage") or 0),
                        "total_charge": charge,
                        "valley_usage": row.get("valley_usage", 0.0),
                        "flat_usage": row.get("flat_usage", 0.0),
                        "peak_usage": row.get("peak_usage", 0.0),
                        "tip_usage": row.get("tip_usage", 0.0),
                    }
                )
                if ok:
                    persisted += 1
            logging.info(
                "Historic TOU backfill for %s: collected=%s persisted=%s range=%s..%s",
                user_id, len(rows), persisted, range_start, range_end,
            )
        finally:
            db.close_connect()

        # Recompute daily total_charge for every row in window so the
        # value reflects current TOU rates even when the row was first
        # persisted under a different config. Local-only, no network.
        self._recompute_daily_charges(user_id, window_start, today_str)

    def _recompute_daily_charges(self, user_id: str, window_start: str, window_end_exclusive: str) -> None:
        """Walk daily_usage rows in [window_start, window_end_exclusive)
        with non-zero TOU breakdown and rewrite total_charge from the
        current TOU price resolver."""
        from scripts.support.db import SqliteDB
        db = SqliteDB()
        if not db.connect_user_db(user_id):
            return
        try:
            cursor = db.connect.cursor()
            cursor.execute(
                f"""
                SELECT date, valley_usage, flat_usage, peak_usage, tip_usage
                FROM {db.DAILY_TABLE}
                WHERE user_id = ? AND date >= ? AND date < ?
                  AND (COALESCE(peak_usage, 0) + COALESCE(valley_usage, 0)
                       + COALESCE(flat_usage, 0) + COALESCE(tip_usage, 0)) > 0
                ORDER BY date
                """,
                (db.user_id, window_start, window_end_exclusive),
            )
            rows = cursor.fetchall()
            cursor.close()
            updated = 0
            for date, valley, flat, peak, tip in rows:
                month_before = db.get_month_total_usage_before(date)
                charge = self.tou_price_resolver.calculate_daily_charge(
                    date, valley, flat, peak, tip, month_before,
                )
                if charge is None:
                    continue
                cur = db.connect.cursor()
                cur.execute(
                    f"UPDATE {db.DAILY_TABLE} SET total_charge = ?, updated_at = CURRENT_TIMESTAMP "
                    f"WHERE user_id = ? AND date = ?",
                    (charge, db.user_id, date),
                )
                cur.close()
                updated += 1
            db.connect.commit()
            logging.info(
                "Recomputed daily total_charge for %s rows in %s..%s",
                updated, window_start, window_end_exclusive,
            )
            # Roll the new daily charges up into monthly_usage and
            # yearly_usage so total_electricity_charge / yearly_*
            # sensors reflect the recomputed values instead of the
            # stale figures persisted at fetch time (which used the
            # provider's old per-kWh rate before tou_peak_rate /
            # tou_valley_rate kicked in).
            months_touched = set()
            for date, *_ in rows:
                if date and len(date) >= 7:
                    months_touched.add(date[:7])
            years_touched = set()
            for month in sorted(months_touched):
                if db.sync_monthly_from_daily(month):
                    years_touched.add(month[:4])
            for year in sorted(years_touched):
                db.sync_yearly_from_monthly(year)
            if months_touched or years_touched:
                logging.info(
                    "Synced monthly_usage for %s and yearly_usage for %s after recompute.",
                    sorted(months_touched), sorted(years_touched),
                )
        finally:
            db.close_connect()

    def _get_yesterday_usage(self, driver):
        """获取最近一次用电量"""
        try:
            # 点击日用电量
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            self._step_sleep(driver, "after_open_daily_tab_for_yesterday")
            # wait for data displayed
            usage_element = driver.find_element(By.XPATH,
                                                "//div[@class='el-tab-pane dayd']//div[@class='el-table__body-wrapper is-scrolling-none']/table/tbody/tr[1]/td[2]/div")
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.visibility_of(usage_element)) # 等待用电量出现

            # 增加是哪一天
            date_element = driver.find_element(By.XPATH,
                                                "//div[@class='el-tab-pane dayd']//div[@class='el-table__body-wrapper is-scrolling-none']/table/tbody/tr[1]/td[1]/div")
            last_daily_date = date_element.text # 获取最近一次用电量的日期
            return last_daily_date, float(usage_element.text)
        except Exception as e:
            logging.error(f"The yesterday data get failed : {e}")
            return None, None

    def _get_latest_daily_usage_breakdown(self, driver):
        """获取最近一天的谷平峰尖用电量"""
        selectors = (
            ".//p[.//text()[contains(.,'谷用电')]]//span[contains(@class,'num')]",
            ".//p[.//text()[contains(.,'平用电')]]//span[contains(@class,'num')]",
            ".//p[.//text()[contains(.,'峰用电')]]//span[contains(@class,'num')]",
            ".//p[.//text()[contains(.,'尖用电')]]//span[contains(@class,'num')]",
        )
        try:
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            self._step_sleep(driver, "after_open_daily_tab_for_tou_breakdown")
            usage_data = normalize_usage(selected_vue_data(driver))
            daily_rows = [row for row in usage_data.get("daily", []) if row.get("usage") is not None]
            if daily_rows:
                latest = daily_rows[0]
                logging.info("Read latest daily TOU data from Vue state: %s", latest.get("date"))
                return (
                    latest.get("valley_usage"),
                    latest.get("flat_usage"),
                    latest.get("peak_usage"),
                    latest.get("tip_usage"),
                )
            expand_icon = driver.find_element(
                By.XPATH,
                "//div[@class='el-tab-pane dayd']//div[contains(@class,'el-table__body-wrapper')]//table/tbody/tr[1]//div[contains(@class,'el-table__expand-icon')]",
            )
            if "el-table__expand-icon--expanded" not in (expand_icon.get_attribute("class") or ""):
                driver.execute_script("arguments[0].click();", expand_icon)
            expanded_cell = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.visibility_of_element_located(
                    (
                        By.XPATH,
                        "//div[@class='el-tab-pane dayd']//table/tbody/tr[1]/following-sibling::tr[1]//td[contains(@class,'el-table__expanded-cell')]",
                    )
                )
            )
            values = []
            for selector in selectors:
                values.append(float(expanded_cell.find_element(By.XPATH, selector).text.strip()))
            return tuple(values)
        except Exception as e:
            logging.error(f"The latest daily usage breakdown data get failed : {e}")
            return None, None, None, None

    def _get_recent_daily_usage_breakdown_map(self, driver, limit_days=7):
        """获取最近 N 天逐天谷平峰尖用电量，返回 {date: {...}}。"""
        selectors = {
            "valley_usage": ".//p[.//text()[contains(.,'谷用电')]]//span[contains(@class,'num')]",
            "flat_usage": ".//p[.//text()[contains(.,'平用电')]]//span[contains(@class,'num')]",
            "peak_usage": ".//p[.//text()[contains(.,'峰用电')]]//span[contains(@class,'num')]",
            "tip_usage": ".//p[.//text()[contains(.,'尖用电')]]//span[contains(@class,'num')]",
        }
        daily_tou_map = {}
        try:
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            self._step_sleep(driver, "after_open_daily_tab_for_recent_tou_breakdown")
            if not self._set_daily_retention_days(driver, retention_days=7):
                return daily_tou_map

            usage_data = normalize_usage(selected_vue_data(driver))
            for row in usage_data.get("daily", [])[:limit_days]:
                row_date = row.get("date")
                if not row_date:
                    continue
                daily_tou_map[row_date] = {
                    "valley_usage": row.get("valley_usage", 0.0) or 0.0,
                    "flat_usage": row.get("flat_usage", 0.0) or 0.0,
                    "peak_usage": row.get("peak_usage", 0.0) or 0.0,
                    "tip_usage": row.get("tip_usage", 0.0) or 0.0,
                }
            if daily_tou_map:
                logging.info("Read %s recent daily TOU rows from Vue state.", len(daily_tou_map))
                return daily_tou_map

            data_rows = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.presence_of_all_elements_located(
                    (
                        By.XPATH,
                        "//*[@id='pane-second']//div[contains(@class,'el-table__body-wrapper')]//table/tbody/tr[./td[1]/div and ./td[2]/div]",
                    )
                )
            )
            for row in data_rows[:limit_days]:
                row_date = (row.find_element(By.XPATH, "./td[1]/div").text or "").strip()
                if not row_date:
                    continue
                expand_icon = row.find_element(By.XPATH, ".//div[contains(@class,'el-table__expand-icon')]")
                if "el-table__expand-icon--expanded" not in (expand_icon.get_attribute("class") or ""):
                    driver.execute_script("arguments[0].click();", expand_icon)

                expanded_cell = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                    EC.visibility_of_element_located(
                        (
                            By.XPATH,
                            f"//*[@id='pane-second']//div[contains(@class,'el-table__body-wrapper')]//table/tbody/tr[./td[1]/div and normalize-space(./td[1]/div)='{row_date}']/following-sibling::tr[1]//td[contains(@class,'el-table__expanded-cell')]",
                        )
                    )
                )
                values = {}
                for key, selector in selectors.items():
                    try:
                        values[key] = float(expanded_cell.find_element(By.XPATH, selector).text.strip())
                    except Exception:
                        values[key] = 0.0
                daily_tou_map[row_date] = values
            return daily_tou_map
        except Exception as e:
            logging.error(f"The recent daily usage breakdown data get failed : {e}")
            return daily_tou_map

    def _set_daily_retention_days(self, driver, retention_days=30) -> bool:
        self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
        self._step_sleep(driver, "after_open_daily_tab_for_retention")
        try:
            if retention_days == 7:
                self._click_button(driver, By.XPATH, "//*[@id='pane-second']/div[1]/div/label[1]/span[1]")
            elif retention_days == 30:
                self._click_button(driver, By.XPATH, "//*[@id='pane-second']/div[1]/div/label[2]/span[1]")
            else:
                logging.error("Unsupported retention days value: %s", retention_days)
                return False
            self._step_sleep(driver, f"after_set_daily_retention_{retention_days}")
            return True
        except Exception as exc:
            logging.warning("Failed to switch daily retention days to %s: %s", retention_days, exc)
            return False

    def _extract_daily_usage_rows(self, driver):
        try:
            usage_data = normalize_usage(selected_vue_data(driver))
            daily_rows = [row for row in usage_data.get("daily", []) if row.get("date")]
            if daily_rows:
                return [row["date"] for row in daily_rows], [str(row.get("usage", 0.0)) for row in daily_rows]
        except Exception as exc:
            logging.debug("Failed to read daily rows from Vue state, fallback to DOM: %s", exc)

        usage_element = driver.find_element(
            By.XPATH,
            "//div[@class='el-tab-pane dayd']//div[@class='el-table__body-wrapper is-scrolling-none']/table/tbody/tr[1]/td[2]/div",
        )
        WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.visibility_of(usage_element))

        days_element = driver.find_elements(
            By.XPATH,
            "//*[@id='pane-second']/div[2]/div[2]/div[1]/div[3]/table/tbody/tr",
        )
        date = []
        usages = []
        for row in days_element:
            cells = row.find_elements(By.XPATH, "./td")
            if len(cells) < 2:
                logging.debug("Skip non-data daily row, td count=%s, text=%s", len(cells), row.text)
                continue

            day_elements = row.find_elements(By.XPATH, "./td[1]/div")
            usage_elements = row.find_elements(By.XPATH, "./td[2]/div")
            if not day_elements or not usage_elements:
                logging.debug("Skip malformed daily row, text=%s", row.text)
                continue

            day = (day_elements[0].text or "").strip()
            usage = (usage_elements[0].text or "").strip()
            if day and usage:
                usages.append(usage)
                date.append(day)
        return date, usages

    def _get_month_usage(self, driver, target_year=None):
        """获取每月用电量"""

        try:
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            self._step_sleep(driver, "after_open_monthly_tab")
            desired_year = target_year
            if desired_year is None and datetime.now().month == 1:
                desired_year = datetime.now().year - 1
            if desired_year is not None and not self._select_usage_year(driver, desired_year):
                return None, None, None
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.visibility_of_element_located(
                    (By.XPATH, "//*[@id='pane-first']//div[contains(@class,'el-table__body-wrapper')]//table/tbody")
                )
            )
            usage_data = normalize_usage(selected_vue_data(driver))
            month_rows = usage_data.get("months", [])
            if month_rows:
                logging.info("Read %s monthly usage rows from Vue state.", len(month_rows))
                return (
                    [row.get("month") for row in month_rows],
                    [row.get("usage") for row in month_rows],
                    [row.get("charge") for row in month_rows],
                )
            month_element = driver.find_element(
                By.XPATH,
                "//*[@id='pane-first']//div[contains(@class,'el-table__body-wrapper')]//table/tbody",
            ).text
            month_element = month_element.split("\n")
            month_element = [x for x in month_element if x != "MAX"]
            if len(month_element) % 3 != 0:
                month_element = month_element[:-(len(month_element) % 3)]
            # 将每月的用电量保存为List
            month = []
            usage = []
            charge = []
            for index in range(0, len(month_element), 3):
                month.append(month_element[index])
                usage.append(month_element[index + 1])
                charge.append(month_element[index + 2])
            return month, usage, charge
        except Exception as e:
            logging.error(f"The month data get failed : {e}")
            return None,None,None

    # 增加获取每日用电量的函数
    def _get_daily_usage_data(self, driver):
        """储存指定天数的用电量"""
        retention_days = self._get_daily_usage_window_days()
        if not self._set_daily_retention_days(driver, retention_days=retention_days):
            return
        return self._extract_daily_usage_rows(driver)

    @staticmethod
    def _get_daily_usage_window_days() -> int:
        raw_value = os.getenv("DAILY_USAGE_WINDOW_DAYS")
        try:
            return int(raw_value)
        except Exception:
            logging.warning("Invalid daily usage window value %r, fallback to 7.", raw_value)
            return 7

    def _save_user_data(
        self,
        user_id,
        last_daily_date,
        last_daily_usage,
        last_daily_charge,
        date,
        usages,
        month,
        month_usage,
        month_charge,
        yearly_charge,
        yearly_usage,
        valley_usage,
        flat_usage,
        peak_usage,
        tip_usage,
        daily_tou_map=None,
    ):
        return self.data_persister.save_user_data(
            user_id,
            last_daily_date,
            last_daily_usage,
            last_daily_charge,
            date,
            usages,
            month,
            month_usage,
            month_charge,
            yearly_charge,
            yearly_usage,
            valley_usage,
            flat_usage,
            peak_usage,
            tip_usage,
            daily_tou_map=daily_tou_map,
        )
