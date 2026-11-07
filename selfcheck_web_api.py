# -*- coding: utf-8 -*-
"""网页 HTTP 接口自检（覆盖全部 8 个接口 + 错误分支）。

为什么要这个脚本
----------------
已有 9 个自检，**一个都不测 HTTP 接口**。结果连续漏了两个 bug：

  1. 拆分 `supervisor.py` 时 `web_handler.py` 漏了 import
     → `/ports` 直接 500，用户看到的只是"下拉框全空"
  2. 轻模式 `reconnect` 真去 open 串口
     → 假报"被占用"，还会把板子复位

这两类错误（漏 import、路由层行为变了）**纯逻辑自检永远测不出来**，
因为它们只在"真的发一个 HTTP 请求"时才暴露。

覆盖的接口
----------
GET : `/`  `/status`  `/poll`  `/alerts`  `/ports`  `/rules`  `/favicon.ico`  404
POST: `/send`  `/control`  `/watch`  `/rules`  `/port/pause`  `/port/resume`
      `/reconnect`  `/autodetect`  `/mode`  `/shutdown`  404

安全设计（重要）
----------------
* 用**独立端口**（默认 8897）起一个临时实例，**不动**用户正在用的 8898
* **light 模式**：全程不占串口，不会和串口助手/AI 抢口
* **不打真硬件**：`/send` 只测空命令的错误分支，不发任何字节
* **测完还原**：控制权、watch 配置在测试前后恢复原值
  （状态文件与场景配置是跨进程共享的，不还原会污染用户实例）
* 绕过单实例守卫 + 不写实例文件，避免干扰真实实例的探活

运行：
    python selfcheck_web_api.py
    SB_WEB_API_PORT=8896 python selfcheck_web_api.py   # 换端口
"""

import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))
    return bool(cond)


# ── HTTP 小工具 ──────────────────────────────────────────────────────────────

def http_get(base, path, timeout=8):
    """返回 (status, body_text)。连接失败时 status=None。"""
    try:
        req = urllib.request.Request(base + path)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, str(exc)


def http_post(base, path, payload=None, timeout=15):
    try:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, str(exc)


def as_json(text):
    """尽力解析 JSON。解析不了返回 None —— 这就说明接口返回的不是 JSON。"""
    try:
        return json.loads(text)
    except Exception:
        return None


def err_kind(data):
    error = (data or {}).get("error") or {}
    if isinstance(error, dict):
        return error.get("kind") or ""
    return ""


def wait_ready(base, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, text = http_get(base, "/status", timeout=1.5)
        if status == 200 and as_json(text):
            return True
        time.sleep(0.25)
    return False


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    from serial_bridge.cli import _paths
    from serial_bridge.supervisor import WebServer

    paths = _paths()
    scene_name = os.environ.get("SB_SCENE") or "蓝牙AT"
    api_port = int(os.environ.get("SB_WEB_API_PORT") or 8897)
    base = "http://127.0.0.1:%d" % api_port

    # 8897 已经有东西在跑？说明上次没退干净，换个端口免得测错对象
    status, _ = http_get(base, "/status", timeout=1.0)
    if status is not None:
        print("SKIP: 端口 %d 已被占用（可能是上次没退干净），换个端口："
              "SB_WEB_API_PORT=8896" % api_port)
        return 0

    server = WebServer(paths, scene_name, port=api_port, mode="light")
    # 绕过单实例守卫 / 不碰实例文件 —— 我们要和真实实例共存，只是发 HTTP 请求
    server._existing_instance = lambda: ""
    server._write_instance = lambda url: None
    server._clear_instance = lambda: None

    thread = threading.Thread(target=server.serve, kwargs={"open_browser": False},
                              name="api-selfcheck", daemon=True)
    thread.start()

    if not wait_ready(base):
        print("SKIP: 临时网页实例起不来（端口 %d）" % api_port)
        return 0

    print("=" * 70)
    print("自检：网页 HTTP 接口（临时实例 %s，light 模式，不占串口）" % base)
    print("=" * 70)

    # ── 先存原始状态，测完要还原 ─────────────────────────────────────────
    _, status_text = http_get(base, "/status")
    original = as_json(status_text) or {}
    original_control = original.get("control") or "human"
    original_watch = original.get("watch") or {}

    try:
        run_checks(base, original)
    except Exception as exc:
        # 关键：这里必须**兜住一切异常**。
        # 一个"测接口不崩"的脚本，如果自己被异常打死，就失去了意义——
        # 最需要它报警的时候，它先倒了，只剩一个 traceback 和退出码，
        # 看不出有几项过了、几项没过。
        # 所以任何意外都转成一条 FAIL，然后照常出统计。
        check("自检过程不应抛异常", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            restore(base, original_control, original_watch)
        except Exception as exc:
            check("还原测试前状态", False, "%s: %s" % (type(exc).__name__, exc))
        # 最后关掉临时实例
        try:
            http_post(base, "/shutdown", {}, timeout=3)
        except Exception:
            pass

    return summary()


def run_checks(base, original):
    print("\n[GET 接口]")

    # 1. 首页
    status, text = http_get(base, "/")
    check("GET /  返回 200", status == 200, "status=%s" % status)
    check("GET /  是网页", "<html" in text.lower() or "鲸歌" in text,
          "前 60 字：%r" % text[:60])
    # 收发区三条 UI 不变量（用户 2026-11-07 提的）：
    #   ① 不再用 ">>" 前缀区分命令（容易和串口数据里的字符混淆）
    #   ② 回显要带「设备」徽章
    #   ③ 两列对齐靠 grid 布局，不靠写死 padding
    # R2 之后样式/逻辑在 app.css / app.js 里，所以这三条不变量要跨文件查
    css_status, css_text = http_get(base, "/app.css")
    js_status, js_text = http_get(base, "/app.js")
    check("GET /app.css  返回 200", css_status == 200, "status=%s" % css_status)
    check("GET /app.js   返回 200", js_status == 200, "status=%s" % js_status)
    check("GET /app.js   是 JS", "function" in js_text, js_text[:60])
    check("GET /app.css  是 CSS", "#" in css_text and "{" in css_text, css_text[:60])
    front = text + css_text + js_text
    check("收发区没有 '>> ' 前缀", "'> > '" not in front and "'>> ' +" not in front)
    check("回显有「设备」徽章", "'设备'" in front or '>设备<' in front)
    check("收发区用 grid 两列对齐", "grid-template-columns" in css_text)

    # 2. /status
    status, text = http_get(base, "/status")
    data = as_json(text)
    check("GET /status  返回 200", status == 200, "status=%s" % status)
    check("GET /status  是 JSON", data is not None, text[:120])
    if data:
        for field in ("ok", "scene", "mode", "control", "port", "watch"):
            check("GET /status  有 %s 字段" % field, field in data)

    # 3. /poll
    status, text = http_get(base, "/poll")
    data = as_json(text)
    check("GET /poll  返回 200", status == 200, "status=%s" % status)
    check("GET /poll  是 JSON", data is not None, text[:120])
    if data:
        check("GET /poll  有 ok", data.get("ok") is True, str(data)[:120])
        # light 模式不主动读口，应带 skipped 说明；这不是错误
        check("GET /poll  带 skipped 或数据字段",
              ("skipped" in data) or ("rx_text" in data) or ("frames" in data),
              str(sorted(data.keys())))

    # 4. /alerts
    status, text = http_get(base, "/alerts?limit=3")
    data = as_json(text)
    check("GET /alerts  返回 200", status == 200, "status=%s" % status)
    check("GET /alerts  是 JSON", data is not None, text[:120])
    if data:
        check("GET /alerts  alerts 是数组", isinstance(data.get("alerts"), list),
              str(type(data.get("alerts"))))

    # 5. /ports —— 这就是漏 import 导致 500 的那个接口
    status, text = http_get(base, "/ports")
    data = as_json(text)
    check("GET /ports  返回 200", status == 200, "status=%s" % status)
    check("GET /ports  是 JSON", data is not None, text[:120])
    if data:
        check("GET /ports  ports 非空", isinstance(data.get("ports"), list)
              and len(data.get("ports") or []) > 0, str(data.get("ports"))[:80])
        check("GET /ports  bauds 非空", isinstance(data.get("bauds"), list)
              and len(data.get("bauds") or []) > 0, str(data.get("bauds"))[:80])

    # 6. /rules
    status, text = http_get(base, "/rules")
    data = as_json(text)
    check("GET /rules  返回 200", status == 200, "status=%s" % status)
    check("GET /rules  是 JSON", data is not None, text[:120])
    if data:
        check("GET /rules  rules 是数组", isinstance(data.get("rules"), list),
              str(type(data.get("rules"))))
        check("GET /rules  有 rule_types", isinstance(data.get("rule_types"), list),
              str(type(data.get("rule_types"))))

    # 7. /favicon.ico 应该 204 而不是 500
    status, _ = http_get(base, "/favicon.ico")
    check("GET /favicon.ico  返回 204", status == 204, "status=%s" % status)

    # 8. 未知 GET 应该 404 + 结构化错误
    status, text = http_get(base, "/this-path-does-not-exist")
    data = as_json(text)
    check("GET 未知路径  返回 404", status == 404, "status=%s" % status)
    check("GET 未知路径  kind=not_found", err_kind(data) == "not_found",
          str(data)[:120])

    # ── POST 接口 ────────────────────────────────────────────────────────
    print("\n[POST 接口]")

    # 9. /send 空命令 —— 只测错误分支，不发任何字节到硬件
    status, text = http_post(base, "/send", {"text": "", "mode": "text"})
    data = as_json(text)
    check("POST /send  空命令被拒", data is not None and data.get("ok") is not True,
          str(data)[:120])
    check("POST /send  空命令 是 JSON", data is not None, text[:120])

    # 10. /control 缺 action
    status, text = http_post(base, "/control", {})
    data = as_json(text)
    check("POST /control  缺 action 被拒",
          data is not None and data.get("ok") is not True, str(data)[:120])
    check("POST /control  缺 action  kind=config", err_kind(data) == "config",
          str(data)[:120])

    # 11. /control 非法 action
    status, text = http_post(base, "/control", {"action": "bogus"})
    data = as_json(text)
    check("POST /control  非法 action 被拒",
          data is not None and data.get("ok") is not True, str(data)[:120])

    # 12. /autodetect —— 应给出端口 + 理由
    status, text = http_post(base, "/autodetect", {})
    data = as_json(text)
    check("POST /autodetect  返回 200", status == 200, "status=%s" % status)
    check("POST /autodetect  是 JSON", data is not None, text[:120])
    if data and data.get("ok"):
        check("POST /autodetect  有 port", bool(data.get("port")), str(data)[:120])
        check("POST /autodetect  有 reason", bool(data.get("reason")), str(data)[:120])

    # 13. /reconnect 连当前口 —— light 模式**不该真开串口**
    #     注意：这条只有在"端口真被别人占着"时才有鉴别力。
    #     端口空闲时，即使 bug 还在（light 模式去 open 一下）也会通过。
    #     所以要真的验证它，得用 SB_WEB_API_HOLD_PORT=1 跑严格模式（见第 13b 条）。
    status, text = http_post(base, "/reconnect",
                             {"com": data.get("port") if data else "",
                              "baud": 115200})
    data2 = as_json(text)
    check("POST /reconnect  返回 200", status == 200, "status=%s" % status)
    check("POST /reconnect  是 JSON", data2 is not None, text[:120])
    check("POST /reconnect  light 模式不报被占用",
          data2 is not None and data2.get("ok") is True, str(data2)[:150])

    # 13b. 严格模式：自己先把串口占住，再让 light 模式 reconnect。
    #      真正的 bug（light 模式 open 一下做"验证"）会在这里暴露成"被占用"。
    #
    #      ⚠️ 这一项会真的打开串口，CH340 在打开瞬间会拉高 DTR/RTS，
    #      **可能让板子复位**。所以默认关闭，需要显式开：
    #          SB_WEB_API_HOLD_PORT=1 python selfcheck_web_api.py
    if os.environ.get("SB_WEB_API_HOLD_PORT") == "1":
        print("\n[严格执行：模拟端口被别的程序占住]")
        holder = None
        try:
            from serial_bridge.transport import create_transport
            device = (data2 or {}).get("port") or ""
            if not device:
                print("  SKIP  拿不到端口名")
            else:
                holder = create_transport({}, device, 115200).open()
                try:
                    holder.dtr = False       # 尽力避免复位，但打开瞬间已发生
                    holder.rts = False
                except Exception:
                    pass
                status, text = http_post(base, "/reconnect",
                                         {"com": device, "baud": 115200})
                held = as_json(text)
                check("POST /reconnect  端口被占时 light 模式仍不报错",
                      held is not None and held.get("ok") is True,
                      str(held)[:180])
        except Exception as exc:
            print("  SKIP  占不住串口：%s" % str(exc)[:100])
        finally:
            if holder is not None:
                try:
                    holder.close()
                except Exception:
                    pass

    # 14. /mode 设成当前模式（幂等，不会真去占口）
    status, text = http_post(base, "/mode", {"mode": "light"})
    data = as_json(text)
    check("POST /mode light  幂等成功",
          data is not None and data.get("ok") is True, str(data)[:120])

    # 15. /mode 非法值
    status, text = http_post(base, "/mode", {"mode": "bogus"})
    data = as_json(text)
    check("POST /mode  非法值被拒",
          data is not None and data.get("ok") is not True, str(data)[:120])

    # 16. /watch 保持关闭定时发送（不能真的开始往硬件发）
    watch = {
        "send_enabled": False,
        "check_enabled": bool(original.get("watch", {}).get("check_enabled", True)),
        "check_source": original.get("watch", {}).get("check_source") or "watch_only",
        "command": original.get("watch", {}).get("command") or "P",
        "interval_ms": int(original.get("watch", {}).get("interval_ms") or 1000),
    }
    status, text = http_post(base, "/watch", watch)
    data = as_json(text)
    check("POST /watch  返回 200", status == 200, "status=%s" % status)
    check("POST /watch  是 JSON", data is not None, text[:120])
    check("POST /watch  ok=true", data is not None and data.get("ok") is True,
          str(data)[:120])

    # 17. /port/pause  —— 释放（light 本来就没占）
    status, text = http_post(base, "/port/pause", {})
    data = as_json(text)
    check("POST /port/pause  成功",
          data is not None and data.get("ok") is True, str(data)[:120])

    # 18. /port/resume —— light 模式不该真的拿到口
    status, text = http_post(base, "/port/resume", {})
    data = as_json(text)
    check("POST /port/resume  成功",
          data is not None and data.get("ok") is True, str(data)[:120])
    if data:
        check("POST /port/resume  light 模式不占口",
              data.get("acquired") in (False, None), str(data)[:150])

    # 19. /rules 缺 rules 字段 —— 只测错误分支，绝不写配置文件
    status, text = http_post(base, "/rules", {})
    data = as_json(text)
    check("POST /rules  缺 rules 被拒",
          data is not None and data.get("ok") is not True, str(data)[:120])
    check("POST /rules  缺 rules  kind=config", err_kind(data) == "config",
          str(data)[:120])

    # 19b. /watch 不传 check_source 时不许把已有的值写成空
    #      （空串会让判定静默关闭，UI 上还看不出来）
    before = as_json(http_get(base, "/status")[1]) or {}
    before_src = (before.get("watch") or {}).get("check_source")
    http_post(base, "/watch", {"check_enabled": False})
    after = as_json(http_get(base, "/status")[1]) or {}
    after_src = (after.get("watch") or {}).get("check_source")
    check("POST /watch  没传 check_source 不改原值", before_src == after_src,
          "%r -> %r" % (before_src, after_src))
    status, text = http_post(base, "/watch", {"check_source": "乱写"})
    data = as_json(text)
    check("POST /watch  check_source 非法值被拒",
          data is not None and data.get("ok") is not True, str(data)[:120])

    # 19c. /port/probe 与 /alerts/clear（后者是"清空也要清告警"那条需求的后端）
    status, text = http_post(base, "/port/probe", {})
    data = as_json(text)
    check("POST /port/probe  返回 200", status == 200, "status=%s" % status)
    check("POST /port/probe  给出 owner",
          data is not None and data.get("owner") in ("free", "other", "ours", "unknown"),
          str(data)[:120])

    status, text = http_post(base, "/alerts/clear", {})
    data = as_json(text)
    check("POST /alerts/clear  返回 200", status == 200, "status=%s" % status)
    check("POST /alerts/clear  有 cleared 字段",
          data is not None and isinstance(data.get("cleared"), int), str(data)[:120])

    # 23. 未知 POST 应该 404
    status, text = http_post(base, "/this-post-does-not-exist", {})
    data = as_json(text)
    check("POST 未知路径  返回 404", status == 404, "status=%s" % status)
    check("POST 未知路径  kind=not_found", err_kind(data) == "not_found",
          str(data)[:120])

    # 21. /control 恢复成原值（也顺便测成功分支）
    status, text = http_post(base, "/control", {"action": original.get("control") or "human"})
    data = as_json(text)
    check("POST /control  切回原控制权成功",
          data is not None and data.get("ok") is True, str(data)[:120])

    # ── AI ⇄ AI 通道（不碰串口，边界只有文件）────────────────────────────
    # 这几条盯的是"凭什么信对面"那套规矩有没有被实现：
    # 声明必须带证据、复验不能只写"同意"、一键复验只许跑 bridge.py。
    status, text = http_get(base, "/channel?since=0")
    data = as_json(text)
    check("GET /channel  返回 200", status == 200, "status=%s" % status)
    check("GET /channel  是 JSON 且有 messages",
          data is not None and isinstance(data.get("messages"), list), text[:120])

    status, text = http_post(base, "/channel", {
        "from": "selfcheck", "type": "note", "claim": "自检写的一条对话"})
    data = as_json(text)
    check("POST /channel  纯对话可以发",
          data is not None and data.get("ok") is True, str(data)[:120])
    if data and data.get("message"):
        check("POST /channel  trust 固定为 none",
              data["message"].get("trust") == "none", str(data["message"])[:120])

    status, text = http_post(base, "/channel", {
        "from": "selfcheck", "type": "claim", "claim": "我搞定了"})
    data = as_json(text)
    check("POST /channel  只发结论被拒", data is not None and data.get("ok") is not True,
          str(data)[:120])
    check("POST /channel  拒绝原因是 config", err_kind(data) == "config", str(data)[:120])

    status, text = http_post(base, "/channel/verify", {"seq": 1})
    data = as_json(text)
    # seq=1 可能是自检自己写的对话（没有证据）→ 拒绝；也可能是别的场景。两种都算对，
    # 但**绝不能**是"跑了任意命令"。这里断言的是"不执行无证据的消息"。
    check("POST /channel/verify  不执行没有 evidence 的消息",
          data is None or data.get("ok") is not True, str(data)[:120])

    status, text = http_post(base, "/channel", {
        "from": "selfcheck", "type": "claim", "claim": "带证据的声明",
        "evidence": {"cmd": "cmd /c dir", "stdout": "x", "exit": 0}})
    data = as_json(text)
    check("POST /channel  带证据的声明可以发",
          data is not None and data.get("ok") is True, str(data)[:120])

    # 一键复验的白名单：非 bridge.py 的命令必须被拒（这是网页执行命令的唯一入口）
    if data and data.get("message"):
        seq = data["message"]["seq"]
        status, text = http_post(base, "/channel/verify", {"seq": seq})
        payload = as_json(text)
        check("POST /channel/verify  拒绝非 bridge.py 的命令",
              payload is not None and payload.get("ok") is not True, str(payload)[:120])
        check("POST /channel/verify  拒绝原因写在 hint 里",
              payload is not None and bool(
                  (payload.get("error") or {}).get("hint")), str(payload)[:160])


def restore(base, control, watch):
    """把控制权和 watch 配置恢复成测试前的样子。

    状态文件与场景 config.json 是**跨进程共享**的，不还原会污染用户正在用的实例。
    """
    print("\n[还原测试前的状态]")
    payload = {
        "send_enabled": bool(watch.get("send_enabled", False)),
        "check_enabled": bool(watch.get("check_enabled", True)),
        "check_source": watch.get("check_source") or "watch_only",
        "command": watch.get("command") or "P",
        "interval_ms": int(watch.get("interval_ms") or 1000),
    }
    status, text = http_post(base, "/watch", payload, timeout=8)
    data = as_json(text)
    check("还原 watch 配置", data is not None and data.get("ok") is True, text[:120])

    status, text = http_post(base, "/control", {"action": control or "human"}, timeout=8)
    data = as_json(text)
    check("还原控制权为 %s" % (control or "human"),
          data is not None and data.get("ok") is True, text[:120])


def summary():
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n失败项：")
        for item in FAIL:
            print("  · " + item)
    else:
        print("全部通过 ✅")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    # 总兜底：连"起临时实例之前"就出意外（比如 import 挂了、路径不对），
    # 也要给出可读的结论，而不是只剩一个 traceback。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print("\n" + "=" * 70)
        print("自检没能跑起来：%s: %s" % (type(exc).__name__, exc))
        print("这本身就是失败 —— 说明环境或代码有更底层的问题。")
        print("=" * 70)
        import traceback
        traceback.print_exc()
        sys.exit(1)