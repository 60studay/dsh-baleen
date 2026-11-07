# -*- coding: utf-8 -*-
"""控制权门禁自测（不需要硬件、不需要网页）。

针对实测反馈：
  serial_send 执行前必须检查控制权状态：只有 state == "ai" 才允许发送，
  human / paused 一律拒绝。

以及另一个反馈：
  /control 用 {"action":"human"} 返回 400，但 {"action":"ai"} 正常。
  ——根因不是动作名，是"已经是 human 时再点[人类接管]"被当成非法迁移。
  现在重复点同一个按钮应当是幂等的 200。

运行：python selfcheck_control_gate.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.state import SharedState, STATE_AI, STATE_HUMAN, STATE_PAUSED

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


# 与 supervisor.WebServer.control / cli.cmd_control 保持同一张表
MAPPING = {
    "ai": (STATE_AI, "交给AI", {STATE_HUMAN, STATE_PAUSED}),
    "pause": (STATE_PAUSED, "暂停AI", {STATE_AI}),
    "resume": (STATE_AI, "继续AI", {STATE_PAUSED}),
    "human": (STATE_HUMAN, "人类接管", {STATE_AI, STATE_PAUSED}),
}


def apply_action(state, action):
    """复刻两处 control 的判定顺序：幂等 → 源状态校验 → 迁移。"""
    target, label, sources = MAPPING[action]
    current = state.read().get("state")
    if current == target:
        return "idempotent", current
    if current not in sources:
        return "rejected", current
    state.set_state(target, "test", label)
    return "changed", state.read().get("state")


def main():
    import tempfile
    import shutil

    root = tempfile.mkdtemp(prefix="gatetest-")
    try:
        st = SharedState(root, "g")

        print("\n[1] AI 门禁：只有 ai 放行")
        for value, expected in ((STATE_AI, True), (STATE_HUMAN, False), (STATE_PAUSED, False)):
            st.force_state(value, "test", "set")
            allowed, _ = st.check_ai_allowed()
            check("state=%-6s → ai_allowed=%s" % (value, expected), allowed is expected)

        print("\n[2] 拒绝时必须给得出可操作的下一步")
        st.force_state(STATE_AI, "test", "set")
        check("ai 时无阻塞原因", st.ai_block_reason() is None)
        st.force_state(STATE_AI, "test", "set")
        st.set_state(STATE_PAUSED, "test", "暂停")
        reason = st.ai_block_reason() or ""
        check("paused 提示去点[继续AI]", "继续AI" in reason, reason)
        st.force_state(STATE_HUMAN, "test", "set")
        reason = st.ai_block_reason() or ""
        check("human 提示去点[交给AI]", "交给AI" in reason, reason)

        print("\n[3] 门禁不受 presence 影响（取消了两段式宽松放行）")
        st.force_state(STATE_HUMAN, "test", "set")
        st.update(presence=0)  # 没人在看网页
        check("没人在看也必须挡住", st.check_ai_allowed()[0] is False)
        import time as _t
        st.update(presence=_t.time())  # 有人在看
        check("有人在看也挡住", st.check_ai_allowed()[0] is False)

        print("\n[4] /control 幂等：重复点同一个按钮应当成功")
        st.force_state(STATE_HUMAN, "test", "set")
        for action in ("human", "human", "human"):
            result, value = apply_action(st, action)
            check("human 重复点（%s）" % action, result == "idempotent" and value == STATE_HUMAN,
                  (result, value))
        apply_action(st, "ai")
        check("human → ai 生效", st.read()["state"] == STATE_AI)
        for action in ("ai", "ai"):
            result, value = apply_action(st, action)
            check("ai 重复点", result == "idempotent" and value == STATE_AI, (result, value))
        apply_action(st, "pause")
        for action in ("pause", "pause"):
            result, value = apply_action(st, action)
            check("pause 重复点", result == "idempotent" and value == STATE_PAUSED, (result, value))
        apply_action(st, "resume")
        for action in ("resume", "resume"):
            result, value = apply_action(st, action)
            check("resume 重复点", result == "idempotent" and value == STATE_AI, (result, value))

        print("\n[5] 真正非法的迁移仍然要被拒绝")
        st.force_state(STATE_HUMAN, "test", "set")
        for action in ("pause", "resume"):
            result, _ = apply_action(st, action)
            check("human + %s 被拒绝" % action, result == "rejected", result)
        # 注意：ai+resume / paused+pause 的目标状态与当前相同，
        # 属于幂等而不是非法迁移（重复点同一个按钮不该报错）。
        st.force_state(STATE_AI, "test", "set")
        result, _ = apply_action(st, "resume")
        check("ai + resume 幂等（目标已是 ai）", result == "idempotent", result)
        st.force_state(STATE_PAUSED, "test", "set")
        result, _ = apply_action(st, "pause")
        check("paused + pause 幂等（目标已是 paused）", result == "idempotent", result)

        print("\n[6] 完整按钮矩阵（幂等优先于源状态校验）")
        matrix = [
            (STATE_HUMAN, "ai", "changed"),
            (STATE_HUMAN, "pause", "rejected"),
            (STATE_HUMAN, "resume", "rejected"),
            (STATE_HUMAN, "human", "idempotent"),
            (STATE_AI, "ai", "idempotent"),
            (STATE_AI, "pause", "changed"),
            (STATE_AI, "resume", "idempotent"),
            (STATE_AI, "human", "changed"),
            (STATE_PAUSED, "ai", "changed"),
            (STATE_PAUSED, "pause", "idempotent"),
            (STATE_PAUSED, "resume", "changed"),
            (STATE_PAUSED, "human", "changed"),
        ]
        for source, action, expected in matrix:
            st.force_state(source, "test", "set")
            result, _ = apply_action(st, action)
            check("%-6s + %-6s → %s" % (source, action, expected), result == expected, result)

        print("\n[7] AI 自授权门禁：有人看网页不许 AI 自己接管")
        from serial_bridge.errors import BridgeError
        import time as _t2

        st.force_state(STATE_HUMAN, "test", "set")
        st.update(presence=0)  # 没人看网页 = "把活交给 AI" 的场景
        try:
            st.assert_self_grant_allowed("ai")
            check("headless 时 AI 可自授权", True)
        except BridgeError:
            check("headless 时 AI 可自授权", False, "被错误拒绝")

        st.update(presence=_t2.time())  # 有人正在看网页
        try:
            st.assert_self_grant_allowed("ai")
            check("有人看网页时拒绝 AI 自授权", False, "没有拒绝")
        except BridgeError as exc:
            check("有人看网页时拒绝 AI 自授权", exc.kind == "control_blocked", exc.kind)

        try:
            st.assert_self_grant_allowed("human")
            check("人类身份不受自授权规则限制", True)
        except BridgeError:
            check("人类身份不受自授权规则限制", False, "被错误拒绝")

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
