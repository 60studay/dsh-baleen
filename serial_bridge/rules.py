# -*- coding: utf-8 -*-
"""判据规则引擎（v2 交接文档 3.6）。

**这个文件里不许出现任何具体设备的命令字或故障字。**
规则的全部语义来自场景配置：
    when.type ∈ {contains, regex, no_response, equals}   ← 只有这四种
    when.value / when.pattern / when.consecutive
    then.send / then.max_retries

本模块只做**纯判定**：给定文本，回答"该不该触发、触发哪条"。
异常在规则级别被吞掉（非法正则、字段缺失等只记 error，不影响其它规则）。

⚠ 关于超时：这里**不做**超时。原因见 runner.py 的说明——CPython 的 re
在匹配期间持有 GIL，线程 join(timeout) 拿不回来，真正的"超时就跳过"必须靠
进程隔离实现。所以常驻监视进程用 runner.RuleRunner 包一层，本模块保持纯粹，
也便于单测。

安全限制：
- 自动命令必须命中 allowed_auto_commands，白名单为空则禁止一切自动命令
- 同一规则冷却期内不重复触发（限流）
- 连续失败达阈值 → 熔断，停止判定
"""

import re
import threading
import time

# contains/equals/regex 判的是**解码后的文本**；
# hex/hex_regex 判的是**原始字节**——串口最需要判的恰恰是二进制（帧头、CRC、故障码）。
# 没有这两个类型时，"正则 \\xff{3,}"永远不可能命中：0xFF 不是合法 UTF-8，
# 解码后变成 U+FFFD（替换符），跟 chr(255) 是两个字符。
RULE_TYPES = ("contains", "regex", "no_response", "equals", "hex", "hex_regex")

DEFAULT_COOLDOWN_S = 5.0

# 判定来源开关。注意 "all" 是历史别名（手写 config 里出现过），统一归到 "both"。
CHECK_SOURCES = ("watch_only", "manual_only", "both")


def normalize_check_source(value):
    text = str(value or "").strip().lower()
    if text == "all":
        return "both"
    return text

# 合并 watch 配置时，这些"空值"不算用户设置过，不许覆盖下层的值。
_EMPTY_VALUES = (None, "", [], {}, ())


def merge_watch(*layers):
    """按顺序合并 watch 配置，**后层的空值不覆盖前层**。

    为什么不能直接 `{**a, **b}`：
      状态文件里会存 `check_source: ""`（前端没传这个字段就被写成了空串），
      于是合法值 "all" 被空串盖掉，`RuleChecker` 里
      `want = watch.get("check_source") or "watch_only"` 回落成 watch_only，
      **判定被静默关掉，界面上还看不出来**。这个坑真实发生过。

    注意 False / 0 是**合法值**，不能当空值跳过（send_enabled=false 就是"关掉"）。
    """
    out = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if value in _EMPTY_VALUES:
                continue
            out[key] = value
    return out


class RuleResult(object):
    __slots__ = ("rule", "matched", "reason", "action", "error", "skipped")

    def __init__(self, rule, matched=False, reason="", action=None, error=None, skipped=None):
        self.rule = rule
        self.matched = matched
        self.reason = reason
        self.action = action
        self.error = error
        self.skipped = skipped

    def to_dict(self):
        data = {"rule": self.rule, "matched": self.matched, "reason": self.reason}
        if self.action:
            data["action"] = self.action
        if self.error:
            data["error"] = self.error
        if self.skipped:
            data["skipped"] = self.skipped
        return data


class RuleEngine(object):
    def __init__(self, watch_cfg, logger=None, cooldown_s=DEFAULT_COOLDOWN_S, clock=None):
        cfg = watch_cfg or {}
        self.rules = cfg.get("rules") or []
        self.cooldown_s = float(cfg.get("rule_cooldown_s", cooldown_s) or cooldown_s)
        self.allowed = _normalize_allowlist(cfg.get("allowed_auto_commands"))
        self.max_consecutive_failures = int(cfg.get("max_consecutive_failures", 3) or 3)
        self.log = logger or (lambda *a, **k: None)
        self._clock = clock or time.time
        self._last_fired = {}
        self._consecutive_failures = 0
        self._stopped = False
        self._lock = threading.Lock()

    @property
    def stopped(self):
        return self._stopped

    @property
    def consecutive_failures(self):
        return self._consecutive_failures

    def allowlist_empty(self):
        return not self.allowed

    def evaluate(self, text, no_response_streak=0, source="watch", raw_bytes=None):
        """对所有规则跑一遍。本函数永不抛异常。

        raw_bytes 是**原始字节**（hex / hex_regex 判据要用）；不传就按 UTF-8 编码 text。
        """
        results = []
        if self._stopped:
            return results
        for rule in self.rules:
            results.append(self._evaluate_one(rule, text, no_response_streak, source,
                                              raw_bytes))
        return results

    def _evaluate_one(self, rule, text, no_response_streak, source, raw_bytes=None):
        name = str((rule or {}).get("name") or "未命名规则")
        when = (rule or {}).get("when") or {}
        rule_type = str(when.get("type") or "").strip()

        if rule_type not in RULE_TYPES:
            return RuleResult(name, error="unknown rule type: %r" % rule_type)

        last = self._last_fired.get(name)
        now = self._clock()
        if last is not None and (now - last) < self.cooldown_s:
            return RuleResult(name, skipped="cooldown %.1fs" % (self.cooldown_s - (now - last)))

        try:
            matched, reason = self._test(rule_type, when, text, no_response_streak,
                                         raw_bytes)
        except Exception as exc:  # 非法正则、类型不对……一律只记 error
            self.log("rule %r error: %s" % (name, exc))
            return RuleResult(name, error=str(exc))

        if not matched:
            return RuleResult(name, matched=False, reason=reason)

        action = (rule or {}).get("then") or {}
        send = action.get("send")
        if send:
            if not self.allowed:
                return RuleResult(name, matched=True, reason=reason,
                                  error="白名单为空，禁止自动发送（配置 allowed_auto_commands）")
            if not self._is_allowed(send):
                return RuleResult(name, matched=True, reason=reason,
                                  error="自动命令 %r 不在白名单内" % send)

        self._last_fired[name] = now
        return RuleResult(name, matched=True, reason=reason, action=action or None)

    def _test(self, rule_type, when, text, no_response_streak, raw_bytes=None):
        raw = "" if text is None else str(text)
        if rule_type == "contains":
            needle = str(when.get("value") or "")
            if not needle:
                raise ValueError("contains 需要 value")
            return needle in raw, "回显包含 %r" % needle
        if rule_type == "equals":
            needle = str(when.get("value") or "")
            if not needle:
                raise ValueError("equals 需要 value")
            return raw.strip() == needle.strip(), "回显等于 %r" % needle
        if rule_type == "regex":
            pattern = str(when.get("pattern") or when.get("value") or "")
            if not pattern:
                raise ValueError("regex 需要 pattern")
            if re.search(pattern, raw):  # re.error 会被上层收成 error
                condition = when.get("condition")
                if condition:
                    return True, "正则 %r 命中（%s）" % (pattern, condition)
                return True, "正则 %r 命中" % pattern
            return False, "正则 %r 未命中" % pattern
        if rule_type == "no_response":
            need = int(when.get("consecutive") or 3)
            return no_response_streak >= need, "连续 %d 次无回显（阈值 %d）" % (
                no_response_streak, need)
        if rule_type in ("hex", "hex_regex"):
            data = _as_bytes(raw_bytes, raw)
            if rule_type == "hex":
                needle = _parse_hex(when.get("value") or when.get("bytes"))
                if not needle:
                    raise ValueError("hex 需要 value，例如 \"AA 55 FF\"")
                hit = needle in data
                return hit, ("原始字节包含 %s" % _hex_text(needle)) if hit else (
                    "原始字节不含 %s（共 %d 字节）" % (_hex_text(needle), len(data)))
            # hex_regex：直接把模式编码成 bytes 去搜，绕开"UTF-8 解码把 0xFF 变成替换符"
            pattern = str(when.get("pattern") or when.get("value") or "")
            if not pattern:
                raise ValueError("hex_regex 需要 pattern")
            try:
                pattern_bytes = pattern.encode("latin-1")
            except UnicodeEncodeError:
                raise ValueError("hex_regex 的模式只能包含 0-255 的字符（建议写成 \\xff）")
            if re.search(pattern_bytes, data):
                return True, "字节正则 %r 命中" % pattern
            return False, "字节正则 %r 未命中（共 %d 字节）" % (pattern, len(data))
        raise ValueError("unknown rule type")

    def _is_allowed(self, command):
        cmd = str(command or "").strip()
        for entry in self.allowed:
            if cmd == entry:
                return True
            if cmd.startswith(entry + " ") or cmd.startswith(entry + "\t"):
                return True
        return False

    def is_allowed(self, command):
        """公开版本，供 CLI 的 --auto 检查使用。"""
        return self._is_allowed(command)

    def record_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_consecutive_failures:
                self._stopped = True
            return self._consecutive_failures

    def record_success(self):
        with self._lock:
            self._consecutive_failures = 0

    def reset_stop(self):
        with self._lock:
            self._stopped = False
            self._consecutive_failures = 0


def _as_bytes(raw_bytes, text):
    """拿到原始字节：优先用调用方给的 raw_bytes，没有才退回把文本按 UTF-8 编码。"""
    if isinstance(raw_bytes, (bytes, bytearray)):
        return bytes(raw_bytes)
    return ("" if text is None else str(text)).encode("utf-8", errors="replace")


def _parse_hex(value):
    """把 "AA 55 FF" / "AA55FF" / ["AA","55","FF"] 解析成 b"\\xaa\\x55\\xff"。"""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    text = str(value or "")
    text = re.sub(r"[^0-9a-fA-F]", "", text)
    if not text or len(text) % 2:
        return b""
    try:
        return bytes.fromhex(text)
    except ValueError:
        return b""


def _hex_text(data):
    return " ".join("%02X" % byte for byte in data)


def _normalize_allowlist(value):
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def allowlist_check(command, allowed):
    """不带规则集的纯白名单检查。"""
    return RuleEngine({"allowed_auto_commands": allowed}).is_allowed(command)
