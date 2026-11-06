# -*- coding: utf-8 -*-
"""传输层：所有"怎么把字节送出去"的实现都放这儿。

为什么要有这一层：现在的业务代码（transact / drain / 监视循环）只关心
"发一串字节、收一串字节"，不该知道底层是串口、TCP、CAN 还是文件。
以前这些逻辑硬写在 io.py 里，加一种新传输方式就得改 io.py 本身。

约定：
- 业务层只调 Transport 的 open/write/read/close/describe
- 具体实现自己负责连接建立与错误分类（错误仍用 BridgeError 的 kind）
- `describe()` 给 UI 用：显示"串口 COM10 @115200"这种人类可读信息

新增一种传输方式 = 写一个子类 + `register_transport("tcp", TcpTransport)`，
不需要改 io.py / supervisor.py。
"""

from .errors import BridgeError


class Transport(object):
    """传输方式统一接口。

    业务层用到的只有这几个方法；子类可以多提供（比如 SerialTransport 还兼容
    pyserial 的 reset_input_buffer / in_waiting），但业务层不该依赖那些。
    """

    kind = "base"

    def __init__(self, cfg=None, device=None, baud=None):
        self.cfg = cfg or {}
        self.device = device
        self.baud = baud

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def open(self):
        """建立连接。返回 self，方便链式调用。失败时抛 BridgeError。"""
        raise NotImplementedError

    def close(self):
        """释放连接。实现必须幂等：没打开时调用也不能报错。"""
        raise NotImplementedError

    def is_open(self):
        return False

    # ── 收发 ────────────────────────────────────────────────────────────────
    def write(self, data):
        """原样写出 bytes。"""
        raise NotImplementedError

    def read(self, size=256):
        """读最多 size 字节。**没有数据就返回 b""**，不要阻塞太久。"""
        raise NotImplementedError

    def reset_input_buffer(self):
        """丢弃尚未读走的接收数据（发新命令前清干净，避免把上次的回显算进来）。

        默认无操作：不是所有传输方式都有"缓冲区"这个概念。
        """
        return None

    # ── 展示 ────────────────────────────────────────────────────────────────
    def describe(self):
        """给 UI/日志用的描述，必须包含 type 与地址。"""
        return {"type": self.kind, "target": self.device or "", "status": "closed"}

    # ── 便捷 ────────────────────────────────────────────────────────────────
    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class SerialTransport(Transport):
    """串口传输（pyserial）。逻辑从 io.py 原样搬过来，行为不变。"""

    kind = "serial"

    def __init__(self, cfg=None, device=None, baud=None):
        Transport.__init__(self, cfg, device, baud)
        self._port = None
        self._description = ""

    # ── 打开 ────────────────────────────────────────────────────────────────
    def open(self):
        serial = _load_serial()
        device = self.device
        baud_rate = int(self.baud or self.cfg.get("baud") or 115200)
        try:
            port = serial.Serial(
                port=device,
                baudrate=baud_rate,
                bytesize=int(self.cfg.get("bytesize", 8)),
                parity=self.cfg.get("parity", "N"),
                stopbits=float(self.cfg.get("stopbits", 1)),
                timeout=0.02,
            )
        except Exception as exc:  # pyserial 的异常类型在 Windows 上五花八门
            raise _classify_open_error(device, exc)

        # 很多开发板（ESP32 / Arduino / STM32 最小系统）把 DTR/RTS 接到复位或 BOOT 脚，
        # 打开串口时这两根线默认拉高会误触发复位。这是通用问题，不是某块板专用。
        # 必须在 open() 之后赋值：构造参数里传 dtr=False 无效（实测打开后仍是 True）。
        try:
            port.dtr = False
            port.rts = False
        except Exception:
            pass

        self._port = port
        self.baud = baud_rate
        return self

    # ── 关闭 ────────────────────────────────────────────────────────────────
    def close(self):
        port, self._port = self._port, None
        if port is None:
            return
        try:
            port.close()
        except Exception:
            pass

    def is_open(self):
        return self._port is not None

    # ── 收发 ────────────────────────────────────────────────────────────────
    def write(self, data):
        if self._port is None:
            self.open()
        return self._port.write(data)

    def read(self, size=256):
        if self._port is None:
            return b""
        return self._port.read(size)

    # 业务层偶尔直接用的 pyserial 语义，这里一并代理，避免调用方再摸到底层对象
    def reset_input_buffer(self):
        if self._port is not None:
            self._port.reset_input_buffer()

    @property
    def in_waiting(self):
        if self._port is None:
            return 0
        return int(getattr(self._port, "in_waiting", 0) or 0)

    # ── 展示 ────────────────────────────────────────────────────────────────
    def describe(self):
        return {
            "type": "serial",
            "device": self.device or "",
            "baud": int(self.baud or self.cfg.get("baud") or 115200),
            "frame": "%s%s%s" % (int(self.cfg.get("bytesize", 8)),
                                 self.cfg.get("parity", "N"),
                                 int(float(self.cfg.get("stopbits", 1)))),
            "status": "open" if self.is_open() else "closed",
        }


def _load_serial():
    try:
        import serial
        return serial
    except ImportError:
        raise BridgeError(
            "config",
            "pyserial 没有加载成功。",
            hint=[
                "插件自带的 pyserial 在 vendor/pyserial-3.5",
                "若手工运行，请设置 PYTHONPATH 指向该目录",
            ],
        )


def _classify_open_error(device, exc):
    """把 pyserial 五花八门的异常归成三类结构化错误（保持原有文案）。"""
    message = str(exc)
    lowered = message.lower()
    busy_markers = ("access is denied", "permissionerror", "拒绝访问", "being used",
                    "cannot open", "could not open")
    if any(marker in lowered for marker in busy_markers):
        return BridgeError(
            "port_busy",
            "%s 被其他程序占用。" % device,
            hint=[
                "串口助手 / 其他串口工具是否开着",
                "网页监视模式（watch 模式）是否正在运行",
                "上一次的 bridge.py 是否还没退出",
            ],
            detail=message,
        )
    if "filenotfound" in lowered or "系统找不到" in message or "找不到" in message:
        return BridgeError(
            "no_device",
            "%s 打不开：系统里没有这个串口。" % device,
            hint=["USB 是否被拔掉", "拔插后串口号可能已变化，重新扫描一次"],
            detail=message,
        )
    return BridgeError(
        "internal",
        "打开 %s 失败。" % device,
        hint=["换一个 USB 口试试", "确认驱动装好"],
        detail=message,
    )


# ── 注册表：加新传输方式只改这里调用一次，不改业务代码 ────────────────────────
TRANSPORTS = {}


def register_transport(kind, cls):
    """注册一种传输方式。重复注册会覆盖（方便测试替换）。"""
    TRANSPORTS[str(kind).lower()] = cls


def create_transport(cfg=None, device=None, baud=None):
    """按场景配置的 `transport` 字段创建传输对象。

    没有该字段时默认 "serial" —— 老场景配置不用改。
    """
    cfg = cfg or {}
    kind = str(cfg.get("transport") or "serial").strip().lower()
    cls = TRANSPORTS.get(kind)
    if cls is None:
        raise BridgeError(
            "config",
            "不支持的传输方式：%r" % kind,
            hint=["当前可用：%s" % "、".join(sorted(TRANSPORTS)) or "（空）",
                  "场景 config.json 里加 \"transport\": \"serial\" 走默认串口"],
        )
    return cls(cfg, device, baud)


def describe_transport(cfg, device=None, baud=None):
    """不开连接也能给出描述（给 /status 用，避免探测时占口）。"""
    cfg = cfg or {}
    kind = str(cfg.get("transport") or "serial").strip().lower()
    if kind == "serial":
        return {
            "type": "serial",
            "device": device or "",
            "baud": int(baud or cfg.get("baud") or 115200),
            "frame": "%s%s%s" % (int(cfg.get("bytesize", 8)),
                                 cfg.get("parity", "N"),
                                 int(float(cfg.get("stopbits", 1)))),
            "status": "configured",
        }
    return {"type": kind, "device": device or "", "status": "configured"}


register_transport("serial", SerialTransport)
