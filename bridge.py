#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鲸须（dsh-baleen）启动器（插件层的唯一入口）。

为什么需要这一层：内核是一个包（serial_bridge/），用相对导入组织，
所以不能直接 `python serial_bridge/cli.py`。这里把插件根目录放进 sys.path
再调用 cli.main()，命令行用法与旧版 bridge.py 保持一致。

用法：
    python bridge.py scan
    python bridge.py info  --scene <场景>
    python bridge.py send  --scene <场景> --command "<指令>"
    python bridge.py read  --scene <场景> --lines 20
    python bridge.py status --scene <场景>
    python bridge.py watch --scene <场景> --action start --set check_enabled=true
    python bridge.py web   --scene <场景> [--port 8898] [--mode light|watch]
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from serial_bridge.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
