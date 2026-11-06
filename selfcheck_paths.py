# -*- coding: utf-8 -*-
"""路径一致性自检：防止"插件和网页各写一份 state"再次发生（Bug 3）。

背景：控制权、告警、定时开关都存在 log_root/.runtime 下。
Python 侧（cli.py）和 JS 侧（index.js）如果默认值不一致，
就会出现"网页点了[交给AI]，工具却还报 human"——这个 bug 反复出现过两次。

运行：python selfcheck_paths.py
"""
import os
import re
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


def main():
    from serial_bridge.cli import BASE, default_log_root, _paths

    print("\n[1] Python 侧默认值 = <插件目录>/logs")
    expected = os.path.join(BASE, "logs")
    check("default_log_root() 指向插件目录", default_log_root() == expected, default_log_root())

    print("\n[2] index.js 的默认值与 Python 一致")
    index_path = os.path.join(BASE, "index.js")
    with open(index_path, encoding="utf-8") as handle:
        source = handle.read()
    # 只认这一种写法：path.join(HERE, 'logs') / path.join(HERE, "logs")
    match = re.search(r"DEFAULT_LOG_ROOT\s*=\s*[\s\S]{0,200}?path\.join\(HERE,\s*['\"]logs['\"]\)",
                      source)
    check("index.js 用 path.join(HERE, 'logs')", bool(match),
          "没找到，可能被改成了别的目录")
    check("两侧都叫 logs（同名）",
          match is not None and os.path.basename(expected) == "logs")

    print("\n[3] 没有显式配置时，_paths() 回落到同一个值")
    env_key = "SERIAL_BRIDGE_LOG_DIR"
    had_env = os.environ.pop(env_key, None)
    try:
        from serial_bridge.config import read_json
        common = read_json(os.path.join(BASE, "config.json"), {}) or {}
        configured = str(common.get("log_root") or "").strip()
        if configured:
            print("  SKIP config.json 里显式配置了 log_root=%r" % configured)
        else:
            paths = _paths()
            check("_paths().log_root == default_log_root()",
                  paths["log_root"] == default_log_root(), paths["log_root"])
            check("runtime 目录就在 log_root 下",
                  paths["runtime_root"] == os.path.join(paths["log_root"], ".runtime"),
                  paths["runtime_root"])
    finally:
        if had_env is not None:
            os.environ[env_key] = had_env

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
