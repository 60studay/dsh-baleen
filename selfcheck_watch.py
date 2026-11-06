# -*- coding: utf-8 -*-
"""监视进程双开关 + 规则隔离 的自测（需要真硬件）。

覆盖交接文档 3.6 与验收清单里的判定部分：
  两个开关相互独立 / check_source 三种取值 / 规则写错不影响收发

⚠ 架构变化说明：定时发送已经从 WatchLoop 搬到 WebServer（Bug 4 的修复），
所以这个自测改成直接驱动 WebServer，和线上跑的代码路径一致。
如果只测"发不发得出去"、不需要真设备，用 selfcheck_autosend.py。

运行：python selfcheck_watch.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.config import load_scene
from serial_bridge.errors import BridgeError
from serial_bridge.state import SharedState

LOG_ROOT = os.environ.get("SERIAL_BRIDGE_LOG_DIR") or os.path.join(HERE, "_testlogs")
PROTO = os.environ.get("SERIAL_BRIDGE_PROTO_DIR") or os.path.join(HERE, "protocols")
RUNTIME = os.path.join(LOG_ROOT, ".runtime")
SCENE = "蓝牙AT"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def log_lines():
    path = os.path.join(LOG_ROOT, SCENE, "log.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readlines()
    except OSError:
        return []


def mark():
    return len(log_lines())


def since(mark_index):
    return log_lines()[mark_index:]


def wait_quiet(idle_s=1.2, timeout_s=10.0):
    """等到日志不再增长为止（用来取"稳定基准点"）。

    固定 sleep 的问题：收发本身耗时随设备/负载变化（1.2~1.7s），
    sleep 短了就会把上一条命令的 TX 算进下一段，产生假失败。
    """
    last = len(log_lines())
    deadline = time.time() + timeout_s
    stable_since = time.time()
    while time.time() < deadline:
        time.sleep(0.2)
        current = len(log_lines())
        if current != last:
            last = current
            stable_since = time.time()
        elif (time.time() - stable_since) >= idle_s:
            return True
    return False


def alerts():
    path = os.path.join(RUNTIME, "alerts.jsonl")
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(l) for l in handle if l.strip()]
    except OSError:
        return []


def main():
    scene = load_scene(os.path.join(HERE, "config.json"), PROTO, SCENE)
    cfg = scene.cfg
    from serial_bridge.ports import resolve_port
    try:
        device = resolve_port(cfg)["device"]
    except BridgeError as exc:
        print("SKIP: 这个自测需要真串口，当前找不到匹配设备。")
        print("  原因: %s" % exc.message)
        print("  插上设备后重跑；不插设备可以跑 selfcheck_autosend.py（假设备）。")
        return 0
    print("using %s @ %s" % (device, cfg.get("baud")))

    # 链路存活探针：USB 插着不代表蓝牙链路通（HC-05 可能卡在 CONNECTING）。
    # 设备不回话时把本测试标成 SKIP，避免把硬件问题误报成代码失败。
    from serial_bridge import io as serial_io
    probe = serial_io.transact(cfg, device, "P")
    if probe["empty"]:
        print("SKIP: 串口能打开，但设备没有任何回显（蓝牙链路可能没建立）。")
        print("  检查：底盘是否上电 / 从机 HC-05 是否在范围 / AT+STATE? 是否为 CONNECTED")
        print("  软硬件无关的部分可跑：selfcheck_autosend.py（假设备）")
        return 0

    from serial_bridge.supervisor import WebServer

    srv = WebServer({
        "protocol_root": PROTO,
        "common_config": os.path.join(HERE, "config.json"),
        "log_root": LOG_ROOT,
        "runtime_root": RUNTIME,
    }, SCENE, port=8896, mode="light")

    # 干净起步
    srv.state.update(watch={"send_enabled": False, "check_enabled": False,
                            "check_source": "both", "command": "P", "interval_ms": 400})

    import threading
    thread = threading.Thread(target=srv._autosend_loop, daemon=True)
    srv._autosend_thread = thread
    thread.start()
    time.sleep(0.3)

    def manual(command):
        """通过 WebServer 走真实的"临时开口-发-读-关"路径。"""
        return srv.send(command, "text", "human")

    try:
        # ── 两个都关：完全手动 ────────────────────────────────────────────
        print("\n[1] 两个都关 → 应无任何自动行为")
        m = mark()
        before_alerts = len(alerts())
        time.sleep(2.0)
        tx = [l for l in since(m) if " TX " in l]
        check("都关时没有自动发送", len(tx) == 0, tx[:3])
        check("都关时不产生告警", len(alerts()) == before_alerts)

        # ── 只开定时发送：应发命令，但判定不跑 ────────────────────────────
        print("\n[2] 只开 send_enabled → 定时发送，判定不执行")
        srv.state.update(watch={"send_enabled": True, "check_enabled": False,
                                "command": "P", "interval_ms": 400})
        time.sleep(0.8)          # 等上一轮可能在途的发送落地
        m = mark()
        after_alerts = len(alerts())
        time.sleep(3.0)
        tx = [l for l in since(m) if " TX " in l]
        check("只开 send 时会定时发命令", len(tx) >= 2, "tx=%d" % len(tx))
        check("只开 send 时判定不运行（无告警）", len(alerts()) == after_alerts)

        # ── 只开判定：不发送，但规则跑 ────────────────────────────────────
        print("\n[3] 只开 check_enabled → 不自动发送，判定照跑")
        srv.state.update(watch={"send_enabled": False, "check_enabled": True,
                                "check_source": "both"})
        # 关掉开关后可能还有一条在途（收发本身 1.2~1.7s），固定 sleep 容易取早基准点。
        # 改成"等日志静默"，无论设备快慢都稳。
        wait_quiet()
        m = mark()
        baseline = len(alerts())
        rule_text = _sample_text_for_first_rule(scene)
        print("    注入回显: %r" % rule_text[:60])
        srv.loop._run_checks(rule_text, "watch")
        time.sleep(0.2)
        produced = alerts()[baseline:]
        check("只开判定时规则能触发", len(produced) >= 1, produced)
        rule_names = [a.get("rule") for a in produced]
        check("触发的是配置里的规则（不是代码里的）", any(rule_names), rule_names)
        sent = [a for a in produced if a.get("sent")]
        check("白名单内的自动命令被执行", len(sent) >= 1, produced)
        tx = [l for l in since(m) if " TX " in l]
        check("只开判定时不发轮询命令", all("P" not in l for l in tx), tx[:5])
        check("自动处置的 TX 记录在日志里", len(tx) >= 1, tx[:3])

        # ── 两个都开 ──────────────────────────────────────────────────────
        print("\n[4] 两个都开 → 协同工作")
        baseline = len(alerts())
        srv.state.update(watch={"send_enabled": True, "check_enabled": True,
                                "command": "P", "interval_ms": 400})
        m = mark()
        time.sleep(2.0)
        tx = [l for l in since(m) if " TX " in l]
        check("都开时轮询在发", any("P" in l for l in tx), tx[:3])

        # ── check_source 生效 ─────────────────────────────────────────────
        print("\n[5] check_source=watch_only 时手动来源不判定")
        srv.state.update(watch={"check_enabled": True, "check_source": "watch_only",
                                "send_enabled": False})
        baseline = len(alerts())
        manual("P")
        srv.loop._run_checks("FAULT 手动制造", "manual")
        time.sleep(0.2)
        check("watch_only 忽略 manual 来源", len(alerts()) == baseline)

        print("\n[6] check_source=manual_only 时 watch 来源不判定")
        srv.state.update(watch={"check_enabled": True, "check_source": "manual_only"})
        baseline = len(alerts())
        srv.loop._run_checks("FAULT 手动制造", "watch")
        time.sleep(0.2)
        check("manual_only 忽略 watch 来源", len(alerts()) == baseline)
        srv.loop._run_checks("FAULT 手动制造", "manual")
        time.sleep(0.2)
        check("manual_only 接受 manual 来源", len(alerts()) > baseline)

        # ── 规则写错：收发照常 ────────────────────────────────────────────
        print("\n[7] 规则写错（非法正则）→ 收发照常")
        srv.state.update(watch={"check_enabled": True, "check_source": "both"})
        bad = {"rules": [{"name": "bad", "when": {"type": "regex", "pattern": "([oops"},
                         "then": {"send": "R"}}],
               "allowed_auto_commands": ["R"]}
        original = scene.watch_cfg
        scene.watch_cfg = lambda: {**original(), **bad, "check_enabled": True,
                                   "check_source": "both"}
        baseline = len(alerts())
        srv.loop._run_checks("anything at all", "watch")
        time.sleep(0.3)
        check("非法正则不产生告警", len(alerts()) == baseline)
        again = manual("P")
        text = again.get("rx_text", "") if isinstance(again, dict) else ""
        check("规则出错后串口仍可收发", bool(text.strip()), repr(text[:60]))
        scene.watch_cfg = original

    finally:
        srv.state.update(watch={"send_enabled": False, "check_enabled": False})
        srv.shutdown_event.set()
        thread.join(timeout=2)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


def _sample_text_for_first_rule(scene):
    """按场景配置里的第一条 contains/equals 规则造一段能命中的回显。"""
    for rule in scene.watch_cfg().get("rules") or []:
        when = rule.get("when") or {}
        if when.get("type") in ("contains", "equals") and when.get("value"):
            return "prefix %s suffix" % when["value"]
    return ""


if __name__ == "__main__":
    sys.exit(main())
