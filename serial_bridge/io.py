# -*- coding: utf-8 -*-
"""串口收发核心。

保持 v1 的两个好设计：
- **不常驻、不占口**：每次调用临时开连接 → 收发 → 关闭
- **回包节奏**：先等首字节（设备可能要想一会儿才回）→ 再等线路安静

v2 新增：
- 三类故障分开报（未连接 / 被占用 / 无响应），见 errors.py
- 收发统一返回原始 bytes，hex 由上层渲染（3.4 要求"不要后端先解码再转，会丢数据"）

v3 起底层改成 Transport：这里只保留"业务级收发"（发一条、按节奏收一段），
具体是串口还是 TCP 由 transport.py 决定，本文件不 import pyserial。
"""

import time

from .errors import BridgeError
from .ports import resolve_port
from .transport import create_transport


def decode_newline(value, default="\\r\\n"):
    """把配置里的 "\\r\\n" 这类转义串变成真正的字节。"""
    text = value if isinstance(value, str) else default
    try:
        return text.encode("utf-8").decode("unicode_escape").encode("latin1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return b"\r\n"


def open_port(cfg, device, baud=None):
    """打开连接并返回 Transport 对象。

    返回的对象仍然是"鸭子类型"的串口（write/read/close 都一样），
    所以老调用方（supervisor 的常驻句柄）不用改一行。
    具体用哪种传输方式由 cfg["transport"] 决定，默认串口。
    """
    return create_transport(cfg, device, baud).open()


def _open_with_retry(cfg, device, baud=None, attempts=3, gap_s=0.25):
    """打开连接，遇到"被占用"时短暂重试。

    为什么需要：同一个口可能被另一个客户端瞬间占用——最常见的是网页的
    定时发送线程刚好在开关口，或者用户的串口助手刚点开一下。
    这种瞬时冲突重试几次就过去了，直接报错反而像"插件坏了"。
    真被长期占用时三次都失败再报，文案仍是"被占用 + 该关掉谁"。
    """
    last = None
    for index in range(max(1, attempts)):
        try:
            return open_port(cfg, device, baud)
        except BridgeError as exc:
            if exc.kind != "port_busy":
                raise
            last = exc
            if index < attempts - 1:
                time.sleep(gap_s)
    raise last


def transact(cfg, device, payload, baud=None, raw_bytes=None):
    """发一条命令并读回显。

    raw_bytes 不为 None 时**原样发送这些字节**（十六进制模式、二进制协议），
    不再把 payload 当文本做 utf-8 编码。没有它就只能发文本，hex 模式会退化成
    把 "AA 55" 这 5 个 ASCII 字符发出去（Bug 11）。

    返回 dict：
      tx           原始发送文本（给人看的那个）
      rx_bytes     原始回显 bytes
      rx_text      UTF-8 解码（errors=replace），给"文字模式"
      rx_hex       空格分隔的十六进制，给"十六进制模式"
      bytes        回显字节数
      elapsed_ms   本次耗时
      empty        是否没收到任何字节

    读不到任何字节不算异常（很多命令本来就不回应），由上层决定是否报
    "设备无响应"——所以这里只回报事实。
    """
    first_byte_ms = int(cfg.get("wait_first_byte_ms", 1500))
    quiet_ms = int(cfg.get("quiet_ms", 200))
    gap_ms = int(cfg.get("timeout_read_ms", 500))
    newline = decode_newline(cfg.get("newline"), "\\r\\n")

    started = time.time()
    ser = _open_with_retry(cfg, device, baud)
    try:
        ser.reset_input_buffer()
        if raw_bytes is not None:
            # 原始字节模式：帧由调用方给全，**不自动补换行**。
            # 二进制协议（Modbus 等）的帧长度是定死的，多补 2 字节会直接破坏帧；
            # 需要 CRLF 就在 hex 里写明 "0D 0A"。
            ser.write(raw_bytes)
        else:
            ser.write(str(payload).encode("utf-8") + newline)
        out = b""
        t0 = time.time()
        last_byte = None
        while True:
            chunk = ser.read(256)
            now = time.time()
            if chunk:
                out += chunk
                last_byte = now
                continue
            if last_byte is None:
                if (now - t0) * 1000 >= first_byte_ms:
                    break
            elif (now - last_byte) * 1000 >= quiet_ms:
                break
            if (now - t0) * 1000 >= first_byte_ms + gap_ms:
                break
            time.sleep(0.01)
        return {
            "tx": str(payload),
            "rx_bytes": out,
            "rx_text": out.decode("utf-8", errors="replace"),
            "rx_hex": " ".join("%02X" % byte for byte in out),
            "bytes": len(out),
            "elapsed_ms": int((time.time() - started) * 1000),
            "empty": len(out) == 0,
        }
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(
            "internal",
            "读写 %s 时出错。" % device,
            hint=["设备可能被拔掉", "换条 USB 线试试"],
            detail=str(exc),
        )
    finally:
        try:
            ser.close()
        except Exception:
            pass


def send_only(cfg, device, payload, baud=None, raw_bytes=None):
    """只发不收：写出去就关。

    批量下命令（PID 调参之类连发几十条）时用——不必每条都等设备回话，
    回显之后用 drain() 统一收。
    """
    newline = decode_newline(cfg.get("newline"), "\\r\\n")
    started = time.time()
    ser = _open_with_retry(cfg, device, baud)
    try:
        ser.write(raw_bytes if raw_bytes is not None
                  else str(payload).encode("utf-8") + newline)
        submit_ms = int((time.time() - started) * 1000)
    except Exception as exc:
        raise BridgeError("internal", "向 %s 写入失败。" % device,
                          hint=["设备可能被拔掉"], detail=str(exc))
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return {"submit_ms": submit_ms, "bytes": 0, "empty": True, "submitted": True}


def drain(cfg, device, duration_ms=400, baud=None):
    """只读不发：短暂开一次口，尽可能收干净。

    网页"轻模式"的 /poll 用它——不长期占口，但也能捡到设备主动上报的数据
    （代价是两次 poll 之间的数据仍可能漏，这点在文档里已如实说明）。
    """
    first_byte_ms = int(cfg.get("wait_first_byte_ms", 1500))
    quiet_ms = int(cfg.get("quiet_ms", 200))
    budget_ms = max(int(duration_ms), first_byte_ms)

    started = time.time()
    ser = _open_with_retry(cfg, device, baud)
    try:
        out = b""
        t0 = time.time()
        last_byte = None
        while True:
            chunk = ser.read(256)
            now = time.time()
            if chunk:
                out += chunk
                last_byte = now
                continue
            if last_byte is None:
                if (now - t0) * 1000 >= budget_ms:
                    break
            elif (now - last_byte) * 1000 >= quiet_ms:
                break
            if (now - t0) * 1000 >= budget_ms:
                break
            time.sleep(0.01)
        return {
            "rx_bytes": out,
            "rx_text": out.decode("utf-8", errors="replace"),
            "rx_hex": " ".join("%02X" % byte for byte in out),
            "bytes": len(out),
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    finally:
        try:
            ser.close()
        except Exception:
            pass


def open_and_check(cfg, baud=None):
    """只验证"能不能打开"，立刻关掉。用于状态显示与占用检测。"""
    resolved = resolve_port(cfg)
    ser = open_port(cfg, resolved["device"], baud)
    ser.close()
    return resolved


def describe_no_response(resolved, cfg, baud=None):
    """打开了但没回显 → 生成 3.2 要求的"设备无响应"提示。

    除了通用检查项，还带上场景配置里的 `no_response_hint`：
    不同硬件的"没回显"原因千差万别（例如某些透传模块会卡在自己的命令模式里），
    这类知识属于设备，写在 protocols/<场景>/config.json，代码保持通用。
    """
    hints = [
        "波特率是否正确（当前 %s）" % int(baud or cfg.get("baud") or 115200),
        "设备是否上电",
        "TX/RX 是否接反",
        "命令是否需要换行结尾（当前 newline=%r）" % cfg.get("newline", "\\r\\n"),
    ]
    extra = cfg.get("no_response_hint")
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, (list, tuple)):
        for item in extra:
            text = str(item or "").strip()
            if text:
                hints.append(text)
    return BridgeError(
        "no_response",
        "已连接 %s，但设备无响应。" % resolved["device"],
        hint=hints,
    )
