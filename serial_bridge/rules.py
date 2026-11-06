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

RULE_TYPES = ("contains", "regex", "no_response", "equals")

DEFAULT_COOLDOWN_S = 5.0


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

    def evaluate(self, text, no_response_streak=0, source="watch"):
        """对所有规则跑一遍。本函数永不抛异常。"""
        results = []
        if self._stopped:
            return results
        for rule in self.rules:
            results.append(self._evaluate_one(rule, text, no_response_streak, source))
        return results

    def _evaluate_one(self, rule, text, no_response_streak, source):
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
            matched, reason = self._test(rule_type, when, text, no_response_streak)
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

    def _test(self, rule_type, when, text, no_response_streak):
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
