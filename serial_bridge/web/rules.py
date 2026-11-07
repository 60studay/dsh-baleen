"""规则读写（get_rules / set_rules）

规则存在场景 config.json 里；运行期开关存在状态文件里，两者不能混。

这里用的是 mixin：方法体拿的还是 self，所以 self.state / self.loop /
self.ports 和拆之前完全一样，行为零变化。
"""

import os
import time

from ..channel import run_evidence
from ..config import load_scene
from ..errors import BridgeError
from ..ports import guess_port, resolve_port, scan_ports
from ..rules import CHECK_SOURCES, RULE_TYPES, merge_watch, normalize_check_source


class RulesMixin(object):

    # ── 规则编辑（写回场景 config.json）─────────────────────────────────────
    def get_rules(self):
        scene = load_scene(self.paths["common_config"], self.paths["protocol_root"],
                           self.scene_name)
        watch = scene.watch_cfg()
        return {
            "ok": True,
            "rules": watch.get("rules") or [],
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
            "rule_types": list(RULE_TYPES),
            "config_path": os.path.join(scene.dir, "config.json"),
        }


    def set_rules(self, body):
        """整体替换规则的列表。

        只动场景 config.json 里的 watch.rules / allowed_auto_commands，
        其它字段原样保留（包括用户手写的注释性键）。
        """
        rules = (body or {}).get("rules")
        if rules is None:
            raise BridgeError("config", "缺少 rules 字段",
                              hint=['请求体示例：{"rules": [{"name":"...","when":{...},"then":{...}}]}'])
        if not isinstance(rules, list):
            raise BridgeError("config", "rules 必须是数组")

        cleaned = []
        for index, rule in enumerate(rules, 1):
            if not isinstance(rule, dict):
                raise BridgeError("config", "第 %d 条规则不是对象" % index)
            name = str(rule.get("name") or "").strip()
            if not name:
                raise BridgeError("config", "第 %d 条规则缺少 name" % index)
            when = rule.get("when") or {}
            wtype = str(when.get("type") or "").strip()
            if wtype not in RULE_TYPES:
                raise BridgeError(
                    "config",
                    "规则「%s」的判据类型 %r 不支持。" % (name, wtype),
                    hint=["只能是：" + " / ".join(RULE_TYPES)])
            entry = {"name": name, "when": dict(when)}
            if wtype in ("contains", "equals") and not str(when.get("value") or ""):
                raise BridgeError("config", "规则「%s」的 %s 需要 value" % (name, wtype))
            if wtype in ("regex", "hex_regex") and not str(when.get("pattern") or when.get("value") or ""):
                raise BridgeError("config", "规则「%s」的 %s 需要 pattern" % (name, wtype))
            if wtype == "hex" and not str(when.get("value") or ""):
                raise BridgeError("config", "规则「%s」的 hex 需要 value，例如 \"AA 55 FF\"" % name)
            if wtype == "regex":
                # 写进配置前先编译一次，别把坏正则存盘
                try:
                    import re as _re
                    _re.compile(str(when.get("pattern") or when.get("value")))
                except Exception as exc:
                    raise BridgeError("config", "规则「%s」的正则不合法：%s" % (name, exc))
            if wtype == "hex_regex":
                # 字节正则的边界：只能匹配 0-255 的字节，所以模式必须能编码成 latin-1
                pattern = str(when.get("pattern") or when.get("value"))
                try:
                    import re as _re
                    _re.compile(pattern.encode("latin-1"))
                except UnicodeEncodeError:
                    raise BridgeError(
                        "config",
                        "规则「%s」的字节正则只能包含 0-255 的字符（建议写成 \\xff）" % name)
                except Exception as exc:
                    raise BridgeError("config", "规则「%s」的正则不合法：%s" % (name, exc))
            if wtype == "no_response":
                try:
                    entry["when"]["consecutive"] = max(1, int(when.get("consecutive") or 3))
                except (TypeError, ValueError):
                    raise BridgeError("config", "规则「%s」的 consecutive 必须是整数" % name)
            then = rule.get("then") or {}
            if then:
                entry["then"] = dict(then)
            if "notify" in rule:
                entry["notify"] = bool(rule.get("notify"))
            cleaned.append(entry)

        allowed = (body or {}).get("allowed_auto_commands")
        scene = load_scene(self.paths["common_config"], self.paths["protocol_root"],
                           self.scene_name)
        config_path = os.path.join(scene.dir, "config.json")

        # 读原始文件（保留用户已有的其它键），只替换 watch 块里这两项
        try:
            with open(config_path, encoding="utf-8-sig") as handle:
                raw = json.loads(handle.read() or "{}")
        except FileNotFoundError:
            raw = {}
        except ValueError as exc:
            raise BridgeError("config", "场景 config.json 不是合法 JSON，未写入。",
                              detail=str(exc))
        watch = dict(raw.get("watch") or {})
        watch["rules"] = cleaned
        if allowed is not None:
            if not isinstance(allowed, list):
                raise BridgeError("config", "allowed_auto_commands 必须是数组")
            watch["allowed_auto_commands"] = [str(x).strip() for x in allowed if str(x).strip()]
        raw["watch"] = watch

        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, config_path)

        # 场景对象缓存的是启动时的配置，改完必须**原地**重读：
        # WatchLoop / RuleChecker 持有的是同一个 Scene 对象，
        # `self.scene = load_scene(...)` 只换掉了 WebServer 这一份，
        # 判定线程还在用旧的规则 → 用户遇到的"改完规则要重启网页才生效"，
        # 以及"告警里写的是上一个规则的名字"（旧规则的 then 也没有 send）。
        self.scene.reload_if_changed(force=True)
        return {
            "ok": True,
            "rules": cleaned,
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
            "config_path": config_path,
        }
