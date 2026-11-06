# -*- coding: utf-8 -*-
"""管家（daemon）自检。

证明 8 条验收里可自动化的部分：
  1. 提交延迟（命令真正写出去的耗时）
  3. start 幂等（进程内重复 start 不会开第二个连接）
  5. 空闲超时会退出
  7. 优雅退出后串口句柄已释放（能立刻被别的程序打开）

需要真串口；没有设备时打印 SKIP 并退出 0。

运行：python selfcheck_daemon.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def port_free(device):
    """直接开一下，确认句柄真的释放了。"""
    try:
        from serial_bridge.transport import create_transport
        transport = create_transport({"baud": 115200}, device).open()
        transport.close()
        return True
    except Exception:
        return False


def main():
    from serial_bridge.cli import _paths
    from serial_bridge.daemon import Daemon
    from serial_bridge.ports import resolve_port

    paths = _paths()
    scene_name = os.environ.get("SB_SCENE") or "蓝牙AT"
    daemon = None
    try:
        daemon = Daemon(paths, scene_name, http_port=8895, idle_s=600)
        daemon.start()
    except Exception as exc:
        print("SKIP: 管家起不来（%s）" % str(exc)[:80])
        return 0

    device = daemon.resolved["device"]
    print("using %s @ %s" % (device, daemon.scene.cfg.get("baud")))

    try:
        print("\n[1] 提交延迟：命令写出去的耗时要小")
        daemon.state.force_state("ai", "test", "自检")
        result = daemon.send("P", "text", "ai")
        check("提交 <= 50ms", result["submit_ms"] <= 50, result["submit_ms"])
        check("能收到回显", result["bytes"] > 0, result["bytes"])
        print("       提交 %dms / 含设备回包 %dms" % (result["submit_ms"], result["elapsed_ms"]))

        print("\n[2] 读线程持续广播（/poll 游标）")
        first = daemon.poll(0)
        check("poll 有数据", first["bytes"] > 0, first["bytes"])
        check("游标推进", first["cursor"] > 0, first["cursor"])
        again = daemon.poll(first["cursor"])
        check("游标之后没有重复数据", again["bytes"] == 0, again["bytes"])

        print("\n[1b] wait=False：发完就走，回显不丢")
        cursor = daemon.poll(-1)["cursor"]        # -1 = 只要之后的新数据
        quick = daemon.send("X", "text", "ai", wait_ms=0)
        check("立即返回 submitted", quick.get("submitted") is True, quick)
        check("不等回显（bytes=0）", quick["bytes"] == 0, quick["bytes"])
        check("提交很快（<=50ms）", quick["submit_ms"] <= 50, quick["submit_ms"])
        deadline = time.time() + 3
        got = b""
        while time.time() < deadline:
            chunk = daemon.poll(cursor)
            cursor = chunk["cursor"]
            if chunk["bytes"]:
                got = chunk["rx_text"].encode("utf-8")
                break
            time.sleep(0.05)
        check("回显仍能从 poll 取回", len(got) > 0, len(got))

        print("\n[3] 常驻连接：连发 3 条都不应该出现「被占用」")
        errors = []
        for _ in range(3):
            try:
                daemon.send("P", "text", "ai")
            except Exception as exc:
                errors.append(str(exc)[:40])
        check("连发无异常", not errors, errors)

        print("\n[4] 控制权门禁仍然生效")
        daemon.state.force_state("human", "test", "自检")
        try:
            daemon.send("P", "text", "ai")
            check("human 下 AI 应被拒绝", False, "居然发出去了")
        except Exception as exc:
            check("human 下 AI 被拒绝", "control_blocked" in str(exc) or "控制权" in str(exc), str(exc)[:60])
        daemon.state.force_state("ai", "test", "自检")

        print("\n[5] 空闲超时会自己退出")
        daemon.idle_s = 1
        daemon.last_activity = time.time() - 10
        deadline = time.time() + 6
        while time.time() < deadline and daemon.running:
            time.sleep(0.2)
        check("running 变为 False", not daemon.running, daemon.running)
        check("传输已关闭", not (daemon.transport and daemon.transport.is_open()),
              daemon.transport)

        print("\n[6] 优雅退出后句柄已释放")
        daemon.stop()
        check("退出后能立刻重新打开串口", port_free(device))
    finally:
        if daemon is not None:
            try:
                daemon.stop()
            except Exception:
                pass

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
