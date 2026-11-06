# -*- coding: utf-8 -*-
"""规则引擎与隔离自测（不需要真串口）。

覆盖交接文档 3.6 与第六章验收清单里的判定部分：
  四种判据 / 非法正则 / 超时隔离 / 白名单 / 限流 / 熔断 / 通用性

运行：python selfcheck_rules.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.rules import RuleEngine, RULE_TYPES
from serial_bridge.runner import evaluate_isolated

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append("%s %s" % (name, detail))


def names(results):
    return [r.rule for r in results if r.matched]


def dnames(results):
    return [r.get("rule") for r in results if r.get("matched")]


# ── 1. 四种判据 ────────────────────────────────────────────────────────────
print("== 四种判据 ==")
engine = RuleEngine({
    "allowed_auto_commands": ["FIX", "RESET"],
    "rules": [
        {"name": "c", "when": {"type": "contains", "value": "ALARM"}, "then": {"send": "FIX"}},
        {"name": "e", "when": {"type": "equals", "value": "READY"}, "then": {"send": "FIX"}},
        {"name": "r", "when": {"type": "regex", "pattern": r"ERR\d+"}, "then": {"send": "RESET"}},
        {"name": "n", "when": {"type": "no_response", "consecutive": 3}, "then": {"send": "FIX"}},
    ],
})
check("contains 命中", "c" in names(engine.evaluate("temp=40 ALARM on")))
check("contains 不误命中", "c" not in names(engine.evaluate("all good")))

eq = RuleEngine({"allowed_auto_commands": ["X"],
                 "rules": [{"name": "e", "when": {"type": "equals", "value": "READY"},
                            "then": {"send": "X"}}]})
check("equals 命中", "e" in names(eq.evaluate("READY")))
check("equals 不误命中", "e" not in names(eq.evaluate("READY NOW")))

rx = RuleEngine({"allowed_auto_commands": ["RESET"],
                 "rules": [{"name": "r", "when": {"type": "regex", "pattern": r"ERR\d+"},
                            "then": {"send": "RESET"}}]})
check("regex 命中", "r" in names(rx.evaluate("status ERR42")))
check("regex 不误命中", "r" not in names(rx.evaluate("status OK")))

nr = RuleEngine({"allowed_auto_commands": ["FIX"],
                 "rules": [{"name": "n", "when": {"type": "no_response", "consecutive": 3},
                            "then": {"send": "FIX"}}]})
check("no_response 未达阈值", "n" not in names(nr.evaluate("", 2)))
check("no_response 达阈值", "n" in names(nr.evaluate("", 3)))

check("只有四种判据", set(RULE_TYPES) == {"contains", "regex", "no_response", "equals"},
      str(RULE_TYPES))
bad_type = RuleEngine({"rules": [{"name": "x", "when": {"type": "wat"}}]})
res = bad_type.evaluate("anything")
check("未知判据只报错不抛异常", bool(res) and res[0].error is not None, res)

# ── 2. 非法正则不影响其它规则 ──────────────────────────────────────────────
print("== 非法正则 ==")
broken = RuleEngine({
    "allowed_auto_commands": ["X"],
    "rules": [
        {"name": "broken", "when": {"type": "regex", "pattern": "([unclosed"},
         "then": {"send": "X"}},
        {"name": "good", "when": {"type": "contains", "value": "HIT"}, "then": {"send": "X"}},
    ],
})
res = broken.evaluate("HIT here")
check("非法正则只记 error", len([r for r in res if r.error]) == 1, res)
check("非法正则不阻断其它规则", "good" in names(res), res)

# ── 3. 超时隔离（子进程，能真的杀掉）──────────────────────────────────────
print("== 超时隔离（子进程）==")
evil = {
    "allowed_auto_commands": ["X"],
    "rules": [{"name": "slow", "when": {"type": "regex", "pattern": "(a+)+$"},
               "then": {"send": "X"}}],
}
worst = "a" * 40 + "!"
t0 = time.time()
results, error = evaluate_isolated(worst, evil, 0, "watch", timeout_ms=600)
elapsed = time.time() - t0
check("ReDoS 被超时杀掉", error is not None and "超时" in str(error), (error, elapsed))
check("超时后进程真的返回了", elapsed < 4.0, "%.2fs" % elapsed)

good_cfg = {
    "allowed_auto_commands": ["FIX"],
    "rules": [{"name": "ok", "when": {"type": "contains", "value": "HIT"},
               "then": {"send": "FIX"}}],
}
results, error = evaluate_isolated("HIT here", good_cfg, 0, "watch", timeout_ms=8000)
check("正常规则在子进程里也算得对",
      error is None and dnames(results) == ["ok"], (results, error))

# ── 4. 白名单 ──────────────────────────────────────────────────────────────
print("== 白名单 ==")
empty_allow = RuleEngine({"allowed_auto_commands": [], "rules": [
    {"name": "c", "when": {"type": "contains", "value": "A"}, "then": {"send": "X"}}]})
res = empty_allow.evaluate("A")
check("白名单为空禁止自动命令", bool(res) and res[0].matched and res[0].error, res)

denied = RuleEngine({"allowed_auto_commands": ["SAFE"], "rules": [
    {"name": "c", "when": {"type": "contains", "value": "A"}, "then": {"send": "DANGER"}}]})
res = denied.evaluate("A")
check("非白名单命令被拒", bool(res) and res[0].matched and res[0].error, res)

ok_allow = RuleEngine({"allowed_auto_commands": ["SAFE"], "rules": [
    {"name": "c", "when": {"type": "contains", "value": "A"}, "then": {"send": "SAFE"}}]})
res = ok_allow.evaluate("A")
check("白名单命令放行", bool(res) and res[0].matched and not res[0].error, res)

args_allow = RuleEngine({"allowed_auto_commands": ["S"], "rules": [
    {"name": "c", "when": {"type": "contains", "value": "A"}, "then": {"send": "S 1 500"}}]})
res = args_allow.evaluate("A")
check("白名单允许带参数的命令", bool(res) and res[0].matched and not res[0].error, res)

# ── 5. 限流 ────────────────────────────────────────────────────────────────
print("== 限流 ==")
now = [100.0]
limited = RuleEngine({"rule_cooldown_s": 5, "allowed_auto_commands": ["X"], "rules": [
    {"name": "c", "when": {"type": "contains", "value": "A"}, "then": {"send": "X"}}]},
    clock=lambda: now[0])
check("首次触发", limited.evaluate("A")[0].matched)
now[0] += 1
check("冷却期内跳过", limited.evaluate("A")[0].skipped)
now[0] += 10
check("冷却期后再触发", limited.evaluate("A")[0].matched)

# ── 6. 熔断 ────────────────────────────────────────────────────────────────
print("== 熔断 ==")
fuse = RuleEngine({"max_consecutive_failures": 3, "allowed_auto_commands": ["X"], "rules": [
    {"name": "c", "when": {"type": "contains", "value": "A"}, "then": {"send": "X"}}]})
check("未达阈值不停", not fuse.stopped)
fuse.record_failure()
fuse.record_failure()
check("2 次失败仍运行", not fuse.stopped)
fuse.record_failure()
check("3 次失败即停", fuse.stopped)
check("停止后不再判定", fuse.evaluate("A") == [])

# ── 7. 通用性：代码里没有设备专有字面量 ────────────────────────────────────
print("== 通用性 ==")
import serial_bridge.errors as errors_mod
import serial_bridge.io as io_mod
import serial_bridge.ports as ports_mod
import serial_bridge.rules as rules_mod
import serial_bridge.state as state_mod

forbidden = ["FAULT", "stall", "CH1", "CH2", "RUNNING", "IDLE", "TARGETS"]
for mod in (errors_mod, io_mod, ports_mod, rules_mod, state_mod):
    src = open(mod.__file__, encoding="utf-8").read()
    hits = [w for w in forbidden if ('"%s"' % w) in src or ("'%s'" % w) in src]
    check("模块 %s 无设备专有字面量" % os.path.basename(mod.__file__), not hits, hits)

# contains 规则的 value 在**配置**里，不在代码里
check("场景配置里才有设备关键字",
      "FAULT" in open(os.path.join(HERE, "protocols", "蓝牙AT", "config.json"),
                      encoding="utf-8").read())

print()
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    for item in FAIL:
        print("  - %s" % item)
    sys.exit(1)
print("全部通过")
