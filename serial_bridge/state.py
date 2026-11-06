# -*- coding: utf-8 -*-
"""跨进程共享状态：控制权状态机 + 告警记录（v2 交接文档 3.5 / 3.6）。

为什么在文件里而不是内存里：网页进程和 DSH 插件进程是两个独立进程，
状态必须两边都看得见、都改得动。用 JSON 文件 + 原子替换 + 文件锁实现。

状态模型（3.5）：
    human  --交给AI-->  ai  --暂停AI-->  paused  --继续AI-->  ai
      ^                 |                 |
      +----人类接管-----+----人类接管-----+

安全规则：
    1. 人类命令永远优先：人类发命令时自动把状态拉回 human
    2. AI 发送前必须检查 state == 'ai'
    3. 状态持久化（就是这个文件）
    4. 状态变化可见（网页轮询 /status 读这个文件）
"""

import json
import os
import threading
import time

STATE_HUMAN = "human"
STATE_AI = "ai"
STATE_PAUSED = "paused"
VALID_STATES = (STATE_HUMAN, STATE_AI, STATE_PAUSED)

# 允许的状态迁移：from -> 可以进入的 to 集合
TRANSITIONS = {
    STATE_HUMAN: {STATE_AI},
    STATE_AI: {STATE_HUMAN, STATE_PAUSED},
    STATE_PAUSED: {STATE_AI, STATE_HUMAN},
}

DEFAULT_STATE = {
    "state": STATE_HUMAN,
    "since": 0.0,
    "changed_by": "init",
    "port": "",
    "baud": 0,
    "scene": "",
    "watch": {},
    "note": "",
}


def _now():
    return time.time()


# 控制权文件的"活跃窗口"：网页进程每 2 秒心跳一次；
# 超过这个秒数没心跳，说明没人正在盯这一局人机协同，门禁按"无人看管"处理。
PRESENCE_TTL_S = 30


class SharedState(object):
    """一个场景一份状态文件：runtime/state/<场景名>.json。

    文件锁用"独占创建 .lock 文件"实现，跨进程可用，不需要额外依赖。
    """

    def __init__(self, runtime_dir, scene):
        self.scene = scene
        self.dir = os.path.join(runtime_dir, "state")
        self.path = os.path.join(self.dir, "%s.json" % _safe_name(scene))
        self.lock_path = self.path + ".lock"
        self.alerts_path = os.path.join(runtime_dir, "alerts.jsonl")

    # ── 基础读写 ────────────────────────────────────────────────────────────
    def _ensure_dir(self):
        if not os.path.isdir(self.dir):
            os.makedirs(self.dir, exist_ok=True)

    def _acquire(self, timeout=1.0):
        self._ensure_dir()
        deadline = _now() + timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                return True
            except FileExistsError:
                # 陈旧锁：持有者进程没了就清掉
                if _now() > deadline:
                    self._break_stale_lock()
                    if _now() > deadline + 0.5:
                        return False
                time.sleep(0.01)
            except OSError:
                return False

    def _release(self):
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def _break_stale_lock(self):
        try:
            age = _now() - os.path.getmtime(self.lock_path)
            if age > 5:
                os.remove(self.lock_path)
        except OSError:
            pass

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("bad state")
        except (OSError, ValueError):
            data = {}
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        if merged.get("state") not in VALID_STATES:
            merged["state"] = STATE_HUMAN
        return merged

    def _write(self, data):
        self._ensure_dir()
        tmp = self.path + ".tmp%d.%d" % (os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        # Windows 上 os.replace 会失败，如果目标文件此刻正被别的读者打开
        # （网页看门狗每秒读一次，自测里读得更密）。重试几次比让写操作失败安全。
        last_error = None
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.01 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                time.sleep(0.01)
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise last_error if last_error else RuntimeError("state write failed")

    # ── 状态机 ──────────────────────────────────────────────────────────────
    def set_state(self, new_state, changed_by="unknown", note=""):
        if new_state not in VALID_STATES:
            raise ValueError("unknown state: %s" % new_state)
        if not self._acquire():
            raise RuntimeError("state file is locked")
        try:
            current = self.read()
            if new_state == current.get("state"):
                return current
            allowed = TRANSITIONS.get(current.get("state"), set())
            if new_state not in allowed:
                # 迁移非法：按文档的按钮表，非法按钮本来就该是禁用的。
                # 这里不抛错，而是明确拒绝并回原状态，前端据此高亮。
                raise ValueError("illegal transition: %s -> %s"
                                 % (current.get("state"), new_state))
            current["state"] = new_state
            current["since"] = _now()
            current["changed_by"] = changed_by
            current["note"] = note
            self._write(current)
            return current
        finally:
            self._release()

    def force_state(self, new_state, changed_by="unknown", note=""):
        """绕过迁移表（用于"人类命令永远优先"这类安全规则）。"""
        if new_state not in VALID_STATES:
            raise ValueError("unknown state: %s" % new_state)
        if not self._acquire():
            raise RuntimeError("state file is locked")
        try:
            current = self.read()
            current["state"] = new_state
            current["since"] = _now()
            current["changed_by"] = changed_by
            current["note"] = note
            self._write(current)
            return current
        finally:
            self._release()

    def update(self, **fields):
        """更新端口/波特率/监视配置等附带信息，不动 state 本身。"""
        if not self._acquire():
            raise RuntimeError("state file is locked")
        try:
            current = self.read()
            for key, value in fields.items():
                current[key] = value
            self._write(current)
            return current
        finally:
            self._release()

    def check_ai_allowed(self):
        """AI 发送前的门禁。返回 (allowed, state_dict)。

        只有 state == 'ai' 才放行；human / paused 一律拒绝。

        （早期版本为了照顾"插件装好但没开网页"的场景做过两段式宽松门禁：
        没人在看网页时直接放行。实测表明这会让控制权形同虚设——用户切到
        human 后 AI 照样能发，所以取消了。要单人使用就直接把控制权设为 ai，
        页面和 serial_control 都能改。）
        """
        current = self.read()
        return current.get("state") == STATE_AI, current

    def ai_block_reason(self):
        """AI 不能发时，给出可操作的下一步（给工具层直接用）。"""
        current = self.read()
        state = current.get("state")
        if state == STATE_AI:
            return None
        if state == STATE_PAUSED:
            return ("AI 当前被暂停（paused）。"
                    "请在网页点[继续AI]，或用 serial_control action=resume 恢复。")
        return ("控制权在人类手上（human）。"
                "请在网页点[交给AI]，或用 serial_control action=ai 交给 AI。")

    def presence_alive(self):
        """网页/监视进程最近是否在心跳（说明有人正在看着这一局）。"""
        beat = self.read().get("presence") or 0
        try:
            beat = float(beat)
        except (TypeError, ValueError):
            return False
        return (_now() - beat) < PRESENCE_TTL_S

    def heartbeat(self, source="web"):
        """网页进程周期性调用，证明"有人在场、会看告警"。"""
        self.update(presence=_now(), presence_by=source)

    def note_human_command(self):
        """人类命令永远优先：把状态拉回 human（如果当前是 ai）。"""
        current = self.read()
        if current.get("state") == STATE_AI:
            return self.force_state(STATE_HUMAN, "human-command",
                                    "人类发了命令，AI 托管已自动让位")
        return current

    # ── 告警 ────────────────────────────────────────────────────────────────
    def append_alert(self, alert):
        self._ensure_dir()
        record = dict(alert)
        record.setdefault("ts", _now())
        record.setdefault("scene", self.scene)
        record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(self.alerts_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read_alerts(self, limit=20):
        try:
            with open(self.alerts_path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(1, int(limit)):]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _safe_name(name):
    keep = []
    for ch in str(name):
        keep.append(ch if (ch.isalnum() or ch in "-_") else "_")
    return "".join(keep) or "scene"
