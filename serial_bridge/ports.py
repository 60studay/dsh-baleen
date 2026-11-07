# -*- coding: utf-8 -*-
"""串口扫描与自动匹配（v2 交接文档 3.1）。

匹配顺序是硬性要求：
  match_vid_pid → match（描述模糊） → com（精确号）
并且**永远先扫描**：配置里写了 com 也不能跳过扫描，因为 COM 号会变。

自动纠正：配置写 COM7，但按 match 找到 CH340 在 COM9 → 用 COM9 并注明。

注意：本模块不认识任何具体设备。CH340 / CP210x 之类只出现在注释里当例子，
真正的规则来自场景配置的 match / match_vid_pid 字段。
"""

from .errors import BridgeError, port_label

# 网页波特率下拉框的候选值。放在这里（而不是 supervisor）是因为
# web_handler（路由层）也要用，而路由层不该反过来依赖业务层。
BAUD_CHOICES = [9600, 19200, 38400, 57600, 74880, 115200, 230400, 250000, 460800, 921600]


def _load_serial():
    try:
        import serial  # noqa: F401
        from serial.tools import list_ports
        return serial, list_ports
    except ImportError:
        raise BridgeError(
            "config",
            "pyserial 没有加载成功。",
            hint=[
                "插件自带的 pyserial 在 vendor/pyserial-3.5",
                "若手工运行 bridge.py，请设置 PYTHONPATH 指向该目录，或 pip install pyserial",
            ],
        )


def scan_ports():
    """返回当前所有串口的结构化列表（device / description / vid_pid / serial_number）。"""
    _serial, list_ports = _load_serial()
    result = []
    for port in list_ports.comports():
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        vid_pid = ""
        if vid is not None and pid is not None:
            vid_pid = "%04X:%04X" % (vid, pid)
        result.append({
            "device": getattr(port, "device", "") or "",
            "description": getattr(port, "description", "") or "",
            "vid_pid": vid_pid,
            "serial_number": getattr(port, "serial_number", "") or "",
        })
    result.sort(key=lambda item: _port_sort_key(item["device"]))
    return result


def _port_sort_key(device):
    """COM10 排在 COM9 后面，而不是字典序的 COM10 < COM9。"""
    digits = "".join(ch for ch in device if ch.isdigit())
    prefix = "".join(ch for ch in device if not ch.isdigit())
    return (prefix, int(digits) if digits else 0, device)


def _normalize_vid_pid(value):
    text = str(value or "").strip().upper().replace("0X", "")
    if ":" in text:
        left, right = text.split(":", 1)
        return "%s:%s" % (left.strip(), right.strip())
    return text


def _match_by_vid_pid(ports, wanted):
    target = _normalize_vid_pid(wanted)
    if not target:
        return []
    return [p for p in ports if _normalize_vid_pid(p.get("vid_pid")) == target]


def _match_by_description(ports, wanted):
    needle = str(wanted or "").strip().lower()
    if not needle:
        return []
    hits = []
    for port in ports:
        haystack = " ".join([
            port.get("description", ""),
            port.get("device", ""),
            port.get("serial_number", ""),
        ]).lower()
        if needle in haystack:
            hits.append(port)
    return hits


def _match_by_device(ports, wanted):
    target = str(wanted or "").strip().lower()
    if not target:
        return []
    return [p for p in ports if p.get("device", "").lower() == target]


def _available_hint(ports):
    if not ports:
        return ["当前没有检测到任何串口设备"]
    return ["当前可用串口：" + "、".join(port_label(p)["device"] for p in ports)]


def _match_by_serial(ports, wanted):
    target = str(wanted or "").strip().lower()
    if not target:
        return []
    return [p for p in ports
            if str(p.get("serial_number") or "").strip().lower() == target]


def real_ports(ports):
    """只留**真实的 USB 串口**，把蓝牙/虚拟串口挡在外面。

    为什么必须挡：Windows 上一对蓝牙虚拟口是没有 VID:PID 的，而且一插就两三个。
    旧代码用 `len(ports) == 1` 做"只有一个设备就自动纠正"，
    用户机器上有 7 个口（6 个蓝牙）→ 这个条件**永远不成立** → 换 USB 口后彻底失效。

    判据：有 VID:PID 才算真设备；描述里带蓝牙/Bluetooth 的一律排除。
    """
    out = []
    for port in ports:
        if not str(port.get("vid_pid") or "").strip():
            continue
        desc = str(port.get("description") or "").lower()
        if "蓝牙" in desc or "bluetooth" in desc:
            continue
        out.append(port)
    return out


def _guess(port, source):
    return {
        "device": port["device"],
        "description": port.get("description", ""),
        "vid_pid": port.get("vid_pid", ""),
        "source": source,
        "note": "",
    }


def guess_port(cfg, ports=None):
    """配置解析不出来时**真的去猜**该用哪个口；猜不出来返回 None。

    这是按钮叫「AI 自动选择」该有的行为：配置写死 COM7、设备却被 Windows
    换成了 COM10 时，用户期待它去找，而不是把候选列在错误信息里然后不动。

    顺序：match_serial（芯片唯一）→ match_vid_pid → match → 只剩一个真实 USB 串口。
    **只在真实 USB 串口里猜**，蓝牙虚拟口不参与（见 real_ports）。
    """
    ports = scan_ports() if ports is None else ports
    real = real_ports(ports)

    hits = _match_by_serial(real, cfg.get("match_serial"))
    if len(hits) == 1:
        return _guess(hits[0], "match_serial")

    hits = _match_by_vid_pid(real, cfg.get("match_vid_pid"))
    if len(hits) == 1:
        return _guess(hits[0], "match_vid_pid")

    hits = _match_by_description(real, cfg.get("match"))
    if len(hits) == 1:
        return _guess(hits[0], "match")

    if len(real) == 1:
        return _guess(real[0], "only_usb_port")

    return None


def resolve_port(cfg):
    """按 3.1 的顺序解析出真正要用的串口。

    返回 dict：
      device        最终使用的串口（如 COM9）
      description   该串口的描述
      vid_pid
      source        命中的规则：match_vid_pid / match / com / only_port
      note          需要告知用户的变化，例如"已从 COM7 自动切换到 COM9"
    解析不出来时抛 BridgeError（含候选与建议）。
    """
    ports = scan_ports()
    wanted_com = str(cfg.get("com") or "").strip()
    wanted_desc = str(cfg.get("match") or "").strip()
    wanted_vid_pid = str(cfg.get("match_vid_pid") or "").strip()
    wanted_serial = str(cfg.get("match_serial") or "").strip()

    # 1) VID:PID 精确匹配
    if wanted_vid_pid:
        hits = _match_by_vid_pid(ports, wanted_vid_pid)
        if len(hits) == 1:
            return _resolved(hits[0], "match_vid_pid", wanted_com)
        if len(hits) > 1:
            # 同型号有多块（比如两块 CH340）：靠序列号再分一次
            narrowed = _match_by_serial(hits, wanted_serial)
            if len(narrowed) == 1:
                return _resolved(narrowed[0], "match_serial", wanted_com)
            raise _ambiguous(wanted_vid_pid, "match_vid_pid", hits, ports)
        raise BridgeError(
            "no_device",
            "没找到匹配的串口设备（按 VID:PID 匹配 %s）。" % wanted_vid_pid,
            hint=[
                "USB 是否插好",
                "设备管理器里有没有黄色感叹号",
                "是否需要装驱动",
            ] + _available_hint(ports),
            candidates=[port_label(p) for p in ports],
        )

    # 1b) 序列号精确匹配：同型号多块板子时唯一能分清的判据（换 USB 口也不变）
    if wanted_serial:
        hits = _match_by_serial(ports, wanted_serial)
        if len(hits) == 1:
            return _resolved(hits[0], "match_serial", wanted_com)
        if len(hits) > 1:
            raise _ambiguous(wanted_serial, "match_serial", hits, ports)
        raise BridgeError(
            "no_device",
            "没找到序列号为 %s 的串口设备。" % wanted_serial,
            hint=["USB 是否插好", "用 serial_list 看 serial_number 字段"]
                 + _available_hint(ports),
            candidates=[port_label(p) for p in ports],
        )

    # 2) 描述模糊匹配
    if wanted_desc:
        hits = _match_by_description(ports, wanted_desc)
        if len(hits) == 1:
            return _resolved(hits[0], "match", wanted_com)
        if len(hits) > 1:
            raise _ambiguous(wanted_desc, "match", hits, ports)
        raise BridgeError(
            "no_device",
            "没找到匹配的串口设备（按描述匹配 %s）。" % wanted_desc,
            hint=[
                "USB 是否插好",
                "设备管理器里有没有黄色感叹号",
                "是否需要装驱动",
            ] + _available_hint(ports),
            candidates=[port_label(p) for p in ports],
        )

    # 3) 精确 COM 号
    if wanted_com:
        hits = _match_by_device(ports, wanted_com)
        if hits:
            return _resolved(hits[0], "com", wanted_com)
        # 配置的号不在了 —— 拔插 USB 后 Windows 换号是常态。
        # 先看"真实 USB 串口"里是不是只剩一个（蓝牙虚拟口不算，否则永远不成立）。
        real = real_ports(ports)
        if len(real) == 1:
            resolved = _resolved(real[0], "only_usb_port", wanted_com)
            resolved["note"] = "%s 已不在（拔插后 Windows 会换号），当前唯一的 USB 串口是 %s（%s），已自动切换" % (
                wanted_com, real[0]["device"], real[0].get("description") or "无描述")
            return resolved
        if len(ports) == 1:
            resolved = _resolved(ports[0], "only_port", wanted_com)
            resolved["note"] = "%s 已不在，当前只有 %s（%s），已自动切换" % (
                wanted_com, ports[0]["device"], ports[0].get("description") or "无描述")
            return resolved
        raise BridgeError(
            "no_device",
            "%s 打不开：这个串口号当前不存在。" % wanted_com,
            hint=[
                "USB 是否插好（拔插后 Windows 可能换号）",
                "点「AI 自动选择」让它按 VID:PID / 序列号重新找",
                "或者在侧边栏手动选一个串口",
            ] + _available_hint(ports),
            candidates=[port_label(p) for p in ports],
        )

    # 4) 什么都没配
    real = real_ports(ports)
    if len(real) == 1:
        resolved = _resolved(real[0], "only_usb_port", "")
        resolved["note"] = "场景没配置串口，当前唯一的 USB 串口是 %s，已默认使用" % real[0]["device"]
        return resolved
    if len(ports) == 1:
        resolved = _resolved(ports[0], "only_port", "")
        resolved["note"] = "场景没配置串口，当前只有 %s，已默认使用" % ports[0]["device"]
        return resolved
    raise BridgeError(
        "config",
        "场景配置里没有指定串口（com / match / match_vid_pid / match_serial 都是空的）。",
        hint=["在该场景的 config.json 里填 \"com\" 或 \"match\" 或 \"match_serial\"",
              "用 serial_list 查看当前可用串口"] + _available_hint(ports),
        candidates=[port_label(p) for p in ports],
    )


def _resolved(port, source, wanted_com):
    device = port["device"]
    note = ""
    if source == "match" and wanted_com and wanted_com.lower() != device.lower():
        desc = port.get("description") or "设备"
        note = "%s 已从 %s 变为 %s，已自动切换" % (desc, wanted_com, device)
    return {
        "device": device,
        "description": port.get("description", ""),
        "vid_pid": port.get("vid_pid", ""),
        "source": source,
        "note": note,
    }


def _ambiguous(value, field, hits, ports):
    return BridgeError(
        "ambiguous_device",
        "按 %s=%s 匹配到 %d 个串口，无法确定用哪个。" % (field, value, len(hits)),
        hint=[
            "在场景 config.json 里把 \"com\" 写成其中一个具体串口号，或改用更精确的 match_vid_pid",
        ] + _available_hint(ports),
        candidates=[port_label(p) for p in hits],
    )
