# -*- coding: utf-8 -*-
"""结构化错误：任何设备/串口问题都在这一层翻译成可读中文，绝不把 Python 堆栈抛给插件层。

设计原则（v2 交接文档 3.2）：
- 三类故障分开报：设备未连接 / 端口被占用 / 设备无响应
- 每类都带"下一步怎么办"和当前可用串口清单
- 插件层（index.js）拿到的永远是干净的中文消息
"""


class BridgeError(Exception):
    """带分类、可读提示、可选候选列表的桥错误。

    kind 取值（供插件层/前端做程序化判断，不是给人看的）：
      no_device          扫描后没有匹配的设备
      ambiguous_device   匹配到多个设备，需要用户指定
      port_busy          端口被别的程序占用
      no_response        打开了、发了，但设备没有回显
      scene_missing      场景不存在
      config             配置本身有问题
      control_blocked    控制权状态不允许这次发送
      allowlist_blocked  自动命令不在白名单内
      internal           其余未归类的内部错误
    """

    def __init__(self, kind, message, hint=None, candidates=None, detail=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint or []
        self.candidates = candidates or []
        self.detail = detail

    def to_dict(self):
        data = {"kind": self.kind, "message": self.message}
        if self.hint:
            data["hint"] = list(self.hint)
        if self.candidates:
            data["candidates"] = list(self.candidates)
        if self.detail:
            data["detail"] = self.detail
        return data

    def render(self):
        """给人看的多行文本：标题 + 请检查项 + 候选。"""
        lines = [self.message]
        if self.hint:
            lines.append("请检查：")
            for i, item in enumerate(self.hint, 1):
                lines.append("%d. %s" % (i, item))
        if self.candidates:
            lines.append("候选设备：")
            for item in self.candidates:
                lines.append("  " + _format_candidate(item))
        return "\n".join(lines)


def _format_candidate(item):
    if isinstance(item, str):
        return item
    device = item.get("device") or "?"
    desc = item.get("description") or ""
    vid_pid = item.get("vid_pid") or ""
    extra = " ".join(part for part in (desc, vid_pid) if part)
    return "%s%s" % (device, ("  " + extra) if extra else "")


def port_label(port):
    """把扫描到的串口信息压成候选条目（结构统一，前端/模型都好读）。"""
    return {
        "device": port.get("device", ""),
        "description": port.get("description", ""),
        "vid_pid": port.get("vid_pid", ""),
        "serial_number": port.get("serial_number", ""),
    }
