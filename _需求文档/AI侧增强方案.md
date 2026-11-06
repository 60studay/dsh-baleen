# AI 侧能力增强方案

> 编写日期：2026-11-04
> 针对版本：`<插件目录>\`（2026-11-04 22:16 版）
> 目标：优化 **AI 调用插件的能力**（不是网页端）
> 前置：先修完 `实测问题反馈.md` 里的 P0 bug

---

## ⚠️ 铁律：通用性优先

本文档新增的所有能力，**都必须满足**：

| 必须 | 禁止 |
|---|---|
| 参数驱动（命令、判据、解析规则全来自参数/配置） | 代码里出现任何具体设备的命令、字段名 |
| 换一台设备不改代码就能用 | 为某个工程做特化 |

**验收标准：代码里搜不到 `"P"`、`"FAULT"`、`"CH1"`、`"stall"` 等字样。**

---

## 一、为什么优先做 AI 侧

当前优化都集中在网页端，但实际使用中：

| 对比项 | 网页端 | AI 端 |
|---|---|---|
| 谁在用 | 用户偶尔看 | **AI 每次都调** |
| 调用频率 | 低 | 高 |
| 现状 | 已比较完善 | **还很基础** |
| 优化收益 | 体验提升 | **直接影响能否做成事** |

**核心痛点**：现在 AI 做多步操作（如调 PID）需要连续多次调用，每次都要重新打开串口、等待、解析，效率低且易出错。

---

## 二、能力清单与优先级

| # | 能力 | 优先级 | 工作量 |
|---|---|---|---|
| 1 | 批量发送 + 期望值校验 | **P0** | 中 |
| 2 | 等待条件 | **P0** | 小 |
| 3 | 状态感知增强 | P1 | 小 |
| 4 | 结构化解析 | P1 | 中 |
| 5 | 并发重试 | P2 | 小 |
| 6 | 场景管理 | P2 | 中 |

---

## 三、能力详解

### 3.1 批量发送 + 期望值校验（P0）

#### 问题

现在发 5 条命令 = 5 次工具调用：

```
serial_send(scene, "A")
serial_send(scene, "W 500 500 500 500")
serial_send(scene, "P")
serial_send(scene, "T 200")
serial_send(scene, "X")
```

每次调用都要：开串口 → 发 → 等 → 关串口，开销大且慢。

#### 新增工具

```
serial_send_batch
```

#### 参数

```json
{
  "scene": "<场景名>",
  "steps": [
    {
      "command": "<命令>",
      "expect": "<期望包含的字符串，可选>",
      "timeout_ms": 1000,
      "capture": true
    }
  ],
  "stop_on_error": true,
  "stop_on_mismatch": true
}
```

| 参数 | 说明 |
|---|---|
| `command` | 要发的命令 |
| `expect` | 可选。回显不包含此字符串则视为失败 |
| `timeout_ms` | 该步超时（默认取场景配置） |
| `capture` | 是否在结果里保留完整回显（默认 true） |
| `stop_on_error` | 出错（无回显/串口异常）即停，默认 true |
| `stop_on_mismatch` | 期望不匹配即停，默认 true |

#### 返回

```json
{
  "scene": "蓝牙AT",
  "port": "COM10",
  "completed": 3,
  "total": 5,
  "stopped": true,
  "stopReason": "mismatch",
  "steps": [
    { "index": 0, "command": "A", "response": "OK RUNNING", "ok": true, "matched": true },
    { "index": 1, "command": "W 500 500 500 500", "response": "OK 4 TARGETS", "ok": true, "matched": true },
    { "index": 2, "command": "P", "response": "STATE RUNNING...", "ok": true, "matched": false }
  ]
}
```

#### 关键设计

| 点 | 要求 |
|---|---|
| **串口只开一次** | 整个批次复用同一个串口句柄，不是每步开关 |
| **失败可定位** | 返回 `completed` / `stopReason` / 每步 `ok` 标记 |
| **不影响单条调用** | `serial_send` 保持原样，两者并存 |
| **每步间有间隔** | 可配置 `gap_ms`，默认 100ms（给设备处理时间） |

#### 配置扩展（可选）

场景 config 里可设默认值：

```json
{
  "batch": {
    "gap_ms": 100,
    "default_timeout_ms": 1000
  }
}
```

---

### 3.2 等待条件（P0）

#### 问题

发完命令就返回，不知道设备什么时候准备好。AI 只能盲等（sleep），浪费时间且不可靠。

#### 新增工具

```
serial_wait
```

#### 参数

```json
{
  "scene": "<场景名>",
  "until": {
    "type": "contains",
    "value": "<期望内容>"
  },
  "timeout_ms": 3000,
  "poll_command": "<可选：主动轮询的命令>"
}
```

#### `until.type` 支持的判据（与规则引擎一致）

| type | 说明 |
|---|---|
| `contains` | 收到包含某字符串的数据 |
| `regex` | 正则匹配 |
| `equals` | 完全等于 |
| `silence` | 线路安静 N 毫秒（适合等数据流结束） |

#### 两种工作模式

| 模式 | 说明 | 参数 |
|---|---|---|
| **被动等待** | 只监听，不发命令 | 不传 `poll_command` |
| **主动轮询** | 每隔 `gap_ms` 发一次 `poll_command` 并检查 | 传 `poll_command` |

#### 返回

```json
{
  "scene": "蓝牙AT",
  "satisfied": true,
  "elapsed_ms": 850,
  "attempts": 3,
  "matchedData": "OK RUNNING",
  "timedOut": false
}
```

#### 使用示例

```
serial_send(scene, "A")          → OK RUNNING
serial_wait(scene, until={type:"contains",value:"OK"}, timeout_ms:2000)
serial_send(scene, "W 500 500 500 500")
```

**价值**：AI 不用盲等，能精确知道设备何时就绪。

---

### 3.3 状态感知增强（P1）

#### 现状

`serial_status` 信息有限，AI 发命令前无法判断“现在能不能发”。

#### 增强后的返回

```json
{
  "scene": "蓝牙AT",
  "port": "COM10",
  "portSource": "match",
  "portAvailable": true,
  "portBusy": false,
  "portBusyBy": null,
  "control": "ai",
  "aiCanSend": true,
  "watchRunning": false,
  "lastSendAt": "2026-11-04T22:30:00",
  "lastResponseAt": "2026-11-04T22:30:01",
  "consecutiveFailures": 0,
  "recentAlerts": []
}
```

| 字段 | 用途 |
|---|---|
| `portAvailable` | 串口在不在（拔了没） |
| `portBusy` | 是否被占用 |
| `portBusyBy` | 被谁占（`self` / `web` / `external`） |
| `aiCanSend` | **AI 现在能不能发**（综合判断） |
| `watchRunning` | 监视是否在跑（跑着时 AI 可能发不了） |
| `consecutiveFailures` | 连续失败次数（判断设备是否健康） |

#### 关键：`aiCanSend`

AI 发命令前**先查这个**，避免盲目失败：

```python
if not status["aiCanSend"]:
    # 根据原因给用户明确提示
    reason = status.get("blockReason")  # "port_busy" / "not_ai_control" / "port_missing"
```

---

### 3.4 结构化解析（P1）

#### 问题

`response` 是一整段文本，AI 要自己解析：

```
STATE RUNNING
CH1 T=500 A=498 O=312 d=2
CH2 T=500 A=497 O=310 d=2
```

#### 改进：可选解析参数

```json
serial_send({
  "scene": "蓝牙AT",
  "command": "P",
  "parse": {
    "type": "regex",
    "pattern": "CH(\\d) T=(\\d+) A=(\\d+) O=(-?\\d+)",
    "fields": ["ch", "T", "A", "O"]
  }
})
```

#### 返回（在原返回基础上增加）

```json
{
  "response": "STATE RUNNING\r\nCH1 T=500...",
  "parsed": [
    { "ch": 1, "T": 500, "A": 498, "O": 312 },
    { "ch": 2, "T": 500, "A": 497, "O": 310 }
  ],
  "parseError": null
}
```

#### 关键设计

| 点 | 要求 |
|---|---|
| **解析规则来自参数** | 不是插件内置，保持通用 |
| **解析失败不影响原文** | `response` 始终返回，`parseError` 说明失败原因 |
| **不做类型推断** | 全部返回字符串或按 `fields` 顺序，避免猜错 |

---

### 3.5 并发重试（P2）

#### 问题

连续调两次 `serial_send` 时，第二次可能因第一次还没关串口而失败。

#### 改进

```
内部加锁 + 自动重试：
  尝试打开串口
  失败且原因是"被占用"
    → 等 200ms 重试
    → 最多 2 次
    → 仍失败才报错
```

#### 返回时说明

```json
{
  "retried": true,
  "retryCount": 1
}
```

---

### 3.6 场景管理（P2）

#### 新增工具

| 工具 | 用途 |
|---|---|
| `serial_scene_create` | 新建场景（name, com/match, baud, protocol） |
| `serial_scene_update` | 改配置（局部更新） |
| `serial_scene_list` | 列出所有场景及状态 |

#### 价值

AI 能帮用户建新场景，不用手动改 JSON。

#### 安全

| 限制 | 说明 |
|---|---|
| 只能改 `protocols/` 下的场景 | 不能碰插件代码 |
| 修改前备份 | 保留 `config.json.bak` |
| 不删场景 | 只允许创建和更新 |

---

## 四、实现优先级与依赖

```
P0（先做）
├── 3.1 批量发送      ← 依赖：串口复用能力
└── 3.2 等待条件      ← 依赖：无

P1（其次）
├── 3.3 状态感知      ← 依赖：无（改 serial_status 返回）
└── 3.4 结构化解析    ← 依赖：无

P2（最后）
├── 3.5 并发重试
└── 3.6 场景管理
```

**建议顺序**：3.2（最简单）→ 3.1（最有价值）→ 3.3 → 3.4 → 3.5 → 3.6

---

## 五、兼容性要求

| 要求 | 说明 |
|---|---|
| **现有 7 个工具全部保持可用** | `serial_list` / `serial_send` / `serial_read` / `serial_info` / `serial_status` / `serial_watch` / `serial_control` |
| **不改变现有参数语义** | `serial_send` 的 `scene`/`command` 保持不变 |
| **新增工具而非替换** | 批量是 `serial_send_batch`，不是改 `serial_send` |
| **配置文件向后兼容** | 旧 config.json 不加新字段也能跑 |

---

## 六、验收清单

### 批量发送
- [ ] 一次调用发多条命令，串口只开一次
- [ ] `expect` 不匹配时按 `stop_on_mismatch` 处理
- [ ] 返回 `completed` / `total` / `stopReason`
- [ ] 每步 `ok` / `matched` 标记正确
- [ ] 不影响 `serial_send` 原有行为

### 等待条件
- [ ] `contains` 能等到
- [ ] 超时正确返回 `timedOut: true`
- [ ] 主动轮询模式能工作
- [ ] `elapsed_ms` / `attempts` 准确

### 状态感知
- [ ] `aiCanSend` 准确反映当前能否发送
- [ ] `portBusyBy` 能区分 self/web/external
- [ ] 拔掉设备时 `portAvailable: false`

### 结构化解析
- [ ] 正则匹配正确返回 `parsed`
- [ ] 解析失败时 `response` 仍返回
- [ ] 解析规则来自参数（代码无硬编码）

### 通用性（最重要）
- [ ] 代码里搜 `"P"`、`"FAULT"`、`"CH1"` → **零命中**
- [ ] 换一个场景（如 Modbus）所有新功能同样工作
- [ ] 新功能不依赖任何具体设备

---

## 七、给开发者的话

AI 是这个插件的主力用户。网页是给人偶尔看的，AI 是每次调试都要用的。

**优化 AI 侧体验，比优化网页更有价值。**

但请记住：**所有新增能力的参数都必须来自配置或调用参数**，不能把某个设备的命令写死在代码里。

判断标准仍然是那一句：

> **换一台完全不同的设备，我需要改代码吗？**
> 需要改 → 设计错了
> 只改配置 → 设计对了
