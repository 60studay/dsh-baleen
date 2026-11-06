# -*- coding: utf-8 -*-
"""网页的 HTTP 路由（从 supervisor.py 抽出来的）。

只做"把 URL 映射到 WebServer 的方法"这一件事：
解析请求体、调回调、把结果序列化成 JSON。
业务判断（控制权、端口、管家）全在 WebServer 里，这里不碰。
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .errors import BridgeError


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "SerialBridge/2.0"

    # ── 基础设施 ────────────────────────────────────────────────────────────
    @property
    def ctx(self):
        return self.server.context

    def log_message(self, *args):
        pass  # 静音，日志我们自己写

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _error(self, exc, status=400):
        data = exc.to_dict() if isinstance(exc, BridgeError) else {
            "kind": "internal", "message": str(exc)}
        self._json({"ok": False, "error": data}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}

    # ── 路由 ────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                html = self.ctx["page"]
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/status":
                return self._json(self.ctx["status"]())
            if path == "/poll":
                force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
                return self._json(self.ctx["poll"](force))
            if path == "/alerts":
                limit = int((query.get("limit") or ["20"])[0])
                return self._json({"ok": True, "alerts": self.ctx["alerts"](limit)})
            if path == "/ports":
                return self._json({"ok": True, "ports": scan_ports(),
                                   "bauds": BAUD_CHOICES})
            if path == "/rules":
                return self._json(self.ctx["get_rules"]())
            if path == "/favicon.ico":
                return self._send(204, b"")
            return self._json({"ok": False, "error": {"kind": "not_found",
                                                      "message": path}}, 404)
        except BridgeError as exc:
            return self._error(exc)
        except Exception as exc:
            return self._error(exc, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/send":
                return self._json(self.ctx["send"](
                    str(body.get("text") or ""),
                    str(body.get("mode") or "text"),
                    str(body.get("actor") or "human"),
                    str(body.get("source") or "web"),
                ))
            if path == "/control":
                # 缺字段/未知动作都用 400，但必须说清楚是哪一种
                action = str(body.get("action") or "").strip()
                if not action:
                    raise BridgeError(
                        "config",
                        "/control 需要一个 action 字段。",
                        hint=['请求体示例：{"action": "human"}',
                              "可选值：ai / pause / resume / human"],
                    )
                return self._json(self.ctx["control"](action))
            if path == "/watch":
                return self._json(self.ctx["set_watch"](body))
            if path == "/rules":
                return self._json(self.ctx["set_rules"](body))
            if path == "/port/pause":
                return self._json(self.ctx["pause_port"]())
            if path == "/port/resume":
                return self._json(self.ctx["resume_port"]())
            if path == "/reconnect":
                return self._json(self.ctx["reconnect"](
                    str(body.get("com") or ""), int(body.get("baud") or 0)))
            if path == "/autodetect":
                return self._json(self.ctx["autodetect"]())
            if path == "/mode":
                return self._json(self.ctx["set_mode"](str(body.get("mode") or "light")))
            if path == "/shutdown":
                self._json({"ok": True, "shutdown": True})
                self.ctx["request_shutdown"]()
                return None
            return self._json({"ok": False, "error": {"kind": "not_found",
                                                      "message": path}}, 404)
        except BridgeError as exc:
            return self._error(exc)
        except Exception as exc:
            return self._error(exc, 500)
