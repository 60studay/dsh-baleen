# -*- coding: utf-8 -*-
"""定时发送 / 轻模式不占口 的自测（不需要硬件）。

对应 Bug修复清单：
  Bug 4  定时发送完全不工作（light 模式下打开开关也不发）
  Bug 5  light 模式仍然占用串口

做法：把 serial_bridge.io 的收发函数替换成假实现，记录"有没有被调用/传了什么"，
所以拔掉所有串口也能跑。

运行：python selfcheck_autosend.py
"""
import os
import shutil
import sys
import tempfile
import threading
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


def install_fakes():
    """把 io 层和端口解析换成假实现，返回调用记录。"""
    from serial_bridge import io as sio
    import serial_bridge.supervisor as sup

    calls = {"transact": [], "drain": 0, "open_check": 0}

    def fake_transact(cfg, device, payload, baud=None):
        calls["transact"].append({"device": device, "payload": payload})
        return {"tx": payload, "rx_bytes": b"OK", "rx_text": "OK\n",
                "rx_hex": "4F 4B", "bytes": 2, "elapsed_ms": 1, "empty": False}

    def fake_drain(cfg, device, duration_ms=400, baud=None):
        calls["drain"] += 1
        return {"rx_bytes": b"", "rx_text": "", "rx_hex": "", "bytes": 0, "elapsed_ms": 1}

    def fake_open_check(cfg, baud=None):
        calls["open_check"] += 1
        return {"device": "FAKE", "description": "fake", "vid_pid": "", "source": "com", "note": ""}

    def fake_resolve(cfg):
        return {"device": "FAKE", "description": "fake", "vid_pid": "",
                "source": "com", "note": ""}

    sio.transact = fake_transact
    sio.drain = fake_drain
    sio.open_and_check = fake_open_check
    # supervisor 用 from .ports import resolve_port，所以打在它自己的命名空间上
    sup.resolve_port = fake_resolve
    sup.scan_ports = lambda: []
    return calls


def main():
    root = tempfile.mkdtemp(prefix="autosend-")
    try:
        proto = os.path.join(root, "protocols")
        os.makedirs(os.path.join(proto, "s"))
        with open(os.path.join(proto, "s", "config.json"), "w", encoding="utf-8") as handle:
            handle.write('{"com":"COM_FAKE","baud":9600,"newline":"\\r\\n"}')
        with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as handle:
            handle.write('{"log_root":"%s"}' % os.path.join(root, "logs").replace("\\", "\\\\"))

        calls = install_fakes()

        from serial_bridge.supervisor import WebServer
        srv = WebServer(
            {
                "protocol_root": proto,
                "common_config": os.path.join(root, "config.json"),
                "log_root": os.path.join(root, "logs"),
                "runtime_root": os.path.join(root, "logs", ".runtime"),
            },
            "s",
            port=8899,
            mode="light",
        )

        print("\n[1] Bug 5：轻模式下 /poll 不碰串口")
        before = calls["drain"]
        for _ in range(5):
            srv.poll()
        check("多次 poll 一次都没打开串口", calls["drain"] == before,
              "drain 调用次数 %d -> %d" % (before, calls["drain"]))
        result = srv.poll(force=True)
        check("显式 force=True 才真的读一帧", calls["drain"] == before + 1 and result["ok"])

        print("\n[2] Bug 4：light 模式下定时发送必须真的发")
        srv.state.update(watch={"send_enabled": False, "command": "P", "interval_ms": 300})
        thread = threading.Thread(target=srv._autosend_loop, daemon=True)
        srv._autosend_thread = thread  # status() 用它判断"后台循环是否活着"
        thread.start()
        time.sleep(0.6)
        check("未开启时不发送", srv.autosend_stats["sent"] == 0,
              srv.autosend_stats)

        srv.state.update(watch={"send_enabled": True, "command": "P", "interval_ms": 300})
        time.sleep(2.0)
        sent = srv.autosend_stats["sent"]
        check("开启后按间隔发送（>=3 次）", sent >= 3, "sent=%d" % sent)
        check("发的是配置里的 command",
              bool(calls["transact"]) and calls["transact"][-1]["payload"] == "P",
              calls["transact"][-3:])

        print("\n[2b] Bug 4：light 模式下自动发送的回显要能从 /poll 拿到")
        polled = srv.poll()
        frames = polled.get("frames") or []
        check("poll 返回了自动发送帧", len(frames) >= 1, polled)
        check("帧里带命令和回显",
              bool(frames) and frames[0].get("tx") == "P" and frames[0].get("rx") == "OK\n",
              frames[:2])
        again = srv.poll()
        check("取走后不重复投递", not (again.get("frames") or []), again)

        print("\n[3] watch_running 语义：开启即 true，关闭即 false")
        srv.state.update(watch={"send_enabled": False, "command": "P", "interval_ms": 300})
        time.sleep(0.4)
        st_off = srv.status()
        srv.state.update(watch={"send_enabled": True})
        time.sleep(0.4)
        st_on = srv.status()
        check("关闭时 watch_running=false", st_off["watch_running"] is False, st_off["watch_running"])
        check("开启时 watch_running=true", st_on["watch_running"] is True, st_on["watch_running"])
        check("轻模式下 port_held 始终 false", st_on["port_held"] is False)

        print("\n[4] paused 时不发送")
        srv.state.force_state("paused", "test", "暂停")
        before_sent = srv.autosend_stats["sent"]
        time.sleep(1.2)
        check("paused 期间不发送", srv.autosend_stats["sent"] == before_sent,
              "%d -> %d" % (before_sent, srv.autosend_stats["sent"]))
        srv.state.force_state("ai", "test", "恢复")

        print("\n[5] 关闭后停止")
        srv.state.update(watch={"send_enabled": False})
        time.sleep(0.5)
        a = srv.autosend_stats["sent"]
        time.sleep(1.5)
        check("关闭后不再发送", srv.autosend_stats["sent"] == a,
              "%d -> %d" % (a, srv.autosend_stats["sent"]))

        print("\n[6] 发送失败不炸线程")
        def boom(cfg, device, payload, baud=None):
            raise RuntimeError("boom")
        from serial_bridge import io as sio
        original = sio.transact
        sio.transact = boom
        srv.state.update(watch={"send_enabled": True, "command": "P", "interval_ms": 300})
        time.sleep(1.0)
        check("失败被记录而不是崩溃", srv.autosend_stats["errors"] >= 1,
              srv.autosend_stats)
        sio.transact = original

        srv.state.update(watch={"send_enabled": False})
        srv.shutdown_event.set()
        thread.join(timeout=2)
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
