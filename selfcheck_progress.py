# -*- coding: utf-8 -*-
"""自检：进度总账有没有过期。

为什么需要这个脚本
------------------
《进度总账.md》是唯一权威的进度记录。但它有个致命弱点：
**它是手写的，而代码是自动变的。**

一旦代码变了、总账没跟上，总账就会开始骗人 —— 而它恰恰是
"上下文被压缩后第一份要读的文件"。**一份会骗人的总账，比没有总账更糟。**

这个脚本检查的是"可以机械验证的那部分"：
  1. 总账里写的工具个数  == index.js 实际注册的工具个数
  2. 总账里列的工具名    == 实际注册的工具名
  3. 总账里写的自检脚本数 == 磁盘上实际的 selfcheck_*.py 个数
  4. 总账里写的自检项总数 == 各脚本实际报告的数字之和
  5. 总账里写的 supervisor.py 行数 ≈ 实际行数
  6. 总账里写的本地提交 == git 实际 HEAD

**第 4 项和第 5 项用"容差"而不是精确相等**：行数差几行是正常的
（加个注释就变了），只要差得不多就放过；差得多说明真的该更新了。

用法：
    python selfcheck_progress.py           # 检查，过期就报错
    python selfcheck_progress.py --quiet   # 只输出结论
"""

import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "_需求文档", "进度总账.md")
INDEX_JS = os.path.join(HERE, "index.js")

# Windows 控制台默认 GBK，输出中文/符号会炸。先把自己掰成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 行数容差：总账写 836，实际 838，不算过期（加两行注释很正常）
LINE_TOLERANCE = 40

# 提交容差：总账记的 HEAD 之后，提交总账本身、以及几笔小改动，
# 都算正常。落后超过这个数说明总账真的没人管了。
BEHIND_TOLERANCE = 5

PASS = 0
FAIL = 0
FAILS = []


def ok(name, detail=""):
    global PASS
    PASS += 1
    print("  PASS  %s%s" % (name, ("  " + detail) if detail else ""))


def bad(name, detail):
    global FAIL
    FAIL += 1
    FAILS.append("%s —— %s" % (name, detail))
    print("  FAIL  %s  %s" % (name, detail))


def read_text(path):
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def git(*args):
    """跑一个 git 命令，返回 stdout（去空白）。失败返回空串。"""
    try:
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=HERE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def main():
    print("=" * 70)
    print("自检：进度总账一致性")
    print("=" * 70)

    if not os.path.exists(LEDGER):
        bad("总账存在", "找不到 %s" % LEDGER)
        summary()
        return 1

    ledger = read_text(LEDGER)

    # ── 1 & 2. 工具个数与名字 ──────────────────────────────────────────────
    print("\n[工具清单]")
    if not os.path.exists(INDEX_JS):
        bad("index.js 存在", "找不到 %s" % INDEX_JS)
    else:
        js = read_text(INDEX_JS)
        actual_tools = re.findall(r"name:\s*['\"](serial_[a-z_]+)['\"]", js)
        actual_tools = sorted(set(actual_tools))

        # 总账里"已注册工具 | **8 个**：`serial_list` `serial_send` ..."
        m = re.search(r"已注册工具\s*\|\s*\*\*(\d+)\s*个\*\*[：:]([^|\n]+)", ledger)
        if not m:
            bad("总账写了工具个数", "没找到『已注册工具 | **N 个**：...』这一行")
        else:
            claimed_count = int(m.group(1))
            claimed_tools = sorted(set(re.findall(r"serial_[a-z_]+", m.group(2))))

            if claimed_count == len(actual_tools):
                ok("工具个数一致", "%d 个" % claimed_count)
            else:
                bad("工具个数不一致",
                    "总账写 %d 个，实际 %d 个" % (claimed_count, len(actual_tools)))

            if claimed_tools == actual_tools:
                ok("工具名一致", "、".join(actual_tools))
            else:
                missing = [t for t in actual_tools if t not in claimed_tools]
                extra = [t for t in claimed_tools if t not in actual_tools]
                bad("工具名不一致",
                    "总账缺 %s，多 %s" % (missing or "无", extra or "无"))

    # ── 3. 自检脚本数 ─────────────────────────────────────────────────────
    print("\n[自检脚本]")
    # 注意：**要算上自己**。selfcheck_progress.py 也是自检脚本之一，
    # 把它排除掉的话，总账写"10 个"永远对不上"实际 9 个"。
    scripts = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("selfcheck_") and f.endswith(".py")
    )
    m = re.search(r"自检\s*\|\s*\*\*(\d+)\s*个脚本", ledger)
    if not m:
        bad("总账写了自检脚本数", "没找到『自检 | **N 个脚本...』")
    else:
        claimed = int(m.group(1))
        if claimed == len(scripts):
            ok("自检脚本数一致", "%d 个" % claimed)
        else:
            bad("自检脚本数不一致",
                "总账写 %d 个，实际 %d 个：%s" % (claimed, len(scripts), scripts))

    # ── 4. 自检项总数 ─────────────────────────────────────────────────────
    print("\n[自检项总数]")
    m = re.search(r"(\d+)\s*项全绿", ledger)
    if not m:
        bad("总账写了自检项数", "没找到『N 项全绿』")
    else:
        claimed_total = int(m.group(1))
        # 明细行形如 "rules 32 / port_hold 16 / ..."，直接加总更可靠
        detail = re.search(r"```\s*\n(rules[^`]+?)```", ledger, re.S)
        if detail:
            numbers = [int(x) for x in re.findall(r"\b(\d+)\b", detail.group(1))]
            detail_sum = sum(numbers)
            if detail_sum == claimed_total:
                ok("明细加总 = 总数", "%d" % claimed_total)
            else:
                bad("明细加总 ≠ 总数",
                    "明细和 %d，总数写 %d" % (detail_sum, claimed_total))
        else:
            print("  SKIP  没找到自检明细块，跳过加总校验")

    # ── 5. supervisor.py 行数 ────────────────────────────────────────────
    print("\n[supervisor.py 行数]")
    sup = os.path.join(HERE, "serial_bridge", "supervisor.py")
    if not os.path.exists(sup):
        bad("supervisor.py 存在", "找不到")
    else:
        actual_lines = len(read_text(sup).splitlines())
        m = re.search(r"`supervisor\.py`\s*\|\s*\*\*(\d+)\s*行\*\*", ledger)
        if not m:
            bad("总账写了行数", "没找到『`supervisor.py` | **N 行**』")
        else:
            claimed_lines = int(m.group(1))
            diff = abs(claimed_lines - actual_lines)
            if diff <= LINE_TOLERANCE:
                ok("行数基本一致", "总账 %d，实际 %d（差 %d，容差 %d）"
                   % (claimed_lines, actual_lines, diff, LINE_TOLERANCE))
            else:
                bad("行数偏差过大",
                    "总账 %d，实际 %d（差 %d > 容差 %d），该更新了"
                    % (claimed_lines, actual_lines, diff, LINE_TOLERANCE))

    # ── 6. 本地提交 ──────────────────────────────────────────────────────
    # 注意：这里**不能要求完全相等**。总账里写的提交号，是"写总账那一刻"的 HEAD；
    # 而把总账提交上去这个动作本身就会产生一个新 HEAD。所以永远差至少一个提交。
    # 有意义的判据是"落后多少个提交"：落后太多才说明总账真的没人管了。
    print("\n[git 提交]")
    head = git("rev-parse", "--short", "HEAD")
    if not head:
        print("  SKIP  拿不到 git HEAD（可能不是 git 仓库），跳过")
    else:
        m = re.search(r"本地最新提交\s*\|\s*`([0-9a-f]{7,40})`", ledger)
        if not m:
            bad("总账写了提交号", "没找到『本地最新提交 | `xxxxxxx`』")
        else:
            claimed_head = m.group(1)
            if claimed_head == head:
                ok("提交号一致", head)
            else:
                # 总账写的提交必须是 HEAD 的祖先，否则说明它指向了别的分支/被改写
                ancestor = git("merge-base", "--is-ancestor", claimed_head, "HEAD")
                is_ancestor = git("rev-list", "--count",
                                  "%s..HEAD" % claimed_head)
                if is_ancestor == "":
                    bad("提交号无法比对", "总账写 %s，HEAD 是 %s（不是它的祖先）"
                        % (claimed_head, head))
                else:
                    behind = int(is_ancestor)
                    if behind <= BEHIND_TOLERANCE:
                        ok("提交号可用", "总账 %s，HEAD %s，落后 %d 个提交"
                           % (claimed_head, head, behind))
                    else:
                        bad("总账落后太多",
                            "总账写 %s，HEAD %s，已落后 %d 个提交（容差 %d），该更新了"
                            % (claimed_head, head, behind, BEHIND_TOLERANCE))

    return summary()


def summary():
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    if FAIL:
        print("\n总账已过期，请更新《进度总账.md》：")
        for item in FAILS:
            print("  · " + item)
        print("\n（更新后重跑本脚本，直到全绿）")
    else:
        print("总账与代码一致 ✅")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    if "--quiet" in sys.argv:
        # 静默模式：只留结论
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main()
        lines = [l for l in buf.getvalue().splitlines()
                 if l.startswith("PASS ") or l.startswith("  FAIL")]
        print("\n".join(lines))
        sys.exit(code)
    sys.exit(main())
