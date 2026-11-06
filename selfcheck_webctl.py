# -*- coding: utf-8 -*-
"""webctl 与默认场景解析的自检（不需要硬件）。

为什么单独写这个：4.8 交付时我漏测了"不传 scene"这条路径，
而它恰好是 AI 最自然的用法（`serial_web({"action":"open"})`）。
上线后发现 `_default_scene()` 把 `list_scenes()` 的参数传少了，直接 TypeError。

这个脚本就是那个洞的补丁：把"不传 scene"和"多场景"两条路都钉死。
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


def main():
    from serial_bridge.cli import _default_scene, _paths
    from serial_bridge.errors import BridgeError

    paths = _paths()
    real_scenes = [d for d in os.listdir(paths["protocol_root"])
                   if os.path.isdir(os.path.join(paths["protocol_root"], d))]
    print("using protocol_root=%s（%d 个场景）" % (paths["protocol_root"], len(real_scenes)))

    print("\n[1] 真实环境：多场景时必须给出可操作的错误，而不是崩")
    try:
        picked = _default_scene(paths)
        check("多场景应拒绝自动选择", len(real_scenes) <= 1,
              "居然选了 %r（场景数 %d）" % (picked, len(real_scenes)))
    except BridgeError as exc:
        check("kind=config", exc.kind == "config", exc.kind)
        joined = " ".join(exc.hint or [])
        check("提示里列出可用场景", all(name in joined for name in real_scenes), exc.hint)
        check("不是 internal 崩", exc.kind != "internal", exc.kind)

    root = tempfile.mkdtemp(prefix="webctl-")
    try:
        print("\n[2] 只有一个场景时，默认就该选它")
        single = os.path.join(root, "single", "protocols")
        os.makedirs(os.path.join(single, "唯一场景"))
        with open(os.path.join(root, "single", "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        fake_paths = {
            "protocol_root": single,
            "common_config": os.path.join(root, "single", "config.json"),
        }
        check("单场景自动选中", _default_scene(fake_paths) == "唯一场景",
              _default_scene(fake_paths))

        print("\n[3] 一个场景都没有时，报 scene_missing")
        empty = os.path.join(root, "empty", "protocols")
        os.makedirs(empty)
        with open(os.path.join(root, "empty", "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        try:
            _default_scene({"protocol_root": empty,
                            "common_config": os.path.join(root, "empty", "config.json")})
            check("空目录应报错", False, "没抛异常")
        except BridgeError as exc:
            check("kind=scene_missing", exc.kind == "scene_missing", exc.kind)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n[4] webctl 子命令存在且参数齐全")
    from serial_bridge.cli import build_parser
    parser = build_parser()
    try:
        args = parser.parse_args(["webctl", "--action", "status"])
        check("可以省略 --scene", args.scene == "", args.scene)
        check("action 默认值可用", args.action == "status", args.action)
        check("mode 默认 light", args.mode == "light", args.mode)
    except SystemExit:
        check("webctl 参数可解析", False, "argparse 退出")

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
