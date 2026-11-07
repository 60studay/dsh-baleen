# -*- coding: utf-8 -*-
"""静默期自测：没人发命令时，插件不该"看见"任何数据。

来历（测试方 2026-11-07 的教训，值得抄到我们这边）：

  他们的靶子固件曾经静置 2 秒就被收到 20480 字节、`OK PONG` 出现 1768 次。
  原因是 C51 的 printf 吃满 128 字节内部 RAM（踩坏环形缓冲）+ 读指针回绕时
  重放旧字节（把之前发过的 `P`/`X`/`R` 当新命令再执行一遍）。

  关键在于：他们之前的验证都是"发一条 → 看回包"，
  **从来没测过"什么都不做的时候它在干什么"**。

对我们这边的意义：如果读路径自己会造数据、重复计数、或者把上一次的尾巴
当成这一次的，那么所有"窗口时间内收到几条"的断言都会失真——而且失真是
往"看起来正常"的方向偏（有数据总比没数据像成功）。

四项检查：
  [1] 静置 2.5 秒，io.drain 收到 0 字节
  [2] 静置期间该场景日志不增长（没 TX，就更不该有 RX）
  [3] 全新日志目录里 serial_read 返回 empty=true（插件层确实"什么都没有"）
  [4] 发一条 P 拿到回包后，再静置 2 秒仍为 0 字节（设备不会因一条命令进入循环）

运行：python selfcheck_silence.py     （需要真硬件；没设备会 SKIP）
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.config import load_scene
from serial_bridge.errors import BridgeError

SCENE = "蓝牙AT"
PROTO = os.environ.get("SERIAL_BRIDGE_PROTO_DIR") or os.path.join(HERE, "protocols")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def main():
    scene = load_scene(os.path.join(HERE, "config.json"), PROTO, SCENE)
    cfg = scene.cfg
    from serial_bridge.ports import resolve_port
    try:
        device = resolve_port(cfg)["device"]
    except BridgeError as exc:
        print("SKIP: 这个自测需要真串口，当前找不到匹配设备。")
        print("  原因: %s" % exc.message)
        return 0
    print("using %s @ %s" % (device, cfg.get("baud")))

    from serial_bridge import io as sio

    # 链路存活探针：能打开但完全没回显，说明是蓝牙链路问题，不是代码问题
    probe = sio.transact(cfg, device, "P")
    if probe["empty"]:
        print("SKIP: 串口能打开，但设备没有任何回显（链路可能没建立）。")
        return 0

    # 用临时日志目录，避免污染真实日志，也保证 [2][3] 从"空"开始
    log_root = tempfile.mkdtemp(prefix="silence-")
    old_log = os.environ.get("SERIAL_BRIDGE_LOG_DIR")
    os.environ["SERIAL_BRIDGE_LOG_DIR"] = log_root
    try:
        print("\n[1] 静置 2.5 秒，不应收到任何字节")
        idle = sio.drain(cfg, device, duration_ms=2500)
        check("静置 rx_bytes = 0", idle["bytes"] == 0,
              "收到 %d 字节: %r" % (idle["bytes"], idle["rx_text"][:80]))

        print("\n[2] 静置期间日志不增长")
        log_path = os.path.join(log_root, SCENE, "log.txt")

        def log_size():
            try:
                return os.path.getsize(log_path)
            except OSError:
                return 0

        before = log_size()
        sio.drain(cfg, device, duration_ms=800)
        after = log_size()
        check("drain 不写日志（%d → %d）" % (before, after), before == after)

        print("\n[3] 插件层：没人发命令时 serial_read 应当为空")
        # 走和 cli.cmd_read 完全相同的路径（Logger.tail），不另写一份读法
        from serial_bridge.log import Logger
        logger = Logger(log_root, SCENE)
        text = logger.tail(50)
        check("serial_read 返回 empty", text.strip() == "",
              text[:80])

        print("\n[4] 一条命令之后，设备不应进入循环")
        one = sio.transact(cfg, device, "P")
        check("P 有回包（证明链路是通的）", not one["empty"],
              "收到 %d 字节" % one["bytes"])
        after_cmd = sio.drain(cfg, device, duration_ms=2000)
        check("命令后静置 2 秒 = 0 字节", after_cmd["bytes"] == 0,
              "又收到 %d 字节: %r" % (after_cmd["bytes"], after_cmd["rx_text"][:80]))
    finally:
        if old_log is None:
            os.environ.pop("SERIAL_BRIDGE_LOG_DIR", None)
        else:
            os.environ["SERIAL_BRIDGE_LOG_DIR"] = old_log
        shutil.rmtree(log_root, ignore_errors=True)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
