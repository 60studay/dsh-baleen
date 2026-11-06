# -*- coding: utf-8 -*-
"""网页服务的"遥控器"：启动 / 查询 / 关闭，给 AI 工具用。

为什么要单独一层：`bridge.py web` 是**前台阻塞**的，AI 调它会把工具调用挂住。
这里改成"派一个后台子进程 + 等它就绪 + 立刻返回 URL"，AI 拿到 URL 直接贴给用户。

三条硬要求（都是实测踩过的）：
1. 幂等：同一场景已经有实例在跑，就返回它，不再起第二个（两个进程会抢串口）
2. 端口要挑：8899 常被 DSH 自己占着，被占就往后找，返回**真实**端口
3. close 要真的释放：不能只停 HTTP，串口句柄也得还回去
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .errors import BridgeError
from .state import _safe_name

DEFAULT_PORT = 8898          # 8899 被 DSH Web GUI 占着，默认从 8898 起
PORT_SEARCH_LIMIT = 20


# ── 实例文件 ────────────────────────────────────────────────────────────────
def instance_path(paths, scene):
    return os.path.join(paths["runtime_root"], "state",
                        "%s.web.json" % _safe_name(scene))


def read_instance(paths, scene):
    """返回实例信息 dict（含 url/pid/port）；没有或已死返回 None。"""
    path = instance_path(paths, scene)
    try:
        with open(path, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    url = str(info.get("url") or "")
    if not url:
        return None
    status = _get(url, "/status", timeout=1.0)
    if not status:
        return None
    info["status"] = status
    return info


def _get(url, path, timeout=2.0):
    try:
        with urllib.request.urlopen(url + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _post(url, path, payload, timeout=5.0):
    request = urllib.request.Request(
        url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


# ── 端口 ────────────────────────────────────────────────────────────────────
def _port_free(port):
    """端口能不能用。

    两个坑，都在 Windows 上实测踩到：
    1. 带 SO_REUSEADDR 的 bind 在 Windows 上**能抢占已占用的端口**，
       于是"检测"永远返回可用 —— 必须不带复用地试绑。
    2. 只试绑还不够稳：先探一下有没有人在监听。
    """
    port = int(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False                    # 已经有人在听
    except Exception:
        pass
    finally:
        probe.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))      # 不设 SO_REUSEADDR
        return True
    except OSError:
        return False
    finally:
        sock.close()


def pick_port(preferred=0):
    """从 preferred 开始找一个能绑的端口。返回 (port, tried)。"""
    start = int(preferred or DEFAULT_PORT)
    tried = []
    for offset in range(PORT_SEARCH_LIMIT):
        port = start + offset
        tried.append(port)
        if _port_free(port):
            return port, tried
    raise BridgeError(
        "config",
        "没有可用端口：从 %d 起试了 %d 个都被占用。" % (start, PORT_SEARCH_LIMIT),
        hint=["显式指定 --port", "关掉占用端口的程序后再试"],
    )


# ── 动作 ────────────────────────────────────────────────────────────────────
def open_web(paths, scene, port=0, mode="light", open_browser=False, wait_s=12.0):
    """起一个后台网页实例并返回它的 URL。已经在跑就直接返回现有的。"""
    existing = read_instance(paths, scene)
    if existing:
        return {
            "running": True,
            "already_running": True,
            "url": existing.get("url"),
            "port": existing.get("port"),
            "pid": existing.get("pid"),
            "scene": scene,
            "mode": (existing.get("status") or {}).get("mode"),
            "note": "该场景已经有网页实例在跑，直接复用（不会再起第二个）",
        }

    chosen, tried = pick_port(port)
    env = dict(os.environ)
    vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "vendor", "pyserial-3.5")
    parts = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    if os.path.isdir(vendor):
        parts.append(vendor)
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)

    argv = [sys.executable, "-m", "serial_bridge.cli", "web",
            "--scene", scene, "--port", str(chosen), "--mode", mode]
    if open_browser:
        argv.append("--open-browser")
    subprocess.Popen(
        argv,
        cwd=parts[0],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + max(3.0, float(wait_s))
    while time.time() < deadline:
        time.sleep(0.2)
        info = read_instance(paths, scene)
        if info:
            status = info.get("status") or {}
            return {
                "running": True,
                "already_running": False,
                "url": info.get("url"),
                "port": info.get("port"),
                "pid": info.get("pid"),
                "scene": scene,
                "mode": status.get("mode") or mode,
                "note": "网页已启动（后台进程，关掉终端也不影响）",
            }
    raise BridgeError(
        "no_device",
        "网页没起来（等了 %.0f 秒）。" % wait_s,
        hint=["手工跑一次看错误：python bridge.py web --scene %s --port %d" % (scene, chosen),
              "端口可能被别的程序抢了，换个 --port"],
    )


def status_web(paths, scene):
    info = read_instance(paths, scene)
    if not info:
        return {"running": False, "scene": scene}
    status = info.get("status") or {}
    return {
        "running": True,
        "url": info.get("url"),
        "port": info.get("port"),
        "pid": info.get("pid"),
        "scene": scene,
        "mode": status.get("mode"),
        "control": status.get("control"),
        "port_held": status.get("port_held"),
        "transport": status.get("transport"),
    }


def close_web(paths, scene, wait_s=10.0):
    """停掉网页实例，并确认端口的**真的**放开了才返回。

    只等"实例文件消失"是不够的：文件是进程退出时删的，但 HTTP 端口
    可能还在收尾（实测要 ~2.5 秒）。这段时间里如果立刻返回"已停止"，
    用户马上开串口助手仍会看到端口被占——所以这里以"端口能重新绑定"为准。
    """
    info = read_instance(paths, scene)
    if not info:
        return {"running": False, "stopped": False, "scene": scene,
                "note": "该场景没有在跑的网页"}
    url = info.get("url")
    port = int(info.get("port") or 0)
    pid = info.get("pid")

    _post(url, "/shutdown", {}, timeout=3.0)

    deadline = time.time() + max(3.0, float(wait_s))
    while time.time() < deadline:
        time.sleep(0.2)
        gone = read_instance(paths, scene) is None
        # 端口能绑定 = 真的释放了（光看文件会早退）
        freed = _port_free(port) if port else True
        if gone and freed:
            return {"running": False, "stopped": True, "scene": scene,
                    "released": True,
                    "note": "网页已停止，端口已释放；Windows 上串口句柄可能还要 1~2 秒"
                            "才彻底归还，立刻打开失败的话稍等一下再试"}

    # 兜底：进程还赖着就结束它，再确认一次
    if pid:
        try:
            os.kill(int(pid), 9)
        except Exception:
            pass
        time.sleep(0.5)
    try:
        os.remove(instance_path(paths, scene))
    except OSError:
        pass
    freed = _port_free(port) if port else True
    return {"running": False, "stopped": True, "scene": scene,
            "released": bool(freed),
            "note": "HTTP 没在时限内退出，已强制结束进程" if freed
                    else "已强制结束进程，但端口仍被占用（可能被别的程序接手）"}

