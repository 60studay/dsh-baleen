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
from http.server import ThreadingHTTPServer
import urllib.request

from . import io as serial_io
from .channel import Channel, run_evidence
from .errors import BridgeError
from .ports import BAUD_CHOICES, resolve_port, scan_ports, guess_port  # noqa: F401
from .port_manager import PortManager
from .rule_checker import RuleChecker
from .watch_loop import WatchLoop
from .web_handler import BridgeHandler
from .runner import RuleWorker
from .state import SharedState, STATE_AI, STATE_HUMAN, STATE_PAUSED, VALID_STATES, _safe_name
from .transport import describe_transport as transport_describe
# BAUD_CHOICES 定义在 ports.py（路由层 web_handler 也要用，不该反过来依赖这里）
from .ports import resolve_port, scan_ports, guess_port

DEFAULT_PORT = 8899


from .config import load_scene
from .web.status import StatusMixin
from .web.rules import RulesMixin
from .web.channel import ChannelMixin
from .web.serial import SerialMixin
from .web.ports import PortMixin


class WebServer(StatusMixin, RulesMixin, ChannelMixin, SerialMixin, PortMixin):
    def __init__(self, paths, scene_name, port=None, mode="light"):
        self.paths = paths
        self.scene_name = scene_name
        self.scene = load_scene(paths["common_config"], paths["protocol_root"], scene_name)
        self.state = SharedState(paths["runtime_root"], scene_name)
        from .cli import Logger  # 延迟导入避免循环
        self.logger = Logger(paths["log_root"], scene_name)
        # AI ⇄ AI 通道：一个共享的 JSONL 留言板（不分场景——它讨论的是"活干完没"，
        # 和具体串口无关；两个 AI 用同一个 log_root 就自动共用）
        self.chan = Channel(paths["log_root"])
        self.mode = mode if mode in ("light", "watch") else "light"
        self.port = int(port or self.scene.cfg.get("web_port") or DEFAULT_PORT)
        self.httpd = None
        self.loop = WatchLoop(self.scene, paths, self.state, self.logger)
        self.shutdown_event = threading.Event()
        self.override_com = ""
        self.override_baud = 0
        # 端口占用/释放/看门狗都交给 PortManager（它不碰界面，只管"谁该拿口"）
        self.ports = PortManager(
            state=self.state,
            loop=self.loop,
            current_device=self.current_device,
            effective_cfg=self.effective_cfg,
            mode_getter=lambda: self.mode,
            shutdown_event=self.shutdown_event,
            daemon_url=self._daemon_url,
        )
        # 监视模式下"刚手动发过命令"的时间窗，用来把回显来源标成 manual
        self._manual_until = 0.0
        # 定时发送（独立于串口占用/模式，见 _autosend_loop）
        self._autosend_thread = None
        self.autosend_stats = {"sent": 0, "errors": 0, "last_error": "", "last_tx": 0.0}
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
        if self.mode == "watch" and self.ports.should_hold():
            try:
                resolved = self.current_device()
                self.loop.start(resolved["device"],
                                int(self.effective_cfg().get("baud") or 115200))
            except BridgeError as exc:
                # 口打不开也要把网页开起来：用户需要看到错误并换口
                boot_error = exc.to_dict()
                self.loop.last_error = exc.message
        elif self.mode == "watch":
            self.ports.hold_released_reason = "控制权不在 AI，监视模式暂不占口"
        # 记住启动时的控制权，避免看门狗第一次跑就误判成"状态变化"
        self.ports._last_control = self.state.read().get("state")

        handler = type("BoundHandler", (BridgeHandler,), {})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.httpd.daemon_threads = True
        self.httpd.context = {
            "page": self._page,
            "asset": _load_asset,
            "status": self.status,
            "poll": self.poll,
            "alerts": self.alerts,
            "send": self.send,
            "control": self.control,
            "set_watch": self.set_watch,
            "set_rules": self.set_rules,
            "get_rules": self.get_rules,
            "pause_port": self.pause_port,
            "probe_port": self.probe_port,
            "clear_alerts": self.clear_alerts,
            "resume_port": self.resume_port,
            "reconnect": self.reconnect,
            "autodetect": self.autodetect,
            "set_mode": self.set_mode,
            "channel_list": self.channel_list,
            "channel_send": self.channel_send,
            "channel_verify": self.channel_verify,
            "request_shutdown": self.request_shutdown,
        }
        url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        # 看门狗：控制权被别处（CLI / serial_control 工具）改掉时，这里自动释放/重占串口
        threading.Thread(target=self.ports.watchdog, name="port-watchdog", daemon=True).start()
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


def _load_page():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "index.html")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return "<!doctype html><meta charset='utf-8'><h3>web/index.html 缺失</h3>"


_ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def _load_asset(name):
    """读 web/ 下的静态文件（app.css / app.js / js/*.js）。

    返回 (bytes, content_type)；不存在返回 None。
    白名单式：只认 .css/.js，**只允许 js/ 这一层子目录**，不许 `..`。
    这是"网页能读服务器文件"的唯一入口，边界必须是死的。
    """
    base = str(name or "").lstrip("/")
    if not base or "\\" in base or ".." in base:
        return None
    parts = [p for p in base.split("/") if p]
    if not parts or len(parts) > 2:
        return None
    if len(parts) == 2 and parts[0] != "js":
        return None
    suffix = os.path.splitext(parts[-1])[1].lower()
    content_type = _ASSET_TYPES.get(suffix)
    if not content_type:
        return None
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", *parts)
    try:
        with open(path, "rb") as handle:
            return handle.read(), content_type
    except OSError:
        return None


def run_web(paths, scene_name, port, mode, open_browser):
    server = WebServer(paths, scene_name, port, mode)
    return server.serve(open_browser=open_browser)
