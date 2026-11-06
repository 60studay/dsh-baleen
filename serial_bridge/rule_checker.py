# -*- coding: utf-8 -*-
"""规则判定的一次执行：开关判断、熔断、告警、自动处置。

抽出来的原因：**网页的 WatchLoop 和管家 Daemon 各写过一份**，逻辑还不太一样
（一个传了 no_response_streak，一个没传；熔断阈值一个用全局、一个用配置）。
两份并发逻辑放在两个文件里，改一处忘另一处就会"管家能判、网页不能判"。

现在两边都只用这一个入口。差异靠参数表达，不靠复制代码。
"""

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
    def run(self, text, source, no_response_streak=0):
        """跑一次判定。**绝不抛异常**：判定再炸也不能影响收发。"""
        watch = self.state.read().get("watch") or {}
        if not watch.get("check_enabled"):
            return
        want = str(watch.get("check_source") or "watch_only")
        if want == "watch_only" and source != "watch":
            return
        if want == "manual_only" and source != "manual":
            return
        if self.stopped:
            return

        try:
            cfg = {**self.scene.watch_cfg(), **watch}
            if self._worker is None:
                # 这个超时是"等判定子进程回一行 JSON"的预算，必须覆盖一次进程冷启动
                # （实测 ~250ms），不能用单条规则的 100ms 语义。
                self._worker = RuleWorker(
                    package_root=self.package_root,
                    timeout_ms=int(cfg.get("rule_worker_timeout_ms") or 2000))
            results, error = self._worker.evaluate(
                text, cfg, int(no_response_streak or 0), source)
            if error:
                self.logger.append("RULE", "判定已跳过：%s" % error)
                self._note_failure(error, cfg)
            else:
                self._failures = 0

            for result in results or []:
                if result.get("error"):
                    self.logger.append("RULE", "规则 %s 出错: %s"
                                       % (result.get("rule"), result.get("error")))
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
        }
        if command and self.send_action is not None:
            try:
                self.send_action(command)
                record["sent"] = True
            except Exception as exc:
                record["sent"] = False
                record["send_error"] = str(exc)
        self.state.append_alert(record)

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
