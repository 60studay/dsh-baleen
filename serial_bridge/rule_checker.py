# -*- coding: utf-8 -*-
"""规则判定的一次执行：开关判断、熔断、告警、自动处置。

抽出来的原因：**网页的 WatchLoop 和管家 Daemon 各写过一份**，逻辑还不太一样
（一个传了 no_response_streak，一个没传；熔断阈值一个用全局、一个用配置）。
两份并发逻辑放在两个文件里，改一处忘另一处就会"管家能判、网页不能判"。

现在两边都只用这一个入口。差异靠参数表达，不靠复制代码。
"""

from .rules import merge_watch, normalize_check_source
from .runner import RuleWorker


class RuleChecker(object):
    def __init__(self, scene, state, logger, send_action=None, package_root=None):
        """
        scene        场景（要 watch_cfg()）
        state        共享状态（读写告警）
        logger       日志
        send_action  执行规则动作的回调：callable(command) -> None
                     由调用方提供，因为"怎么发"在网页和管家里不一样
        """
        self.scene = scene
        self.state = state
        self.logger = logger
        self.send_action = send_action
        self.package_root = package_root
        self._worker = None
        self._failures = 0
        self.stopped = False

    # ── 对外唯一入口 ────────────────────────────────────────────────────────
    def run(self, text, source, no_response_streak=0, raw_bytes=None):
        """跑一次判定。**绝不抛异常**：判定再炸也不能影响收发。

        raw_bytes 是原始字节，hex / hex_regex 判据要用（文本解码会丢 0xFF 之类）。
        """
        # 配置改了就重载：否则用户改完规则必须重启网页才生效（真实 bug）。
        # 先比 mtime，没变就不读盘——判定可能每秒跑很多次。
        try:
            self.scene.reload_if_changed()
        except Exception:
            pass
        # 开关也要**合并**看：只看状态文件的话，config 里写的 check_source 被忽略，
        # 于是"配置写 both、判定却按 watch_only 走"——判定被静默关掉，很难查。
        watch = merge_watch(self.scene.watch_cfg(), self.state.read().get("watch") or {})
        if not watch.get("check_enabled"):
            return
        want = normalize_check_source(watch.get("check_source") or "watch_only")
        if want == "watch_only" and source != "watch":
            return
        if want == "manual_only" and source != "manual":
            return
        if self.stopped:
            return

        try:
            # 合并顺序：配置打底、状态覆盖；但状态里的空值不算"设置过"
            # （否则 check_source="" 会把 "all" 盖掉，判定被静默关掉）
            cfg = watch
            if self._worker is None:
                # 这个超时是"等判定子进程回一行 JSON"的预算，必须覆盖一次进程冷启动
                # （实测 ~250ms），不能用单条规则的 100ms 语义。
                self._worker = RuleWorker(
                    package_root=self.package_root,
                    timeout_ms=int(cfg.get("rule_worker_timeout_ms") or 2000))
            results, error = self._worker.evaluate(
                text, cfg, int(no_response_streak or 0), source, raw_bytes=raw_bytes)
            if error:
                self.logger.append("RULE", "判定已跳过：%s" % error)
                self._note_failure(error, cfg)
            else:
                self._failures = 0

            for result in results or []:
                if result.get("error"):
                    self.logger.append("RULE", "规则 %s 出错: %s"
                                       % (result.get("rule"), result.get("error")))
                    # 规则**匹配上了**但动作被拒（白名单/熔断），也算"系统想动手"，
                    # 必须让用户看见。只写日志的话，用户会以为规则根本没触发。
                    if result.get("matched"):
                        self._alert_blocked(result, text, source)
                    continue
                if not result.get("matched"):
                    continue
                self._apply(result, text, source)
        except Exception as exc:      # 判定再怎么炸也不许拖垮收发
            self.logger.append("RULE", "判定异常已忽略: %s" % exc)

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _apply(self, result, text, source):
        action = result.get("action") or {}
        command = str(action.get("send") or "").strip()
        record = {
            "rule": result.get("rule"),
            "reason": result.get("reason"),
            "source": source,
            "response": (text or "")[:400],
            "action": command or "(仅通知)",
            # sent 一定要有默认值：以前没发送时这个键根本不存在，
            # 前端/脚本读不到就得靠"字段缺失"猜，很容易看成"发了"。
            "sent": False,
        }
        if command and self.send_action is not None:
            try:
                self.send_action(command)
                record["sent"] = True
            except Exception as exc:
                record["sent"] = False
                record["send_error"] = str(exc)
        self.state.append_alert(record)

    def _alert_blocked(self, result, text, source):
        """匹配上了但动作没执行：记一条"被拒绝"的告警，而不是只写日志。"""
        self.state.append_alert({
            "rule": result.get("rule"),
            "reason": result.get("reason"),
            "source": source,
            "response": (text or "")[:400],
            "action": "(被拒绝)",
            "sent": False,
            "error": result.get("error"),
        })

    def _note_failure(self, reason, cfg):
        """连续判定失败 → 停判定并记一条大警告（收发完全不受影响）。"""
        self._failures += 1
        limit = int(cfg.get("max_consecutive_failures")
                    or self.scene.watch_cfg().get("max_consecutive_failures") or 3)
        if self._failures >= limit and not self.stopped:
            self.stopped = True
            self.logger.append("RULE", "连续 %d 次判定失败，已停止判定：%s"
                               % (self._failures, reason))
            self.state.append_alert({
                "rule": "(判定熔断)",
                "reason": "连续 %d 次判定失败，已自动停止判定；串口收发不受影响" % self._failures,
                "source": "watch",
                "response": "",
                "action": "(已停止判定)",
            })
