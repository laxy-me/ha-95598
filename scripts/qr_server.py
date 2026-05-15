"""Tiny HTTP server exposing the latest QR code via HA Ingress.

When the addon's ``ingress`` config flag is on, Home Assistant
supervisor proxies HTTP requests at
``/api/hassio_ingress/<token>`` to this server. The user can open
the addon's "Web UI" link from the HA UI and immediately see the
current QR PNG (or a placeholder when not in QR-fallback mode).
This drops the need for a /share -> /config/www mirror automation
and a publicly-served /local/95598_qr.png file.
"""

from __future__ import annotations

import http.server
import logging
import os
import socketserver
import threading
from pathlib import Path


QR_PATH = Path("/app/data/login_qr_code.png")


_PLACEHOLDER_HTML = """
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>95598 QR</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #f5f5f5; color: #333;
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; min-height: 100vh; gap: 16px; padding: 24px; }
  .card { background: white; padding: 24px; border-radius: 16px;
          box-shadow: 0 4px 16px rgba(0,0,0,0.08); text-align: center;
          max-width: 400px; }
  h2 { margin: 0 0 12px; font-size: 1.2em; }
  .empty { color: #888; font-size: 0.95em; line-height: 1.6; }
  img { max-width: 280px; width: 100%; height: auto; }
</style></head><body>
<div class="card">
  <h2>95598 登录二维码</h2>
  <p class="empty">现在不需要扫码。<br>add-on 只在 session 过期 且
  密码配额用完时才会生成 QR。</p>
</div>
</body></html>
""".encode("utf-8")


class _QRHandler(http.server.BaseHTTPRequestHandler):
    server_version = "HA95598QR/1.0"

    def log_message(self, format, *args):
        # Quiet — HA Ingress fronts us, no need to spam addon stdout.
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if QR_PATH.exists() and QR_PATH.stat().st_size > 0:
                self._serve_qr_html()
            else:
                self._serve_html(_PLACEHOLDER_HTML)
            return
        if self.path.startswith("/qr.png"):
            self._serve_qr_png()
            return
        self.send_error(404, "Not Found")

    def _serve_qr_html(self):
        # Cache-bust by appending mtime so the browser always sees the
        # latest QR even when Ingress / HA fronts cache responses.
        try:
            mtime = int(QR_PATH.stat().st_mtime)
        except OSError:
            mtime = 0
        body = (
            "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            "<title>95598 QR</title>"
            "<meta http-equiv=\"refresh\" content=\"15\">"
            "<style>body{margin:0;background:#f5f5f5;color:#333;"
            "display:flex;flex-direction:column;align-items:center;"
            "justify-content:center;min-height:100vh;gap:14px;"
            "font-family:-apple-system,system-ui,sans-serif;}"
            ".card{background:white;padding:20px;border-radius:16px;"
            "box-shadow:0 4px 16px rgba(0,0,0,.08);}"
            "img{max-width:280px;width:100%;height:auto;}"
            "</style></head><body><div class=\"card\">"
            f"<img src=\"qr.png?t={mtime}\" alt=\"QR\">"
            "</div></body></html>"
        ).encode("utf-8")
        self._serve_html(body)

    def _serve_html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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


def start(port: int | None = None) -> None:
    """Start the QR server in a background thread. Safe to call once
    at process startup; does nothing when the port is unset/invalid."""
    raw = port if port is not None else os.getenv("INGRESS_PORT", "")
    try:
        port_int = int(raw)
    except (TypeError, ValueError):
        logging.info("QR server: INGRESS_PORT %r not set or invalid; skipping.", raw)
        return

    def _run():
        try:
            with _ReusableTCPServer(("0.0.0.0", port_int), _QRHandler) as httpd:
                logging.info("QR server listening on 0.0.0.0:%s", port_int)
                httpd.serve_forever()
        except Exception as exc:
            logging.warning("QR server crashed: %s", exc)

    thread = threading.Thread(target=_run, name="qr-server", daemon=True)
    thread.start()
