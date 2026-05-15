import os

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService


# Anti-detection JS injected on every page load via CDP.
#
# 95598 (Tencent waterproof wall) fingerprints selenium and silently
# refuses to issue a real captcha — the widget DOM is rendered but
# positioned at top: -1000000 (offscreen decoy) so any human + LLM
# solver pipeline never sees it.
#
# Vanilla chromedriver leaks several easy tells even with
# --disable-blink-features=AutomationControlled and excludeSwitches:
#   - navigator.webdriver === true
#   - navigator.plugins.length === 0
#   - navigator.languages === []
#   - window.chrome === undefined
#   - permissions.query for 'notifications' inconsistent with
#     Notification.permission
#   - WebGL UNMASKED_VENDOR / UNMASKED_RENDERER report "Google Inc."
#     + "ANGLE / SwiftShader" (headless GPU)
#   - global names matching ^cdc_ (chromedriver injects them)
#
# Override all of the above before any 95598 / Tencent script runs.
_STEALTH_JS = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['zh-CN', 'zh', 'en-US', 'en'],
    });
  } catch (e) {}
  try {
    const fakePlugins = [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format' },
    ];
    Object.defineProperty(navigator, 'plugins', { get: () => fakePlugins });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => [{ type: 'application/pdf', suffixes: 'pdf' }],
    });
  } catch (e) {}
  try {
    if (!window.chrome) window.chrome = {};
    window.chrome.runtime = window.chrome.runtime || {
      OnInstalledReason: {}, OnRestartRequiredReason: {},
      PlatformArch: {}, PlatformNaclArch: {}, PlatformOs: {},
      RequestUpdateCheckStatus: {},
    };
    window.chrome.app = window.chrome.app || {
      isInstalled: false,
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
    };
    window.chrome.csi = window.chrome.csi || (() => ({}));
    window.chrome.loadTimes = window.chrome.loadTimes || (() => ({}));
  } catch (e) {}
  try {
    const _origQuery = navigator.permissions && navigator.permissions.query;
    if (_origQuery) {
      navigator.permissions.query = (parameters) =>
        (parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : _origQuery.call(navigator.permissions, parameters));
    }
  } catch (e) {}
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter.call(this, p);
    };
    if (window.WebGL2RenderingContext) {
      const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function (p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter2.call(this, p);
      };
    }
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
  } catch (e) {}
  try {
    for (const k of Object.keys(window)) {
      if (/^cdc_/.test(k) || /^\$cdc_/.test(k)) {
        try { delete window[k]; } catch (e) {}
      }
    }
  } catch (e) {}
  try {
    const original = Function.prototype.toString;
    Function.prototype.toString = function () {
      if (this === navigator.permissions.query) return 'function query() { [native code] }';
      return original.call(this);
    };
  } catch (e) {}
})();
"""


def create_chromium_driver(driver_wait_time: int):
    browser_window_size = os.getenv("BROWSER_WINDOW_SIZE", "1158,848")
    browser_language = os.getenv("BROWSER_LANGUAGE", "zh-CN,zh,en-US,en")
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
    chrome_options.add_argument(f"--window-size={browser_window_size}")
    chrome_options.add_argument(f"--lang={browser_language_primary}")
    chrome_options.add_argument("--disable-features=Translate,IsolateOrigins,site-per-process")
    chrome_options.add_argument(f"--force-device-scale-factor={browser_device_scale_factor}")
    chrome_options.add_argument("--high-dpi-support=1")
    chrome_options.add_argument("--password-store=basic")
    chrome_options.add_argument("--use-mock-keychain")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
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

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _STEALTH_JS},
        )
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": browser_ua,
                "acceptLanguage": browser_language,
                "platform": "MacIntel",
            },
        )
    except Exception:
        pass

    return driver
