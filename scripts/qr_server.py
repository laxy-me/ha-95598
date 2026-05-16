"""Tiny HTTP server exposing the login QR + status via HA Ingress.

When the addon's ``ingress`` config flag is on, Home Assistant
supervisor proxies HTTP requests at ``/api/hassio_ingress/<token>``
to this server. Endpoints:

  GET  /          -> login UI HTML (banner + QR + button)
  GET  /status    -> JSON snapshot from state_registry + QR mtime
  GET  /qr.png    -> current QR image
  POST /trigger   -> ask main process to run a fetch now
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import socketserver
import threading
from pathlib import Path
from typing import Callable, Optional

from scripts.state_registry import registry


QR_PATH = Path("/app/data/login_qr_code.png")
QR_LIFETIME_SECONDS = 60

_trigger_callback: Optional[Callable[[], None]] = None


_PAGE_HTML = (
    "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>95598 登录</title>"
    "<style>"
    ":root{font-family:-apple-system,system-ui,sans-serif;color:#333;}"
    "body{margin:0;background:#f5f5f5;min-height:100vh;"
    "display:flex;flex-direction:column;align-items:center;"
    "padding:24px 16px;gap:18px;}"
    ".topbar{width:100%;max-width:380px;display:flex;align-items:center;"
    "gap:8px;margin-bottom:-4px;}"
    ".back{appearance:none;border:0;background:transparent;color:#1976d2;"
    "font-size:15px;padding:6px 4px;cursor:pointer;"
    "display:inline-flex;align-items:center;gap:4px;}"
    ".back:hover{color:#125ea0;}"
    ".back svg{width:18px;height:18px;}"
    ".banner{width:100%;max-width:380px;padding:14px 18px;"
    "border-radius:14px;background:#fff;"
    "box-shadow:0 2px 8px rgba(0,0,0,.06);"
    "border-left:4px solid #999;}"
    ".banner.running{border-left-color:#1976d2;}"
    ".banner.qr{border-left-color:#2e7d32;}"
    ".banner.ok{border-left-color:#2e7d32;}"
    ".banner.locked{border-left-color:#d32f2f;}"
    ".banner h3{margin:0 0 4px;font-size:15px;font-weight:600;}"
    ".banner .meta{font-size:12.5px;color:#666;line-height:1.5;}"
    ".banner .lockout{margin-top:6px;font-size:12px;color:#c62828;}"
    ".card{background:#fff;padding:20px;border-radius:16px;"
    "box-shadow:0 4px 16px rgba(0,0,0,.08);}"
    ".card img{display:block;max-width:280px;width:100%;height:auto;"
    "transition:filter .2s ease,opacity .2s ease;}"
    ".card img.expired{filter:grayscale(1);opacity:.45;}"
    ".card .countdown{text-align:center;margin-top:10px;"
    "font-size:13px;color:#666;}"
    ".card .countdown.expired{color:#c62828;}"
    ".btn{appearance:none;border:0;background:#1976d2;color:#fff;"
    "font-size:15px;padding:12px 28px;border-radius:10px;cursor:pointer;"
    "box-shadow:0 2px 8px rgba(25,118,210,.25);}"
    ".btn[disabled]{background:#bbb;cursor:not-allowed;box-shadow:none;}"
    ".btn:not([disabled]):hover{background:#125ea0;}"
    "</style></head><body>"
    "<div class=\"topbar\">"
    "<button class=\"back\" onclick=\"goBack()\">"
    "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
    "<path d=\"M15 18l-6-6 6-6\"/></svg>返回"
    "</button>"
    "</div>"
    "<div id=\"banner\" class=\"banner\">"
    "<h3 id=\"banner-title\">加载中…</h3>"
    "<div class=\"meta\" id=\"banner-meta\"></div>"
    "<div class=\"lockout\" id=\"banner-lockout\" style=\"display:none;\"></div>"
    "</div>"
    "<div id=\"qr-card\" class=\"card\" style=\"display:none;\">"
    "<img id=\"qr-img\" alt=\"登录 QR 码\">"
    "<div class=\"countdown\" id=\"qr-countdown\"></div>"
    "</div>"
    "<button id=\"trigger-btn\" class=\"btn\">手动触发 QR 登录</button>"
    "<script>"
    "const QR_LIFETIME=" + str(QR_LIFETIME_SECONDS) + ";"
    "const $=(id)=>document.getElementById(id);"
    "function goBack(){"
    "  // Ingress is iframed inside HA — bubble up to the parent's"
    "  // history so we land on whatever HA view the user came from."
    "  try{if(window.parent&&window.parent!==window){window.parent.history.back();return;}}catch(e){}"
    "  if(history.length>1){history.back();return;}"
    "  try{if(window.top){window.top.location.href='/';return;}}catch(e){}"
    "  location.href='/';"
    "}"
    "function fmtTs(ts){if(!ts)return '';"
    "return new Date(ts*1000).toLocaleString('zh-CN',{hour12:false});}"
    "function fmtDur(sec){if(sec<60)return Math.max(0,Math.ceil(sec))+'s';"
    "const m=Math.floor(sec/60),s=Math.floor(sec%60);"
    "if(m<60)return m+'m'+(s?s+'s':'');"
    "const h=Math.floor(m/60),mm=m%60;return h+'h'+(mm?mm+'m':'');}"
    "let lastStatus=null;"
    "async function refreshStatus(){"
    "  try{const r=await fetch('status?t='+Date.now(),{cache:'no-store'});"
    "  lastStatus=await r.json();}catch(e){return;}"
    "  applyStatus(lastStatus);}"
    "function applyStatus(s){"
    "  const banner=$('banner'),title=$('banner-title'),meta=$('banner-meta');"
    "  const lockoutEl=$('banner-lockout');"
    "  const qrCard=$('qr-card'),qrImg=$('qr-img'),cd=$('qr-countdown');"
    "  const trigger=$('trigger-btn');"
    "  const now=Date.now()/1000;"
    "  banner.className='banner';"
    "  const lockoutLeft=s.lockout_until?(s.lockout_until-now):0;"
    "  if(lockoutLeft>0){"
    "    lockoutEl.style.display='';"
    "    lockoutEl.textContent='密码登录配额耗尽 (RK001) · 自动解锁 '+fmtTs(s.lockout_until)+' (剩 '+fmtDur(lockoutLeft)+')';"
    "  }else{lockoutEl.style.display='none';}"
    "  if(s.state==='logged_in'){"
    "    banner.classList.add('ok');"
    "    title.textContent='已登录';"
    "    meta.textContent=(s.last_login_method?'方式: '+s.last_login_method:'')+"
    "      (s.last_login_at?'  ·  '+fmtTs(s.last_login_at):'');"
    "  }else if(s.state==='waiting_qr'){"
    "    banner.classList.add('qr');"
    "    title.textContent='等待扫码登录';"
    "    meta.textContent='请用「网上国网」APP 扫描下方 QR 码';"
    "  }else if(s.state==='running'){"
    "    banner.classList.add('running');"
    "    title.textContent='正在登录…';"
    "    meta.textContent='请稍候';"
    "  }else{"
    "    title.textContent='空闲';"
    "    meta.textContent=(lockoutLeft>0?'密码登录被锁定，可手动触发 QR 登录':'add-on 当前未在登录流程');"
    "  }"
    "  const qrActive=!!s.qr_active && !!s.qr_mtime;"
    "  if(qrActive){"
    "    qrCard.style.display='';"
    "    const age=now-s.qr_mtime;"
    "    qrImg.src='qr.png?t='+s.qr_mtime;"
    "    if(age>=QR_LIFETIME){"
    "      qrImg.classList.add('expired');"
    "      cd.classList.add('expired');"
    "      cd.textContent='QR 已过期 ('+Math.floor(age)+'s)，等待 add-on 自动刷新…';"
    "    }else{"
    "      qrImg.classList.remove('expired');"
    "      cd.classList.remove('expired');"
    "      cd.textContent='剩 '+fmtDur(QR_LIFETIME-age)+' 过期';"
    "    }"
    "  }else{qrCard.style.display='none';}"
    "  const busy=(s.state==='running'||s.state==='waiting_qr');"
    "  trigger.disabled=busy;"
    "  trigger.textContent=busy?'登录中，请稍候':'手动触发 QR 登录';"
    "}"
    "$('trigger-btn').addEventListener('click',async()=>{"
    "  const btn=$('trigger-btn');btn.disabled=true;btn.textContent='正在触发…';"
    "  try{await fetch('trigger',{method:'POST'});}catch(e){}"
    "  setTimeout(refreshStatus,800);"
    "});"
    "refreshStatus();setInterval(refreshStatus,1000);"
    "</script></body></html>"
).encode("utf-8")


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HA95598QR/2.0"

    def log_message(self, format, *args):
        # Quiet — HA Ingress fronts us, no need to spam addon stdout.
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_bytes(_PAGE_HTML, "text/html; charset=utf-8")
            return
        if self.path.startswith("/status"):
            self._serve_status()
            return
        if self.path.startswith("/qr.png"):
            self._serve_qr_png()
            return
        self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/trigger":
            cb = _trigger_callback
            if cb is None:
                self.send_error(503, "Trigger not wired up")
                return
            try:
                cb()
            except Exception as exc:
                logging.warning("Manual trigger raised: %s", exc)
                self.send_error(500, "Trigger failed")
                return
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404, "Not Found")

    def _serve_bytes(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self):
        snap = registry.snapshot()
        try:
            snap["qr_mtime"] = QR_PATH.stat().st_mtime if QR_PATH.exists() else None
        except OSError:
            snap["qr_mtime"] = None
        body = json.dumps(snap).encode("utf-8")
        self._serve_bytes(body, "application/json")

    def _serve_qr_png(self):
        try:
            data = QR_PATH.read_bytes()
        except OSError:
            self.send_error(404, "QR not available")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class _ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(port: int | None = None, on_trigger: Callable[[], None] | None = None) -> None:
    """Start the QR server in a background thread. Safe to call once
    at process startup; does nothing when the port is unset/invalid."""
    global _trigger_callback
    _trigger_callback = on_trigger
    raw = port if port is not None else os.getenv("INGRESS_PORT", "")
    try:
        port_int = int(raw)
    except (TypeError, ValueError):
        logging.info("QR server: INGRESS_PORT %r not set or invalid; skipping.", raw)
        return

    def _run():
        try:
            with _ReusableTCPServer(("0.0.0.0", port_int), _Handler) as httpd:
                logging.info("QR server listening on 0.0.0.0:%s", port_int)
                httpd.serve_forever()
        except Exception as exc:
            logging.warning("QR server crashed: %s", exc)

    thread = threading.Thread(target=_run, name="qr-server", daemon=True)
    thread.start()
