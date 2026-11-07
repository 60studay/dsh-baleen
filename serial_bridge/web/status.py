"""网页状态查询（status / poll / alerts）

网页每 700ms 轮询一次，所有只读展示都走这里。

这里用的是 mixin：方法体拿的还是 self，所以 self.state / self.loop /
self.ports 和拆之前完全一样，行为零变化。
"""

import os
import time

from ..channel import run_evidence
from ..config import load_scene
from ..errors import BridgeError
from ..ports import BAUD_CHOICES, guess_port, resolve_port, scan_ports
from ..rules import CHECK_SOURCES, RULE_TYPES, merge_watch, normalize_check_source
from ..transport import describe_transport as transport_describe
from .. import io as serial_io


class StatusMixin(object):

    # ── 状态 ────────────────────────────────────────────────────────────────
    def effective_watch(self):
        """网页看到、判定用的 watch 配置：**配置打底、状态覆盖，空值不覆盖**。

        以前 status() 只读状态文件，而状态文件里根本不存 rules
        （rules 在场景 config.json）→ 三方打架：
            config.json 5 条 / GET /rules 5 条 / GET /status 0 条
        用户点完"添加规则"看到列表还是空的，就会以为没保存、反复添加同一条。
        """
        try:
            self.scene.reload_if_changed()
        except Exception:
            pass
        return merge_watch(self.scene.watch_cfg(), self.state.read().get("watch") or {})


    def status(self):
        current = self.state.read()
        watch_cfg = self.effective_watch()
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
            "hold_released_reason": self.ports.hold_released_reason,
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
        data["port_owner"] = self.ports.owner()
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


    def clear_alerts(self):
        return {"ok": True, "cleared": self.state.clear_alerts()}
