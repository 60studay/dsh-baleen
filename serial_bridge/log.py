# -*- coding: utf-8 -*-
"""TX/RX 日志。

单独成模块的原因：CLI 和管家（daemon）都要写同一份日志，
如果留在 cli.py 里，daemon 就得 import cli，容易绕成循环依赖。
"""

import os
import time


class Logger(object):
    """按场景写 TX/RX 日志。行格式与 v1 兼容，老的日志文件继续可读。"""

    def __init__(self, log_root, scene):
        self.dir = os.path.join(log_root, scene)
        self.path = os.path.join(self.dir, "log.txt")

    def _ensure(self):
        os.makedirs(self.dir, exist_ok=True)

    def append(self, direction, text):
        self._ensure()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.path, "a", encoding="utf-8") as handle:
            for line in str(text).splitlines() or [""]:
                handle.write("[%s] %s %s\n" % (stamp, direction, line))

    def tail(self, lines):
        try:
            with open(self.path, encoding="utf-8", errors="replace") as handle:
                data = handle.readlines()
        except OSError:
            data = []
        return "".join(data[-max(1, int(lines)):]).rstrip("\n")
