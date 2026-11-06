# -*- coding: utf-8 -*-
"""控制权 ↔ 串口占用 的解耦自测（不需要网页、不需要真设备）。

针对 `实测问题反馈.md` 的 P0：控制权切到 human/paused 时，网页必须**主动关闭
串口句柄**；切回 ai 时再重新打开。

这里不真的开串口，而是用假 Loop 记录"句柄开/关"的调用，
所以拔掉设备也能跑。

运行：python selfcheck_port_hold.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.state import SharedState, STATE_AI, STATE_HUMAN, STATE_PAUSED
from serial_bridge.port_manager import PortManager

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


class FakeLoop(object):
    """记录 start/stop，模拟 WatchLoop 的句柄生命周期。"""

    def __init__(self):
        self.running = False
        self.ser = None
        self.starts = 0
        self.stops = 0
        self.last_error = None

    def start(self, device, baud):
        self.running = True
        self.ser = object()
        self.starts += 1

    def stop(self):
        if self.running:
            self.stops += 1
        self.running = False
        self.ser = None

    def take(self):
        return b""


class FakeServer(object):
    """只装配 PortManager 需要的那几个依赖，绕开 HTTP 和真串口。"""

    def __init__(self, runtime_dir, scene="t", mode="watch"):
        self.state = SharedState(runtime_dir, scene)
        self.mode = mode
        self.loop = FakeLoop()
        # 被测对象：真正在用的 PortManager（不再是测试里手抄的一份逻辑）
        self.ports = PortManager(
            state=self.state,
            loop=self.loop,
            current_device=lambda: {"device": "FAKE", "source": "fake"},
            effective_cfg=lambda: {"baud": 115200},
            mode_getter=lambda: self.mode,
        )

    # 兼容测试正文里的老名字，转调 PortManager
    def _acquire_port_if_needed(self):
        return self.ports.acquire_if_needed()

    def reconcile_port(self, trigger="change"):
        return self.ports.reconcile(trigger)

    @property
    def hold_released_reason(self):
        return self.ports.hold_released_reason


def main():
    import tempfile
    import shutil

    root = tempfile.mkdtemp(prefix="holdtest-")
    try:
        srv = FakeServer(root)
        srv.state.set_state(STATE_AI, "test", "start")
        srv._last_control = STATE_AI
        srv._acquire_port_if_needed()

        print("\n[1] ai 状态持有句柄")
        check("ai 时 loop 在跑", srv.loop.running)
        check("ai 时句柄存在", srv.loop.ser is not None)

        print("\n[2] 外部把控制权改成 human → 必须释放句柄")
        # 关键：这里**不经过网页**，模拟 CLI / serial_control 工具直接改状态文件
        srv.state.set_state(STATE_HUMAN, "cli", "人类接管")
        result = srv.reconcile_port("watchdog")
        check("检测到控制权变化", result is not None and result["action"] == "released", result)
        check("human 时 loop 已停", not srv.loop.running)
        check("human 时句柄已关闭（这就是 P0 的核心）", srv.loop.ser is None)
        check("记录了释放原因", "human" in srv.hold_released_reason, srv.hold_released_reason)

        print("\n[3] 停在 human：再收敛多次不应重新占口")
        before = srv.loop.starts
        for _ in range(5):
            srv.reconcile_port("watchdog")
        check("human 期间反复收敛不会重占", srv.loop.starts == before)
        check("human 期间句柄始终为空", srv.loop.ser is None)

        print("\n[4] 改回 ai → 必须重新打开句柄")
        srv.state.set_state(STATE_AI, "cli", "交给AI")
        result = srv.reconcile_port("watchdog")
        check("检测到切回 ai", result is not None and result["action"] == "acquired", result)
        check("ai 时 loop 重新在跑", srv.loop.running)
        check("ai 时句柄重新打开", srv.loop.ser is not None)

        print("\n[5] paused 也要释放")
        srv.state.set_state(STATE_PAUSED, "cli", "暂停AI")
        result = srv.reconcile_port("watchdog")
        check("paused 触发释放", result is not None and result["action"] == "released", result)
        check("paused 时句柄已关闭", srv.loop.ser is None)

        print("\n[6] 状态没变时不做多余动作（避免抖动）")
        before_starts, before_stops = srv.loop.starts, srv.loop.stops
        for _ in range(5):
            srv.reconcile_port("watchdog")
        check("无变化时不再 start", srv.loop.starts == before_starts)
        check("无变化时不再 stop", srv.loop.stops == before_stops)

        print("\n[7] 轻模式永不持有句柄")
        light = FakeServer(tempfile.mkdtemp(), scene="l", mode="light")
        light.state.set_state(STATE_AI, "test", "start")
        light._last_control = STATE_AI
        light.reconcile_port("force")
        check("light 模式不占口", light.loop.ser is None and not light.loop.running)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
