# -*- coding: utf-8 -*-
"""监视模式下常驻持口的读循环（从 supervisor.py 抽出来的）。

职责很窄：拿着一个已打开的连接，持续读、广播给网页、按需触发判定。
它不管 HTTP，也不管"该不该持口"（那是 port_manager 的事）。
"""

import threading
import time

from . import io as serial_io
from .ports import resolve_port
from .rule_checker import RuleChecker


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
        # 判定统一交给 RuleChecker（与管家共用同一份逻辑，不再各写一份）
        self.checker = RuleChecker(scene, state, logger, send_action=self.send_raw)

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
        if self.checker._worker is not None:
            self.checker._worker.close()
            self.checker._worker = None
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


    def _run_checks(self, text, source):
        """判定入口：委托给共用的 RuleChecker（开关/熔断/告警都在那边）。"""
        self.checker.run(text, source, self.no_response_streak)
