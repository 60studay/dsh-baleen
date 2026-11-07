# -*- coding: utf-8 -*-
"""网页 / 监视进程（v2 交接文档 3.3 / 3.4 / 3.5 / 3.6）。

两种模式：
    light（默认）  不长期占口：每个请求临时开口 → 读 → 关。
                   和 AI、和用户的串口助手都能共存。
    watch          占口持续读，才能看到连续数据流和主动上报。

控制权、监视开关都存在共享状态文件里（state.py），所以网页和插件进程看到的是同一份。

退出按钮真的会停服务并释放串口：/shutdown 设置标记 + 关闭 socket。
"""

import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
import urllib.request

from . import io as serial_io
from .config import load_scene
from .errors import BridgeError
from .ports import resolve_port, scan_ports
from .port_manager import PortManager
from .rule_checker import RuleChecker
from .watch_loop import WatchLoop
from .web_handler import BridgeHandler
from .rules import RULE_TYPES
from .runner import RuleWorker
from .state import SharedState, STATE_AI, STATE_HUMAN, STATE_PAUSED, VALID_STATES, _safe_name
from .transport import describe_transport as transport_describe
# BAUD_CHOICES 定义在 ports.py（路由层 web_handler 也要用，不该反过来依赖这里）
from .ports import BAUD_CHOICES  # noqa: F401  （对外仍可 from supervisor import BAUD_CHOICES）

DEFAULT_PORT = 8899



class WebServer(object):
    def __init__(self, paths, scene_name, port=None, mode="light"):
        self.paths = paths
        self.scene_name = scene_name
        self.scene = load_scene(paths["common_config"], paths["protocol_root"], scene_name)
        self.state = SharedState(paths["runtime_root"], scene_name)
        from .cli import Logger  # 延迟导入避免循环
        self.logger = Logger(paths["log_root"], scene_name)
        self.mode = mode if mode in ("light", "watch") else "light"
        self.port = int(port or self.scene.cfg.get("web_port") or DEFAULT_PORT)
        self.httpd = None
        self.loop = WatchLoop(self.scene, paths, self.state, self.logger)
        self.shutdown_event = threading.Event()
        self.override_com = ""
        self.override_baud = 0
        # 端口占用/释放/看门狗都交给 PortManager（它不碰界面，只管"谁该拿口"）
        self.ports = PortManager(
            state=self.state,
            loop=self.loop,
            current_device=self.current_device,
            effective_cfg=self.effective_cfg,
            mode_getter=lambda: self.mode,
            shutdown_event=self.shutdown_event,
            daemon_url=self._daemon_url,
        )
        # 监视模式下"刚手动发过命令"的时间窗，用来把回显来源标成 manual
        self._manual_until = 0.0
        # 定时发送（独立于串口占用/模式，见 _autosend_loop）
        self._autosend_thread = None
        self.autosend_stats = {"sent": 0, "errors": 0, "last_error": "", "last_tx": 0.0}
        # 单实例守卫：同一场景同时只允许一个网页实例，避免两个进程抢串口
        self._instance_file = os.path.join(paths["runtime_root"], "state",
                                           "%s.web.json" % _safe_name(scene_name))
        self._page = _load_page()

    # ── 配置与端口 ──────────────────────────────────────────────────────────
    def effective_cfg(self):
        cfg = dict(self.scene.cfg)
        if self.override_com:
            cfg["com"] = self.override_com
        if self.override_baud:
            cfg["baud"] = self.override_baud
        return cfg

    def current_device(self):
        resolved = resolve_port(self.effective_cfg())
        return resolved

    # ── 串口占用生命周期（与控制权解耦）────────────────────────────────────
    #
    # 关键设计：**控制权决定谁有权发命令，占用决定谁持有句柄**，两者分开。
    #   ai      → 网页需要口来发命令、跑轮询，所以持有
    #   human   → 口要还给用户（他要开串口助手），网页一律不持有
    #   paused  → 谁都不发，口也还回去
    # 轻模式下本来就不常占口，这里主要影响 watch 模式。

    # ── 管家（可选）─────────────────────────────────────────────────────────
    def _daemon_url(self):
        """管家在跑就返回它的地址；缓存 2 秒，避免每次刷新状态都探测。"""
        now = time.time()
        cached = getattr(self, "_daemon_cache", None)
        if cached and now - cached[0] < 2.0:
            return cached[1]
        url = None
        try:
            from . import daemon as daemon_mod
            info = daemon_mod.read_instance(self.paths, self.scene.name)
            if info:
                status = daemon_mod.request(info.get("url") or "", "/status", timeout=0.5)
                if status and status.get("ok") and status.get("running"):
                    url = info.get("url")
        except Exception:
            url = None
        self._daemon_cache = (now, url)
        return url

    def _proxy_send_to_daemon(self, command, mode, actor, source):
        """管家在跑就让它发；返回 None 表示"没管家，走原来的路"。"""
        url = self._daemon_url()
        if not url:
            return None
        from . import daemon as daemon_mod
        result = daemon_mod.request(url, "/send",
                                    {"text": command, "mode": mode,
                                     "actor": actor, "source": source},
                                    timeout=10.0)
        if result is None:
            self._daemon_cache = (0, None)      # 连不上：下次重新探测
            return None
        if not result.get("ok"):
            err = result.get("error") or {}
            raise BridgeError(err.get("kind") or "internal",
                              err.get("message") or "管家拒绝了这个命令",
                              hint=err.get("hint"))
        payload = result.get("data") or {}
        return {
            "ok": True,
            "accepted": True,
            "mode": self.mode,
            "rx_text": payload.get("response") or "",
            "rx_hex": payload.get("response_hex") or "",
            "bytes": int(payload.get("bytes") or 0),
            "elapsed_ms": int(payload.get("elapsed_ms") or 0),
            "note": "经由管家（常驻连接）",
        }

    # ── 状态 ────────────────────────────────────────────────────────────────
    def status(self):
        current = self.state.read()
        watch_cfg = current.get("watch") or {}
        autosend_alive = self._autosend_thread is not None and self._autosend_thread.is_alive()
        data = {
            "ok": True,
            "scene": self.scene.name,
            "mode": self.mode,
            "control": current.get("state"),
            "control_since": current.get("since"),
            "watch": watch_cfg,
            "bauds": BAUD_CHOICES,
            # watch_running 表示"后台自动循环是否在跑"：
            # 监视模式占口、或定时发送已开启，都算在跑（UI 用它显示状态）。
            "watch_running": self.loop.running or (autosend_alive and bool(watch_cfg.get("send_enabled"))),
            "port_held": self.loop.running,
            "sent_count": self.autosend_stats.get("sent", 0),
            "autosend_error": self.autosend_stats.get("last_error", ""),
            "watch_error": self.loop.last_error,
            "alerts": self.state.read_alerts(5),
            "port": current.get("port") or "",
            "baud": current.get("baud") or int(self.effective_cfg().get("baud") or 0),
            "hold_released_reason": self.ports.hold_released_reason,
            "state_path": self.state.path,
        }
        try:
            resolved = self.current_device()
            data["resolved"] = resolved
            data["port"] = resolved["device"]
            # 传输方式描述：网页侧边栏据此显示"串口 COM10 @115200"
            data["transport"] = transport_describe(
                self.effective_cfg(), resolved["device"], resolved.get("baud"))
        except BridgeError as exc:
            data["resolved"] = None
            data["resolve_error"] = exc.to_dict()
            data["transport"] = transport_describe(self.effective_cfg())
        data["port_owner"] = self.ports.owner()
        return data

    def poll(self, force=False):
        """把后端攒下的数据交给网页。

        自动发送（定时发送 / 规则自动处置）的每一帧都会进 `auto_frames`，
        这里**不分 mode** 都取走——否则 light 模式下定时发送在页面上看不到回显
        （Bug 4：用户说"没看到"，不是"没发送"）。

        管家在跑时改从管家取（游标式增量），网页就完全不用碰串口了。
        """
        url = self._daemon_url()
        if url:
            from . import daemon as daemon_mod
            # 第一次接上管家时用 -1：只收从现在起的新数据，不倒历史
            cursor = getattr(self, "_daemon_cursor", -1)
            cursor = -1 if cursor is None else int(cursor)
            proxied = daemon_mod.request(url, "/poll?cursor=%d" % cursor, timeout=2.0)
            if proxied and proxied.get("ok"):
                self._daemon_cursor = int(proxied.get("cursor") or cursor)
                if proxied.get("bytes"):
                    return {
                        "ok": True,
                        "frames": [],
                        "rx_text": proxied.get("rx_text") or "",
                        "rx_hex": proxied.get("rx_hex") or "",
                        "bytes": int(proxied.get("bytes") or 0),
                        "via": "daemon",
                    }
                return {"ok": True, "frames": [], "rx_text": "", "rx_hex": "",
                        "bytes": 0, "via": "daemon"}
            self._daemon_cache = (0, None)      # 管家掉线：回退到本地逻辑

        frames = self.loop.take_frames()
        data = self.loop.take()
        if frames or data:
            return {
                "ok": True,
                "frames": frames,
                "rx_text": data.decode("utf-8", errors="replace"),
                "rx_hex": " ".join("%02X" % b for b in data),
                "bytes": len(data),
            }
        # 轻模式**默认完全不碰串口**。
        # 早先这里每 700ms 临时开口读一次（每次最多等 1500ms 首字节），
        # 结果网页开着的时候串口几乎一直被占着——外部程序和 AI 都打不开（Bug 5）。
        # 现在只有用户手动点「读一次」(force) 才真正开口。
        if not force:
            return {"ok": True, "rx_text": "", "rx_hex": "", "bytes": 0,
                    "skipped": "轻模式不主动读串口（点「读一次」可临时读一帧）"}
        try:
            resolved = self.current_device()
            with self.loop.port_lock:
                out = serial_io.drain(self.effective_cfg(), resolved["device"])
        except BridgeError as exc:
            return {"ok": False, "error": exc.to_dict()}
        if out["rx_text"]:
            self.logger.append("RX", out["rx_text"])
        return {
            "ok": True,
            "rx_text": out["rx_text"],
            "rx_hex": out["rx_hex"],
            "bytes": out["bytes"],
        }

    def alerts(self, limit):
        return self.state.read_alerts(limit)

    # ── 动作 ────────────────────────────────────────────────────────────────
    def send(self, text, mode="text", actor="human", source="web"):
        """发一条命令。

        source 表示"谁发的"：web(页面按钮) / ai(插件 serial_send 转发过来)。
        AI 发起的会在 auto_frames 里记一帧，网页回显区就能看到（Bug 8）。

        管家在跑时**交给管家发**：它已经常驻持有连接，网页就不该再去开关口
        ——否则用户在网页上点一下，AI 的速度就从 0ms 掉回 1 秒（第 4 件核心目标）。
        """
        command = str(text or "").strip()
        if not command:
            raise BridgeError("config", "命令不能为空")
        if actor == "ai":
            allowed, current = self.state.check_ai_allowed()
            if not allowed:
                raise BridgeError(
                    "control_blocked",
                    "控制权是 %s，AI 不能发命令。" % current.get("state"),
                    hint=["点网页上的[交给AI]（或 [继续AI]）",
                          "也可以让 AI 调 serial_control action=ai",
                          "人类发任意命令会自动把控制权拉回 human"],
                )
        else:
            # 安全规则 1：人类命令永远优先，AI 托管立即让位
            self.state.note_human_command()

        proxied = self._proxy_send_to_daemon(command, mode, actor, source)
        if proxied is not None:
            return proxied

        if mode == "hex":
            payload_bytes = _parse_hex(command)
            if payload_bytes is None:
                raise BridgeError("config", "十六进制格式不对，示例：AA 55 01 00")
        else:
            payload_bytes = command.encode("utf-8")

        if self.mode == "watch" and self.loop.running and self.loop.ser is not None:
            if mode == "hex":
                # 原始字节模式不补换行，帧由调用方给全（见 io.transact 的说明）
                self.loop.ser.write(payload_bytes)
            else:
                self.loop.ser.write(payload_bytes + serial_io.decode_newline(
                    self.effective_cfg().get("newline"), "\\r\\n"))
            self.logger.append("TX", command)
            # 标记"接下来这一小段回显是手动触发的"，读线程据此把来源标成 manual
            self._manual_until = time.time() + 2.0
            if source != "web":
                self.loop._push_frame(command, "", "", 0, source)
            return {"ok": True, "accepted": True, "mode": self.mode}

        resolved = self.current_device()
        cfg = self.effective_cfg()
        # Bug 11：hex 模式必须把解析出来的原始字节传下去，
        # 否则 transact 会把 "AA 55" 当成 5 个 ASCII 字符发出去。
        with self.loop.port_lock:
            result = serial_io.transact(
                cfg, resolved["device"], command,
                raw_bytes=payload_bytes if mode == "hex" else None)
        self.logger.append("TX", command)
        if result["rx_text"]:
            self.logger.append("RX", result["rx_text"])
        self.state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0))

        # Bug 7：手动发的回显也要跑判定，否则"设备回了 FAULT 但 alerts 是空的"
        if not result["empty"]:
            self._maybe_check(result["rx_text"], "manual")
        # AI 发起的命令记一帧，网页才看得到（Bug 8）
        if source != "web":
            self.loop._push_frame(command, result["rx_text"], result["rx_hex"],
                                  result["bytes"], source)
        return {
            "ok": True,
            "accepted": True,
            "mode": self.mode,
            "rx_text": result["rx_text"],
            "rx_hex": result["rx_hex"],
            "bytes": result["bytes"],
            "elapsed_ms": result["elapsed_ms"],
            "note": resolved["note"],
        }

    def _maybe_check(self, text, source):
        """按 check_enabled / check_source 决定要不要判定。绝不抛异常。"""
        try:
            watch = self.state.read().get("watch") or {}
            if not watch.get("check_enabled"):
                return
            want = str(watch.get("check_source") or "watch_only")
            if want == "watch_only" and source != "watch":
                return
            if want == "manual_only" and source != "manual":
                return
            self.loop._run_checks(text, source)
        except Exception:
            pass

    def control(self, action, actor="human"):
        # 按"动作 + 源状态"校验：resume 的目标也是 ai，不能只看目标状态
        mapping = {
            "ai": (STATE_AI, "交给AI", {STATE_HUMAN, STATE_PAUSED}),
            "pause": (STATE_PAUSED, "暂停AI", {STATE_AI}),
            "resume": (STATE_AI, "继续AI", {STATE_PAUSED}),
            "human": (STATE_HUMAN, "人类接管", {STATE_AI, STATE_PAUSED}),
        }
        if action not in mapping:
            raise BridgeError("config", "未知的控制动作：%r" % action)
        target, label, sources = mapping[action]
        # AI 不能在人看着网页时自己把控制权抢回去
        # "自授权"= 调用方是 AI 且当前没人在看网页；这一步必须留痕（见 cli.cmd_control）
        actor_id = str(actor or "human").lower()
        self_granted = bool(
            target == STATE_AI and actor_id == "ai" and not self.state.presence_alive()
        )
        if target == STATE_AI:
            self.state.assert_self_grant_allowed(actor)
        # 状态文件里的 changed_by 要记"谁真正点的"，不能一律写 web：
        # 否则 AI 自己接管和人类点按钮在审计里长得一模一样。
        changed_by = "ai" if actor_id == "ai" else "web"
        audit = " ".join([
            "⚠ 你现在是以 AI 身份自授权：当前无人在看网页，控制权由 AI 自己取得，"
            "已记入状态文件 changed_by=ai（可审计）。" if self_granted else ""
        ]).strip()
        current_state = self.state.read().get("state")
        # 幂等：已经是目标状态就当作成功，不要报 400。
        # （早期版本把 human->human 当成非法迁移，导致"已经在人类手中"时
        #  再点[人类接管]会报错，同一个按钮有时好有时坏，非常迷惑。）
        if current_state == target:
            return {
                "ok": True,
                "control": current_state,
                "state": self.state.read(),
                "port_action": "unchanged",
                "watch_running": self.loop.running,
                "self_granted": self_granted,
                "note": " ".join(["已经是%s，无需切换" % label, audit]).strip(),
            }
        if current_state not in sources:
            raise BridgeError(
                "control_blocked",
                "当前是 %s，不能执行[%s]。" % (current_state, label),
                hint=["human 只能[交给AI]；ai 只能[暂停AI]/[人类接管]；"
                      "paused 可以[交给AI]/[继续AI]/[人类接管]"],
            )
        try:
            current = self.state.set_state(target, changed_by, label)
        except ValueError as exc:
            raise BridgeError("control_blocked", "状态迁移被拒绝（%s）。" % exc)

        # 控制权一变，串口占用跟着变。走统一的收敛点，
        # 和看门狗、CLI、serial_control 工具用的是同一段逻辑。
        forced = self.ports.reconcile("web-control") or {}
        port_action = forced.get("action", "unchanged")

        result = {
            "ok": True,
            "control": current.get("state"),
            "state": current,
            "port_action": port_action,
            "watch_running": self.loop.running,
            "self_granted": self_granted,
        }
        if audit:
            result["note"] = audit
        return result

    def set_watch(self, body):
        current = self.state.read()
        merged = dict(current.get("watch") or {})
        allowed_keys = {
            "send_enabled", "check_enabled", "check_source", "command",
            "interval_ms", "timeout_ms", "command_mode",
        }
        for key, value in (body or {}).items():
            if key in allowed_keys:
                merged[key] = value
        self.state.update(watch=merged)
        return {"ok": True, "watch": merged}

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
            if wtype == "regex" and not str(when.get("pattern") or when.get("value") or ""):
                raise BridgeError("config", "规则「%s」的 regex 需要 pattern" % name)
            if wtype == "regex":
                # 写进配置前先编译一次，别把坏正则存盘
                try:
                    import re as _re
                    _re.compile(str(when.get("pattern") or when.get("value")))
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

        # 场景对象缓存的是启动时的配置，改完要重新读一次
        self.scene = load_scene(self.paths["common_config"], self.paths["protocol_root"],
                                self.scene_name)
        return {
            "ok": True,
            "rules": cleaned,
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
            "config_path": config_path,
        }

    def reconnect(self, com, baud):
        if com:
            self.override_com = com
        if baud:
            self.override_baud = int(baud)
        # 真重建：watch 模式先停旧口再按新参数开
        if self.mode == "watch":
            resolved = self.current_device()
            self.loop.start(resolved["device"], int(self.effective_cfg().get("baud") or 115200))
        else:
            # 轻模式**不占口**，所以这里绝不能真去 open 一下做"验证"。
            # 两个理由（都是实测踩出来的）：
            #   1) 打开会撞上真正持口的那个进程（管家/定时发送线程/用户的串口助手），
            #      于是报"COM10 被其他程序占用"——但轻模式本来就不该占口，这是假警报；
            #   2) CH340 在打开瞬间会拉高 DTR/RTS，等于给板子来了一次复位。
            # 轻模式只需要确认"这个口现在存在"，扫描就够。
            resolved = self.current_device()
            self.state.update(port=resolved["device"],
                              baud=int(self.effective_cfg().get("baud") or 0))
        return {"ok": True, "port": self.current_device()["device"], "baud": self.effective_cfg().get("baud")}

    def autodetect(self):
        """AI 自动选择：按场景的 match / match_vid_pid 规则扫描并说明理由。"""
        resolved = resolve_port(self.scene.cfg)
        reasons = {
            "match_vid_pid": "按 VID:PID %s 精确匹配" % self.scene.cfg.get("match_vid_pid"),
            "match": "按描述关键字 %r 匹配" % self.scene.cfg.get("match"),
            "com": "配置里指定的串口号可用",
            "only_port": "当前只有一个串口设备",
        }
        return {
            "ok": True,
            "port": resolved["device"],
            "description": resolved["description"],
            "vid_pid": resolved["vid_pid"],
            "reason": reasons.get(resolved["source"], resolved["source"]),
            "note": resolved["note"],
        }

    def set_mode(self, mode):
        if mode not in ("light", "watch"):
            raise BridgeError("config", "模式只能是 light 或 watch")
        if mode == self.mode:
            return {"ok": True, "mode": self.mode}

        # 管家在跑：监视模式交给它，网页仍然不拿口（否则两边抢口）
        daemon_url = self._daemon_url()
        if daemon_url:
            from . import daemon as daemon_mod
            result = daemon_mod.request(daemon_url, "/mode", {"mode": mode}, timeout=2.0)
            if result is None:
                self._daemon_cache = (0, None)      # 管家没了：退回本地逻辑
            elif not result.get("ok"):
                err = result.get("error") or {}
                raise BridgeError(err.get("kind") or "internal",
                                  err.get("message") or "管家拒绝切换模式",
                                  hint=err.get("hint"))
            else:
                self.mode = mode
                self.loop.stop()                    # 网页不再持口
                self.ports.hold_released_reason = "由管家持有（监视模式）"
                return {"ok": True, "mode": self.mode, "watch_running": True,
                        "via": "daemon"}

        if mode == "watch":
            # 切监视模式也遵守控制权：控制权不在 AI 就先不抢口
            self.mode = "watch"
            if self.ports.should_hold():
                self.ports.acquire_if_needed()
            else:
                self.ports.hold_released_reason = "控制权不在 AI，监视模式暂不占口"
        else:
            self.loop.stop()
            self.mode = "light"
            self.ports.hold_released_reason = ""
        return {"ok": True, "mode": self.mode, "watch_running": self.loop.running}

    # ── 暂停（网页留着） vs 退出（网页也关）───────────────────────────────
    def pause_port(self):
        """暂停：释放串口，但网页继续可访问。想临时用串口助手时点这个。"""
        if self.loop.running:
            self.loop.stop()
        self.ports.hold_released_reason = "已手动暂停，串口已释放"
        return {"ok": True, "paused": True, "watch_running": self.loop.running,
                "port_owner": self.ports.owner()}

    def resume_port(self):
        """从"暂停"恢复占用（只有控制权在 AI 且是 watch 模式才真的占口）。"""
        self.ports.hold_released_reason = ""
        acquired = False
        if self.ports.should_hold():
            acquired = self.ports.acquire_if_needed()
        return {"ok": True, "paused": False, "acquired": acquired,
                "watch_running": self.loop.running, "port_owner": self.ports.owner()}

    def request_shutdown(self):
        self.shutdown_event.set()
        threading.Thread(target=self._shutdown_worker, name="shutdown", daemon=True).start()

    def _autosend_loop(self):
        """定时发送：独立于模式、独立于串口占用。

        Bug 4 的根因是它原来活在 WatchLoop 里，而 WatchLoop 只在
        「监视模式 + 控制权 ai」时启动 —— light 模式下打开开关既不发也不报错。
        放在 WebServer 层后，两种模式都按 interval_ms 发；light 模式每次临时开口。
        控制权为 paused 时跳过（paused 的语义是"谁都不发"）。
        """
        next_at = 0.0
        while not self.shutdown_event.is_set():
            try:
                current = self.state.read()
                watch = current.get("watch") or {}
                enabled = bool(watch.get("send_enabled"))
                command = str(watch.get("command") or "").strip()
                interval = max(0.2, float(watch.get("interval_ms") or 1000) / 1000.0)
                control = current.get("state") or "human"
                if not enabled or not command or control == STATE_PAUSED:
                    next_at = 0.0
                    self.shutdown_event.wait(0.2)
                    continue
                now = time.time()
                if now < next_at:
                    self.shutdown_event.wait(min(0.2, max(0.01, next_at - now)))
                    continue
                next_at = now + interval
                # 场景可以声明 command_mode: "hex"，让定时发送的 command 按十六进制解析
                cmd_mode = str(watch.get("command_mode")
                               or self.scene.cfg.get("command_mode") or "text").lower()
                raw = _parse_hex(command) if cmd_mode == "hex" else None
                if cmd_mode == "hex" and raw is None:
                    raise BridgeError("config", "定时发送命令不是合法十六进制：%r" % command)
                text = self.loop.send_raw(command, raw_bytes=raw)
                self.autosend_stats["sent"] = self.autosend_stats.get("sent", 0) + 1
                self.autosend_stats["last_tx"] = time.time()
                self.autosend_stats["last_error"] = ""
                # 定时轮询的回显按 check_source 决定要不要判定
                self._maybe_check(text, "watch")
            except BridgeError as exc:
                self.autosend_stats["errors"] = self.autosend_stats.get("errors", 0) + 1
                self.autosend_stats["last_error"] = exc.message
                self.logger.append("WATCH", "定时发送失败：%s" % exc.message)
                self.shutdown_event.wait(0.5)
            except Exception as exc:
                self.autosend_stats["errors"] = self.autosend_stats.get("errors", 0) + 1
                self.autosend_stats["last_error"] = str(exc)
                self.shutdown_event.wait(0.5)

    def _existing_instance(self):
        """检测同一场景是否已有网页实例在跑（避免两个进程抢串口）。"""
        try:
            with open(self._instance_file, encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError):
            return ""
        url = str(info.get("url") or "")
        if not url:
            return ""
        # 用一次 HTTP 探测判断它是否还活着（Windows 上不能安全用 os.kill(pid,0) 探活）
        try:
            with urllib.request.urlopen(url + "/status", timeout=1.0) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            pass
        return ""

    def _write_instance(self, url):
        try:
            os.makedirs(os.path.dirname(self._instance_file), exist_ok=True)
            with open(self._instance_file, "w", encoding="utf-8") as handle:
                json.dump({"url": url, "port": self.port, "pid": os.getpid(),
                           "scene": self.scene.name}, handle, ensure_ascii=False)
        except OSError:
            pass

    def _clear_instance(self):
        try:
            os.remove(self._instance_file)
        except OSError:
            pass

    def _heartbeat_loop(self):
        """每 2 秒声明一次"有人在看着这一局"，AI 的门禁据此切严格/宽松。"""
        while not self.shutdown_event.is_set():
            try:
                self.state.heartbeat("web")
            except Exception:
                pass
            self.shutdown_event.wait(2.0)

    def _shutdown_worker(self):
        time.sleep(0.15)  # 让 /shutdown 的响应先写完
        self.loop.stop()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    # ── 服务生命周期 ────────────────────────────────────────────────────────
    def serve(self, open_browser=False):
        # 单实例守卫：同一场景只允许一个网页实例，否则两个进程会互相抢串口。
        existing = self._existing_instance()
        if existing:
            print(json.dumps({
                "ok": False,
                "error": "这个场景已经有一个网页实例在运行，不再重复启动。",
                "url": existing,
            }, ensure_ascii=False))
            sys_flush()
            return 0

        # 启动时是否抢口，也遵守控制权：
        #   watch + ai      → 抢口（要看连续数据流）
        #   watch + human   → 不抢，把口留给用户
        #   light           → 本来就不常占口
        boot_error = None
        if self.mode == "watch" and self.ports.should_hold():
            try:
                resolved = self.current_device()
                self.loop.start(resolved["device"],
                                int(self.effective_cfg().get("baud") or 115200))
            except BridgeError as exc:
                # 口打不开也要把网页开起来：用户需要看到错误并换口
                boot_error = exc.to_dict()
                self.loop.last_error = exc.message
        elif self.mode == "watch":
            self.ports.hold_released_reason = "控制权不在 AI，监视模式暂不占口"
        # 记住启动时的控制权，避免看门狗第一次跑就误判成"状态变化"
        self.ports._last_control = self.state.read().get("state")

        handler = type("BoundHandler", (BridgeHandler,), {})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.httpd.daemon_threads = True
        self.httpd.context = {
            "page": self._page,
            "status": self.status,
            "poll": self.poll,
            "alerts": self.alerts,
            "send": self.send,
            "control": self.control,
            "set_watch": self.set_watch,
            "set_rules": self.set_rules,
            "get_rules": self.get_rules,
            "pause_port": self.pause_port,
            "resume_port": self.resume_port,
            "reconnect": self.reconnect,
            "autodetect": self.autodetect,
            "set_mode": self.set_mode,
            "request_shutdown": self.request_shutdown,
        }
        url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        # 看门狗：控制权被别处（CLI / serial_control 工具）改掉时，这里自动释放/重占串口
        threading.Thread(target=self.ports.watchdog, name="port-watchdog", daemon=True).start()
        # 定时发送：独立线程，light / watch 两种模式都生效（Bug 4）
        self._autosend_thread = threading.Thread(
            target=self._autosend_loop, name="autosend", daemon=True)
        self._autosend_thread.start()
        self._write_instance(url)
        print(json.dumps({
            "ok": True,
            "url": url,
            "port": self.port,
            "scene": self.scene.name,
            "mode": self.mode,
            **({"warning": boot_error} if boot_error else {}),
        }, ensure_ascii=False))
        sys_flush()
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self.loop.stop()
            self._clear_instance()
            try:
                self.httpd.server_close()
            except Exception:
                pass
        return 0


def sys_flush():
    try:
        import sys
        sys.stdout.flush()
    except Exception:
        pass


def _parse_hex(text):
    cleaned = str(text).replace(",", " ").replace("0x", " ").replace("0X", " ")
    parts = [p for p in cleaned.split() if p]
    out = bytearray()
    for part in parts:
        try:
            value = int(part, 16)
        except ValueError:
            return None
        if value < 0 or value > 255:
            return None
        out.append(value)
    return bytes(out) if out else None


def _load_page():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "index.html")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return "<!doctype html><meta charset='utf-8'><h3>web/index.html 缺失</h3>"


def run_web(paths, scene_name, port, mode, open_browser):
    server = WebServer(paths, scene_name, port, mode)
    return server.serve(open_browser=open_browser)
