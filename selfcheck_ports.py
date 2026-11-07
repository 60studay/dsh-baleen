# -*- coding: utf-8 -*-
"""串口解析 / 自动选择 的自测（不需要硬件）。

针对一个用户真踩到的坑（2026-11-07 14:15）：

    拔插 USB 后 Windows 把 CH340 从 COM7 换成了 COM10，
    点「AI 自动选择」→ 400「COM7 打不开」，候选里明明列着 COM10 却不去选。

根因是 `len(ports) == 1` 这个条件：用户机器上有 7 个口（6 个蓝牙虚拟口），
所以永远不成立。这个自检锁住四件事：

  1. 蓝牙虚拟口必须被排除在"候选"之外（它们没有 VID:PID）
  2. 只有一个真实 USB 口时，配置写死的老号要能自动纠正
  3. `guess_port` 真的会猜（vid_pid / 序列号 / 描述 / 只剩一个）
  4. 同型号两块板子（都是 CH340）必须能用 `match_serial` 分开

运行：python selfcheck_ports.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge import ports as ports_mod
from serial_bridge.errors import BridgeError
from serial_bridge.ports import guess_port, real_ports, resolve_port

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


BT = [
    {"device": "COM3", "description": "蓝牙链接上的标准串行 (COM3)", "vid_pid": "", "serial_number": ""},
    {"device": "COM4", "description": "蓝牙链接上的标准串行 (COM4)", "vid_pid": "", "serial_number": ""},
    {"device": "COM5", "description": "蓝牙链接上的标准串行 (COM5)", "vid_pid": "", "serial_number": ""},
]
CH340_A = {"device": "COM10", "description": "USB-SERIAL CH340 (COM10)",
           "vid_pid": "1A86:7523", "serial_number": "SN-A"}
CH340_B = {"device": "COM11", "description": "USB-SERIAL CH340 (COM11)",
           "vid_pid": "1A86:7523", "serial_number": "SN-B"}


def use(ports):
    """把 scan_ports 换成假数据（纯逻辑自检，不碰真硬件）。"""
    ports_mod.scan_ports = lambda: list(ports)


def main():
    original = ports_mod.scan_ports
    try:
        print("\n[1] 蓝牙虚拟口不算候选")
        real = real_ports(BT + [CH340_A])
        check("排除蓝牙口后只剩 1 个", len(real) == 1 and real[0]["device"] == "COM10", real)
        check("没有 VID:PID 的一律不算", all(p.get("vid_pid") for p in real))

        print("\n[2] 配置写死的老号不在了 → 自动切到唯一的真实 USB 口")
        use(BT + [CH340_A])
        got = resolve_port({"com": "COM7"})
        check("resolve_port 不再抛错", got.get("device") == "COM10", got)
        check("source 标成 only_usb_port", got.get("source") == "only_usb_port", got)
        check("note 说清了为什么换", "COM7" in got.get("note", ""), got.get("note"))

        print("\n[3] 多个真实 USB 口 + 老号不在 → 仍然要报错（不能瞎猜）")
        use(BT + [CH340_A, CH340_B])
        try:
            resolve_port({"com": "COM7"})
            check("多口歧义要报错", False, "没有报错")
        except BridgeError as exc:
            check("多口歧义要报错", exc.kind == "no_device", exc.kind)

        print("\n[4] guess_port：真的会猜")
        use(BT + [CH340_A])
        guess = guess_port({})
        check("只有一个真实口就选它", guess and guess["device"] == "COM10", guess)
        use(BT + [CH340_A, CH340_B])
        guess = guess_port({"match_vid_pid": "1A86:7523"})
        check("同型号两块 → 猜不出来（返回 None，交给上层报歧义）", guess is None, guess)
        guess = guess_port({"match_serial": "SN-B"})
        check("按序列号能唯一命中", guess and guess["device"] == "COM11", guess)
        guess = guess_port({"match": "CH340", "match_serial": "SN-A"})
        check("序列号优先于描述", guess and guess["device"] == "COM10", guess)

        print("\n[5] match_serial 在 resolve_port 里也是一等判据")
        use(BT + [CH340_A, CH340_B])
        got = resolve_port({"match_vid_pid": "1A86:7523", "match_serial": "SN-B"})
        check("vid_pid 撞车时用序列号分开", got.get("device") == "COM11", got)
        check("source=match_serial", got.get("source") == "match_serial", got)

        got = resolve_port({"match_serial": "SN-A"})
        check("单独用 match_serial 也行", got.get("device") == "COM10", got)

        print("\n[6] 精确 COM 号命中时不做多余的事")
        use(BT + [CH340_A])
        got = resolve_port({"com": "COM10"})
        check("source=com", got.get("source") == "com", got)
        check("没有多余的 note", got.get("note") == "", got.get("note"))
    finally:
        ports_mod.scan_ports = original

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
