# -*- coding: utf-8 -*-
"""发版助手（通用版）：把"更新版本号"从几小时压到几条命令。

这个脚本不绑定任何具体项目，任何带 package.json 的项目都能用。

来源
----
最初为 `dsh-serial-bridge`（鲸歌）写的，跑通后抽出来泛化。
那次实测发现：**发版最费时间的不是改版本号，是"不知道有没有漏东西"**。
所以这个脚本的重点在 `--check`，不在 `--prepare`。

三步流程
--------
    python release.py --check              # 体检，只读，不改任何文件
    python release.py --prepare 2.2.0      # 改版本号 + 生成 CHANGELOG
    #   ↑ 中间需要人工润色 CHANGELOG（机器写的话用户看不懂）
    python release.py --tag 2.2.0          # 提交 + 打 tag
    python release.py --push               # 推送（默认最多试 2 次）

为什么 `--check` 最重要
------------------------
发版出事的典型方式不是"版本号写错"，而是：
- 内部笔记（含本机路径、设备信息）被带进公开仓库
- 忘了跑自检，发出去是坏的
- 工作区有未提交的改动，发出去的和你以为的不一样

这些**全是机械可查的**，所以全放进 `--check`。

设计原则
--------
1. `--check` 绝不改文件
2. 任何一步失败都停下来，不做下一半
3. 推送失败不纠缠（默认 2 次，可调）
4. **不替你决定"该不该发版"** —— 那是人的判断

配置
----
项目根放一个 `release.json`（可选），覆盖默认行为：

    {
      "must_ignore": ["_需求文档/", "logs/"],
      "secret_patterns": [
        ["C:\\\\\\\\Users\\\\\\\\[A-Za-z0-9_]+", "Windows 用户目录"]
      ],
      "selfcheck_glob": "selfcheck_*.py",
      "version_files": ["package.json"]
    }

不配也行，脚本会用一组通用默认值。
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
CONF = os.path.join(HERE, "release.json")

# ── 通用默认值（项目可用 release.json 覆盖）─────────────────────────────────
DEFAULT_MUST_IGNORE = [
    "logs/",
    ".runtime/",
    "_testlogs/",
]

DEFAULT_SECRET_PATTERNS = [
    [r"C:\\\\Users\\\\[A-Za-z0-9_]+", "Windows 用户目录绝对路径"],
    [r"ghp_[A-Za-z0-9]{20,}", "GitHub token"],
    [r"github_pat_[A-Za-z0-9_]{20,}", "GitHub token"],
    [r"sk-[A-Za-z0-9]{20,}", "API key"],
]

DEFAULT_SELFCHECK_GLOB = "selfcheck_*.py"
DEFAULT_VERSION_FILES = ["package.json"]

# 提交信息的约定式前缀 → 中文分组
CONV = [
    (r"^feat", "新功能"),
    (r"^fix", "修复"),
    (r"^perf", "性能"),
    (r"^refactor", "重构"),
    (r"^test", "测试"),
    (r"^docs", "文档"),
    (r"^chore", "杂项"),
    (r"^style", "样式"),
]
CONV_ORDER = ["新功能", "修复", "性能", "重构", "测试", "文档", "样式", "杂项", "其他"]

# 推送重试次数（用户定的规矩：不纠缠）
PUSH_ATTEMPTS = 2


# ── 基础设施 ────────────────────────────────────────────────────────────────
def load_conf():
    conf = {}
    if os.path.exists(CONF):
        try:
            with io.open(CONF, encoding="utf-8") as handle:
                conf = json.load(handle) or {}
        except Exception as exc:
            print("⚠️ release.json 读不了，用默认值: %s" % exc)
    return {
        "must_ignore": conf.get("must_ignore", DEFAULT_MUST_IGNORE),
        "secret_patterns": conf.get("secret_patterns", DEFAULT_SECRET_PATTERNS),
        "selfcheck_glob": conf.get("selfcheck_glob", DEFAULT_SELFCHECK_GLOB),
        "version_files": conf.get("version_files", DEFAULT_VERSION_FILES),
    }


def run(args, check=True):
    proc = subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
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


def glob_files(pattern):
    """支持前缀匹配的简单 glob（不引 glob 模块，因为要跨平台一致）。"""
    import fnmatch
    return sorted(f for f in os.listdir(HERE) if fnmatch.fnmatch(f, pattern))


# ── 体检 ────────────────────────────────────────────────────────────────────
def do_check():
    conf = load_conf()
    problems = []
    warnings = []

    print("=" * 68)
    print("发版体检（只读，不改任何文件）")
    print("=" * 68)

    # 1. 工作区
    print("\n[1] 工作区")
    status = git("status", "--short").stdout.strip()
    if status:
        print("  ⚠️ 有未提交的改动：")
        for line in status.splitlines()[:8]:
            print("     " + line)
        problems.append("工作区不干净")
    else:
        print("  ✅ 干净")

    # 2. 未推送提交
    ahead = git("log", "--oneline", "origin/main..HEAD", check=False).stdout.strip()
    n_ahead = len(ahead.splitlines()) if ahead else 0
    print("\n[2] 未推送提交: %d 个" % n_ahead)
    if n_ahead > 0:
        warnings.append("%d 个提交还没推（发版时一起推）" % n_ahead)

    # 3. 自检
    print("\n[3] 自检")
    scripts = glob_files(conf["selfcheck_glob"])
    if not scripts:
        print("  （没有匹配 %s 的自检脚本，跳过）" % conf["selfcheck_glob"])
    else:
        total_pass = total_fail = 0
        failed_names = []
        for name in scripts:
            proc = subprocess.run([sys.executable, name], cwd=HERE,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
            found = re.search(r"PASS (\d+) / FAIL (\d+)", proc.stdout or "")
            if found:
                p, f = int(found.group(1)), int(found.group(2))
                total_pass += p
                total_fail += f
                if f:
                    failed_names.append("%s(%d)" % (name, f))
        print("  %d 个脚本 / %d 项通过 / %d 项失败 %s"
              % (len(scripts), total_pass, total_fail,
                 "✅" if total_fail == 0 else "❌"))
        if failed_names:
            print("  失败的: " + ", ".join(failed_names))
            problems.append("%d 项自检失败" % total_fail)

    # 4. gitignore 挡住内部文件
    print("\n[4] 内部文件是否被挡住")
    if not conf["must_ignore"]:
        print("  （没配置，跳过）")
    for path in conf["must_ignore"]:
        probe = os.path.join(HERE, path.rstrip("/"), "_probe_.txt")
        proc = subprocess.run(["git", "check-ignore", "-q", probe],
                              cwd=HERE, capture_output=True)
        ignored = proc.returncode == 0
        print("  %s %s" % ("✅" if ignored else "❌ 没挡住", path))
        if not ignored:
            problems.append("%s 没被 gitignore" % path)

    # 5. 敏感信息
    print("\n[5] 敏感信息扫描")
    tracked = run(["git", "ls-files"], check=False).stdout.splitlines()
    # 配置文件自己会列出敏感词，扫它必然自命中
    SKIP = {"release.json", "release.py"}
    hits = []
    for rel in tracked:
        if os.path.basename(rel) in SKIP:
            continue
        if not rel.endswith((".py", ".js", ".ts", ".html", ".css",
                             ".json", ".md", ".yml", ".yaml", ".toml")):
            continue
        full = os.path.join(HERE, rel)
        if not os.path.exists(full):
            continue
        try:
            text = read(full)
        except Exception:
            continue
        for item in conf["secret_patterns"]:
            pattern, label = item[0], item[1]
            if re.search(pattern, text):
                hits.append((rel, label))
    if hits:
        for rel, label in hits[:12]:
            print("  ❌ %s → %s" % (rel, label))
        problems.append("发现 %d 处敏感信息" % len(hits))
    else:
        print("  ✅ 没发现")

    # 6. 版本号
    print("\n[6] 版本号")
    for name in conf["version_files"]:
        full = os.path.join(HERE, name)
        if not os.path.exists(full):
            continue
        try:
            data = json.loads(read(full))
            print("  %s: %s" % (name, data.get("version", "?")))
        except Exception:
            print("  %s: 读不了" % name)

    # 结论
    print("\n" + "=" * 68)
    if warnings:
        for item in warnings:
            print("  ℹ️ " + item)
    if problems:
        print("体检没通过，先处理：")
        for item in problems:
            print("  · " + item)
        return 1
    print("体检通过，可以发版 ✅")
    print("下一步: python release.py --prepare <新版本号>")
    return 0


# ── CHANGELOG ──────────────────────────────────────────────────────────────
def collect_commits(since_tag=None):
    args = ["log", "--pretty=%s", "--no-merges"]
    if since_tag:
        args.append("%s..HEAD" % since_tag)
    out = git(*args, check=False).stdout.strip()
    groups = {}
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        for pattern, label in CONV:
            if re.match(pattern, text):
                groups.setdefault(label, []).append(text)
                break
        else:
            groups.setdefault("其他", []).append(text)
    return groups


def do_prepare(new_version):
    if not re.match(r"^\d+\.\d+\.\d+$", new_version):
        raise SystemExit("版本号格式应为 x.y.z，收到 %r" % new_version)

    conf = load_conf()
    for name in conf["version_files"]:
        full = os.path.join(HERE, name)
        if not os.path.exists(full):
            continue
        try:
            data = json.loads(read(full))
        except Exception:
            continue
        old = data.get("version", "?")
        data["version"] = new_version
        write(full, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print("  ✅ %s: %s → %s" % (name, old, new_version))

    tag_out = git("describe", "--tags", "--abbrev=0", check=False).stdout.strip()
    groups = collect_commits(tag_out or None)
    print("  收集到 %d 组提交（自 %s）"
          % (len(groups), tag_out or "仓库开始"))

    lines = ["## v%s（%s）" % (new_version, _today()), ""]
    for label in CONV_ORDER:
        items = groups.get(label)
        if not items:
            continue
        lines.append("### %s" % label)
        for item in items:
            clean = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", item)
            lines.append("- %s" % clean)
        lines.append("")
    block = "\n".join(lines)

    changelog = os.path.join(HERE, "CHANGELOG.md")
    if os.path.exists(changelog):
        old = read(changelog)
        idx = old.find("\n## v")
        text = (old[:idx + 1] + block + old[idx + 1:]) if idx > 0 else (old + "\n" + block)
    else:
        text = ("# 更新日志\n\n"
                "> 条目由 `release.py` 从 git log 机械生成，"
                "**发版前请人工润色成用户看得懂的话**"
                "（机器写的话通常带内部黑话）。\n\n" + block)
    write(changelog, text)
    print("  ✅ CHANGELOG.md（%d 条）" % sum(len(v) for v in groups.values()))

    print("\n" + "=" * 68)
    print("已准备好，但**没有提交**。请你先：")
    print("  1. 打开 CHANGELOG.md，把机器生成的话改成用户看得懂的")
    print("  2. 确认 README 有没有需要更新的内容")
    print("  3. 然后跑: python release.py --tag %s" % new_version)
    print("=" * 68)
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


def do_push(attempts=PUSH_ATTEMPTS):
    for i in range(1, attempts + 1):
        print("推送第 %d/%d 次 ..." % (i, attempts))
        proc = git("push", "origin", "main", "--follow-tags", check=False)
        if proc.returncode == 0:
            print("  ✅ 推送成功")
            print((proc.stdout or "")[-300:])
            return 0
        print("  失败: %s" % (proc.stderr or proc.stdout).strip()[-200:])
        if i < attempts:
            print("  等 3 秒重试 ...")
            import time
            time.sleep(3)
    print("\n连续 %d 次失败，按规矩停手（不纠缠）。" % attempts)
    print("把这件事记进待办文件，下次会话再试。")
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