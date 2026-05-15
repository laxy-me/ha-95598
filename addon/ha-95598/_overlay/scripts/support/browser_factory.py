import os

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService


def create_chromium_driver(driver_wait_time: int):
    browser_window_size = os.getenv("BROWSER_WINDOW_SIZE", "1158,848")
    browser_language = os.getenv("BROWSER_LANGUAGE", "zh-HK,zh,en-US,en,zh-CN")
    browser_ua = os.getenv(
        "BROWSER_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    )
    browser_device_scale_factor = float(os.getenv("BROWSER_DEVICE_SCALE_FACTOR", "2"))
    browser_language_primary = browser_language.split(",")[0]
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument(f"--window-size={browser_window_size}")
    chrome_options.add_argument(f"--lang={browser_language_primary}")
    chrome_options.add_argument("--disable-features=Translate")
    chrome_options.add_argument(f"--force-device-scale-factor={browser_device_scale_factor}")
    chrome_options.add_argument("--high-dpi-support=1")
    chrome_options.add_argument("--password-store=basic")
    chrome_options.add_argument("--use-mock-keychain")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument(f"user-agent={browser_ua}")
    chrome_options.add_experimental_option(
        "prefs",
        {
            "intl.accept_languages": browser_language,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
    )
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})

    if "PYTHON_IN_DOCKER" in os.environ:
        chrome_options.binary_location = "/usr/bin/chromium"
        service = ChromeService(executable_path="/usr/bin/chromedriver")
    else:
        service = ChromeService()

    driver = webdriver.Chrome(
        options=chrome_options,
        service=service,
    )
    driver.implicitly_wait(driver_wait_time)
    return driver
