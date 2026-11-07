"""网页层的公共小工具（拆模块之后才有的，避免互相 import 造成循环依赖）。"""


def parse_hex(text):
    cleaned = str(text).replace(",", " ").replace("0x", " ").replace("0X", " ")
    parts = [p for p in cleaned.split() if p]
    out = bytearray()
    for part in parts:
        try:
            value = int(part, 16)
        except ValueError:
            return None
        if value < 0 or value > 255:
            return None
        out.append(value)
    return bytes(out) if out else None
