# -*- coding: utf-8 -*-
"""鲸歌管家：常驻持有连接、事件驱动广播的独立进程。

为什么要有它：不开网页时，AI 每条命令要"临时开串口 → 发 → 等安静 → 关"，
1.2 秒/条。管家把它变成"常驻一个连接 + 有数据立刻广播"，而且**跟网页开不开无关**。

设计要点：
- 只依赖 Transport，不认 pyserial（以后 TCP / 文件通道同样能挂进来）
- 广播不等安静（那是"临时开关"才需要的妥协）；但**规则判定单独用时间窗**，
  因为设备会在 OK 之后几百毫秒才异步上报故障（实测 424ms）
- 单实例：instance 文件 + HTTP 探活，重复 start 返回同一个 pid
- 空闲自动退出并释放连接（默认 10 分钟，可配）

命令行：
    python -m serial_bridge.daemon --scene 蓝牙AT [--port 8898] [--idle 600]
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_scene
from .errors import BridgeError
from .log import Logger
from .ports import resolve_port
from .runner import RuleWorker
from .state import SharedState, STATE_PAUSED, _safe_name
from .transport import create_transport, describe_transport

DEFAULT_PORT = 8898
DEFAULT_IDLE_S = 600
# 判定窗口：发送后这么久内到达的字节归同一条命令（覆盖异步 FAULT）
JUDGE_WINDOW_S = 1.0
# 等回显的默认预算。
# 注意：这台底盘的**设备侧**首字节就要 500~600ms（实测 X/H/A/P 全都这样），
# 所以"含回包"的耗时不可能低于 600ms —— 那不是软件开销。
# 管家能做到的是：命令**提交**只要几毫秒（不再开关口、不再等安静），
# 并且在等回包期间不阻塞别的客户端。
DEFAULT_REPLY_WAIT_MS = 1500
# 短等（只收已经到的字节）用这个，给需要"发完就走"的调用方
SEND_QUIET_MS = 25


# ── 进程内状态 ──────────────────────────────────────────────────────────────
class Daemon(object):
    def __init__(self, paths, scene_name, http_port=DEFAULT_PORT, idle_s=DEFAULT_IDLE_S):
        self.paths = paths
        self.scene = load_scene(paths["common_config"], paths["protocol_root"], scene_name)
        self.state = SharedState(paths["runtime_root"], self.scene.name)
        self.logger = Logger(paths["log_root"], self.scene.name)
        self.http_port = int(http_port)
        self.idle_s = max(30, int(idle_s))

        self.transport = None
        self.resolved = None
        self.running = False
        self.lock = threading.Lock()
        self.buffer = bytearray()
        self.judge_buf = bytearray()
        self.judge_deadline = 0.0
        self.last_activity = time.time()
        self.started_at = time.time()
        self.last_error = ""
        self._rule_worker = None
        self._check_failures = 0
        self._checks_stopped = False
        self.stats = {"sent": 0, "rx_bytes": 0, "checks": 0, "errors": 0}
        # light = 有人发命令就回应（默认）；watch = 持续监视（网页"监视模式"）
        # 两者读线程都在跑，区别是：watch 会把每段数据都送判定，并且保留更长的历史。
        self.mode = "light"
        self.mode_changed_at = time.time()

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def start(self):
        self.resolved = resolve_port(self.scene.cfg)
        self.transport = create_transport(
            self.scene.cfg, self.resolved["device"]).open()
        self.state.update(port=self.resolved["device"],
                          baud=int(self.scene.cfg.get("baud") or 0),
                          scene=self.scene.name)
        self.running = True
        threading.Thread(target=self._read_loop, name="daemon-read", daemon=True).start()
        threading.Thread(target=self._idle_loop, name="daemon-idle", daemon=True).start()
        return self

    def stop(self):
        self.running = False
        if self._rule_worker is not None:
            try:
                self._rule_worker.close()
            except Exception:
                pass
            self._rule_worker = None
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None

    def touch(self):
        self.last_activity = time.time()

    def set_mode(self, mode):
        """切换轻模式 / 监视模式。

        监视模式不是"另一套读法"——读线程一直在跑，区别在于监视模式下
        **每段到达的数据都送判定**（不再只盯着"刚发出去那条命令的窗口"），
        并且保留更长的历史供 /poll 取。
        """
        mode = str(mode or "").strip().lower()
        if mode not in ("light", "watch"):
            raise BridgeError("config", "mode 只能是 light 或 watch，收到 %r" % mode)
        self.mode = mode
        self.mode_changed_at = time.time()
        self.touch()
        return {"ok": True, "mode": self.mode, "changed_at": self.mode_changed_at}

    # ── 读线程 ──────────────────────────────────────────────────────────────
    def _read_loop(self):
        while self.running:
            try:
                chunk = self.transport.read(256)
            except Exception as exc:
                self.last_error = str(exc)
                self.stats["errors"] += 1
                time.sleep(0.1)
                continue
            now = time.time()
            if chunk:
                with self.lock:
                    self.buffer.extend(chunk)
                    # 判定窗口内到达的字节归同一条命令（异步故障就靠这个收）
                    if now <= self.judge_deadline:
                        self.judge_buf.extend(chunk)
                    cap = 256 * 1024 if self.mode == "watch" else 64 * 1024
                    if len(self.buffer) > cap:      # 防止长时间没人取把内存吃满
                        del self.buffer[:-(cap // 2)]
                self.stats["rx_bytes"] += len(chunk)
                self.logger.append("RX", chunk.decode("utf-8", errors="replace"))
                self.touch()
                # 监视模式：不等"某条命令的窗口"，来什么判什么
                if self.mode == "watch" and now > self.judge_deadline:
                    self._maybe_check(chunk.decode("utf-8", errors="replace"), "watch")
            elif self.judge_buf and now > self.judge_deadline:
                self._flush_judge()
            else:
                time.sleep(0.003)          # 5ms 以内轮询，接近事件驱动，几乎不占 CPU

    def _flush_judge(self):
        with self.lock:
            text = bytes(self.judge_buf).decode("utf-8", errors="replace")
            self.judge_buf.clear()
            self.judge_deadline = 0.0
        if text.strip():
            self._maybe_check(text, "watch")

    # ── 发 ──────────────────────────────────────────────────────────────────
    def send(self, text, mode="text", actor="human", wait_ms=None):
        self.touch()
        current = self.state.read()
        control = current.get("state") or "human"
        if control == STATE_PAUSED:
            raise BridgeError("control_blocked", "控制权是 paused（已暂停），现在不能发命令。",
                              hint=["在网页点[继续AI]，或 human 接管后由人操作"])
        if actor == "ai":
            allowed, _ = self.state.check_ai_allowed()
            if not allowed:
                raise BridgeError("control_blocked",
                                  "控制权是 %s，AI 不能发命令。" % control,
                                  hint=["在网页点[交给AI]", "或调用 serial_control action=ai"])

        newline = _decode_newline(self.scene.cfg.get("newline"))
        if mode == "hex":
            payload = _parse_hex(text)
            if payload is None:
                raise BridgeError("config", "十六进制格式不对，示例：AA 55 01 00")
            frame = payload                      # 原始字节不补换行
        else:
            frame = str(text).encode("utf-8") + newline

        started = time.time()
        with self.lock:
            base = len(self.buffer)
        self.transport.write(frame)
        submit_ms = int((time.time() - started) * 1000)     # 命令真正发出去的耗时
        self.logger.append("TX", text)
        self.stats["sent"] += 1
        with self.lock:
            self.judge_deadline = time.time() + JUDGE_WINDOW_S
            self.judge_buf.clear()

        if wait_ms is None:
            wait_ms = DEFAULT_REPLY_WAIT_MS

        # wait_ms=0 表示"发完就走"：批量下命令时不必每条都等设备回话。
        # 回显仍然会被读线程收进 buffer，之后用 /poll 或 serial_read 取。
        if wait_ms <= 0:
            return {
                "scene": self.scene.name,
                "command": text,
                "mode": mode,
                "port": self.resolved["device"] if self.resolved else "",
                "transport": describe_transport(self.scene.cfg,
                                                (self.resolved or {}).get("device")),
                "response": "",
                "response_hex": "",
                "bytes": 0,
                "submit_ms": submit_ms,
                "elapsed_ms": int((time.time() - started) * 1000),
                "empty": True,
                "submitted": True,
                "via": "daemon",
            }

        # 等首字节 + 短安静；预算内收多少算多少，绝不拖到 1 秒
        deadline = time.time() + wait_ms / 1000.0
        quiet_until = 0.0
        while time.time() < deadline:
            with self.lock:
                size = len(self.buffer) - base
            if size > 0:
                if quiet_until == 0.0:
                    quiet_until = time.time() + SEND_QUIET_MS / 1000.0
                elif time.time() >= quiet_until:
                    break
                with self.lock:
                    if len(self.buffer) - base != size:
                        quiet_until = time.time() + SEND_QUIET_MS / 1000.0
            time.sleep(0.002)

        with self.lock:
            data = bytes(self.buffer[base:])
        if data:
            self.logger.append("RX", data.decode("utf-8", errors="replace"))
        return {
            "scene": self.scene.name,
            "command": text,
            "mode": mode,
            "port": self.resolved["device"] if self.resolved else "",
            "transport": describe_transport(self.scene.cfg,
                                            (self.resolved or {}).get("device")),
            "response": data.decode("utf-8", errors="replace"),
            "response_hex": " ".join("%02X" % b for b in data),
            "bytes": len(data),
            "submit_ms": submit_ms,
            "elapsed_ms": int((time.time() - started) * 1000),
            "empty": len(data) == 0,
            "via": "daemon",
        }

    # ── 判定 ────────────────────────────────────────────────────────────────
    def _maybe_check(self, text, source):
        if self._checks_stopped:
            return
        watch = self.state.read().get("watch") or {}
        if not watch.get("check_enabled"):
            return
        want = str(watch.get("check_source") or "watch_only")
        if want == "watch_only" and source != "watch":
            return
        if want == "manual_only" and source != "manual":
            return
        cfg = dict(self.scene.watch_cfg())
        cfg.update({k: watch[k] for k in watch if k in
                    ("rules", "allowed_auto_commands", "rule_cooldown_s",
                     "rule_timeout_ms", "max_consecutive_failures")})
        if self._rule_worker is None:
            self._rule_worker = RuleWorker(package_root=os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
        results, error = self._rule_worker.evaluate(
            text, cfg, timeout_ms=int(cfg.get("rule_worker_timeout_ms", 2000)))
        self.stats["checks"] += 1
        if error:
            self._check_failures += 1
            if self._check_failures >= int(cfg.get("max_consecutive_failures", 3)):
                self._checks_stopped = True
            return
        self._check_failures = 0
        for item in results or []:
            if not item.get("matched"):
                continue
            action = item.get("action")
            sent = False
            if action:
                try:
                    self.transport.write(str(action).encode("utf-8") + _decode_newline(
                        self.scene.cfg.get("newline")))
                    self.logger.append("TX", action)
                    sent = True
                except Exception:
                    sent = False
            self.state.append_alert({
                "rule": item.get("rule"), "reason": item.get("reason"),
                "response": text[:200], "action": action or "",
                "sent": sent, "source": source, "scene": self.scene.name,
            })

    # ── 空闲退出 ────────────────────────────────────────────────────────────
    def _idle_loop(self):
        while self.running:
            time.sleep(2.0)
            if not self.running:
                return
            if time.time() - self.last_activity >= self.idle_s:
                self.shutdown("空闲超时，自动释放连接")
                return

    def shutdown(self, reason=""):
        self.logger.append("SYS", "管家退出：%s" % (reason or "收到 shutdown"))
        self.stop()
        clear_instance(self.paths, self.scene.name)
        # 光关连接不够：HTTP 线程还在跑，别人会以为管家活着（然后报警"transport 为 None"）。
        server = getattr(self, "server", None)
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    # ── 查询 ────────────────────────────────────────────────────────────────
    def status(self):
        current = self.state.read()
        return {
            "ok": True,
            "running": self.running,
            "pid": os.getpid(),
            "scene": self.scene.name,
            "port": (self.resolved or {}).get("device") or "",
            "baud": int(self.scene.cfg.get("baud") or 0),
            "transport": describe_transport(self.scene.cfg,
                                            (self.resolved or {}).get("device")),
            "control": current.get("state") or "human",
            "mode": self.mode,
            "uptime_s": int(time.time() - self.started_at),
            "idle_s": int(time.time() - self.last_activity),
            "idle_timeout_s": self.idle_s,
            "stats": dict(self.stats),
            "last_error": self.last_error,
        }

    def poll(self, cursor=0, limit=65536):
        with self.lock:
            total = len(self.buffer)
            # cursor < 0 表示"只要从现在开始的新数据"（网页刚接上来时别倒历史）
            cursor = total if int(cursor) < 0 else max(0, min(int(cursor), total))
            data = bytes(self.buffer[cursor:cursor + limit])
            next_cursor = cursor + len(data)
        return {
            "ok": True,
            "cursor": next_cursor,
            "total": total,
            "rx_text": data.decode("utf-8", errors="replace"),
            "rx_hex": " ".join("%02X" % b for b in data),
            "bytes": len(data),
        }


# ── HTTP ───────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    daemon = None

    def log_message(self, fmt, *args):      # 不往 stderr 刷访问日志
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/status":
                return self._json(self.daemon.status())
            if path == "/poll":
                cursor = 0
                if "?" in self.path:
                    from urllib.parse import parse_qs
                    query = parse_qs(self.path.split("?", 1)[1])
                    cursor = int((query.get("cursor") or ["0"])[0] or 0)
                return self._json(self.daemon.poll(cursor))
            if path in ("/health", "/"):
                return self._json({"ok": True, "pid": os.getpid()})
            return self._json({"ok": False, "error": {"kind": "not_found",
                                                     "message": "未知路径 %s" % path}}, 404)
        except BridgeError as exc:
            return self._json({"ok": False, "error": exc.to_dict()}, 400)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        try:
            if path == "/send":
                result = self.daemon.send(
                    str(body.get("text") or ""),
                    str(body.get("mode") or "text"),
                    str(body.get("actor") or "human"),
                    wait_ms=body.get("wait_ms"))
                return self._json({"ok": True, "data": result})
            if path == "/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.daemon.shutdown,
                                 kwargs={"reason": "HTTP shutdown"}, daemon=True).start()
                return None
            if path == "/mode":
                return self._json(self.daemon.set_mode(str(body.get("mode") or "")))
            return self._json({"ok": False, "error": {"kind": "not_found",
                                                     "message": "未知路径 %s" % path}}, 404)
        except BridgeError as exc:
            return self._json({"ok": False, "error": exc.to_dict()}, 400)
        except Exception as exc:      # 兜底：任何异常都要变成 JSON，不能让连接裸断
            return self._json({"ok": False, "error": {
                "kind": "internal", "message": "管家内部错误：%s" % exc}}, 500)


def serve(paths, scene_name, port=DEFAULT_PORT, idle_s=DEFAULT_IDLE_S):
    daemon = Daemon(paths, scene_name, port, idle_s)
    daemon.start()
    _Handler.daemon = daemon
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), _Handler)
    daemon.server = server          # 退出时要连 HTTP 一起停，否则进程赖着不走
    write_instance(paths, scene_name, port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        clear_instance(paths, scene_name)


# ── instance 文件 / 客户端 ──────────────────────────────────────────────────
def instance_path(paths, scene_name):
    return os.path.join(paths["runtime_root"], "state", "%s.daemon.json" % _safe_name(scene_name))


def write_instance(paths, scene_name, port):
    path = instance_path(paths, scene_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"pid": os.getpid(), "port": int(port),
               "url": "http://127.0.0.1:%d" % int(port),
               "scene": scene_name, "started": time.time()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return payload
        except OSError:
            time.sleep(0.02)
    return payload


def read_instance(paths, scene_name):
    try:
        with open(instance_path(paths, scene_name), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def clear_instance(paths, scene_name):
    try:
        os.remove(instance_path(paths, scene_name))
    except OSError:
        pass


def request(url, path, payload=None, timeout=1.0):
    """向管家发一个请求。返回 dict；连不上返回 None。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


def running(paths, scene_name, timeout=0.6):
    """管家在跑就返回它的 status，否则 None。

    必须同时看 running：进程刚 shutdown 时 HTTP 可能还能应答一下，
    这时候 transport 已经是 None，再往里发命令会报"NoneType 没有 write"。
    """
    info = read_instance(paths, scene_name)
    if not info:
        return None
    status = request(info.get("url") or "", "/status", timeout=timeout)
    if status and status.get("ok") and status.get("running"):
        status["url"] = info.get("url")
        return status
    return None


def ensure_started(paths, scene_name, port=None, idle_s=DEFAULT_IDLE_S, wait_s=6.0):
    """幂等启动：已经在跑就直接返回它，不重复起进程。"""
    current = running(paths, scene_name)
    if current:
        return current
    port = int(port or DEFAULT_PORT)
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root] + [p for p in [env.get("PYTHONPATH")] if p])
    subprocess.Popen(
        [sys.executable, "-m", "serial_bridge.daemon",
         "--scene", scene_name, "--port", str(port), "--idle", str(idle_s)],
        cwd=package_root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.15)
        current = running(paths, scene_name)
        if current:
            return current
    raise BridgeError("no_device", "管家没起来。",
                      hint=["确认串口没被别的程序占用", "手工跑一次看错误：python -m serial_bridge.daemon --scene %s" % scene_name])


def stop(paths, scene_name):
    info = read_instance(paths, scene_name)
    if not info:
        return {"ok": True, "stopped": False, "note": "没有在跑的管家"}
    request(info.get("url") or "", "/shutdown", {}, timeout=1.5)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if running(paths, scene_name, timeout=0.3) is None:
            clear_instance(paths, scene_name)
            return {"ok": True, "stopped": True}
        time.sleep(0.1)
    clear_instance(paths, scene_name)
    return {"ok": True, "stopped": False, "note": "已发退出请求，但进程未在 5 秒内消失"}


# ── 小工具 ─────────────────────────────────────────────────────────────────
def _decode_newline(value, default="\\r\\n"):
    from .io import decode_newline
    return decode_newline(value, default)


def _parse_hex(text):
    raw = str(text or "").replace(",", " ").replace("0x", " ").replace("0X", " ")
    parts = raw.split()
    if not parts:
        return b""
    try:
        return bytes(int(part, 16) for part in parts)
    except ValueError:
        return None


def main(argv=None):
    from .cli import _paths
    parser = argparse.ArgumentParser(description="鲸歌管家（常驻持有连接）")
    parser.add_argument("--scene", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle", type=int, default=DEFAULT_IDLE_S)
    args = parser.parse_args(argv)
    paths = _paths()
    serve(paths, args.scene, args.port, args.idle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
