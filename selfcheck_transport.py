# -*- coding: utf-8 -*-
"""Transport 抽象层自检（不需要硬件）。

要证明三件事：
1. 默认仍然是串口，老配置不用改
2. 业务层（transact / drain）完全不认识 pyserial —— 换一种传输方式照样能收发
3. 未知传输方式报结构化错误，而不是崩

运行：python selfcheck_transport.py
"""
import os
import sys

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


class EchoTransport(object):
    """假的传输方式：写什么回什么，用来证明业务层不绑死串口。"""

    kind = "test-echo"

    def __init__(self, cfg=None, device=None, baud=None):
        self.cfg = cfg or {}
        self.device = device
        self._pending = b""
        self._open = False

    def open(self):
        self._open = True
        return self

    def close(self):
        self._open = False

    def is_open(self):
        return self._open

    def write(self, data):
        self._pending += bytes(data)
        return len(data)

    def read(self, size=256):
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def reset_input_buffer(self):
        self._pending = b""

    def describe(self):
        return {"type": "test-echo", "device": self.device or "", "status": "open"}


def main():
    from serial_bridge import io as sio
    from serial_bridge.errors import BridgeError
    from serial_bridge.transport import Transport, SerialTransport, create_transport, register_transport

    print("\n[1] 默认走串口，老配置不用改")
    cfg = {"baud": 115200, "com": "COM_TEST"}
    made = create_transport(cfg, "COM_TEST")
    check("无 transport 字段 → SerialTransport", isinstance(made, SerialTransport), type(made).__name__)
    info = made.describe()
    check("describe 含 type/device/baud",
          info.get("type") == "serial" and info.get("device") == "COM_TEST" and info.get("baud") == 115200,
          info)

    print("\n[2] describe() 给 UI 的信息够用")
    check("帧格式可读", info.get("frame") == "8N1", info.get("frame"))

    print("\n[3] 未知传输方式 → 结构化错误（不是崩）")
    try:
        create_transport({"transport": "nope"}, "X")
        check("未知类型应抛错", False, "没有抛异常")
    except BridgeError as exc:
        check("kind=config", exc.kind == "config", exc.kind)
        check("提示里列出可用类型", any("serial" in h for h in (exc.hint or [])), exc.hint)

    print("\n[4] 业务层换传输方式照样能收发（证明没绑死 pyserial）")
    register_transport("test-echo", EchoTransport)
    result = sio.transact({"transport": "test-echo", "wait_first_byte_ms": 100,
                           "quiet_ms": 30, "timeout_read_ms": 200, "newline": "\\r\\n"},
                          "FAKE", "HELLO")
    check("文本模式回显正确", result["rx_text"] == "HELLO\r\n", repr(result["rx_text"]))
    check("字节数正确", result["bytes"] == 7, result["bytes"])

    raw = sio.transact({"transport": "test-echo", "wait_first_byte_ms": 100,
                        "quiet_ms": 30, "timeout_read_ms": 200},
                       "FAKE", "AA 55", raw_bytes=bytes([0xAA, 0x55]))
    check("原始字节模式仍不补换行", raw["rx_hex"] == "AA 55", raw["rx_hex"])

    drained = sio.drain({"transport": "test-echo", "wait_first_byte_ms": 50,
                         "quiet_ms": 20}, "FAKE", duration_ms=60)
    check("drain 也走传输层（无异常）", drained["bytes"] == 0, drained["bytes"])

    print("\n[5] 抽象约束：io.py 不该 import pyserial")
    with open(os.path.join(HERE, "serial_bridge", "io.py"), encoding="utf-8") as handle:
        io_source = handle.read()
    check("io.py 里没有 'import serial'", "import serial" not in io_source)
    check("io.py 通过 transport 拿连接", "create_transport" in io_source)

    print("\n[6] 基类接口齐全")
    for method in ("open", "close", "write", "read", "describe"):
        check("Transport 有 %s()" % method, callable(getattr(Transport, method, None)))

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
