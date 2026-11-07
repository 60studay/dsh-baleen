"""AI 通道的三个入口（已冻结，只搬运）

判定逻辑在 channel.py，这里只做转发。

这里用的是 mixin：方法体拿的还是 self，所以 self.state / self.loop /
self.ports 和拆之前完全一样，行为零变化。
"""

import os
import time

from ..channel import run_evidence
from ..config import load_scene
from ..errors import BridgeError
from ..ports import guess_port, resolve_port, scan_ports
from ..rules import CHECK_SOURCES, RULE_TYPES, merge_watch, normalize_check_source


class ChannelMixin(object):

    # ── AI ⇄ AI 通道 ────────────────────────────────────────────────────────
    # 网页只做"窗口 + 发送口"，判定（有没有证据、能不能复验）全在 channel.py。
    def channel_list(self, since=0, limit=100):
        return dict({"ok": True}, **self.chan.list(since, limit))


    def channel_send(self, body):
        return {"ok": True, "message": self.chan.append(body)}


    def channel_verify(self, seq, actor="human"):
        """把某条消息的证据重跑一遍，并把**复验结果本身**作为一条新消息贴回通道。

        这一步是整个通道存在的理由：
        如果复验比信任更麻烦，人就会选择信任，那"声明/证据分离"就白设计了。
        """
        msg = self.chan.get(seq)
        result = run_evidence((msg.get("evidence") or {}).get("cmd") or "")
        stored = self.chan.append({
            "from": actor or "human",
            "type": "verify_res",
            "claim": "复验 #%s：%s" % (msg.get("seq"), "通过" if result["ok"] else "失败"),
            "evidence": {
                "cmd": result["cmd"],
                "stdout": result["stdout"] or result["stderr"],
                "exit": result["exit"],
            },
            "reply_to": msg.get("seq"),
        })
        return {"ok": True, "result": result, "message": stored}
