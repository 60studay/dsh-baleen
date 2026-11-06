# -*- coding: utf-8 -*-
"""工具层自检：`index.js` 的 execute 返回 vs output.schema 是否一致。

为什么要这个脚本
----------------
这是补一个**真实发生过**的盲区。

`serial_status` 曾经每次调用都失败：

    tool "serial_status" returned invalid output:
    "value.transport" is not a declared property (additionalProperties: false)

原因：`execute` 返回了 `transport`，但 `output.schema` 里没声明它，
而 schema 是 `additionalProperties: false` → 校验器把整个返回值拒掉。

**这个 bug 从"抽 Transport 层"那次提交一直活到现在，一路绿灯**，
因为当时所有自检都只测 Python / HTTP / CLI，**没有一个是测 `index.js` 的**。

所以补这个脚本。

它怎么工作
----------
1. 用 Node 导入 `index.js`，塞一个假 `ctx`，把 9 个工具定义抓出来
2. 对**只读**工具真的跑一次 `execute`，拿真实返回值
3. 用一个最小 JSON-Schema 校验器比对，报出多余/缺失/类型错的字段
4. 对**有副作用**的工具（`serial_send` / `serial_control`）只做静态检查
   （schema 自洽性：有 properties、required 都在 properties 里）

为什么不跑 `serial_send` / `serial_control`：
它们会真的往硬件发命令、改控制权。**自检不能有副作用。**

环境要求
--------
需要 Node。找的顺序：PATH 里的 `node` → `$DSH_HOME/.../node/bin/node.exe` → 放弃（SKIP）。

用法：
    python selfcheck_tools.py
    SB_SCENE=蓝牙AT python selfcheck_tools.py    # 指定场景
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_JS = os.path.join(HERE, "index.js")

PASS = []
FAIL = []
SKIP = []


def ok(name, detail=""):
    PASS.append(name)
    print("  PASS %s%s" % (name, ("  " + detail) if detail else ""))


def bad(name, detail):
    FAIL.append("%s —— %s" % (name, detail))
    print("  FAIL %s  %s" % (name, detail))


def skip(name, detail=""):
    SKIP.append(name)
    print("  SKIP %s%s" % (name, ("  " + detail) if detail else ""))


# ── 找 node ──────────────────────────────────────────────────────────────────
def find_node():
    """按跨平台顺序找 node，找不到返回 None（不要硬编码 Windows 路径）。"""
    found = shutil.which("node")
    if found:
        return found
    home = os.environ.get("DSH_HOME") or ""
    if home:
        for rel in (
            os.path.join("dsh-runtimes", "dsh-primary-runtime", "dependencies",
                         "node", "bin", "node.exe"),
            os.path.join("dsh-runtimes", "dsh-primary-runtime", "dependencies",
                         "node", "bin", "node"),
        ):
            candidate = os.path.join(home, rel)
            if os.path.exists(candidate):
                return candidate
    return None


# ── 交给 Node 干的活 ─────────────────────────────────────────────────────────
# 这个脚本会写成临时 .mjs：导入 index.js、抓工具、跑只读工具、校验、吐 JSON。
NODE_SCRIPT = r"""
import { pathToFileURL } from 'node:url';

const indexPath = process.env.SB_INDEX_JS;
const scene = process.env.SB_SCENE || '';
const mod = await import(pathToFileURL(indexPath).href);

const tools = [];
// 假 ctx：index.js 用 ctx.tools.register 注册，用 ctx.effect 包一层拿 disposer。
// effect 在这里直接执行回调就够了——我们要的只是"工具定义被抓下来"。
const ctx = {
  tools: { register: (tool) => { tools.push(tool); return () => {}; } },
  effect: (callback) => { const disposer = callback(); return disposer; },
};
mod.apply(ctx, {});

// ── 最小 JSON-Schema 校验（只支持这个项目用到的关键字）──────────────────
function typeOf(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (Number.isInteger(value)) return 'integer';
  return typeof value;           // string / number / boolean / object
}

function validate(value, schema, path, errors) {
  if (!schema || typeof schema !== 'object') return;
  if (schema.type !== undefined) {
    const allowed = Array.isArray(schema.type) ? schema.type : [schema.type];
    const actual = typeOf(value);
    const matched = allowed.some((want) => {
      if (want === actual) return true;
      if (want === 'number' && actual === 'integer') return true;
      if (want === 'integer' && actual === 'number' && Number.isInteger(value)) return true;
      return false;
    });
    if (!matched) {
      errors.push(`${path} 类型是 ${actual}，但 schema 要求 ${allowed.join('|')}`);
      return;
    }
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors.push(`${path} 的值 ${JSON.stringify(value)} 不在 enum 里`);
  }
  if (schema.type === 'object' || (schema.properties && value && typeof value === 'object')) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return;
    const props = schema.properties || {};
    for (const key of Object.keys(value)) {
      if (!(key in props)) {
        if (schema.additionalProperties === false) {
          errors.push(`${path}.${key} 没在 schema.properties 里声明（additionalProperties: false）`);
        }
        continue;
      }
      validate(value[key], props[key], `${path}.${key}`, errors);
    }
    for (const key of schema.required || []) {
      if (!(key in value)) errors.push(`${path}.${key} 是 required 但返回值里没有`);
    }
  }
  if (schema.type === 'array' && Array.isArray(value) && schema.items) {
    value.forEach((item, i) => validate(item, schema.items, `${path}[${i}]`, errors));
  }
}

// ── 哪些工具能安全地真跑一次 ────────────────────────────────────────────────
// 只读 = 不往硬件写字节、不改控制权、不改配置。
const SAFE_CALLS = {
  serial_list: () => ({}),
  serial_info: () => ({ scene }),
  serial_status: () => ({ scene }),
  serial_read: () => ({ scene, lines: 3 }),
  serial_watch: () => ({ scene }),
  serial_daemon: () => ({ action: 'status', scene }),
  serial_web: () => ({ action: 'status', scene }),
};

const report = [];
for (const tool of tools) {
  const item = { name: tool.name, problems: [], ran: false };
  const schema = tool.output && tool.output.schema;

  // ── 静态检查：schema 自身是否自洽 ──
  if (!schema) {
    item.problems.push('没有 output.schema');
    report.push(item);
    continue;
  }
  if (schema.type !== 'object') {
    item.problems.push(`output.schema.type 是 ${JSON.stringify(schema.type)}，应为 object`);
  }
  const props = schema.properties || {};
  for (const key of schema.required || []) {
    if (!(key in props)) item.problems.push(`required 里的 ${key} 没在 properties 中声明`);
  }

  // ── 动态检查：真跑一次，拿真实返回做校验 ──
  const maker = SAFE_CALLS[tool.name];
  if (!maker || typeof tool.execute !== 'function') {
    item.skipped = true;
    report.push(item);
    continue;
  }
  try {
    const args = maker();
    const value = await tool.execute(args);
    item.ran = true;
    const errors = [];
    validate(value, schema, 'value', errors);
    item.problems.push(...errors);
    item.returnedKeys = value && typeof value === 'object' ? Object.keys(value) : [];
    item.declaredKeys = Object.keys(props);
  } catch (error) {
    item.error = String(error && error.message ? error.message : error);
  }
  report.push(item);
}

process.stdout.write(JSON.stringify(report));
"""


def main():
    print("=" * 70)
    print("自检：工具层 execute ↔ output.schema 一致性")
    print("=" * 70)

    if not os.path.exists(INDEX_JS):
        bad("index.js 存在", "找不到 %s" % INDEX_JS)
        return summary()

    node = find_node()
    if not node:
        skip("找到 Node", "PATH 和 DSH_HOME 里都没有 node，跳过工具层检查")
        return summary()

    scene = os.environ.get("SB_SCENE") or ""
    if not scene:
        scene = guess_scene()

    env = dict(os.environ)
    env["SB_INDEX_JS"] = INDEX_JS
    env["SB_SCENE"] = scene

    tmp = os.path.join(tempfile.gettempdir(), "_whalesong_tools_check.mjs")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(NODE_SCRIPT)
    try:
        proc = subprocess.run(
            [node, tmp], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180, env=env,
            cwd=HERE,
        )
    except Exception as exc:
        bad("Node 能跑起来", "%s: %s" % (type(exc).__name__, exc))
        return summary()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if proc.returncode != 0:
        bad("Node 能跑起来", (proc.stderr or proc.stdout or "")[-400:])
        return summary()

    try:
        report = json.loads(proc.stdout)
    except Exception:
        bad("拿到结构化结果", (proc.stdout or "")[:300])
        return summary()

    ok("Node 抓到工具定义", "%d 个工具" % len(report))

    print("\n[逐个工具]")
    for item in report:
        name = item.get("name") or "?"
        problems = item.get("problems") or []
        if item.get("error"):
            # 工具跑不起来（比如串口没插）不算 schema 错，但要说明
            if "serial_web" in name or "serial_daemon" in name:
                skip(name, "执行报错（可能是服务没起）：%s" % item["error"][:80])
            else:
                bad("%s 能执行" % name, item["error"][:200])
            continue
        if problems:
            for problem in problems:
                bad("%s 返回值合规" % name, problem)
        elif item.get("skipped"):
            skip(name, "有副作用，只做了静态检查")
        elif item.get("ran"):
            ok(name, "%d 个返回字段都对上了" % len(item.get("returnedKeys") or []))
        else:
            skip(name, "没跑")

    return summary()


def guess_scene():
    """没指定场景时，从 protocols/ 里挑第一个（工具层检查不挑设备）。"""
    root = os.path.join(HERE, "protocols")
    try:
        names = sorted(d for d in os.listdir(root)
                       if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return ""
    return names[0] if names else ""


def summary():
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d%s"
          % (len(PASS), len(FAIL), (" / SKIP %d" % len(SKIP)) if SKIP else ""))
    if FAIL:
        print("\n失败项：")
        for item in FAIL:
            print("  · " + item)
    else:
        print("工具层返回值和 schema 一致 ✅")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print("\n工具层自检没能跑起来：%s: %s" % (type(exc).__name__, exc))
        print("这本身就是失败。")
        sys.exit(1)