# -*- coding: utf-8 -*-
"""网页 / 监视进程（v2 交接文档 3.3 / 3.4 / 3.5 / 3.6）。

两种模式：
    light（默认）  不长期占口：每个请求临时开口 → 读 → 关。
                   和 AI、和用户的串口助手都能共存。
    watch          占口持续读，才能看到连续数据流和主动上报。

控制权、监视开关都存在共享状态文件里（state.py），所以网页和插件进程看到的是同一份。

退出按钮真的会停服务并释放串口：/shutdown 设置标记 + 关闭 socket。
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.request

from . import io as serial_io
from .config import load_scene
from .errors import BridgeError
from .ports import resolve_port, scan_ports
from .rules import RULE_TYPES
from .runner import RuleWorker
from .state import SharedState, STATE_AI, STATE_HUMAN, STATE_PAUSED, VALID_STATES, _safe_name
from .transport import describe_transport as transport_describe

BAUD_CHOICES = [9600, 19200, 38400, 57600, 74880, 115200, 230400, 250000, 460800, 921600]
DEFAULT_PORT = 8899


class WatchLoop(object):
    """watch 模式的常驻读取线程。

    两个开关在这里也是独立的：
      定时发送线程（send_enabled）负责写
      判定（check_enabled）在数据到达时同步跑，但有规则级超时保护
    任一线程出错都不影响另一个；规则引擎出错更不会影响读口。
    """

    def __init__(self, scene, paths, state, logger):
        self.scene = scene
        self.paths = paths
        self.state = state
        self.logger = logger
        self.ser = None
        self.thread = None
        self.send_thread = None
        self.stop_event = threading.Event()
        self.buffer = bytearray()
        self.lock = threading.Lock()
        # 自动发送的"发送记录"：网页靠它显示 `>> 命令` + 回显
        self.auto_frames = []
        # 串口互斥：定时发送线程、手动 /send、/poll 读一次，三路都要开关同一个口。
        # 不加锁会自己跟自己抢出 "被占用"（真机实测复现）。
        self.port_lock = threading.Lock()
        self.no_response_streak = 0
        self.last_error = None
        self.device = ""
        self.running = False
        # 熔断：连续判定失败达阈值后停掉判定（不影响收发）
        self._checks_stopped = False
        self._check_failures = 0
        # 判定跑在常驻子进程里：卡住能杀掉，出错不冒泡（见 runner.py）
        self._rule_worker = None

    def start(self, device, baud):
        self.stop()
        self.device = device
        self.baud = baud
        self.ser = serial_io.open_port(self.scene.cfg, device, baud)
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, name="watch-read", daemon=True)
        self.thread.start()
        self.send_thread = threading.Thread(target=self._send_loop, name="watch-send", daemon=True)
        self.send_thread.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        for thread in (self.thread, self.send_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self.thread = None
        self.send_thread = None
        if self._rule_worker is not None:
            self._rule_worker.close()
            self._rule_worker = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def take(self):
        with self.lock:
            data = bytes(self.buffer)
            self.buffer.clear()
            return data

    def send_raw(self, command, raw_bytes=None):
        """发一条命令并返回回显文本。

        持有句柄就写句柄（监视模式）；没持有就临时开口（轻模式）。
        定时发送和"规则自动处置"都走这里，所以两种模式行为一致。

        raw_bytes 不为 None 时按原始字节发送（hex 场景的命令），两条路径都要尊重它。

        每次发送都会往 auto_frames 里记一帧，网页据此显示
        `>> 命令` + 回显——否则自动发送的内容只进日志，用户在页面上看不到（Bug 4）。
        """
        cfg = self.scene.cfg
        newline = serial_io.decode_newline(cfg.get("newline"), "\\r\\n")
        if self.running and self.ser is not None:
            # 原始字节模式不补换行，帧由调用方给全（见 io.transact 的说明）
            if raw_bytes is not None:
                self.ser.write(raw_bytes)
            else:
                self.ser.write(str(command).encode("utf-8") + newline)
            self.logger.append("TX", command)
            # 监视模式：回显走读线程进 buffer，这里只记"发了什么"
            self._push_frame(command, "", "", 0)
            return ""
        resolved = resolve_port(cfg)
        with self.port_lock:
            result = serial_io.transact(cfg, resolved["device"], command,
                                        raw_bytes=raw_bytes)
        self.logger.append("TX", command)
        if result["rx_text"]:
            self.logger.append("RX", result["rx_text"])
        self._push_frame(command, result["rx_text"], result["rx_hex"], result["bytes"])
        # 轻模式的自动发送也要更新"无回显计数"，否则 no_response 规则永远不触发
        if result["empty"]:
            self.no_response_streak += 1
        else:
            self.no_response_streak = 0
        return result["rx_text"]

    def _push_frame(self, tx, rx, rx_hex, nbytes, source="auto"):
        with self.lock:
            self.auto_frames.append({
                "tx": tx, "rx": rx, "hex": rx_hex,
                "bytes": int(nbytes or 0), "ts": time.time(), "source": source,
            })
            if len(self.auto_frames) > 200:
                del self.auto_frames[:-200]

    def take_frames(self):
        """取走待显示的自动发送帧。"""
        with self.lock:
            frames = list(self.auto_frames)
            self.auto_frames.clear()
            return frames

    def _read_loop(self):
        while not self.stop_event.is_set():
            try:
                chunk = self.ser.read(512)
            except Exception as exc:
                self.last_error = str(exc)
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            with self.lock:
                self.buffer.extend(chunk)
            text = chunk.decode("utf-8", errors="replace")
            self.logger.append("RX", text)
            self.no_response_streak = 0
            # 刚手动/网页发过命令 → 这段回显算 manual，否则算轮询 watch
            source = "manual" if time.time() < self._manual_until else "watch"
            self._run_checks(text, source)

    def _send_loop(self):
        """（已停用）定时发送现在由 WebServer._autosend_loop 统一负责。

        原因：这个循环只活在 WatchLoop 里，而 WatchLoop 只在"监视模式 + 控制权 ai"
        时才启动。于是 light 模式下把 send_enabled 打开，watch_running 永远是 false、
        一条命令也发不出去（Bug 4）。定时发送属于"用户配置的自动化"，不该依赖
        网页是不是在占口，所以搬到 WebServer 层，两种模式都能跑。
        """
        return


    def _watch_settings(self):
        return self.state.read().get("watch") or {}

    def _run_checks(self, text, source):
        """判定入口：完全受 check_enabled 控制，且绝不向上抛异常。

        判定跑在**子进程**里（runner.evaluate_isolated），所以：
        - 灾难性回溯的正则会被 terminate 掉，不会卡住读线程
        - 非法正则/字段缺失只让这一批规则作废，收发照常
        """
        watch = self._watch_settings()
        if not watch.get("check_enabled"):
            return
        check_source = str(watch.get("check_source") or "watch_only")
        if check_source == "watch_only" and source != "watch":
            return
        if check_source == "manual_only" and source != "manual":
            return
        if self._checks_stopped:
            return
        try:
            cfg = {**self.scene.watch_cfg(), **watch}
            if self._rule_worker is None:
                # 注意：这个超时是"等判定子进程回一行 JSON"的预算，必须覆盖
                # 一次进程冷启动（实测 ~250ms），不能用单条规则的 100ms 语义。
                self._rule_worker = RuleWorker(
                    timeout_ms=int(cfg.get("rule_worker_timeout_ms") or 2000))
            results, error = self._rule_worker.evaluate(
                text, cfg, self.no_response_streak, source)
            if error:
                self.logger.append("RULE", "判定已跳过：%s" % error)
                self._note_check_failure(error)
            else:
                self._check_failures = 0
            for result in results:
                if result.get("error"):
                    self.logger.append("RULE", "规则 %s 出错: %s"
                                       % (result.get("rule"), result.get("error")))
                    continue
                if not result.get("matched"):
                    continue
                action = result.get("action") or {}
                command = str(action.get("send") or "").strip()
                record = {
                    "rule": result.get("rule"),
                    "reason": result.get("reason"),
                    "source": source,
                    "response": text[:400],
                    "action": command or "(仅通知)",
                }
                if command:
                    try:
                        self.send_raw(command)
                        record["sent"] = True
                    except Exception as exc:
                        record["sent"] = False
                        record["send_error"] = str(exc)
                self.state.append_alert(record)
        except Exception as exc:  # 判定再怎么炸也不许拖垮收发
            self.logger.append("RULE", "判定异常已忽略: %s" % exc)

    def _note_check_failure(self, reason):
        """连续判定失败 → 停判定并记一条大警告（收发完全不受影响）。"""
        self._check_failures += 1
        limit = int(self.scene.watch_cfg().get("max_consecutive_failures") or 3)
        if self._check_failures >= limit and not self._checks_stopped:
            self._checks_stopped = True
            self.logger.append("RULE", "连续 %d 次判定失败，已停止判定：%s"
                               % (self._check_failures, reason))
            self.state.append_alert({
                "rule": "(判定熔断)",
                "reason": "连续 %d 次判定失败，已自动停止判定；串口收发不受影响" % self._check_failures,
                "source": "watch",
                "response": "",
                "action": "(已停止判定)",
            })


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


class WebServer(object):
    def __init__(self, paths, scene_name, port=None, mode="light"):
        self.paths = paths
        self.scene_name = scene_name
        self.scene = load_scene(paths["common_config"], paths["protocol_root"], scene_name)
        self.state = SharedState(paths["runtime_root"], scene_name)
        from .cli import Logger  # 延迟导入避免循环
        self.logger = Logger(paths["log_root"], scene_name)
        self.mode = mode if mode in ("light", "watch") else "light"
        self.port = int(port or self.scene.cfg.get("web_port") or DEFAULT_PORT)
        self.httpd = None
        self.loop = WatchLoop(self.scene, paths, self.state, self.logger)
        self.shutdown_event = threading.Event()
        self.override_com = ""
        self.override_baud = 0
        # 因控制权让位而主动放开串口时，记一句原因（网页上显示）
        self.hold_released_reason = ""
        # 上次收敛时的控制权，供 reconcile_port 判断"状态是否被外部改了"
        self._last_control = None
        self.last_reconcile = None
        # 监视模式下"刚手动发过命令"的时间窗，用来把回显来源标成 manual
        self._manual_until = 0.0
        # 定时发送（独立于串口占用/模式，见 _autosend_loop）
        self._autosend_thread = None
        self.autosend_stats = {"sent": 0, "errors": 0, "last_error": "", "last_tx": 0.0}
        self._port_owner_cache = {"at": 0.0, "value": None}
        # 单实例守卫：同一场景同时只允许一个网页实例，避免两个进程抢串口
        self._instance_file = os.path.join(paths["runtime_root"], "state",
                                           "%s.web.json" % _safe_name(scene_name))
        self._page = _load_page()

    # ── 配置与端口 ──────────────────────────────────────────────────────────
    def effective_cfg(self):
        cfg = dict(self.scene.cfg)
        if self.override_com:
            cfg["com"] = self.override_com
        if self.override_baud:
            cfg["baud"] = self.override_baud
        return cfg

    def current_device(self):
        resolved = resolve_port(self.effective_cfg())
        return resolved

    # ── 串口占用生命周期（与控制权解耦）────────────────────────────────────
    #
    # 关键设计：**控制权决定谁有权发命令，占用决定谁持有句柄**，两者分开。
    #   ai      → 网页需要口来发命令、跑轮询，所以持有
    #   human   → 口要还给用户（他要开串口助手），网页一律不持有
    #   paused  → 谁都不发，口也还回去
    # 轻模式下本来就不常占口，这里主要影响 watch 模式。

    def _should_hold_port(self, control=None):
        # 管家在跑时绝不由网页再拿一次口——两个持有者必然抢口
        if self._daemon_url():
            return False
        state = (control or self.state.read().get("state") or "human")
        return self.mode == "watch" and state == "ai"

    # ── 管家（可选）─────────────────────────────────────────────────────────
    def _daemon_url(self):
        """管家在跑就返回它的地址；缓存 2 秒，避免每次刷新状态都探测。"""
        now = time.time()
        cached = getattr(self, "_daemon_cache", None)
        if cached and now - cached[0] < 2.0:
            return cached[1]
        url = None
        try:
            from . import daemon as daemon_mod
            info = daemon_mod.read_instance(self.paths, self.scene.name)
            if info:
                status = daemon_mod.request(info.get("url") or "", "/status", timeout=0.5)
                if status and status.get("ok") and status.get("running"):
                    url = info.get("url")
        except Exception:
            url = None
        self._daemon_cache = (now, url)
        return url

    def _proxy_send_to_daemon(self, command, mode, actor, source):
        """管家在跑就让它发；返回 None 表示"没管家，走原来的路"。"""
        url = self._daemon_url()
        if not url:
            return None
        from . import daemon as daemon_mod
        result = daemon_mod.request(url, "/send",
                                    {"text": command, "mode": mode,
                                     "actor": actor, "source": source},
                                    timeout=10.0)
        if result is None:
            self._daemon_cache = (0, None)      # 连不上：下次重新探测
            return None
        if not result.get("ok"):
            err = result.get("error") or {}
            raise BridgeError(err.get("kind") or "internal",
                              err.get("message") or "管家拒绝了这个命令",
                              hint=err.get("hint"))
        payload = result.get("data") or {}
        return {
            "ok": True,
            "accepted": True,
            "mode": self.mode,
            "rx_text": payload.get("response") or "",
            "rx_hex": payload.get("response_hex") or "",
            "bytes": int(payload.get("bytes") or 0),
            "elapsed_ms": int(payload.get("elapsed_ms") or 0),
            "note": "经由管家（常驻连接）",
        }

    def _release_port_if_idle(self, reason=""):
        """按当前控制权决定是否放开串口。返回是否真的放开了。"""
        if self.loop.running and not self._should_hold_port():
            self.loop.stop()
            self.hold_released_reason = reason or "控制权不在 AI"
            return True
        return False

    def _acquire_port_if_needed(self):
        """控制权回到 ai 时重新拿口（watch 模式才需要）。"""
        if self.mode != "watch" or self.loop.running:
            return False
        resolved = self.current_device()
        self.loop.start(resolved["device"],
                        int(self.effective_cfg().get("baud") or 115200))
        self.hold_released_reason = ""
        return True

    def reconcile_port(self, trigger="change"):
        """让串口占用对齐当前控制权——**唯一的收敛点**。

        为什么需要它：控制权存在共享文件里，改它的路径有三条
          1. 网页按钮 POST /control
          2. serial_control 工具（CLI 子进程）
          3. 人类在别处跑 bridge.py control
        只有第 1 条会顺手调 release/acquire。如果只在按钮里做，
        CLI 把状态改成 human 时网页仍攥着口——这正是实测反馈的场景。

        所以释放/获取不挂在"谁改了状态"上，而是挂在"状态变了"这件事上：
        看门狗每秒比对一次，发现控制权或模式变了就收敛一次。
        """
        try:
            current = self.state.read().get("state") or "human"
        except Exception:
            return None
        previous = self._last_control
        if previous == current and trigger != "force":
            return None
        self._last_control = current

        action = "unchanged"
        try:
            if current == STATE_AI:
                if self._acquire_port_if_needed():
                    action = "acquired"
            else:
                label = "控制权切到 %s" % current
                if self._release_port_if_idle(label):
                    action = "released"
        except BridgeError as exc:
            action = "failed: %s" % exc.message
            self.loop.last_error = exc.message
        self.last_reconcile = {"action": action, "control": current, "trigger": trigger}
        return self.last_reconcile

    def _port_watchdog(self):
        """每秒看一次控制权是否被别人改了；变了就收敛串口占用。"""
        while not self.shutdown_event.is_set():
            try:
                self.reconcile_port("watchdog")
            except Exception:
                pass
            self.shutdown_event.wait(1.0)

    def port_owner(self):
        """谁占着这个口：ours / other / free。用于网页状态栏。

        探测本身要临时开一次口，所以缓存 5 秒：网页每 700ms 拉一次 /status，
        不缓存就变成"状态栏自己在占口"。
        """
        now = time.time()
        cached = self._port_owner_cache.get("value")
        if cached is not None and (now - self._port_owner_cache.get("at", 0)) < 5.0:
            return cached
        device = ""
        try:
            device = self.current_device()["device"]
        except BridgeError:
            value = {"owner": "unknown", "device": "", "detail": "串口未解析出来"}
            self._port_owner_cache = {"at": now, "value": value}
            return value
        if self.loop.running and self.loop.ser is not None:
            value = {"owner": "ours", "device": device,
                     "detail": "本网页的监视模式正持有 %s" % device}
            self._port_owner_cache = {"at": now, "value": value}
            return value
        try:
            with self.loop.port_lock:
                serial_io.open_and_check(self.effective_cfg())
            value = {"owner": "free", "device": device, "detail": "%s 空闲，可用" % device}
        except BridgeError as exc:
            if exc.kind == "port_busy":
                value = {"owner": "other", "device": device,
                         "detail": "被其他程序占用（串口助手/其他终端工具）"}
            else:
                value = {"owner": "unknown", "device": device, "detail": exc.message}
        self._port_owner_cache = {"at": now, "value": value}
        return value

    # ── 状态 ────────────────────────────────────────────────────────────────
    def status(self):
        current = self.state.read()
        watch_cfg = current.get("watch") or {}
        autosend_alive = self._autosend_thread is not None and self._autosend_thread.is_alive()
        data = {
            "ok": True,
            "scene": self.scene.name,
            "mode": self.mode,
            "control": current.get("state"),
            "control_since": current.get("since"),
            "watch": watch_cfg,
            "bauds": BAUD_CHOICES,
            # watch_running 表示"后台自动循环是否在跑"：
            # 监视模式占口、或定时发送已开启，都算在跑（UI 用它显示状态）。
            "watch_running": self.loop.running or (autosend_alive and bool(watch_cfg.get("send_enabled"))),
            "port_held": self.loop.running,
            "sent_count": self.autosend_stats.get("sent", 0),
            "autosend_error": self.autosend_stats.get("last_error", ""),
            "watch_error": self.loop.last_error,
            "alerts": self.state.read_alerts(5),
            "port": current.get("port") or "",
            "baud": current.get("baud") or int(self.effective_cfg().get("baud") or 0),
            "hold_released_reason": self.hold_released_reason,
            "state_path": self.state.path,
        }
        try:
            resolved = self.current_device()
            data["resolved"] = resolved
            data["port"] = resolved["device"]
            # 传输方式描述：网页侧边栏据此显示"串口 COM10 @115200"
            data["transport"] = transport_describe(
                self.effective_cfg(), resolved["device"], resolved.get("baud"))
        except BridgeError as exc:
            data["resolved"] = None
            data["resolve_error"] = exc.to_dict()
            data["transport"] = transport_describe(self.effective_cfg())
        data["port_owner"] = self.port_owner()
        return data

    def poll(self, force=False):
        """把后端攒下的数据交给网页。

        自动发送（定时发送 / 规则自动处置）的每一帧都会进 `auto_frames`，
        这里**不分 mode** 都取走——否则 light 模式下定时发送在页面上看不到回显
        （Bug 4：用户说"没看到"，不是"没发送"）。

        管家在跑时改从管家取（游标式增量），网页就完全不用碰串口了。
        """
        url = self._daemon_url()
        if url:
            from . import daemon as daemon_mod
            # 第一次接上管家时用 -1：只收从现在起的新数据，不倒历史
            cursor = getattr(self, "_daemon_cursor", -1)
            cursor = -1 if cursor is None else int(cursor)
            proxied = daemon_mod.request(url, "/poll?cursor=%d" % cursor, timeout=2.0)
            if proxied and proxied.get("ok"):
                self._daemon_cursor = int(proxied.get("cursor") or cursor)
                if proxied.get("bytes"):
                    return {
                        "ok": True,
                        "frames": [],
                        "rx_text": proxied.get("rx_text") or "",
                        "rx_hex": proxied.get("rx_hex") or "",
                        "bytes": int(proxied.get("bytes") or 0),
                        "via": "daemon",
                    }
                return {"ok": True, "frames": [], "rx_text": "", "rx_hex": "",
                        "bytes": 0, "via": "daemon"}
            self._daemon_cache = (0, None)      # 管家掉线：回退到本地逻辑

        frames = self.loop.take_frames()
        data = self.loop.take()
        if frames or data:
            return {
                "ok": True,
                "frames": frames,
                "rx_text": data.decode("utf-8", errors="replace"),
                "rx_hex": " ".join("%02X" % b for b in data),
                "bytes": len(data),
            }
        # 轻模式**默认完全不碰串口**。
        # 早先这里每 700ms 临时开口读一次（每次最多等 1500ms 首字节），
        # 结果网页开着的时候串口几乎一直被占着——外部程序和 AI 都打不开（Bug 5）。
        # 现在只有用户手动点「读一次」(force) 才真正开口。
        if not force:
            return {"ok": True, "rx_text": "", "rx_hex": "", "bytes": 0,
                    "skipped": "轻模式不主动读串口（点「读一次」可临时读一帧）"}
        try:
            resolved = self.current_device()
            with self.loop.port_lock:
                out = serial_io.drain(self.effective_cfg(), resolved["device"])
        except BridgeError as exc:
            return {"ok": False, "error": exc.to_dict()}
        if out["rx_text"]:
            self.logger.append("RX", out["rx_text"])
        return {
            "ok": True,
            "rx_text": out["rx_text"],
            "rx_hex": out["rx_hex"],
            "bytes": out["bytes"],
        }

    def alerts(self, limit):
        return self.state.read_alerts(limit)

    # ── 动作 ────────────────────────────────────────────────────────────────
    def send(self, text, mode="text", actor="human", source="web"):
        """发一条命令。

        source 表示"谁发的"：web(页面按钮) / ai(插件 serial_send 转发过来)。
        AI 发起的会在 auto_frames 里记一帧，网页回显区就能看到（Bug 8）。

        管家在跑时**交给管家发**：它已经常驻持有连接，网页就不该再去开关口
        ——否则用户在网页上点一下，AI 的速度就从 0ms 掉回 1 秒（第 4 件核心目标）。
        """
        command = str(text or "").strip()
        if not command:
            raise BridgeError("config", "命令不能为空")
        if actor == "ai":
            allowed, current = self.state.check_ai_allowed()
            if not allowed:
                raise BridgeError(
                    "control_blocked",
                    "控制权是 %s，AI 不能发命令。" % current.get("state"),
                    hint=["点网页上的[交给AI]（或 [继续AI]）",
                          "也可以让 AI 调 serial_control action=ai",
                          "人类发任意命令会自动把控制权拉回 human"],
                )
        else:
            # 安全规则 1：人类命令永远优先，AI 托管立即让位
            self.state.note_human_command()

        proxied = self._proxy_send_to_daemon(command, mode, actor, source)
        if proxied is not None:
            return proxied

        if mode == "hex":
            payload_bytes = _parse_hex(command)
            if payload_bytes is None:
                raise BridgeError("config", "十六进制格式不对，示例：AA 55 01 00")
        else:
            payload_bytes = command.encode("utf-8")

        if self.mode == "watch" and self.loop.running and self.loop.ser is not None:
            if mode == "hex":
                # 原始字节模式不补换行，帧由调用方给全（见 io.transact 的说明）
                self.loop.ser.write(payload_bytes)
            else:
                self.loop.ser.write(payload_bytes + serial_io.decode_newline(
                    self.effective_cfg().get("newline"), "\\r\\n"))
            self.logger.append("TX", command)
            # 标记"接下来这一小段回显是手动触发的"，读线程据此把来源标成 manual
            self._manual_until = time.time() + 2.0
            if source != "web":
                self.loop._push_frame(command, "", "", 0, source)
            return {"ok": True, "accepted": True, "mode": self.mode}

        resolved = self.current_device()
        cfg = self.effective_cfg()
        # Bug 11：hex 模式必须把解析出来的原始字节传下去，
        # 否则 transact 会把 "AA 55" 当成 5 个 ASCII 字符发出去。
        with self.loop.port_lock:
            result = serial_io.transact(
                cfg, resolved["device"], command,
                raw_bytes=payload_bytes if mode == "hex" else None)
        self.logger.append("TX", command)
        if result["rx_text"]:
            self.logger.append("RX", result["rx_text"])
        self.state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0))

        # Bug 7：手动发的回显也要跑判定，否则"设备回了 FAULT 但 alerts 是空的"
        if not result["empty"]:
            self._maybe_check(result["rx_text"], "manual")
        # AI 发起的命令记一帧，网页才看得到（Bug 8）
        if source != "web":
            self.loop._push_frame(command, result["rx_text"], result["rx_hex"],
                                  result["bytes"], source)
        return {
            "ok": True,
            "accepted": True,
            "mode": self.mode,
            "rx_text": result["rx_text"],
            "rx_hex": result["rx_hex"],
            "bytes": result["bytes"],
            "elapsed_ms": result["elapsed_ms"],
            "note": resolved["note"],
        }

    def _maybe_check(self, text, source):
        """按 check_enabled / check_source 决定要不要判定。绝不抛异常。"""
        try:
            watch = self.state.read().get("watch") or {}
            if not watch.get("check_enabled"):
                return
            want = str(watch.get("check_source") or "watch_only")
            if want == "watch_only" and source != "watch":
                return
            if want == "manual_only" and source != "manual":
                return
            self.loop._run_checks(text, source)
        except Exception:
            pass

    def control(self, action):
        # 按"动作 + 源状态"校验：resume 的目标也是 ai，不能只看目标状态
        mapping = {
            "ai": (STATE_AI, "交给AI", {STATE_HUMAN, STATE_PAUSED}),
            "pause": (STATE_PAUSED, "暂停AI", {STATE_AI}),
            "resume": (STATE_AI, "继续AI", {STATE_PAUSED}),
            "human": (STATE_HUMAN, "人类接管", {STATE_AI, STATE_PAUSED}),
        }
        if action not in mapping:
            raise BridgeError("config", "未知的控制动作：%r" % action)
        target, label, sources = mapping[action]
        current_state = self.state.read().get("state")
        # 幂等：已经是目标状态就当作成功，不要报 400。
        # （早期版本把 human->human 当成非法迁移，导致"已经在人类手中"时
        #  再点[人类接管]会报错，同一个按钮有时好有时坏，非常迷惑。）
        if current_state == target:
            return {
                "ok": True,
                "control": current_state,
                "state": self.state.read(),
                "port_action": "unchanged",
                "watch_running": self.loop.running,
                "note": "已经是%s，无需切换" % label,
            }
        if current_state not in sources:
            raise BridgeError(
                "control_blocked",
                "当前是 %s，不能执行[%s]。" % (current_state, label),
                hint=["human 只能[交给AI]；ai 只能[暂停AI]/[人类接管]；"
                      "paused 可以[交给AI]/[继续AI]/[人类接管]"],
            )
        try:
            current = self.state.set_state(target, "web", label)
        except ValueError as exc:
            raise BridgeError("control_blocked", "状态迁移被拒绝（%s）。" % exc)

        # 控制权一变，串口占用跟着变。走统一的收敛点，
        # 和看门狗、CLI、serial_control 工具用的是同一段逻辑。
        forced = self.reconcile_port("web-control") or {}
        port_action = forced.get("action", "unchanged")

        return {
            "ok": True,
            "control": current.get("state"),
            "state": current,
            "port_action": port_action,
            "watch_running": self.loop.running,
        }

    def set_watch(self, body):
        current = self.state.read()
        merged = dict(current.get("watch") or {})
        allowed_keys = {
            "send_enabled", "check_enabled", "check_source", "command",
            "interval_ms", "timeout_ms", "command_mode",
        }
        for key, value in (body or {}).items():
            if key in allowed_keys:
                merged[key] = value
        self.state.update(watch=merged)
        return {"ok": True, "watch": merged}

    # ── 规则编辑（写回场景 config.json）─────────────────────────────────────
    def get_rules(self):
        scene = load_scene(self.paths["common_config"], self.paths["protocol_root"],
                           self.scene_name)
        watch = scene.watch_cfg()
        return {
            "ok": True,
            "rules": watch.get("rules") or [],
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
            "rule_types": list(RULE_TYPES),
            "config_path": os.path.join(scene.dir, "config.json"),
        }

    def set_rules(self, body):
        """整体替换规则的列表。

        只动场景 config.json 里的 watch.rules / allowed_auto_commands，
        其它字段原样保留（包括用户手写的注释性键）。
        """
        rules = (body or {}).get("rules")
        if rules is None:
            raise BridgeError("config", "缺少 rules 字段",
                              hint=['请求体示例：{"rules": [{"name":"...","when":{...},"then":{...}}]}'])
        if not isinstance(rules, list):
            raise BridgeError("config", "rules 必须是数组")

        cleaned = []
        for index, rule in enumerate(rules, 1):
            if not isinstance(rule, dict):
                raise BridgeError("config", "第 %d 条规则不是对象" % index)
            name = str(rule.get("name") or "").strip()
            if not name:
                raise BridgeError("config", "第 %d 条规则缺少 name" % index)
            when = rule.get("when") or {}
            wtype = str(when.get("type") or "").strip()
            if wtype not in RULE_TYPES:
                raise BridgeError(
                    "config",
                    "规则「%s」的判据类型 %r 不支持。" % (name, wtype),
                    hint=["只能是：" + " / ".join(RULE_TYPES)])
            entry = {"name": name, "when": dict(when)}
            if wtype in ("contains", "equals") and not str(when.get("value") or ""):
                raise BridgeError("config", "规则「%s」的 %s 需要 value" % (name, wtype))
            if wtype == "regex" and not str(when.get("pattern") or when.get("value") or ""):
                raise BridgeError("config", "规则「%s」的 regex 需要 pattern" % name)
            if wtype == "regex":
                # 写进配置前先编译一次，别把坏正则存盘
                try:
                    import re as _re
                    _re.compile(str(when.get("pattern") or when.get("value")))
                except Exception as exc:
                    raise BridgeError("config", "规则「%s」的正则不合法：%s" % (name, exc))
            if wtype == "no_response":
                try:
                    entry["when"]["consecutive"] = max(1, int(when.get("consecutive") or 3))
                except (TypeError, ValueError):
                    raise BridgeError("config", "规则「%s」的 consecutive 必须是整数" % name)
            then = rule.get("then") or {}
            if then:
                entry["then"] = dict(then)
            if "notify" in rule:
                entry["notify"] = bool(rule.get("notify"))
            cleaned.append(entry)

        allowed = (body or {}).get("allowed_auto_commands")
        scene = load_scene(self.paths["common_config"], self.paths["protocol_root"],
                           self.scene_name)
        config_path = os.path.join(scene.dir, "config.json")

        # 读原始文件（保留用户已有的其它键），只替换 watch 块里这两项
        try:
            with open(config_path, encoding="utf-8-sig") as handle:
                raw = json.loads(handle.read() or "{}")
        except FileNotFoundError:
            raw = {}
        except ValueError as exc:
            raise BridgeError("config", "场景 config.json 不是合法 JSON，未写入。",
                              detail=str(exc))
        watch = dict(raw.get("watch") or {})
        watch["rules"] = cleaned
        if allowed is not None:
            if not isinstance(allowed, list):
                raise BridgeError("config", "allowed_auto_commands 必须是数组")
            watch["allowed_auto_commands"] = [str(x).strip() for x in allowed if str(x).strip()]
        raw["watch"] = watch

        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, config_path)

        # 场景对象缓存的是启动时的配置，改完要重新读一次
        self.scene = load_scene(self.paths["common_config"], self.paths["protocol_root"],
                                self.scene_name)
        return {
            "ok": True,
            "rules": cleaned,
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
            "config_path": config_path,
        }

    def reconnect(self, com, baud):
        if com:
            self.override_com = com
        if baud:
            self.override_baud = int(baud)
        # 真重建：watch 模式先停旧口再按新参数开
        if self.mode == "watch":
            resolved = self.current_device()
            self.loop.start(resolved["device"], int(self.effective_cfg().get("baud") or 115200))
        else:
            resolved = self.current_device()
            serial_io.open_and_check(self.effective_cfg())
            self.state.update(port=resolved["device"],
                              baud=int(self.effective_cfg().get("baud") or 0))
        return {"ok": True, "port": self.current_device()["device"], "baud": self.effective_cfg().get("baud")}

    def autodetect(self):
        """AI 自动选择：按场景的 match / match_vid_pid 规则扫描并说明理由。"""
        resolved = resolve_port(self.scene.cfg)
        reasons = {
            "match_vid_pid": "按 VID:PID %s 精确匹配" % self.scene.cfg.get("match_vid_pid"),
            "match": "按描述关键字 %r 匹配" % self.scene.cfg.get("match"),
            "com": "配置里指定的串口号可用",
            "only_port": "当前只有一个串口设备",
        }
        return {
            "ok": True,
            "port": resolved["device"],
            "description": resolved["description"],
            "vid_pid": resolved["vid_pid"],
            "reason": reasons.get(resolved["source"], resolved["source"]),
            "note": resolved["note"],
        }

    def set_mode(self, mode):
        if mode not in ("light", "watch"):
            raise BridgeError("config", "模式只能是 light 或 watch")
        if mode == self.mode:
            return {"ok": True, "mode": self.mode}

        # 管家在跑：监视模式交给它，网页仍然不拿口（否则两边抢口）
        daemon_url = self._daemon_url()
        if daemon_url:
            from . import daemon as daemon_mod
            result = daemon_mod.request(daemon_url, "/mode", {"mode": mode}, timeout=2.0)
            if result is None:
                self._daemon_cache = (0, None)      # 管家没了：退回本地逻辑
            elif not result.get("ok"):
                err = result.get("error") or {}
                raise BridgeError(err.get("kind") or "internal",
                                  err.get("message") or "管家拒绝切换模式",
                                  hint=err.get("hint"))
            else:
                self.mode = mode
                self.loop.stop()                    # 网页不再持口
                self.hold_released_reason = "由管家持有（监视模式）"
                return {"ok": True, "mode": self.mode, "watch_running": True,
                        "via": "daemon"}

        if mode == "watch":
            # 切监视模式也遵守控制权：控制权不在 AI 就先不抢口
            self.mode = "watch"
            if self._should_hold_port():
                self._acquire_port_if_needed()
            else:
                self.hold_released_reason = "控制权不在 AI，监视模式暂不占口"
        else:
            self.loop.stop()
            self.mode = "light"
            self.hold_released_reason = ""
        return {"ok": True, "mode": self.mode, "watch_running": self.loop.running}

    # ── 暂停（网页留着） vs 退出（网页也关）───────────────────────────────
    def pause_port(self):
        """暂停：释放串口，但网页继续可访问。想临时用串口助手时点这个。"""
        if self.loop.running:
            self.loop.stop()
        self.hold_released_reason = "已手动暂停，串口已释放"
        return {"ok": True, "paused": True, "watch_running": self.loop.running,
                "port_owner": self.port_owner()}

    def resume_port(self):
        """从"暂停"恢复占用（只有控制权在 AI 且是 watch 模式才真的占口）。"""
        self.hold_released_reason = ""
        acquired = False
        if self._should_hold_port():
            acquired = self._acquire_port_if_needed()
        return {"ok": True, "paused": False, "acquired": acquired,
                "watch_running": self.loop.running, "port_owner": self.port_owner()}

    def request_shutdown(self):
        self.shutdown_event.set()
        threading.Thread(target=self._shutdown_worker, name="shutdown", daemon=True).start()

    def _autosend_loop(self):
        """定时发送：独立于模式、独立于串口占用。

        Bug 4 的根因是它原来活在 WatchLoop 里，而 WatchLoop 只在
        「监视模式 + 控制权 ai」时启动 —— light 模式下打开开关既不发也不报错。
        放在 WebServer 层后，两种模式都按 interval_ms 发；light 模式每次临时开口。
        控制权为 paused 时跳过（paused 的语义是"谁都不发"）。
        """
        next_at = 0.0
        while not self.shutdown_event.is_set():
            try:
                current = self.state.read()
                watch = current.get("watch") or {}
                enabled = bool(watch.get("send_enabled"))
                command = str(watch.get("command") or "").strip()
                interval = max(0.2, float(watch.get("interval_ms") or 1000) / 1000.0)
                control = current.get("state") or "human"
                if not enabled or not command or control == STATE_PAUSED:
                    next_at = 0.0
                    self.shutdown_event.wait(0.2)
                    continue
                now = time.time()
                if now < next_at:
                    self.shutdown_event.wait(min(0.2, max(0.01, next_at - now)))
                    continue
                next_at = now + interval
                # 场景可以声明 command_mode: "hex"，让定时发送的 command 按十六进制解析
                cmd_mode = str(watch.get("command_mode")
                               or self.scene.cfg.get("command_mode") or "text").lower()
                raw = _parse_hex(command) if cmd_mode == "hex" else None
                if cmd_mode == "hex" and raw is None:
                    raise BridgeError("config", "定时发送命令不是合法十六进制：%r" % command)
                text = self.loop.send_raw(command, raw_bytes=raw)
                self.autosend_stats["sent"] = self.autosend_stats.get("sent", 0) + 1
                self.autosend_stats["last_tx"] = time.time()
                self.autosend_stats["last_error"] = ""
                # 定时轮询的回显按 check_source 决定要不要判定
                self._maybe_check(text, "watch")
            except BridgeError as exc:
                self.autosend_stats["errors"] = self.autosend_stats.get("errors", 0) + 1
                self.autosend_stats["last_error"] = exc.message
                self.logger.append("WATCH", "定时发送失败：%s" % exc.message)
                self.shutdown_event.wait(0.5)
            except Exception as exc:
                self.autosend_stats["errors"] = self.autosend_stats.get("errors", 0) + 1
                self.autosend_stats["last_error"] = str(exc)
                self.shutdown_event.wait(0.5)

    def _existing_instance(self):
        """检测同一场景是否已有网页实例在跑（避免两个进程抢串口）。"""
        try:
            with open(self._instance_file, encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError):
            return ""
        url = str(info.get("url") or "")
        if not url:
            return ""
        # 用一次 HTTP 探测判断它是否还活着（Windows 上不能安全用 os.kill(pid,0) 探活）
        try:
            with urllib.request.urlopen(url + "/status", timeout=1.0) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            pass
        return ""

    def _write_instance(self, url):
        try:
            os.makedirs(os.path.dirname(self._instance_file), exist_ok=True)
            with open(self._instance_file, "w", encoding="utf-8") as handle:
                json.dump({"url": url, "port": self.port, "pid": os.getpid(),
                           "scene": self.scene.name}, handle, ensure_ascii=False)
        except OSError:
            pass

    def _clear_instance(self):
        try:
            os.remove(self._instance_file)
        except OSError:
            pass

    def _heartbeat_loop(self):
        """每 2 秒声明一次"有人在看着这一局"，AI 的门禁据此切严格/宽松。"""
        while not self.shutdown_event.is_set():
            try:
                self.state.heartbeat("web")
            except Exception:
                pass
            self.shutdown_event.wait(2.0)

    def _shutdown_worker(self):
        time.sleep(0.15)  # 让 /shutdown 的响应先写完
        self.loop.stop()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    # ── 服务生命周期 ────────────────────────────────────────────────────────
    def serve(self, open_browser=False):
        # 单实例守卫：同一场景只允许一个网页实例，否则两个进程会互相抢串口。
        existing = self._existing_instance()
        if existing:
            print(json.dumps({
                "ok": False,
                "error": "这个场景已经有一个网页实例在运行，不再重复启动。",
                "url": existing,
            }, ensure_ascii=False))
            sys_flush()
            return 0

        # 启动时是否抢口，也遵守控制权：
        #   watch + ai      → 抢口（要看连续数据流）
        #   watch + human   → 不抢，把口留给用户
        #   light           → 本来就不常占口
        boot_error = None
        if self.mode == "watch" and self._should_hold_port():
            try:
                resolved = self.current_device()
                self.loop.start(resolved["device"],
                                int(self.effective_cfg().get("baud") or 115200))
            except BridgeError as exc:
                # 口打不开也要把网页开起来：用户需要看到错误并换口
                boot_error = exc.to_dict()
                self.loop.last_error = exc.message
        elif self.mode == "watch":
            self.hold_released_reason = "控制权不在 AI，监视模式暂不占口"
        # 记住启动时的控制权，避免看门狗第一次跑就误判成"状态变化"
        self._last_control = self.state.read().get("state")

        handler = type("BoundHandler", (BridgeHandler,), {})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.httpd.daemon_threads = True
        self.httpd.context = {
            "page": self._page,
            "status": self.status,
            "poll": self.poll,
            "alerts": self.alerts,
            "send": self.send,
            "control": self.control,
            "set_watch": self.set_watch,
            "set_rules": self.set_rules,
            "get_rules": self.get_rules,
            "pause_port": self.pause_port,
            "resume_port": self.resume_port,
            "reconnect": self.reconnect,
            "autodetect": self.autodetect,
            "set_mode": self.set_mode,
            "request_shutdown": self.request_shutdown,
        }
        url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        # 看门狗：控制权被别处（CLI / serial_control 工具）改掉时，这里自动释放/重占串口
        threading.Thread(target=self._port_watchdog, name="port-watchdog", daemon=True).start()
        # 定时发送：独立线程，light / watch 两种模式都生效（Bug 4）
        self._autosend_thread = threading.Thread(
            target=self._autosend_loop, name="autosend", daemon=True)
        self._autosend_thread.start()
        self._write_instance(url)
        print(json.dumps({
            "ok": True,
            "url": url,
            "port": self.port,
            "scene": self.scene.name,
            "mode": self.mode,
            **({"warning": boot_error} if boot_error else {}),
        }, ensure_ascii=False))
        sys_flush()
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self.loop.stop()
            self._clear_instance()
            try:
                self.httpd.server_close()
            except Exception:
                pass
        return 0


def sys_flush():
    try:
        import sys
        sys.stdout.flush()
    except Exception:
        pass


def _parse_hex(text):
    cleaned = str(text).replace(",", " ").replace("0x", " ").replace("0X", " ")
    parts = [p for p in cleaned.split() if p]
    out = bytearray()
    for part in parts:
        try:
            value = int(part, 16)
        except ValueError:
            return None
        if value < 0 or value > 255:
            return None
        out.append(value)
    return bytes(out) if out else None


def _load_page():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "index.html")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return "<!doctype html><meta charset='utf-8'><h3>web/index.html 缺失</h3>"


def run_web(paths, scene_name, port, mode, open_browser):
    server = WebServer(paths, scene_name, port, mode)
    return server.serve(open_browser=open_browser)
