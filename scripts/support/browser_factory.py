import json
import logging
import os

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService


# Identity profile: Windows 11 / Chrome 148. Picked because:
#   - Windows is the dominant OS among Chinese 95598 users → highest blend-in
#     factor in Tencent 防水墙's risk-score baseline.
#   - Chrome major version 148 matches the chromium binary shipped with this
#     add-on image. Claiming a different major version vs the actual chromium
#     would leak via feature-detect timing or version-gated APIs.
#
# Every UA-revealing surface MUST agree, otherwise Tencent has a deterministic
# bot signal (which surfaces as 95598's RK001 "网络连接超时" — Tencent refused to
# issue a real captcha challenge, 95598 masks that with a generic error):
#   - navigator.userAgent
#   - navigator.userAgentData + getHighEntropyValues() promise result
#   - Sec-CH-UA / Sec-CH-UA-Platform / Sec-CH-UA-Mobile / etc. request headers
#     (Chrome sends these automatically; CDP setUserAgentOverride controls them)
#   - navigator.platform
#   - WebGL UNMASKED_VENDOR / UNMASKED_RENDERER (must be a Windows-plausible GPU)
#   - Timezone (Intl.DateTimeFormat().resolvedOptions().timeZone must match)
#
# Anything we leave at the container's default (Linux / UTC / ANGLE+SwiftShader)
# while claiming Windows is an instant tell.

UA_MAJOR = "148"
PLATFORM = "Windows"
PLATFORM_VERSION = "15.0.0"  # UA-CH platformVersion for Windows 11 (NT 10).
PLATFORM_ARCH = "x86"
PLATFORM_BITNESS = "64"
TIMEZONE = "Asia/Shanghai"
PRIMARY_LANGUAGE = "zh-CN"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
DEFAULT_WINDOW = "1280,720"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{UA_MAJOR}.0.0.0 Safari/537.36"
)

USER_AGENT_METADATA = {
    "brands": [
        {"brand": "Chromium", "version": UA_MAJOR},
        {"brand": "Not_A Brand", "version": "24"},
        {"brand": "Google Chrome", "version": UA_MAJOR},
    ],
    "fullVersionList": [
        {"brand": "Chromium", "version": f"{UA_MAJOR}.0.7778.167"},
        {"brand": "Not_A Brand", "version": "24.0.0.0"},
        {"brand": "Google Chrome", "version": f"{UA_MAJOR}.0.7778.167"},
    ],
    "platform": PLATFORM,
    "platformVersion": PLATFORM_VERSION,
    "architecture": PLATFORM_ARCH,
    "bitness": PLATFORM_BITNESS,
    "wow64": False,
    "model": "",
    "mobile": False,
}

# Common Windows GPU exposed via WebGL. Picked Intel UHD 630 because it's the
# most prevalent integrated GPU in Chinese consumer Windows machines; an Nvidia
# RTX-flavored renderer would stand out (and a high-end gaming machine sitting
# on State Grid is a behavioral oddity).
WEBGL_VENDOR = "Google Inc. (Intel)"
WEBGL_RENDERER = (
    "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
)


# Injected via Page.addScriptToEvaluateOnNewDocument so it runs in every
# document (main frame + same-origin and cross-origin subframes — including
# the Tencent captcha iframe).
_STEALTH_JS_TEMPLATE = r"""
(() => {
  // ---------- Function.prototype.toString defense ----------
  // Every property we override below would otherwise leak via .toString().
  // Route all toString calls for hijacked functions through a fixed
  // native-shaped string. Keep the original toString as the proxy target so
  // toString of toString itself still returns native code.
  const nativeToString = Function.prototype.toString;
  const hijacked = new WeakMap();
  Function.prototype.toString = new Proxy(nativeToString, {
    apply(target, thisArg, args) {
      if (hijacked.has(thisArg)) return hijacked.get(thisArg);
      return Reflect.apply(target, thisArg, args);
    },
  });
  const native = (fn, name) => {
    try {
      hijacked.set(fn, 'function ' + (name || fn.name || '') + '() { [native code] }');
    } catch (e) {}
    return fn;
  };
  const defineGetter = (obj, key, getter, displayName) => {
    try {
      Object.defineProperty(obj, key, {
        configurable: true,
        enumerable: true,
        get: native(getter, displayName || ('get ' + key)),
      });
    } catch (e) {}
  };

  // ---------- navigator.* ----------
  defineGetter(Navigator.prototype, 'webdriver', () => undefined);
  defineGetter(Navigator.prototype, 'platform', () => 'Win32');
  defineGetter(Navigator.prototype, 'vendor', () => 'Google Inc.');
  defineGetter(Navigator.prototype, 'languages', () => ['zh-CN', 'zh', 'en-US', 'en']);
  defineGetter(Navigator.prototype, 'hardwareConcurrency', () => 8);
  defineGetter(Navigator.prototype, 'deviceMemory', () => 8);
  defineGetter(Navigator.prototype, 'maxTouchPoints', () => 0);

  // ---------- navigator.userAgentData ----------
  const UA_DATA = ___UA_DATA_JSON___;
  try {
    const fakeData = {
      get brands() { return UA_DATA.brands.slice(); },
      get mobile() { return UA_DATA.mobile; },
      get platform() { return UA_DATA.platform; },
      getHighEntropyValues: native(function(hints) {
        const all = {
          brands: UA_DATA.brands.slice(),
          mobile: UA_DATA.mobile,
          platform: UA_DATA.platform,
          architecture: UA_DATA.architecture,
          bitness: UA_DATA.bitness,
          model: UA_DATA.model,
          platformVersion: UA_DATA.platformVersion,
          uaFullVersion: UA_DATA.fullVersionList[0].version,
          fullVersionList: UA_DATA.fullVersionList.slice(),
          wow64: UA_DATA.wow64,
        };
        const out = { brands: all.brands, mobile: all.mobile, platform: all.platform };
        for (const h of hints) if (h in all) out[h] = all[h];
        return Promise.resolve(out);
      }, 'getHighEntropyValues'),
      toJSON: native(function() {
        return { brands: UA_DATA.brands.slice(), mobile: UA_DATA.mobile, platform: UA_DATA.platform };
      }, 'toJSON'),
    };
    defineGetter(Navigator.prototype, 'userAgentData', () => fakeData);
  } catch (e) {}

  // ---------- navigator.plugins / mimeTypes ----------
  // Empty plugins is the single strongest bot signal for headless Chrome.
  try {
    const makePlugin = (name) => {
      const mime = {
        type: 'application/pdf',
        suffixes: 'pdf',
        description: 'Portable Document Format',
        enabledPlugin: null,
      };
      const plugin = {
        name,
        filename: 'internal-pdf-viewer',
        description: 'Portable Document Format',
        length: 1,
        0: mime,
        item: native(function(i) { return i === 0 ? mime : null; }, 'item'),
        namedItem: native(function(n) { return n === mime.type ? mime : null; }, 'namedItem'),
        refresh: native(function() {}, 'refresh'),
      };
      mime.enabledPlugin = plugin;
      return plugin;
    };
    const plugins = [makePlugin('PDF Viewer'), makePlugin('Chrome PDF Viewer')];
    Object.defineProperty(plugins, 'item', { value: native(function(i) { return plugins[i] || null; }, 'item') });
    Object.defineProperty(plugins, 'namedItem', { value: native(function(n) { return plugins.find(p => p.name === n) || null; }, 'namedItem') });
    Object.defineProperty(plugins, 'refresh', { value: native(function() {}, 'refresh') });
    defineGetter(Navigator.prototype, 'plugins', () => plugins);
    const mimes = [plugins[0][0]];
    Object.defineProperty(mimes, 'item', { value: native(function(i) { return mimes[i] || null; }, 'item') });
    Object.defineProperty(mimes, 'namedItem', { value: native(function(n) { return mimes.find(m => m.type === n) || null; }, 'namedItem') });
    defineGetter(Navigator.prototype, 'mimeTypes', () => mimes);
  } catch (e) {}

  // ---------- window.chrome ----------
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
        PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
        RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
      };
    }
    if (!window.chrome.app) {
      window.chrome.app = {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      };
    }
    if (!window.chrome.csi) {
      window.chrome.csi = native(function() {
        return { startE: Date.now(), onloadT: Date.now() + 50, pageT: 100, tran: 15 };
      }, 'csi');
    }
    if (!window.chrome.loadTimes) {
      window.chrome.loadTimes = native(function() {
        const t = Date.now() / 1000;
        return {
          requestTime: t - 1.5, startLoadTime: t - 1.4, commitLoadTime: t - 1.3,
          finishDocumentLoadTime: t - 1.0, finishLoadTime: t - 0.8,
          firstPaintTime: t - 0.7, firstPaintAfterLoadTime: 0,
          navigationType: 'Other',
          wasFetchedViaSpdy: true, wasNpnNegotiated: true, npnNegotiatedProtocol: 'h2',
          wasAlternateProtocolAvailable: false, connectionInfo: 'h2',
        };
      }, 'loadTimes');
    }
  } catch (e) {}

  // ---------- permissions.query ----------
  try {
    const orig = navigator.permissions && navigator.permissions.query;
    if (orig) {
      navigator.permissions.query = native(function(p) {
        if (p && p.name === 'notifications') {
          return Promise.resolve({
            state: typeof Notification !== 'undefined' ? Notification.permission : 'default',
            onchange: null,
          });
        }
        return orig.call(navigator.permissions, p);
      }, 'query');
    }
  } catch (e) {}

  // ---------- WebGL ----------
  const FAKE_VENDOR = ___WEBGL_VENDOR_JSON___;
  const FAKE_RENDERER = ___WEBGL_RENDERER_JSON___;
  const patchWebGL = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = native(function(p) {
      // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
      if (p === 37445) return FAKE_VENDOR;
      if (p === 37446) return FAKE_RENDERER;
      return orig.call(this, p);
    }, 'getParameter');
  };
  try { patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}

  // ---------- Canvas fingerprint perturbation ----------
  // Constant per-session noise so the hash differs from any known headless
  // canvas signature without being random per call (random-per-call is itself
  // a bot signal).
  try {
    const seed = Math.floor(Math.random() * 1e6);
    const tweak = (data) => {
      for (let i = 0; i < data.length; i += 4) {
        data[i] = (data[i] + ((seed + i) % 3 - 1) + 256) % 256;
      }
    };
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = native(function(...args) {
      try {
        const ctx = this.getContext && this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
          const w = Math.min(this.width, 8), h = Math.min(this.height, 8);
          const img = ctx.getImageData(0, 0, w, h);
          tweak(img.data);
          ctx.putImageData(img, 0, 0);
        }
      } catch (e) {}
      return origToDataURL.apply(this, args);
    }, 'toDataURL');
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = native(function(...args) {
      const result = origGetImageData.apply(this, args);
      try { tweak(result.data); } catch (e) {}
      return result;
    }, 'getImageData');
  } catch (e) {}

  // ---------- AudioContext fingerprint perturbation ----------
  try {
    if (window.AudioBuffer) {
      const orig = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = native(function(...args) {
        const data = orig.apply(this, args);
        try {
          const noise = 1e-7;
          for (let i = 0; i < data.length; i += 100) {
            data[i] += (Math.random() - 0.5) * noise;
          }
        } catch (e) {}
        return data;
      }, 'getChannelData');
    }
  } catch (e) {}

  // ---------- Battery API (laptop-realistic) ----------
  try {
    if (navigator.getBattery) {
      navigator.getBattery = native(function() {
        const noop = function() {};
        return Promise.resolve({
          charging: true,
          chargingTime: Infinity,
          dischargingTime: Infinity,
          level: 0.85,
          addEventListener: noop, removeEventListener: noop,
          dispatchEvent: function() { return true; },
          onchargingchange: null, onchargingtimechange: null,
          ondischargingtimechange: null, onlevelchange: null,
        });
      }, 'getBattery');
    }
  } catch (e) {}

  // ---------- Remove $cdc_ chromedriver globals ----------
  try {
    for (const k of Object.keys(window)) {
      if (/^\$?cdc_/.test(k)) { try { delete window[k]; } catch (e) {} }
    }
  } catch (e) {}

  // ---------- Iframe contentWindow inheritance ----------
  // Some bot detectors create an iframe and check
  // `iframe.contentWindow.navigator.webdriver`. Vanilla Chrome inherits
  // prototype patches; selenium with isolated worlds can leak.
  try {
    const desc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (desc && desc.get) {
      const origGet = desc.get;
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        configurable: true,
        get: native(function() {
          const win = origGet.call(this);
          try {
            if (win && 'webdriver' in win.navigator) {
              Object.defineProperty(win.navigator, 'webdriver', {
                configurable: true, get: () => undefined,
              });
            }
          } catch (e) {}
          return win;
        }, 'get contentWindow'),
      });
    }
  } catch (e) {}
})();
"""


def _build_stealth_js() -> str:
    return (
        _STEALTH_JS_TEMPLATE
        .replace("___UA_DATA_JSON___", json.dumps(USER_AGENT_METADATA))
        .replace("___WEBGL_VENDOR_JSON___", json.dumps(WEBGL_VENDOR))
        .replace("___WEBGL_RENDERER_JSON___", json.dumps(WEBGL_RENDERER))
    )


# Fingerprint surfaces to log at driver creation so we can diagnose mismatches
# from logs without needing live JS access.
_FINGERPRINT_PROBE_JS = r"""
(async () => {
  const out = {
    ua: navigator.userAgent,
    platform: navigator.platform,
    vendor: navigator.vendor,
    languages: navigator.languages,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    webdriver: navigator.webdriver,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    plugins: Array.from(navigator.plugins || []).map(p => p.name),
    chrome_runtime: !!(window.chrome && window.chrome.runtime),
    screen: { width: screen.width, height: screen.height, colorDepth: screen.colorDepth },
  };
  try {
    out.uaData = navigator.userAgentData
      ? await navigator.userAgentData.getHighEntropyValues([
          'architecture','bitness','model','platformVersion','uaFullVersion','fullVersionList'
        ])
      : null;
  } catch (e) { out.uaData_error = String(e); }
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      out.webgl = {
        vendor: gl.getParameter(ext ? ext.UNMASKED_VENDOR_WEBGL : 0),
        renderer: gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : 0),
      };
    }
  } catch (e) { out.webgl_error = String(e); }
  try {
    out.toString_check = {
      webdriver: Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').get.toString(),
      query: navigator.permissions.query.toString(),
      getParameter: WebGLRenderingContext.prototype.getParameter.toString(),
    };
  } catch (e) { out.toString_check_error = String(e); }
  return out;
})()
"""


def _log_fingerprint(driver) -> None:
    """Evaluate the stealth surfaces and log them so we can audit from logs."""
    try:
        result = driver.execute_cdp_cmd(
            "Runtime.evaluate",
            {
                "expression": _FINGERPRINT_PROBE_JS,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": False,
            },
        )
        value = (result or {}).get("result", {}).get("value")
        if value is None:
            logging.warning("Selenium fingerprint probe returned no value: %s", result)
            return
        logging.info("Selenium fingerprint probe: %s", json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logging.warning("Selenium fingerprint probe failed: %s", exc)


def create_chromium_driver(driver_wait_time: int):
    browser_window_size = os.getenv("BROWSER_WINDOW_SIZE", DEFAULT_WINDOW)
    browser_language = os.getenv("BROWSER_LANGUAGE", ACCEPT_LANGUAGE)
    browser_ua = os.getenv("BROWSER_USER_AGENT", USER_AGENT)

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(f"--window-size={browser_window_size}")
    chrome_options.add_argument(f"--lang={PRIMARY_LANGUAGE}")
    chrome_options.add_argument(
        "--disable-features=Translate,IsolateOrigins,site-per-process"
    )
    chrome_options.add_argument("--password-store=basic")
    chrome_options.add_argument("--use-mock-keychain")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-infobars")
    # WebRTC: don't expose container's internal IP via ICE candidates.
    chrome_options.add_argument(
        "--force-webrtc-ip-handling-policy=default_public_interface_only"
    )
    chrome_options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation", "enable-logging"],
    )
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

    driver = webdriver.Chrome(options=chrome_options, service=service)
    driver.implicitly_wait(driver_wait_time)

    # Stealth JS must be registered BEFORE the first navigation. CDP guarantees
    # it runs on every new document (main frame + subframes, regardless of
    # origin), which matters because the Tencent captcha widget loads in a
    # cross-origin iframe that also probes for bot signals.
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _build_stealth_js()},
        )
    except Exception as exc:
        logging.warning("Failed to install stealth JS: %s", exc)

    # Network-layer UA + client-hints override. Without userAgentMetadata Chrome
    # sends Sec-CH-UA-Platform with the real container OS (Linux) regardless of
    # what navigator.userAgent says — Tencent cross-checks the two.
    try:
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": browser_ua,
                "acceptLanguage": browser_language,
                "platform": "Win32",
                "userAgentMetadata": USER_AGENT_METADATA,
            },
        )
    except Exception as exc:
        logging.warning("Failed to set CDP UA override: %s", exc)

    # Timezone: container runs UTC; claiming Chinese Chrome from UTC TZ is a
    # tell. Override at the rendering engine layer so Date / Intl agree.
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": TIMEZONE})
    except Exception as exc:
        logging.warning("Failed to set CDP timezone override: %s", exc)

    # Locale override aligns Intl, RTL, date formats, etc. with the claimed
    # browser language.
    try:
        driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": PRIMARY_LANGUAGE})
    except Exception as exc:
        logging.debug("Locale override unsupported on this Chrome: %s", exc)

    # Dump the stealth surface to log so we can audit consistency from log
    # output. Critical when iterating on disguise — without this we have no
    # visibility into what selenium is actually presenting to Tencent.
    _log_fingerprint(driver)

    return driver
