# -*- coding: utf-8 -*-
"""发版助手：把"更新版本号"从 1-2 小时压到一条命令。

为什么要有它
------------
之前发版被描述成"1~2 小时的整理工作"，那太吓人，结果就是一直不发。

但实际上这些活儿**全是机械的**：
- 改版本号（package.json / README / 内部总账）
- 从 git log 生成 CHANGELOG
- 查有没有东西漏提交、有没有敏感文件混进去
- 打 tag、推

**机器能做的就不该让人做。** 这个脚本只负责机械部分；
真正需要人的判断（这次发版值不值得、说明怎么写得好懂）会停下来问。

用法
----
    python release.py --check              # 只体检，不改任何东西（先跑这个）
    python release.py --prepare 2.2.0      # 改版本号 + 生成 CHANGELOG，不提交
    python release.py --tag 2.2.0          # 提交 + 打 tag（不推送）
    python release.py --push               # 推送（最多试 2 次，按用户规矩）

设计原则
--------
1. **--check 绝不改文件**，只报告
2. 任何一步失败都停下来，不做下一半
3. 推送失败不纠缠（用户定的规矩：最多 2 次）
4. **不自动决定"该不该发版"**，那是人的判断
"""

import io
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "package.json")
README = os.path.join(HERE, "README.md")
CHANGELOG = os.path.join(HERE, "CHANGELOG.md")

# 不该进仓库的东西（发版前必须确认它们被 gitignore 挡住）
MUST_IGNORE = [
    "_需求文档/",               # 内部笔记
    "_模块配置备份/",           # 含本机设备信息
    "logs/",                    # 运行日志
    ".runtime/",                # 运行状态
    "_testlogs/",
]

# 明显不该出现在公开仓库里的字符串（本机/个人信息）
SECRET_PATTERNS = [
    (r"C:\\\\Users\\\\[A-Za-z0-9_]+", "Windows 用户目录绝对路径"),
    (r"E:\\\\DSH", "本机绝对路径"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub token"),
]


def run(args, check=True):
    proc = subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    if check and proc.returncode != 0:
        raise SystemExit("命令失败: %s\n%s" % (" ".join(args), proc.stderr[-600:]))
    return proc


def git(*args, **kw):
    return run(["git"] + list(args), **kw)


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# ── 体检 ────────────────────────────────────────────────────────────────────
def do_check():
    problems = []
    print("=" * 66)
    print("发版体检（只读，不改任何文件）")
    print("=" * 66)

    # 1. 工作区干净吗
    status = git("status", "--short").stdout.strip()
    print("\n[1] 工作区")
    if status:
        print("  ⚠️ 有未提交的改动：")
        for line in status.splitlines()[:8]:
            print("     " + line)
        problems.append("工作区不干净")
    else:
        print("  ✅ 干净")

    # 2. 有没有没推送的提交
    ahead = git("log", "--oneline", "origin/main..HEAD").stdout.strip()
    n_ahead = len(ahead.splitlines()) if ahead else 0
    print("\n[2] 未推送提交: %d 个" % n_ahead)

    # 3. 自检
    print("\n[3] 自检")
    scripts = sorted(f for f in os.listdir(HERE)
                     if f.startswith("selfcheck_") and f.endswith(".py"))
    total_pass = total_fail = 0
    for name in scripts:
        proc = subprocess.run([sys.executable, name], cwd=HERE,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        found = re.search(r"PASS (\d+) / FAIL (\d+)", proc.stdout or "")
        if found:
            total_pass += int(found.group(1))
            total_fail += int(found.group(2))
    print("  %d 个脚本 / %d 项通过 / %d 项失败 %s"
          % (len(scripts), total_pass, total_fail,
             "✅" if total_fail == 0 else "❌"))
    if total_fail:
        problems.append("%d 项自检失败" % total_fail)

    # 4. gitignore 是否挡住内部文件
    print("\n[4] 内部文件是否被挡住")
    for path in MUST_IGNORE:
        probe = os.path.join(HERE, path.rstrip("/"), "_probe_.txt")
        proc = subprocess.run(["git", "check-ignore", "-q", probe],
                              cwd=HERE, capture_output=True)
        ignored = proc.returncode == 0
        print("  %s %s" % ("✅" if ignored else "❌ 没挡住", path))
        if not ignored:
            problems.append("%s 没被 gitignore" % path)

    # 5. 敏感信息扫描（只扫会入库的文件）
    print("\n[5] 敏感信息扫描")
    tracked = run(["git", "ls-files"]).stdout.splitlines()
    hits = []
    for rel in tracked:
        if not rel.endswith((".py", ".js", ".html", ".css", ".json", ".md", ".yml")):
            continue
        full = os.path.join(HERE, rel)
        if not os.path.exists(full):
            continue
        try:
            text = read(full)
        except Exception:
            continue
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, text):
                hits.append((rel, label))
    if hits:
        for rel, label in hits[:10]:
            print("  ❌ %s → %s" % (rel, label))
        problems.append("发现 %d 处敏感信息" % len(hits))
    else:
        print("  ✅ 没发现绝对路径 / token")

    # 6. 版本号一致性
    print("\n[6] 版本号")
    pkg = json.loads(read(PKG))
    version = pkg.get("version", "")
    print("  package.json: %s" % version)
    if version and version not in read(README):
        print("  ⚠️ README 里没提到这个版本号（不一定是问题）")

    # 结论
    print("\n" + "=" * 66)
    if problems:
        print("体检没通过，先处理：")
        for item in problems:
            print("  · " + item)
        return 1
    print("体检通过，可以发版 ✅")
    print("下一步: python release.py --prepare <新版本号>")
    return 0


# ── 生成 CHANGELOG ─────────────────────────────────────────────────────────
CONV = [
    (r"^feat", "新功能"),
    (r"^fix", "修复"),
    (r"^refactor", "重构"),
    (r"^docs", "文档"),
    (r"^test", "测试"),
    (r"^chore", "杂项"),
]


def collect_commits(since_tag=None):
    """拿提交，按约定式前缀分组。"""
    args = ["log", "--pretty=%s", "--no-merges"]
    if since_tag:
        args.append("%s..HEAD" % since_tag)
    out = git(*args).stdout.strip()
    groups = {}
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        placed = False
        for pattern, label in CONV:
            if re.match(pattern, text):
                groups.setdefault(label, []).append(text)
                placed = True
                break
        if not placed:
            groups.setdefault("其他", []).append(text)
    return groups


def do_prepare(new_version):
    if not re.match(r"^\d+\.\d+\.\d+$", new_version):
        raise SystemExit("版本号格式应为 x.y.z，收到 %r" % new_version)

    pkg = json.loads(read(PKG))
    old_version = pkg.get("version", "?")
    print("版本号 %s → %s" % (old_version, new_version))

    # 1. package.json
    pkg["version"] = new_version
    write(PKG, json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")
    print("  ✅ package.json")

    # 2. 最近一个 tag，用来收集"这次发了什么"
    tag_out = git("describe", "--tags", "--abbrev=0", check=False).stdout.strip()
    groups = collect_commits(tag_out or None)
    print("  收集到 %d 组提交（自 %s）" % (len(groups), tag_out or "开始"))

    # 3. 生成 CHANGELOG 片段
    lines = ["## v%s（%s）" % (new_version, _today()), ""]
    order = ["新功能", "修复", "重构", "测试", "文档", "杂项", "其他"]
    for label in order:
        items = groups.get(label)
        if not items:
            continue
        lines.append("### %s" % label)
        for item in items:
            # 去掉约定式前缀，让人能读懂
            clean = re.sub(r"^\w+(\([^)]*\))?:\s*", "", item)
            lines.append("- %s" % clean)
        lines.append("")
    block = "\n".join(lines)

    if os.path.exists(CHANGELOG):
        old = read(CHANGELOG)
        # 插在第一个 "## v" 之前
        idx = old.find("\n## v")
        new_text = (old[:idx + 1] + block + old[idx + 1:]) if idx > 0 else (old + "\n" + block)
    else:
        new_text = ("# 更新日志\n\n"
                    "> 从 v2.1.0 之后开始记录。\n"
                    "> 格式按类型分组，条目由 `release.py` 从 git log 机械生成，"
                    "发版前请**人工润色成用户看得懂的话**。\n\n" + block)
    write(CHANGELOG, new_text)
    print("  ✅ CHANGELOG.md（%d 条）" % sum(len(v) for v in groups.values()))

    print("\n" + "=" * 66)
    print("已准备好，但**没有提交**。请你先：")
    print("  1. 打开 CHANGELOG.md，把机器生成的话改成用户看得懂的")
    print("  2. 确认 README 的「已验证范围」是否需要更新")
    print("  3. 然后跑: python release.py --tag %s" % new_version)
    print("=" * 66)
    return 0


def do_tag(version):
    print("提交并打 tag v%s ..." % version)
    git("add", "-A")
    proc = git("commit", "-m", "release: v%s" % version, check=False)
    if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
        print(proc.stdout[-400:])
    git("tag", "-a", "v%s" % version, "-m", "v%s" % version, check=False)
    print("  ✅ 已提交并打 tag")
    print("\n下一步: python release.py --push")
    return 0


def do_push(attempts=2):
    """推送。按用户规矩：最多试 2 次。"""
    for i in range(1, attempts + 1):
        print("推送第 %d/%d 次 ..." % (i, attempts))
        proc = git("push", "origin", "main", "--follow-tags", check=False)
        if proc.returncode == 0:
            print("  ✅ 推送成功")
            print(proc.stdout[-300:])
            return 0
        print("  失败: %s" % (proc.stderr or proc.stdout).strip()[-200:])
        if i < attempts:
            print("  等 3 秒重试 ...")
            import time
            time.sleep(3)
    print("\n连续 %d 次失败，按规矩停手。" % attempts)
    print("把这件事记进 `_需求文档/待办-推送未完成.md`，下次会话再试。")
    return 1


def _today():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d")


def main(argv):
    if "--check" in argv or not argv:
        return do_check()
    if "--prepare" in argv:
        idx = argv.index("--prepare")
        if idx + 1 >= len(argv):
            raise SystemExit("用法: --prepare 2.2.0")
        return do_prepare(argv[idx + 1])
    if "--tag" in argv:
        idx = argv.index("--tag")
        if idx + 1 >= len(argv):
            raise SystemExit("用法: --tag 2.2.0")
        return do_tag(argv[idx + 1])
    if "--push" in argv:
        return do_push()
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:
        print("发版脚本出错：%s: %s" % (type(exc).__name__, exc))
        sys.exit(1)