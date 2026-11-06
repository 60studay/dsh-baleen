# -*- coding: utf-8 -*-
"""规则执行的超时隔离（v2 交接文档 3.6 "隔离要求"）。

## 为什么不能用"线程 + join(timeout)"

第一版就是这个，实测**卡死**：

    re.search("(a+)+$", "aaaa…!")      # 灾难性回溯
    thread.join(0.05)                   # ← 永远不返回

原因：CPython 的 `re` 在 C 层匹配期间**持有 GIL**，主线程连切换都做不到，
`join(timeout)` 形同虚设。"单条规则超时就跳过"用线程实现不了。

## 为什么也不用"每次判定起一个子进程"

能杀，但实测 Windows 上 `python -m serial_bridge.runner` 冷启动要 ~245 ms，
比规则本身的预算还长，等于每条规则都超时。

## 实际做法：常驻工作子进程

`RuleWorker` 起一个长驻子进程，用 stdin/stdout 收发一行行 JSON：
- 正常：一次判定只花 IPC 的几毫秒
- 卡住（ReDoS / 死循环）：父进程等满预算就 `kill()`，然后**重新起一个**，
  崩溃的进程连同它的回溯一起消失
- 子进程里的任何异常都不会冒泡到收发线程

代价是每次重起 ~250 ms，但只发生在"规则真的卡住"时，符合"宁可跳过也不能拖住收发"。
"""

import json
import os
import queue
import subprocess
import sys
import threading

DEFAULT_TIMEOUT_MS = 500

# 父进程侧看到的失败形态（都只记日志，不抛异常）
ERR_TIMEOUT = "timeout"
ERR_CRASH = "crash"


class RuleWorker(object):
    """常驻判定子进程。线程安全的 evaluate()，永不抛异常。"""

    def __init__(self, package_root=None, timeout_ms=DEFAULT_TIMEOUT_MS):
        self.package_root = package_root or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.timeout_ms = int(timeout_ms)
        self._proc = None
        self._lock = threading.Lock()
        self._stderr_thread = None
        self.last_stderr = ""
        self.restarts = 0

    # ── 进程管理 ────────────────────────────────────────────────────────────
    def _spawn(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "serial_bridge.runner"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=self.package_root, env=env,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="rule-worker-stderr", daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self.last_stderr = line.strip()
        except Exception:
            pass

    def _kill(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def close(self):
        with self._lock:
            self._kill()

    # ── 判定 ────────────────────────────────────────────────────────────────
    def evaluate(self, text, watch_cfg, streak=0, source="watch"):
        """返回 (results, error)。results 是 RuleResult.to_dict() 列表。"""
        request = {
            "text": "" if text is None else str(text),
            "streak": int(streak or 0),
            "source": source,
            "watch": _jsonable(watch_cfg or {}),
        }
        with self._lock:
            return self._evaluate_locked(request)

    def _evaluate_locked(self, request):
        try:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
                self.restarts += 1
            proc = self._proc
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()

            box = queue.Queue(maxsize=1)

            def reader():
                try:
                    box.put(proc.stdout.readline())
                except Exception:
                    box.put("")

            thread = threading.Thread(target=reader, name="rule-worker-read", daemon=True)
            thread.start()
            try:
                line = box.get(timeout=self.timeout_ms / 1000.0)
            except queue.Empty:
                # 卡住了：杀掉重来，本次判定放弃
                self._kill()
                return [], "规则判定超时（>%dms），已终止并重启判定进程" % self.timeout_ms

            if not line:
                detail = self.last_stderr or "判定子进程意外退出"
                self._kill()
                return [], "判定子进程异常：%s" % detail

            data = json.loads(line)
            return data.get("results") or [], data.get("error")
        except Exception as exc:
            self._kill()
            return [], "判定调度失败：%s" % exc


def _jsonable(cfg):
    try:
        json.dumps(cfg)
        return cfg
    except (TypeError, ValueError):
        out = {}
        for key, value in dict(cfg).items():
            try:
                json.dumps(value)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = str(value)
        return out


# 一次性便捷函数（测试用；常驻场景请用 RuleWorker）
def evaluate_isolated(text, watch_cfg, streak=0, source="watch", timeout_ms=DEFAULT_TIMEOUT_MS):
    worker = RuleWorker(timeout_ms=timeout_ms)
    try:
        return worker.evaluate(text, watch_cfg, streak, source)
    finally:
        worker.close()


# ── 子进程入口 ──────────────────────────────────────────────────────────────
def main(argv=None):
    """一行 JSON 进，一行 JSON 出，循环到 stdin 关闭。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from .rules import RuleEngine

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            engine = RuleEngine(payload.get("watch") or {})
            results = engine.evaluate(
                payload.get("text", ""),
                payload.get("streak", 0),
                payload.get("source", "watch"),
            )
            out = {
                "results": [r.to_dict() for r in results],
                "stopped": engine.stopped,
                "consecutive_failures": engine.consecutive_failures,
            }
        except Exception as exc:
            out = {"results": [], "error": "%s: %s" % (type(exc).__name__, exc)}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
