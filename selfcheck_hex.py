# -*- coding: utf-8 -*-
"""hex 模式字节级自测（不需要硬件，回环用假串口模拟）。

对应 Bug 11：`send(mode="hex")` 之前会把 "AA 55" 这 5 个 ASCII 字符发出去，
而不是 2 个字节。这个自测直接断言"串口上到底写了哪些字节"。

运行：python selfcheck_hex.py
"""
import os
import shutil
import sys
import tempfile

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


class FakePort(object):
    """假串口：写什么就回显什么（模拟 TX/RX 短接），并记录真实写入字节。"""

    def __init__(self):
        self.written = b""
        self._pending = b""

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data):
        self.written += data
        self._pending += data
        return len(data)

    def read(self, size=1):
        if not self._pending:
            return b""
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def close(self):
        pass

    @property
    def in_waiting(self):
        return len(self._pending)


def install_fake():
    from serial_bridge import io as sio
    port = FakePort()

    sio._open_with_retry = lambda cfg, device, baud=None: port
    return port


def main():
    root = tempfile.mkdtemp(prefix="hextest-")
    try:
        port = install_fake()
        from serial_bridge import io as sio
        cfg = {"baud": 115200, "newline": "\r\n", "wait_first_byte_ms": 300,
               "quiet_ms": 50, "timeout_read_ms": 200}

        print("\n[1] 原始字节模式：AA 55 必须只发 2 字节")
        port.written = b""
        r = sio.transact(cfg, "FAKE", "AA 55", raw_bytes=bytes([0xAA, 0x55]))
        check("写入字节 = AA 55（不补 CRLF）", port.written == b"\xaa\x55", port.written)
        check("回环 rx_hex = 'AA 55'", r["rx_hex"] == "AA 55", r["rx_hex"])
        check("回环字节数 = 2", r["bytes"] == 2, r["bytes"])

        print("\n[2] 多字节帧：01 03 00 00 00 0A C5 CD 必须原样 8 字节")
        port.written = b""
        frame = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A, 0xC5, 0xCD])
        r = sio.transact(cfg, "FAKE", "01 03 00 00 00 0A C5 CD", raw_bytes=frame)
        check("写入 = 原帧 8 字节", port.written == frame, port.written.hex())
        check("回环 8 字节", r["bytes"] == 8, r["bytes"])

        print("\n[3] 文本模式不能被改坏：HELLO 仍补 CRLF，按 ASCII 发")
        port.written = b""
        r = sio.transact(cfg, "FAKE", "HELLO")
        check("写入 = 'HELLO\\r\\n'", port.written == b"HELLO\r\n", port.written)
        check("回环 rx_hex = '48 45 4C 4C 4F 0D 0A'",
              r["rx_hex"] == "48 45 4C 4C 4F 0D 0A", r["rx_hex"])

        print('\n[4] 空 raw_bytes（b""）也要按原始字节处理，不能回退到文本')
        port.written = b""
        r = sio.transact(cfg, "FAKE", "", raw_bytes=b"")
        check("写入为空（没有 CRLF）", port.written == b"", port.written)
        check("回环为空", r["bytes"] == 0, r["bytes"])

        print("\n[5] supervisor.send(mode='hex') 必须把 raw_bytes 传下去")
        from serial_bridge.supervisor import WebServer
        proto = os.path.join(root, "protocols")
        os.makedirs(os.path.join(proto, "s"))
        with open(os.path.join(proto, "s", "config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"com":"COM_FAKE","baud":115200,"newline":"\\r\\n"}')
        with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"log_root":"%s"}' % os.path.join(root, "logs").replace("\\", "\\\\"))

        import serial_bridge.supervisor as sup
        sup.resolve_port = lambda cfg: {"device": "FAKE", "description": "fake",
                                        "vid_pid": "", "source": "com", "note": ""}

        calls = []
        real_transact = sio.transact

        def spy(cfg_, device_, payload_, baud_=None, raw_bytes=None):
            calls.append({"payload": payload_, "raw": raw_bytes})
            # 假设备要"回话"：无回显现在会抛 no_response（见 cli._require_reply），
            # 这里测的是 raw_bytes 有没有传下去，不是无响应分支。
            return {"tx": payload_, "rx_bytes": b"OK", "rx_text": "OK", "rx_hex": "4F 4B",
                    "bytes": 2, "elapsed_ms": 1, "empty": False}

        sio.transact = spy
        try:
            srv = WebServer({
                "protocol_root": proto,
                "common_config": os.path.join(root, "config.json"),
                "log_root": os.path.join(root, "logs"),
                "runtime_root": os.path.join(root, "logs", ".runtime"),
            }, "s", port=8898, mode="light")
            srv.send("AA 55", "hex", "human")
            srv.send("HELLO", "text", "human")
        finally:
            sio.transact = real_transact

        check("hex 调用带 raw_bytes", bool(calls) and calls[0]["raw"] == b"\xaa\x55", calls[:1])
        check("text 调用不带 raw_bytes", len(calls) > 1 and calls[1]["raw"] is None, calls[1:2])

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
