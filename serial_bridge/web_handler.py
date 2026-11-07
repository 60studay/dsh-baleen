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
from .ports import BAUD_CHOICES, scan_ports


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
            if path in ("/app.css", "/app.js") or (
                    path.startswith("/js/") and path.endswith(".js")):
                # R2/R2b 把样式和逻辑拆成了独立文件，必须真的能把它们发出去，
                # 否则页面一加载就是白屏（而且只在浏览器控制台报 404，很难查）。
                asset = self.ctx["asset"](path)
                if asset is None:
                    return self._json({"ok": False, "error": {
                        "kind": "not_found", "message": path}}, 404)
                body, content_type = asset
                return self._send(200, body, content_type)
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
            if path == "/channel":
                since = int((query.get("since") or ["0"])[0])
                limit = int((query.get("limit") or ["100"])[0])
                return self._json(self.ctx["channel_list"](since, limit))
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
                # actor 缺省当人类（网页按钮就是人类点的）；
                # 显式传 ai 时，会受"有人看着网页不许自授权"的规则约束
                return self._json(self.ctx["control"](action, str(body.get("actor") or "human")))
            if path == "/watch":
                return self._json(self.ctx["set_watch"](body))
            if path == "/rules":
                return self._json(self.ctx["set_rules"](body))
            if path == "/channel":
                # AI ⇄ AI 通道：body 就是一条消息（from/type/claim/evidence/not_verified）
                return self._json(self.ctx["channel_send"](body))
            if path == "/channel/verify":
                # 一键复验：把这条消息的 evidence.cmd 重跑一遍，结果贴回通道
                return self._json(self.ctx["channel_verify"](
                    body.get("seq"), str(body.get("actor") or "human")))
            if path == "/port/pause":
                return self._json(self.ctx["pause_port"]())
            if path == "/port/probe":
                return self._json(self.ctx["probe_port"]())
            if path == "/alerts/clear":
                return self._json(self.ctx["clear_alerts"]())
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
