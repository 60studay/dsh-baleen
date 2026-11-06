# -*- coding: utf-8 -*-
"""鲸须（dsh-baleen）的 Python 内核。

分层（改代码前先看这里）：
    errors.py   结构化错误（三类故障分开报）
    ports.py    扫描 + 自动匹配（match_vid_pid → match → com）
    io.py       收发（不常驻、不占口；返回原始 bytes）
    rules.py    判据引擎（四种判据，超时/限流/熔断隔离）
    state.py    跨进程共享状态（控制权 + 告警）
    config.py   公共+场景配置合并
    cli.py      命令行入口，index.js 调的就是它
    supervisor.py  网页/监视模式进程（轻模式不占口）

**通用性红线**：这个包里不允许出现任何具体设备的命令字（如某底盘的状态查询）、
故障字（如某个项目的报警字样）或字段名。设备知识一律在 protocols/<场景>/ 里。
"""

__all__ = ["errors", "ports", "io", "rules", "state", "config"]
