# -*- coding: utf-8 -*-
"""场景配置加载：公共 config.json + 场景 config.json 合并。

合并规则与 v1 一致（场景覆盖公共），但 **watch 块做深合并**：
场景只想改一个 interval 时，不必把整块重抄一遍。
"""

import json
import os

from .errors import BridgeError


def _strip_bom(text):
    return text[1:] if text and text[0] == "\ufeff" else text


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.loads(_strip_bom(handle.read()))
    except FileNotFoundError:
        return default
    except ValueError as exc:
        raise BridgeError(
            "config",
            "配置文件不是合法 JSON：%s" % path,
            hint=["检查逗号、引号、注释（JSON 不支持注释）"],
            detail=str(exc),
        )


def deep_merge(base, override):
    """只对 dict 做递归合并；其余类型直接覆盖。"""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Scene(object):
    def __init__(self, name, directory, cfg):
        self.name = name
        self.dir = directory
        self.cfg = cfg

    @property
    def protocol_path(self):
        return os.path.join(self.dir, "protocol.md")

    def protocol_text(self):
        try:
            with open(self.protocol_path, encoding="utf-8-sig") as handle:
                return _strip_bom(handle.read()).strip()
        except OSError:
            return "(protocol.md 不存在，可以在这个场景目录里补一份)"

    def watch_cfg(self):
        return self.cfg.get("watch") or {}


def load_scene(common_path, protocol_root, name):
    common = read_json(common_path, {}) or {}
    safe = str(name or "").strip()
    if not safe or "/" in safe or "\\" in safe or safe in (".", ".."):
        raise BridgeError("scene_missing", "场景名不合法：%r" % name)
    scene_dir = os.path.join(protocol_root, safe)
    if not os.path.isdir(scene_dir):
        raise BridgeError(
            "scene_missing",
            "场景不存在：%s" % safe,
            hint=["可用的场景见 serial_list 的 scenes 字段"],
        )
    scene_cfg = read_json(os.path.join(scene_dir, "config.json"), {}) or {}
    merged = deep_merge(common, scene_cfg)
    return Scene(safe, scene_dir, merged)


def list_scenes(common_path, protocol_root):
    common = read_json(common_path, {}) or {}
    try:
        entries = sorted(os.listdir(protocol_root))
    except OSError:
        return []
    scenes = []
    for entry in entries:
        directory = os.path.join(protocol_root, entry)
        if not os.path.isdir(directory):
            continue
        scene_cfg = read_json(os.path.join(directory, "config.json"), {}) or {}
        merged = deep_merge(common, scene_cfg)
        watch = merged.get("watch") or {}
        scenes.append({
            "name": entry,
            "com": str(merged.get("com") or ""),
            "match": str(merged.get("match") or ""),
            "match_vid_pid": str(merged.get("match_vid_pid") or ""),
            "baud": int(merged.get("baud") or 0),
            "newline": str(merged.get("newline") if merged.get("newline") is not None else "\\r\\n"),
            "has_protocol": os.path.isfile(os.path.join(directory, "protocol.md")),
            "watch": {
                "send_enabled": bool(watch.get("send_enabled")),
                "check_enabled": bool(watch.get("check_enabled")),
                "rules": len(watch.get("rules") or []),
                "auto_commands": len(watch.get("allowed_auto_commands") or []),
            },
        })
    return scenes
