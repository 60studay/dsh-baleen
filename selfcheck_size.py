# -*- coding: utf-8 -*-
"""结构体检自检：单文件行数超阈值就报出来。

为什么要这个脚本
----------------
代码会自己长大。没有机械拦截，"这次拆干净了"只能保证几个月。

这个项目已经拆过一次（`supervisor.py` 1262 → 836 行），但**没有守卫**，
于是它又慢慢回到 836，`index.js` 涨到 986、`web/index.html` 涨到 1019。

所以阈值不能靠自觉，要靠脚本。**超了就 FAIL，下次加功能前先处理。**

阈值（写死在这里，改阈值要说明理由）
------------------------------------
    > 400 行  → WARN  还行，但要说清为什么这么大
    > 600 行  → FAIL  必须体检，下次功能前处理
    > 800 行  → FAIL  必须拆，不许再往里加

这个脚本自己也有边界：它只量**行数**，不知道代码好坏。
行数少但职责混乱的文件它照样放过。所以它是**下限守卫**，不是质量证明。

用法：
    python selfcheck_size.py            # 严格：>600 就 FAIL
    python selfcheck_size.py --strict   # 更严：>400 就 FAIL
    python selfcheck_size.py --list     # 只列出各文件行数
"""

import io
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# 阈值：改这里等于改团队约定，改之前想清楚
WARN_LINES = 400
FAIL_LINES = 600
HARD_LINES = 800

# 只量这些后缀
SUFFIXES = (".py", ".js", ".html", ".css")

# 这些目录整体跳过（第三方 / 生成物 / 归档）
SKIP_DIRS = {
    "vendor", "__pycache__", ".git", "_归档", "logs", ".runtime",
    "node_modules", "_testlogs", "_模块配置备份",
}

# 这些具体文件跳过（第三方代码的拷贝）
SKIP_FILES = {
    os.path.join("web", "index.html.bak"),
}

# 已知的、有明确理由的超标文件。
#
# 这是"棘轮"：**允许它现在这么大，但绝不许再涨**。
# 超过登记的行数 = FAIL。等你把它拆小了，把登记删掉。
#
# 为什么要有这个机制：如果自检永远红，人就会开始忽略它，
# 那守卫就白写了。所以给现状一个"合法的存量"，只拦新增。
#
# ⚠️ 往这里加新条目 = 承认你在欠债。加之前先问：能不能现在拆？
KNOWN_OVERSIZE = {
    "web/index.html": {
        "lines": 1019,
        "reason": "单文件 HTML+CSS+JS 全在一起；计划在结构体检 R2 拆成 "
                  "index.html / app.css / app.js",
    },
    "index.js": {
        "lines": 986,
        "reason": "9 个工具的定义挤在一个文件；计划在结构体检 R2 按"
                  "『工具组』拆（串口/管家/网页）",
    },
    "serial_bridge/supervisor.py": {
        "lines": 836,
        "reason": "已从 1262 拆过一轮（第 4.5 件），仍偏大；"
                  "计划在结构体检 R1（多轨做完）再拆",
    },
}

PASS = []
FAIL = []
WARN = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        return True
    FAIL.append("%s %s" % (name, detail))
    return False


def iter_source_files():
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(SUFFIXES):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, HERE)
            if rel in SKIP_FILES:
                continue
            yield rel, full


def count_lines(path):
    """数行数。读不了就返回 None（不要让一个坏文件把整个自检打死）。"""
    try:
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            return len(handle.read().splitlines())
    except Exception:
        return None


def main(argv):
    strict = "--strict" in argv
    list_only = "--list" in argv

    print("=" * 70)
    print("自检：文件行数（反屎山守卫）")
    print("=" * 70)

    rows = []
    unreadable = []
    for rel, full in iter_source_files():
        lines = count_lines(full)
        if lines is None:
            unreadable.append(rel)
            continue
        rows.append((lines, rel.replace("\\", "/")))

    rows.sort(reverse=True)

    if list_only:
        print("\n%-6s %s" % ("行数", "文件"))
        for lines, rel in rows:
            print("%-6d %s" % (lines, rel))
        print("\n合计 %d 个文件，%d 行" % (len(rows), sum(n for n, _ in rows)))
        return 0

    print("\n共 %d 个源文件，%d 行" % (len(rows), sum(n for n, _ in rows)))

    # ── 1. 所有文件都读得出来 ─────────────────────────────────────────────
    print("\n[可读性]")
    check("所有源文件可读", not unreadable,
          "读不了：%s" % unreadable[:5])

    # ── 2. 硬上限：>800 必须拆（存量登记过的走棘轮，不许涨）─────────────
    print("\n[硬上限 %d 行：必须拆]" % HARD_LINES)
    hard = []
    for lines, rel in rows:
        if lines <= HARD_LINES:
            continue
        registered = KNOWN_OVERSIZE.get(rel)
        if registered is None:
            hard.append((lines, rel))
            print("  ✗ %-32s %d 行  （没登记，新超标）" % (rel, lines))
            continue
        budget = int(registered.get("lines") or 0)
        if lines > budget:
            hard.append((lines, rel))
            print("  ✗ %-32s %d 行  （登记 %d，涨了 %d）"
                  % (rel, lines, budget, lines - budget))
        else:
            print("  ~ %-32s %d 行  （存量登记 %d，没涨）" % (rel, lines, budget))
    check("没有新增或变大的超标文件", not hard,
          "超标：%s" % ", ".join("%s(%d)" % (r, n) for n, r in hard[:6]))

    # ── 2b. 棘轮：登记的行数只许降不许升 ────────────────────────────────
    print("\n[棘轮：登记额度要写清理由]")
    for rel, info in sorted(KNOWN_OVERSIZE.items()):
        has_reason = bool(str(info.get("reason") or "").strip())
        has_budget = int(info.get("lines") or 0) > 0
        print("  %s %-30s 额度 %s 行" % ("✓" if has_reason and has_budget else "✗",
                                        rel, info.get("lines")))
        check("登记 %s 写了理由和额度" % rel, has_reason and has_budget,
              "reason=%r lines=%r" % (info.get("reason"), info.get("lines")))

    # ── 3. 失败线：>600 必须体检 ─────────────────────────────────────────
    print("\n[体检线 %d 行：下次功能前处理]" % FAIL_LINES)
    over = [(n, r) for n, r in rows
            if FAIL_LINES < n <= HARD_LINES and r not in KNOWN_OVERSIZE]
    # 已登记的老账不重复算进"体检线"，它已经在硬上限段里走棘轮了
    over = [(n, r) for n, r in over if r not in KNOWN_OVERSIZE]
    for lines, rel in over:
        print("  ! %-32s %d 行" % (rel, lines))
    if strict:
        check("没有文件超过 %d 行（严格模式）" % FAIL_LINES,
              not (hard or over),
              "超标 %d 个" % len(hard + over))
    else:
        # 默认模式下，600~800 只是记录，不算失败 —— 但会打印出来提醒
        if over:
            print("  （记录在案，不算失败；下次动这些文件前先体检）")

    # ── 4. 提示线：>400 有理由吗 ─────────────────────────────────────────
    print("\n[提示线 %d 行]" % WARN_LINES)
    warn = [(n, r) for n, r in rows if WARN_LINES < n <= FAIL_LINES]
    for lines, rel in warn:
        print("  · %-32s %d 行" % (rel, lines))
    if not warn:
        print("  （无）")

    return summary(len(over), len(hard))


def summary(over_count, hard_count):
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n结构体检没通过：")
        for item in FAIL:
            print("  · " + item)
    elif over_count:
        print("通过，但有 %d 个文件在体检线以上 —— 下次动它们之前先体检。" % over_count)
    else:
        print("所有文件都在体检线以下 ✅")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:
        # 和别的自检一样：宁可报错，也不要只留一个 traceback
        print("\n结构自检没能跑起来：%s: %s" % (type(exc).__name__, exc))
        print("这本身就是失败。")
        sys.exit(1)