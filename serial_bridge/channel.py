# -*- coding: utf-8 -*-
"""AI ⇄ AI 通道：一块给两个 AI 共用的留言板。

**不是聊天室，是"带证据的对话"。**

为什么长这样（设计依据见 `_需求文档/功能规划清单.md` 3.7.1）：

    两个 AI 都会错，所以权威不在任何一方手里，
    而在"别人能不能照着复现"里（自检、真机实测、编译器、git）。

所以每条消息强制分「声明」和「证据」两半：

    claim         你要对方相信的结论
    evidence      别人能照着复现的东西（命令 + 输出 + 退出码）
    not_verified  你自己**没验过**的路径   ← 最重要的字段
    trust         默认 none：通道只传，不背书

`not_verified` 为什么最重要：出过事。一方写完"已完成"，
把"省略 scene 的路径"漏在没验的名单外，另一方就得多花一轮才发现必崩。

存储：一个 JSONL 文件（一行一条，追加写）。
  · 两个 AI 可能跑在不同进程/不同模型里，文件是最低公共接口
  · 人可以直接 tail 看，出问题不需要工具
  · 追加写 + 一次写一行，跨进程基本正确（单条约 < 8KB）
"""

import io
import json
import os
import shlex
import subprocess
import sys
import threading
import time

from .errors import BridgeError

# 消息类型。"note" 是纯对话，其余都是"要对方信点什么"，必须带证据或未验证清单。
MSG_TYPES = ("note", "claim", "verify_req", "verify_res", "reject")
# 这几种必须有证据或未验证清单——不允许只发结论
NEED_SUPPORT = ("claim", "verify_req", "verify_res", "reject")
MAX_TEXT = 8000          # 单字段上限，防止有人把整个日志贴进来
MAX_MESSAGES = 2000      # 超过就裁剪，保留最近 KEEP_MESSAGES 条
KEEP_MESSAGES = 1200
MAX_VERIFY_OUTPUT = 200 * 1024
# 一键复验只认这一种形态：**解释器 + bridge.py**。
# 网页能执行命令 = 攻击面，白名单必须是死板的，宁可不够用也不能放宽。
VERIFY_EXECUTABLES = ("python", "python3", "py", "python.exe", "py.exe")
VERIFY_SCRIPT = "bridge.py"


def run_evidence(cmd, timeout=30):
    """执行证据里的命令，把 stdout + 退出码拿回来——**一键复验**。

    这是全项目唯一"网页能让服务器跑命令"的入口，所以边界必须死守：

      · 只允许 `<解释器> bridge.py ...` 这一种形态，别的一律拒绝
      · 不用 shell：shlex 拆成参数列表直接 exec，没有管道/重定向/命令替换
      · 有超时、有输出上限

    为了在本机真跑得动，第一个词（python/py）会被换成**当前**解释器——
    因为这台机器上 `py` 不在 PATH，而网页进程知道自己用的是哪个 python。
    """
    raw = str(cmd or "").strip()
    if not raw:
        raise BridgeError("config", "这条消息没有 evidence.cmd，没法复验。")

    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise BridgeError("config", "evidence.cmd 解析失败：%s" % exc)
    if not argv:
        raise BridgeError("config", "evidence.cmd 是空的。")

    first = os.path.basename(argv[0]).lower()
    if first not in VERIFY_EXECUTABLES:
        raise BridgeError(
            "config",
            "一键复验只允许跑 `python bridge.py …`，拒绝执行：%s" % argv[0],
            hint=["这是网页进程执行命令的安全边界，不是可配置项",
                  "要跑别的命令请在本地终端跑，然后把输出贴到 evidence"],
        )
    if len(argv) < 2 or os.path.basename(argv[1]).lower() != VERIFY_SCRIPT:
        raise BridgeError(
            "config",
            "一键复验只允许跑 `python bridge.py …`，拒绝执行：%s" % " ".join(argv[:2]),
            hint=["第二个参数必须是 bridge.py"],
        )

    # 换成当前解释器，并把 bridge.py 换成绝对路径（不依赖工作目录）
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    argv = [sys.executable, os.path.join(root, VERIFY_SCRIPT)] + argv[2:]

    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=root, capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise BridgeError("no_response", "复验超时（%d 秒）：%s" % (timeout, raw),
                          hint=["命令可能卡在串口上", "可以考虑在本地终端跑一遍"])
    except OSError as exc:
        raise BridgeError("internal", "复验跑不起来：%s" % exc)

    stdout = (proc.stdout or "")[:MAX_VERIFY_OUTPUT]
    stderr = (proc.stderr or "")[:MAX_VERIFY_OUTPUT]
    return {
        "cmd": raw,
        "argv": argv,
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_ms": int((time.time() - started) * 1000),
        "ok": proc.returncode == 0,
    }


class Channel(object):
    """一个追加写的 JSONL 留言板。线程安全；跨进程靠"一行一次写"。"""

    def __init__(self, root, name="board"):
        self.name = str(name or "board")
        self.dir = os.path.join(root, "channel")
        self.path = os.path.join(self.dir, "%s.jsonl" % self.name)
        self._lock = threading.Lock()

    # ── 读 ────────────────────────────────────────────────────────────────
    def _load(self):
        """全量读一遍。坏行跳过（别让一行手改坏的文件把整个通道打死）。"""
        try:
            with io.open(self.path, encoding="utf-8", errors="replace") as handle:
                raw = handle.read().splitlines()
        except OSError:
            return []
        out = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("seq"):
                out.append(item)
        return out

    def list(self, since=0, limit=100):
        """取 seq > since 的消息，最多 limit 条（按 seq 升序）。"""
        try:
            since = int(since or 0)
        except (TypeError, ValueError):
            since = 0
        try:
            limit = max(1, min(int(limit or 100), 500))
        except (TypeError, ValueError):
            limit = 100
        items = self._load()
        fresh = [m for m in items if int(m.get("seq") or 0) > since]
        last_seq = items[-1]["seq"] if items else 0
        return {
            "channel": self.name,
            "messages": fresh[:limit],
            "last_seq": last_seq,
            "count": len(items),
        }

    def get(self, seq):
        for item in self._load():
            if int(item.get("seq") or 0) == int(seq):
                return item
        raise BridgeError("config", "通道里没有 seq=%s 这条消息。" % seq,
                          hint=["先刷新一下通道列表"])

    # ── 写 ────────────────────────────────────────────────────────────────
    def append(self, body):
        """追加一条消息。返回存下来的完整消息（带 seq/ts）。"""
        msg = _normalize(body)
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            items = self._load()
            msg["seq"] = (items[-1]["seq"] if items else 0) + 1
            msg["ts"] = time.time()
            try:
                with io.open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(msg, ensure_ascii=False) + "\n")
            except OSError as exc:
                raise BridgeError("internal", "通道写入失败：%s" % exc)
            if len(items) + 1 > MAX_MESSAGES:
                self._trim(items + [msg])
        return msg

    def _trim(self, items):
        """裁剪老消息。保留尾部 KEEP_MESSAGES 条，seq 不重排（重排会让两边对不上）。"""
        keep = items[-KEEP_MESSAGES:]
        tmp = self.path + ".tmp"
        try:
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                for item in keep:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass


def _clip(value, limit=MAX_TEXT):
    text = str(value or "").strip()
    return text[:limit]


def _normalize(body):
    """把请求体洗成一条规范消息；不合规就报可操作的错。"""
    body = body or {}
    sender = _clip(body.get("from") or body.get("sender"), 60)
    if not sender:
        raise BridgeError("config", "通道消息必须写明 from（谁发的）。",
                          hint=["例如 from=ai-a / ai-b / human"])

    kind = str(body.get("type") or "note").strip()
    if kind not in MSG_TYPES:
        raise BridgeError("config", "type 只能是 %s。" % " / ".join(MSG_TYPES),
                          hint=["note=纯对话；claim=声明；verify_req=请对方复验；"
                                "verify_res=复验结果；reject=驳回"])

    claim = _clip(body.get("claim") or body.get("text"))
    if not claim:
        raise BridgeError("config", "消息正文是空的。")

    # 证据：{cmd, stdout, exit}
    raw_ev = body.get("evidence")
    evidence = {}
    if isinstance(raw_ev, dict):
        for key in ("cmd", "stdout"):
            value = _clip(raw_ev.get(key), 4000)
            if value:
                evidence[key] = value
        if raw_ev.get("exit") is not None:
            try:
                evidence["exit"] = int(raw_ev.get("exit"))
            except (TypeError, ValueError):
                pass
    not_verified = body.get("not_verified")
    if isinstance(not_verified, str):
        not_verified = [item.strip() for item in not_verified.split(",") if item.strip()]
    if not isinstance(not_verified, (list, tuple)):
        not_verified = []
    not_verified = [_clip(item, 300) for item in not_verified if _clip(item, 300)]

    # 这条规矩是本模块的重点：**不允许只发结论**
    if kind in NEED_SUPPORT and not evidence and not not_verified:
        raise BridgeError(
            "config",
            "「%s」这类消息必须带证据或未验证清单，不能只发结论。" % kind,
            hint=[
                "证据填 command + 输出 + 退出码（别人要能照着复现）",
                "实在没证据就写 not_verified：哪些路径你没验",
                "纯聊天请用 type=note",
            ],
        )
    if kind == "verify_res" and not evidence:
        raise BridgeError(
            "config",
            "复验结果必须附你自己跑出来的输出，不能只写「同意」。",
            hint=["把命令、stdout、退出码填进 evidence"],
        )

    return {
        "from": sender,
        "type": kind,
        "claim": claim,
        "evidence": evidence,
        "not_verified": not_verified,
        # 通道只传不背书：trust 永远是 none，除非将来有人实现真正的自动复验
        "trust": "none",
        "reply_to": int(body.get("reply_to") or 0) or None,
    }
