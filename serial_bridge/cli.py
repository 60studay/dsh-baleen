# -*- coding: utf-8 -*-
"""命令行入口：index.js 只跟这一层说话。

约定：
- stdout 永远是一行 JSON（``{"ok": true, "data": ...}`` 或 ``{"ok": false, "error": {...}}``）
- 失败时 **不打印 Python 堆栈**，退出码 1
- 所有设备知识来自场景配置，这里只有通用逻辑

子命令（与 index.js 的工具一一对应）：
    scan                         列出串口 + 场景
    info   --scene S             场景参数 + 协议档案
    send   --scene S --command C [--as human|ai] [--auto]
    read   --scene S [--lines N]
    status --scene S             控制权状态 + 最近告警
    watch  --scene S --action start|stop|status [--set key=value ...]
    web    --scene S [--port P] [--mode light|watch]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import io as serial_io
from .config import list_scenes, load_scene, read_json
from .errors import BridgeError
from .ports import resolve_port, scan_ports
from .rules import RuleEngine
from .state import SharedState, _safe_name
from .transport import describe_transport

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认 GBK，输出中文会崩；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _paths():
    """解析运行目录。

    注意：**手动启动网页时没有插件注入的环境变量**，所以不能只靠 env。
    早期版本这里把 log_root 默认成插件目录下的 logs/，而插件进程用的是配置里的路径，
    于是两边各写一份 state 文件——网页点了[交给AI]，serial_status 却还显示 human（Bug 3）。

    现在统一：env > config.json 的 log_root > 与 index.js 完全相同的默认值。
    默认值必须是"同一个表达式"，否则开源版把 log_root 留空后又会分裂。
    """
    common_path = os.path.join(BASE, "config.json")
    common = read_json(common_path, {}) or {}
    protocol_root = (os.environ.get("SERIAL_BRIDGE_PROTO_DIR")
                     or common.get("protocol_root")
                     or os.path.join(BASE, "protocols"))
    log_root = (os.environ.get("SERIAL_BRIDGE_LOG_DIR")
                or common.get("log_root")
                or default_log_root())
    runtime_root = (os.environ.get("SERIAL_BRIDGE_RUNTIME_DIR")
                    or common.get("runtime_root")
                    or os.path.join(log_root, ".runtime"))
    return {
        "protocol_root": protocol_root,
        "common_config": common_path,
        "log_root": log_root,
        "runtime_root": runtime_root,
    }


def default_log_root():
    """默认日志/状态目录。

    **必须和 index.js 的 DEFAULT_LOG_ROOT 是同一个值**，否则会出现
    "两份 state、控制权不同步"（Bug 3：网页点了[交给AI]，工具却还报 human）。

    当前约定：跟着插件走，`<插件目录>/logs`。
    index.js 那边是 `path.join(HERE, 'logs')`，这里对应 `BASE/logs`。
    改一边必须同时改另一边。
    """
    return os.path.join(BASE, "logs")


class Logger(object):
    """兼容旧引用：实现已移到 log.py（daemon 也要用，避免循环 import）。"""

    def __new__(cls, log_root, scene):
        from .log import Logger as _Logger
        return _Logger(log_root, scene)


def _emit(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(data):
    _emit({"ok": True, "data": data})
    return 0


def _fail(error):
    payload = error.to_dict() if isinstance(error, BridgeError) else {
        "kind": "internal", "message": str(error)}
    _emit({"ok": False, "error": payload})
    return 1


def _forward_to_daemon(paths, scene_name, command, actor, no_wait=False):
    """管家在跑就把命令交给它（三级降级的第一级）。

    返回**已经组织好的 data dict**；管家没跑/不可达时返回 None，调用方继续降级。
    管家返回的业务错误（控制权/格式）照原样抛出去，不能降级——那不是"连不上"。
    """
    from . import daemon as daemon_mod
    info = daemon_mod.read_instance(paths, scene_name)
    if not info:
        return None
    result = daemon_mod.request(
        info.get("url") or "", "/send",
        {"text": command, "mode": "text", "actor": actor,
         "wait_ms": 0 if no_wait else None},
        timeout=5.0)
    if result is None:
        return None                       # 管家死了/端口不通 → 降级
    if not result.get("ok"):
        err = result.get("error") or {}
        raise BridgeError(err.get("kind") or "internal",
                          err.get("message") or "管家拒绝了这个命令",
                          hint=err.get("hint"))
    payload = result.get("data") or {}
    return {
        "scene": scene_name,
        "command": command,
        "port": payload.get("port") or "",
        "port_source": "daemon",
        "note": "已发出（不等回显）" if no_wait else "经由管家（常驻连接）",
        "response": payload.get("response") or "",
        "response_hex": payload.get("response_hex") or "",
        "bytes": int(payload.get("bytes") or 0),
        "submit_ms": int(payload.get("submit_ms") or 0),
        "elapsed_ms": int(payload.get("elapsed_ms") or 0),
        "empty": bool(payload.get("empty")),
        "submitted": bool(payload.get("submitted")),
        "via": "daemon",
        "transport": payload.get("transport"),
    }


def _forward_to_web(paths, scene_name, command, actor):
    """网页在跑时，把命令交给它发（Bug 8）。

    返回网页的 JSON 结果；网页没跑/不可达时返回 None，调用方回退到直接开串口。
    这样"网页开着 → 双方都看得见"，"网页没开 → 照常能用"。
    """
    inst = os.path.join(paths["runtime_root"], "state",
                        "%s.web.json" % _safe_name(scene_name))
    try:
        with open(inst, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    url = str(info.get("url") or "").rstrip("/")
    if not url:
        return None
    payload = json.dumps({
        "text": command,
        "mode": "text",
        "actor": actor,
        "source": "ai" if actor == "ai" else "web",
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url + "/send", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 4xx 也带 JSON 错误体（比如 control_blocked），要原样传出去
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


def _scene(paths, name):
    return load_scene(paths["common_config"], paths["protocol_root"], name)


# ── 子命令 ──────────────────────────────────────────────────────────────────
def cmd_scan(paths, args):
    ports = scan_ports()
    scenes = list_scenes(paths["common_config"], paths["protocol_root"])
    # 给每个场景预解析一次，让 AI 一眼看到"这个场景现在会落到哪个口"
    for scene in scenes:
        try:
            merged = _scene(paths, scene["name"]).cfg
            resolved = resolve_port(merged)
            scene["resolved"] = {
                "device": resolved["device"],
                "source": resolved["source"],
                "note": resolved["note"],
            }
        except BridgeError as exc:
            scene["resolved"] = None
            scene["resolve_error"] = exc.to_dict()
    return _ok({"ports": ports, "scenes": scenes, "log_root": paths["log_root"]})


def cmd_info(paths, args):
    scene = _scene(paths, args.scene)
    cfg = scene.cfg
    resolved = None
    resolve_error = None
    try:
        resolved = resolve_port(cfg)
    except BridgeError as exc:
        resolve_error = exc.to_dict()
    watch = scene.watch_cfg()
    return _ok({
        "scene": scene.name,
        "com": str(cfg.get("com") or ""),
        "match": str(cfg.get("match") or ""),
        "match_vid_pid": str(cfg.get("match_vid_pid") or ""),
        "baud": int(cfg.get("baud") or 0),
        "transport": describe_transport(cfg,
                                        (resolved or {}).get("device")),
        "newline": cfg.get("newline") if cfg.get("newline") is not None else "\\r\\n",
        "protocol": scene.protocol_text(),
        "protocol_path": scene.protocol_path,
        "resolved": resolved,
        "resolve_error": resolve_error,
        "watch": {
            "send_enabled": bool(watch.get("send_enabled")),
            "check_enabled": bool(watch.get("check_enabled")),
            "check_source": str(watch.get("check_source") or "watch_only"),
            "command": watch.get("command") or "",
            "interval_ms": int(watch.get("interval_ms") or 0),
            "rules": watch.get("rules") or [],
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
        },
    })


def cmd_send(paths, args):
    scene = _scene(paths, args.scene)
    cfg = scene.cfg
    state = SharedState(paths["runtime_root"], scene.name)
    logger = Logger(paths["log_root"], scene.name)

    actor = (args.actor or "human").lower()
    if actor == "ai":
        allowed, current = state.check_ai_allowed()
        if not allowed:
            reason = state.ai_block_reason() or ""
            raise BridgeError(
                "control_blocked",
                "AI 现在不能发命令：控制权是 %s。" % current.get("state"),
                hint=[reason,
                      "人类发任意命令会自动把控制权拉回 human",
                      "查看当前状态：serial_status"],
            )
    else:
        # 安全规则 1：人类命令永远优先，AI 托管立即让位
        state.note_human_command()

    if args.auto:
        engine = RuleEngine(scene.watch_cfg())
        if not engine._is_allowed(args.command):
            raise BridgeError(
                "allowlist_blocked",
                "自动命令 %r 不在白名单内。" % args.command,
                hint=["在场景 config.json 的 watch.allowed_auto_commands 里加上它",
                      "白名单为空时禁止一切自动命令"],
            )

    # 一级：管家在跑 → 走它（常驻连接，~几十毫秒）
    via_daemon = _forward_to_daemon(paths, scene.name, args.command, actor,
                                    no_wait=bool(getattr(args, "no_wait", False)))
    if via_daemon is not None:
        return _ok(via_daemon)

    # 二级：网页在跑时统一走它的 /send，这样用户能在页面上看到 AI 发了什么。
    forwarded = _forward_to_web(paths, scene.name, args.command, actor)
    if forwarded is not None:
        if not forwarded.get("ok"):
            err = forwarded.get("error") or {}
            raise BridgeError(err.get("kind") or "internal",
                              err.get("message") or "网页转发失败",
                              hint=err.get("hint"))
        accepted_only = forwarded.get("mode") == "watch"
        data = {
            "scene": scene.name,
            "command": args.command,
            "port": forwarded.get("port") or "",
            "port_source": "web",
            "note": forwarded.get("note") or "已交给网页发送",
            "response": forwarded.get("rx_text") or "",
            "response_hex": forwarded.get("rx_hex") or "",
            "bytes": int(forwarded.get("bytes") or 0),
            "elapsed_ms": int(forwarded.get("elapsed_ms") or 0),
            "empty": not bool(forwarded.get("rx_text")),
        }
        if accepted_only:
            data["note"] = "已交给网页发送（监视模式，回显在网页上）"
        if data["empty"] and not accepted_only:
            data["warning"] = "网页转发了命令，但设备没有回显。"
        return _ok(data)

    resolved = resolve_port(cfg)

    # 没有管家、又要求"发完就走"：写完立刻关，回显之后用 read 取
    if getattr(args, "no_wait", False):
        sent = serial_io.send_only(cfg, resolved["device"], args.command)
        logger.append("TX", args.command)
        state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0),
                     scene=scene.name)
        return _ok({
            "scene": scene.name,
            "command": args.command,
            "port": resolved["device"],
            "port_source": resolved["source"],
            "note": resolved["note"],
            "response": "",
            "response_hex": "",
            "bytes": 0,
            "submit_ms": sent["submit_ms"],
            "elapsed_ms": sent["submit_ms"],
            "empty": True,
            "submitted": True,
        })

    result = serial_io.transact(cfg, resolved["device"], args.command)
    logger.append("TX", args.command)
    if result["rx_text"]:
        logger.append("RX", result["rx_text"])
    state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0), scene=scene.name)

    data = {
        "scene": scene.name,
        "command": args.command,
        "port": resolved["device"],
        "port_source": resolved["source"],
        "note": resolved["note"],
        "response": result["rx_text"],
        "response_hex": result["rx_hex"],
        "bytes": result["bytes"],
        "elapsed_ms": result["elapsed_ms"],
        "empty": result["empty"],
    }
    if result["empty"]:
        data["warning"] = serial_io.describe_no_response(resolved, cfg).render()
    return _ok(data)


def cmd_read(paths, args):
    scene = _scene(paths, args.scene)
    logger = Logger(paths["log_root"], scene.name)
    text = logger.tail(args.lines)
    return _ok({
        "scene": scene.name,
        "text": text,
        "log_path": logger.path,
        "empty": text.strip() == "",
    })


def cmd_status(paths, args):
    scene = _scene(paths, args.scene)
    state = SharedState(paths["runtime_root"], scene.name)
    current = state.read()
    # ai_may_send 必须是**真实门禁结果**，不能只看 state 字符串：
    # 没人开网页时门禁是宽松的，此时 state 仍是 human 但 AI 其实可以发。
    allowed, _ = state.check_ai_allowed()
    try:
        resolved = resolve_port(scene.cfg)
        transport_info = describe_transport(scene.cfg, resolved["device"])
    except BridgeError:
        transport_info = describe_transport(scene.cfg)
    return _ok({
        "scene": scene.name,
        "state": current,
        "ai_may_send": bool(allowed),
        "watched": state.presence_alive(),
        "transport": transport_info,
        "alerts": state.read_alerts(args.alerts),
        "alerts_path": state.alerts_path,
    })


def cmd_control(paths, args):
    """改控制权。按文档的按钮表校验"当前状态能不能点这个按钮"。

    action: ai(交给AI) / pause(暂停AI) / resume(继续AI) / human(人类接管)

    注意：不能只看目标状态。resume 的目标也是 ai，而 ai→resume 在按钮表里是
    禁用的（那是"继续"已经停掉的托管，不是重复授予）。所以这里按"动作 + 源状态"校验。
    """
    scene = _scene(paths, args.scene)
    state = SharedState(paths["runtime_root"], scene.name)
    from .state import STATE_AI, STATE_HUMAN, STATE_PAUSED

    mapping = {
        "ai": (STATE_AI, "交给AI", {STATE_HUMAN, STATE_PAUSED}),
        "pause": (STATE_PAUSED, "暂停AI", {STATE_AI}),
        "resume": (STATE_AI, "继续AI", {STATE_PAUSED}),
        "human": (STATE_HUMAN, "人类接管", {STATE_AI, STATE_PAUSED}),
    }
    if args.action not in mapping:
        raise BridgeError("config", "未知的控制动作：%r" % args.action)
    target, label, sources = mapping[args.action]

    # AI 不能在人看着网页时自己把控制权抢回去（见 state.assert_self_grant_allowed）
    requester = (getattr(args, "by", None) or "cli")
    # "自授权"= 调用方是 AI 且当前没人在看网页（有人看时上面那行会直接拒绝）。
    # 这一步要留痕：调用方自己也该看到"这次是你自己拿的"，否则等于偷偷越权。
    self_granted = bool(
        target == STATE_AI
        and str(requester).lower() == "ai"
        and not state.presence_alive()
    )
    if target == STATE_AI:
        state.assert_self_grant_allowed(requester)

    notes = []
    if self_granted:
        notes.append(
            "⚠ 你现在是以 AI 身份自授权：当前无人在看网页，控制权由 AI 自己取得，"
            "已记入状态文件 changed_by=ai（可审计）。"
        )

    current_state = state.read().get("state")
    # 幂等：已经在目标状态就直接成功（重按同一个按钮不该报错）
    if current_state == target:
        return _ok({"scene": scene.name, "state": state.read(),
                    "self_granted": self_granted,
                    "note": " ".join(["已经是%s，无需切换" % label] + notes)})
    if current_state not in sources:
        raise BridgeError(
            "control_blocked",
            "当前是 %s，不能执行[%s]。" % (current_state, label),
            hint=["human 只能[交给AI]；ai 只能[暂停AI]/[人类接管]；"
                  "paused 可以[交给AI]/[继续AI]/[人类接管]"],
        )

    try:
        current = state.set_state(target, args.by, label)
    except ValueError as exc:
        raise BridgeError("control_blocked", "状态迁移被拒绝（%s）。" % exc)
    data = {"scene": scene.name, "state": current, "self_granted": self_granted}
    if notes:
        data["note"] = " ".join(notes)
    return _ok(data)


def _default_scene(paths):
    """没指定场景时：只有一个场景就用它，多个就报错让调用方明确。"""
    scenes = [s["name"] for s in list_scenes(paths["common_config"], paths["protocol_root"])]
    if len(scenes) == 1:
        return scenes[0]
    if not scenes:
        raise BridgeError("scene_missing", "一个场景都没有。",
                          hint=["在 protocols/ 下建一个场景目录"])
    raise BridgeError("config", "有多个场景，必须指定 scene。",
                      hint=["可用场景：%s" % "、".join(scenes),
                            "也可以先调 serial_list 看每个场景现在会落到哪个口"])


def cmd_webctl(paths, args):
    """网页服务遥控：open / status / close（给 AI 的 serial_web 工具用）。"""
    from . import web_service
    scene = (args.scene or "").strip() or _default_scene(paths)
    action = args.action
    if action == "open":
        return _ok(web_service.open_web(
            paths, scene,
            port=int(args.port or 0),
            mode=args.mode,
            open_browser=bool(getattr(args, "open_browser", False))))
    if action == "close":
        return _ok(web_service.close_web(paths, scene))
    return _ok(web_service.status_web(paths, scene))


def cmd_daemon(paths, args):
    """管家：常驻持有连接的独立进程（start / stop / status）。

    start 是幂等的：已经在跑就返回现有 pid，不会起第二个。
    """
    from . import daemon as daemon_mod
    scene = _scene(paths, args.scene)
    port = int(args.port or daemon_mod.DEFAULT_PORT)
    idle = int(args.idle or daemon_mod.DEFAULT_IDLE_S)
    action = args.action

    if action == "start":
        status = daemon_mod.ensure_started(paths, scene.name, port=port, idle_s=idle)
        current = status.get("status") if isinstance(status, dict) else None
        return _ok({
            "running": True,
            "pid": (current or {}).get("pid") or status.get("pid"),
            "url": status.get("url"),
            "port": (current or status).get("port") or "",
            "control": (current or status).get("control") or "",
            "note": "管家已启动，占用 %s，期间串口助手打不开；空闲 %d 秒自动释放"
                    % ((current or status).get("port") or "串口",
                       (current or status).get("idle_timeout_s") or idle),
            "transport": (current or status).get("transport"),
        })

    if action == "stop":
        return _ok(daemon_mod.stop(paths, scene.name))

    status = daemon_mod.running(paths, scene.name)
    if not status:
        return _ok({"running": False, "scene": scene.name})
    return _ok(status)


def cmd_watch(paths, args):
    """监视/轮询的开关与运行时状态。

    **两个开关完全独立**（文档 3.6 的核心要求）：
      send_enabled  只管定时发送
      check_enabled 只管规则判定
    关掉任意一个，另一个照常工作；改动立即生效（settings 写进共享状态）。
    """
    scene = _scene(paths, args.scene)
    state = SharedState(paths["runtime_root"], scene.name)

    if args.action == "status":
        current = state.read()
        return _ok({
            "scene": scene.name,
            "watch": current.get("watch") or {},
            "state": current.get("state"),
            "alerts": state.read_alerts(10),
        })

    updates = {}
    for item in args.set or []:
        if "=" not in item:
            raise BridgeError("config", "--set 需要 key=value 形式：%r" % item)
        key, value = item.split("=", 1)
        updates[key.strip()] = _coerce(value.strip())
    if not updates:
        raise BridgeError("config", "--set 至少要给一个键，例如 send_enabled=true")
    if args.action == "stop":
        updates.setdefault("running", False)
    elif args.action == "start":
        updates.setdefault("running", True)

    current = state.read()
    merged = dict(current.get("watch") or {})
    merged.update(updates)
    state.update(watch=merged)
    return _ok({"scene": scene.name, "watch": merged, "changed": updates})


def _coerce(text):
    lowered = text.lower()
    if lowered in ("true", "yes", "on", "1"):
        return True
    if lowered in ("false", "no", "off", "0"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def cmd_web(paths, args):
    from .supervisor import run_web
    return run_web(paths, args.scene, args.port, args.mode, args.open_browser)


# ── 入口 ────────────────────────────────────────────────────────────────────
def build_parser():
    parser = argparse.ArgumentParser(description="DSH Whalesong（鲸歌）v2")
    sub = parser.add_subparsers(dest="action_name")

    sub.add_parser("scan")

    info = sub.add_parser("info")
    info.add_argument("--scene", required=True)

    send = sub.add_parser("send")
    send.add_argument("--scene", required=True)
    send.add_argument("--command", required=True)
    send.add_argument("--as", dest="actor", default="human", choices=["human", "ai"])
    send.add_argument("--auto", action="store_true", help="按自动命令处理，强制白名单检查")
    send.add_argument("--no-wait", action="store_true",
                      help="发完立刻返回，不等回显（批量下命令用，回显之后用 read 取）")

    read = sub.add_parser("read")
    read.add_argument("--scene", required=True)
    read.add_argument("--lines", type=int, default=20)

    status = sub.add_parser("status")
    status.add_argument("--scene", required=True)
    status.add_argument("--alerts", type=int, default=20)

    control = sub.add_parser("control")
    control.add_argument("--scene", required=True)
    control.add_argument("--action", required=True,
                         choices=["ai", "pause", "resume", "human"])
    control.add_argument("--by", default="cli", help="谁改的（记在状态文件里）")

    watch = sub.add_parser("watch")
    watch.add_argument("--scene", required=True)
    watch.add_argument("--action", default="status", choices=["start", "stop", "status"])
    watch.add_argument("--set", action="append", metavar="KEY=VALUE")

    web = sub.add_parser("web")
    web.add_argument("--scene", required=True)
    web.add_argument("--port", type=int, default=0)
    web.add_argument("--mode", default="light", choices=["light", "watch"])
    web.add_argument("--open-browser", action="store_true")

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--scene", required=True)
    daemon.add_argument("--action", default="status",
                        choices=["start", "stop", "status"])
    daemon.add_argument("--port", type=int, default=0)
    daemon.add_argument("--idle", type=int, default=0, help="空闲多少秒自动退出")

    # 网页遥控：ai 工具 serial_web 用的。web 是前台阻塞，webctl 是"起完就返回"
    webctl = sub.add_parser("webctl")
    webctl.add_argument("--action", default="status",
                        choices=["open", "status", "close"])
    webctl.add_argument("--scene", default="", help="留空 = 唯一的场景")
    webctl.add_argument("--port", type=int, default=0, help="0 = 自动挑（8898 起）")
    webctl.add_argument("--mode", default="light", choices=["light", "watch"])
    webctl.add_argument("--open-browser", action="store_true")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.action_name:
        parser.print_help()
        return 1
    paths = _paths()
    handler = {
        "scan": cmd_scan,
        "info": cmd_info,
        "send": cmd_send,
        "read": cmd_read,
        "status": cmd_status,
        "control": cmd_control,
        "watch": cmd_watch,
        "web": cmd_web,
        "daemon": cmd_daemon,
        "webctl": cmd_webctl,
    }[args.action_name]
    try:
        return handler(paths, args)
    except BridgeError as exc:
        return _fail(exc)
    except KeyboardInterrupt:
        return _fail(BridgeError("internal", "已中断"))
    except Exception as exc:  # 兜底：绝不把堆栈漏给插件层
        return _fail(BridgeError(
            "internal",
            "鲸歌内部错误。",
            hint=["把这条消息连同下面的 detail 一起反馈"],
            detail="%s: %s" % (type(exc).__name__, exc),
        ))


if __name__ == "__main__":
    sys.exit(main())
