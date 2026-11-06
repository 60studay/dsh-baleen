/**
 * dsh-whalesong（鲸歌）v2 —— 通用串口工具插件。
 *
 * 设计要点：
 * - 不常驻、不占口：每次调用临时打开串口，发完就读回显并关闭。
 * - 逻辑全在 Python 内核（serial_bridge/）：匹配、错误分类、判据引擎、控制权状态。
 *   这里只做参数编排、结果整形、以及把结构化错误翻译成模型能直接照着行动的提示。
 * - 零设备知识：本文件不认识任何具体命令字/故障字，一切来自场景配置。
 *
 * 每个"场景"= protocols/<场景名>/ 下的 config.json（串口、匹配规则、watch 块）+ protocol.md。
 */
import { execFile } from 'node:child_process';
import { promises as fsp, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENTRY = path.join(HERE, 'bridge.py');
const VENDOR = path.join(HERE, 'vendor', 'pyserial-3.5');
const BUNDLED_PROTOCOLS = path.join(HERE, 'protocols');

export const name = 'whalesong';
export const inject = ['tools'];

const DEFAULT_PYTHON = process.env.DSH_PYTHON || 'python';
// 日志默认写到插件自己的目录下（跟着插件走，不碰用户家目录，也不暴露用户名）。
// 需要换位置就设环境变量 SERIAL_BRIDGE_LOG_DIR。
const DEFAULT_LOG_ROOT =
  process.env.SERIAL_BRIDGE_LOG_DIR || path.join(HERE, 'logs');
const CALL_TIMEOUT_MS = 25000;

/** 结构化错误的 kind → 给模型的一句话行动建议（与 kind 一一对应，不含设备语义）。 */
const KIND_ADVICE = {
  no_device: '设备没找到：确认 USB 插好、驱动装好；用 serial_list 看当前有哪些串口。',
  ambiguous_device: '匹配到多个设备：让用户选一个，写进场景 config.json 的 com 字段。',
  port_busy: '口被占用：关掉串口助手 / 网页监视模式后重试。',
  no_response: '设备没回话：检查波特率、供电、TX/RX 是否接反、换行结尾。',
  scene_missing: '场景名不对：用 serial_list 看 scenes 里有哪些可用场景。',
  control_blocked: '控制权不在 AI：需要人类在网页上点[交给AI]或[继续AI]。',
  allowlist_blocked: '自动命令不在白名单：把该命令加进场景配置的 watch.allowed_auto_commands。',
  config: '场景配置有问题：按提示改 protocols/<场景>/config.json。',
  internal: '内部错误：把 message 与 detail 一起反馈。',
};

function stripBom(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

async function readJson(file) {
  return JSON.parse(stripBom(await fsp.readFile(file, 'utf8')));
}

function makeEnv({ logRoot, protocolRoot }) {
  const pythonPath = [VENDOR, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  return {
    ...process.env,
    PYTHONPATH: pythonPath,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    SERIAL_BRIDGE_LOG_DIR: logRoot,
    SERIAL_BRIDGE_PROTO_DIR: protocolRoot,
    SERIAL_BRIDGE_RUNTIME_DIR: path.join(logRoot, '.runtime'),
  };
}

/** 把结构化错误渲染成"该做什么"的一段话。 */
function renderError(kind, error, fallback) {
  const lines = [];
  const advice = KIND_ADVICE[kind];
  lines.push(error?.message || fallback || '鲸歌调用失败');
  if (Array.isArray(error?.hint) && error.hint.length > 0) {
    lines.push('请检查：');
    for (const [index, item] of error.hint.entries()) lines.push(`  ${index + 1}. ${item}`);
  }
  if (Array.isArray(error?.candidates) && error.candidates.length > 0) {
    lines.push('候选设备：');
    for (const item of error.candidates) {
      const device = item?.device ?? '?';
      const extra = [item?.description, item?.vid_pid].filter(Boolean).join('  ');
      lines.push(`  ${device}${extra ? '  ' + extra : ''}`);
    }
  }
  if (advice) lines.push(`下一步：${advice}`);
  if (error?.detail) lines.push(`细节：${error.detail}`);
  return lines.join('\n');
}

export function apply(ctx, config = {}) {
  const configuredPython =
    typeof config.pythonPath === 'string' && config.pythonPath.trim() !== ''
      ? config.pythonPath.trim()
      : DEFAULT_PYTHON;

  const pythonCandidates = [{ file: configuredPython, args: [] }];
  if (process.env.DSH_HOME) {
    pythonCandidates.push({
      file: path.join(process.env.DSH_HOME, 'dsh-runtimes', 'dsh-primary-runtime',
        'dependencies', 'python', 'python.exe'),
      args: [],
    });
  }
  pythonCandidates.push({ file: 'python', args: [] }, { file: 'py', args: ['-3'] });

  // logRoot 的决定顺序：插件配置 > 插件自带 config.json 的 log_root > 默认值。
  // 读 config.json 是为了让"插件进程"和"手动启动的网页"用同一个目录——
  // 否则两边各写一份 state，控制权就同步不上（Bug 3）。
  const bundledLogRoot = (() => {
    try {
      const common = JSON.parse(readFileSync(path.join(HERE, 'config.json'), 'utf8').replace(/^\uFEFF/, ''));
      return typeof common?.log_root === 'string' ? common.log_root.trim() : '';
    } catch {
      return '';
    }
  })();

  const logRoot =
    typeof config.logRoot === 'string' && config.logRoot.trim() !== ''
      ? config.logRoot.trim()
      : (bundledLogRoot || DEFAULT_LOG_ROOT);

  const configuredProtocolRoot =
    typeof config.protocolRoot === 'string' && config.protocolRoot.trim() !== ''
      ? config.protocolRoot.trim()
      : undefined;

  const state = { python: undefined, protocolRoot: configuredProtocolRoot };

  async function resolveProtocolRoot() {
    if (state.protocolRoot) {
      const ok = await fsp.stat(state.protocolRoot).then((s) => s.isDirectory()).catch(() => false);
      if (ok) return state.protocolRoot;
    }
    state.protocolRoot = BUNDLED_PROTOCOLS;
    return state.protocolRoot;
  }

  async function resolvePython(env) {
    if (state.python) return state.python;
    const failures = [];
    for (const candidate of pythonCandidates) {
      try {
        const { stdout } = await execFileAsync(
          candidate.file,
          [...candidate.args, '-c',
            'import sys, serial; print(sys.version.split()[0] + " serial " + serial.VERSION)'],
          { env, encoding: 'utf8', timeout: 10000, windowsHide: true, maxBuffer: 1 << 20 },
        );
        state.python = { ...candidate, version: String(stdout).trim() };
        return state.python;
      } catch (error) {
        const detail = String(error?.stderr ?? error?.message ?? '').trim().split(/\r?\n/)[0];
        failures.push(`${candidate.file}${candidate.args.length ? ' ' + candidate.args.join(' ') : ''}: ${detail || '不可用'}`);
      }
    }
    throw new Error(
      '找不到可用的 Python 解释器（需要 Python 3.8+）。\n' +
        `已尝试：\n  ${failures.join('\n  ')}\n` +
        '可在插件配置里用 pythonPath 指定解释器绝对路径。',
    );
  }

  /**
   * 跑一次 Python 内核，返回 data。
   * 内核总是输出一行 JSON：{ok:true,data} 或 {ok:false,error}。
   * 非零退出码不代表"调用失败"，只代表"业务失败"——两者都解析 JSON。
   */
  async function runCli(argv) {
    const protocolRoot = await resolveProtocolRoot();
    const env = makeEnv({ logRoot, protocolRoot });
    const python = await resolvePython(env);

    let stdout = '';
    let stderr = '';
    let timedOut = false;
    try {
      const out = await execFileAsync(python.file, [...python.args, ENTRY, ...argv],
        { env, encoding: 'utf8', timeout: CALL_TIMEOUT_MS, windowsHide: true, maxBuffer: 8 << 20 });
      stdout = String(out.stdout ?? '');
      stderr = String(out.stderr ?? '');
    } catch (error) {
      stdout = String(error?.stdout ?? '');
      stderr = String(error?.stderr ?? '');
      timedOut = Boolean(error?.killed);
      if (!stdout.trim()) {
        if (timedOut) {
          throw new Error(`鲸歌调用超时（${CALL_TIMEOUT_MS} ms）：串口可能被占用，或设备没有回应`);
        }
        const detail = stderr.trim() || String(error?.message ?? error);
        throw new Error(`鲸歌启动失败：${detail.split(/\r?\n/).slice(-4).join('\n')}`);
      }
    }

    const line = stdout.trim().split(/\r?\n/).filter((item) => item.trim() !== '').pop() ?? '';
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      throw new Error(`鲸歌返回了非 JSON 输出：${line.slice(0, 400)}`);
    }
    if (!parsed?.ok) {
      const error = parsed?.error ?? {};
      const failure = new Error(renderError(error.kind, error, '鲸歌返回失败'));
      failure.kind = error.kind;
      throw failure;
    }
    return parsed.data;
  }

  function register(tool) {
    ctx.effect(() => {
      const disposer = ctx.tools.register(tool);
      return typeof disposer === 'function' ? disposer : () => {};
    });
  }

  const sceneParam = {
    type: 'string',
    description:
      'Scene name: the folder under the protocols directory (for example "蓝牙AT"). ' +
      'Use serial_list to see the configured scenes.',
  };

  // ── serial_list ────────────────────────────────────────────────────────────
  register({
    name: 'serial_list',
    description:
      'List the serial ports present on this machine (device, description, VID:PID) together with the ' +
      'configured scenes and which port each scene would resolve to right now. Call this first: it tells ' +
      'you which port to use and which scene names the other serial_* tools accept.',
    parameters: {},
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['ports', 'scenes', 'logRoot'],
        properties: {
          ports: {
            type: 'array',
            description: 'Serial ports detected by pyserial; empty when none is connected.',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['device', 'description', 'vidPid'],
              properties: {
                device: { type: 'string' },
                description: { type: 'string' },
                vidPid: { type: 'string' },
              },
            },
          },
          scenes: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['name', 'com', 'match', 'baud', 'newline', 'hasProtocol', 'resolved'],
              properties: {
                name: { type: 'string' },
                com: { type: 'string' },
                match: { type: 'string' },
                baud: { type: 'integer' },
                newline: { type: 'string' },
                hasProtocol: { type: 'boolean' },
                resolved: {
                  type: 'object',
                  additionalProperties: false,
                  required: ['device', 'source', 'note'],
                  properties: {
                    device: { type: 'string' },
                    source: { type: 'string', description: 'Which rule matched: match_vid_pid / match / com / only_port.' },
                    note: { type: 'string', description: 'Non-empty when the port moved and was auto-corrected.' },
                  },
                },
                resolveError: { type: 'string', description: 'Present when this scene cannot resolve a port.' },
              },
            },
          },
          logRoot: { type: 'string' },
        },
      },
      render: (_args, value) => {
        const lines = ['<type>serial_ports</type>'];
        if (value.ports.length === 0) {
          lines.push('<ports>（没有检测到串口：检查 USB 线/驱动是否插好）</ports>');
        } else {
          lines.push('<ports>');
          for (const port of value.ports) {
            const extra = port.vidPid ? `  [${port.vidPid}]` : '';
            lines.push(`  ${port.device}  ${port.description}${extra}`);
          }
          lines.push('</ports>');
        }
        if (value.scenes.length === 0) {
          lines.push('<scenes>（protocols 目录下还没有场景）</scenes>');
        } else {
          lines.push('<scenes>');
          for (const scene of value.scenes) {
            const head = `${scene.name}  →  ${scene.baud || '?'} 8N1`;
            if (!scene.resolved || scene.resolved.device === '') {
              // 列表里只放一行原因；完整提示交给 serial_info，免得刷屏
              const reason = String(scene.resolveError || '串口不可用').split('\n')[0];
              lines.push(`  ${head}  ✕ ${reason}`);
              continue;
            }
            lines.push(`  ${head}  ${scene.resolved.device}（${scene.resolved.source}）` +
              `${scene.hasProtocol ? '' : '  (无 protocol.md)'}`);
            if (scene.resolved.note) lines.push(`      ⚠ ${scene.resolved.note}`);
          }
          lines.push('</scenes>');
        }
        lines.push(`<log_root>${value.logRoot}</log_root>`);
        return [{ type: 'text', text: lines.join('\n') }];
      },
    },
    isConcurrencySafe: () => true,
    async execute() {
      const data = await runCli(['scan']);
      return {
        ports: (data.ports ?? []).map((port) => ({
          device: port.device ?? '',
          description: port.description ?? '',
          vidPid: port.vid_pid ?? '',
        })),
        scenes: (data.scenes ?? []).map((scene) => ({
          name: scene.name,
          com: scene.com ?? '',
          match: scene.match ?? '',
          baud: Number(scene.baud) || 0,
          newline: scene.newline ?? '\\r\\n',
          hasProtocol: Boolean(scene.has_protocol),
          resolved: scene.resolved
            ? {
                device: scene.resolved.device ?? '',
                source: scene.resolved.source ?? '',
                note: scene.resolved.note ?? '',
              }
            : {
                device: '',
                source: '',
                note: '',
              },
          ...(scene.resolve_error
            ? { resolveError: renderError(scene.resolve_error.kind, scene.resolve_error) }
            : {}),
        })),
        logRoot,
      };
    },
  });

  // ── serial_send ────────────────────────────────────────────────────────────
  register({
    name: 'serial_send',
    description:
      'Send one command line to the device of a scene and return whatever the device echoes back. ' +
      'Requires control: it is refused unless the scene control state is "ai" — if it is refused, ' +
      'ask the human to hand over control (web button [交给AI], or serial_control with action="ai"). ' +
      'The port is opened only for this call and closed immediately, so a running serial assistant is not ' +
      'blocked. The port is resolved by the scene config every call, so a changed COM number is handled ' +
      'automatically. Motion commands make real hardware move — lift the wheels off the desk before driving.',
    parameters: {
      scene: { ...sceneParam, required: true },
      command: {
        type: 'string',
        required: true,
        description:
          'The command text without line ending; the scene config appends its own (default CRLF).',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['scene', 'command', 'port', 'response', 'responseHex', 'bytes', 'empty'],
        properties: {
          scene: { type: 'string' },
          command: { type: 'string' },
          port: { type: 'string', description: 'The port actually used after auto-matching.' },
          portSource: { type: 'string' },
          note: { type: 'string', description: 'Non-empty when the port was auto-corrected.' },
          response: { type: 'string' },
          responseHex: { type: 'string' },
          bytes: { type: 'integer' },
          empty: { type: 'boolean' },
          warning: { type: 'string', description: 'Present when the device stayed silent.' },
        },
      },
      render: (_args, value) => {
        const head = [`<scene>${value.scene}</scene>`, `<tx>${value.command}</tx>`, `<port>${value.port}</port>`];
        if (value.note) head.push(`<note>${value.note}</note>`);
        const body = value.empty
          ? '(无回显)'
          : `${value.response}\n【hex】${value.responseHex}`;
        const lines = [...head, '<rx>', body, '</rx>'];
        if (value.warning) lines.push(`<warning>${value.warning}</warning>`);
        return [{ type: 'text', text: lines.join('\n') }];
      },
      presentationMeta: (_args, value) => ({ scene: value.scene }),
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const scene = String(args?.scene ?? '').trim();
      const command = typeof args?.command === 'string' ? args.command : '';
      if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
      if (command.trim() === '') throw new Error('command 不能为空');

      const data = await runCli(['send', '--scene', scene, '--command', command, '--as', 'ai']);
      return {
        scene: data.scene ?? scene,
        command: data.command ?? command,
        port: data.port ?? '',
        portSource: data.port_source ?? '',
        note: data.note ?? '',
        response: data.response ?? '',
        responseHex: data.response_hex ?? '',
        bytes: Number(data.bytes) || 0,
        empty: Boolean(data.empty),
        ...(data.warning ? { warning: data.warning } : {}),
      };
    },
  });

  // ── serial_read ────────────────────────────────────────────────────────────
  register({
    name: 'serial_read',
    description:
      'Read the most recent lines of the TX/RX log that the bridge keeps for a scene. ' +
      'Every serial_send is logged there, so this shows what was sent and what came back.',
    parameters: {
      scene: { ...sceneParam, required: true },
      lines: {
        type: 'integer',
        description: 'How many trailing log lines to return (default 20, max 1000).',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['scene', 'text', 'logPath', 'empty'],
        properties: {
          scene: { type: 'string' },
          text: { type: 'string' },
          logPath: { type: 'string' },
          empty: { type: 'boolean' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.empty
          ? `<scene>${value.scene}</scene>\n<log>（还没有日志：这个场景还没发过命令）</log>`
          : `<scene>${value.scene}</scene>\n<log>\n${value.text}\n</log>`,
      }],
      presentationMeta: (_args, value) => ({ path: value.logPath }),
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const scene = String(args?.scene ?? '').trim();
      if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
      const lines = Number.isFinite(args?.lines)
        ? Math.min(1000, Math.max(1, Math.trunc(args.lines)))
        : 20;
      const data = await runCli(['read', '--scene', scene, '--lines', String(lines)]);
      return {
        scene: data.scene ?? scene,
        text: data.text ?? '',
        logPath: data.log_path ?? '',
        empty: Boolean(data.empty),
      };
    },
  });

  // ── serial_info ────────────────────────────────────────────────────────────
  register({
    name: 'serial_info',
    description:
      'Show one scene in detail: COM port, matching rule, baud rate, line ending, the watch/auto-recovery ' +
      'configuration, and the full protocol notes (command set, observed responses, known pitfalls). ' +
      'Read this before driving unfamiliar hardware.',
    parameters: {
      scene: { ...sceneParam, required: true },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['scene', 'baud', 'newline', 'protocol', 'protocolPath', 'com', 'match'],
        properties: {
          scene: { type: 'string' },
          com: { type: 'string' },
          match: { type: 'string' },
          matchVidPid: { type: 'string' },
          baud: { type: 'integer' },
          newline: { type: 'string' },
          protocol: { type: 'string' },
          protocolPath: { type: 'string' },
          resolved: {
            type: 'object',
            additionalProperties: false,
            required: ['device', 'source', 'note'],
            properties: {
              device: { type: 'string' },
              source: { type: 'string' },
              note: { type: 'string' },
            },
          },
          watch: {
            type: 'object',
            additionalProperties: false,
            required: ['sendEnabled', 'checkEnabled', 'checkSource', 'command', 'intervalMs'],
            properties: {
              sendEnabled: { type: 'boolean' },
              checkEnabled: { type: 'boolean' },
              checkSource: { type: 'string' },
              command: { type: 'string' },
              intervalMs: { type: 'integer' },
              rules: { type: 'integer' },
              autoCommands: { type: 'integer' },
            },
          },
          resolveError: { type: 'string' },
        },
      },
      render: (_args, value) => {
        const lines = [
          `<scene>${value.scene}</scene>`,
          `<serial>${value.resolved?.device || value.com || '(未解析)'}  ${value.baud || '?'} 8N1  newline=${JSON.stringify(value.newline)}</serial>`,
          `<matching>com=${value.com || '-'}  match=${value.match || '-'}  vidpid=${value.matchVidPid || '-'}</matching>`,
        ];
        if (value.resolved?.note) lines.push(`<note>${value.resolved.note}</note>`);
        if (value.resolveError) lines.push(`<error>${value.resolveError}</error>`);
        lines.push(
          `<watch>定时发送=${value.watch.sendEnabled ? '开' : '关'}  异常判定=${value.watch.checkEnabled ? '开' : '关'}` +
            `（来源 ${value.watch.checkSource}）  规则 ${value.watch.rules} 条  自动命令白名单 ${value.watch.autoCommands} 条</watch>`,
        );
        lines.push(`<protocol path="${value.protocolPath}">`, value.protocol, '</protocol>');
        return [{ type: 'text', text: lines.join('\n') }];
      },
      presentationMeta: (_args, value) => ({ path: value.protocolPath }),
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const scene = String(args?.scene ?? '').trim();
      if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
      const data = await runCli(['info', '--scene', scene]);
      const watch = data.watch ?? {};
      return {
        scene: data.scene ?? scene,
        com: data.com ?? '',
        match: data.match ?? '',
        matchVidPid: data.match_vid_pid ?? '',
        baud: Number(data.baud) || 0,
        newline: data.newline ?? '\\r\\n',
        protocol: data.protocol ?? '',
        protocolPath: data.protocol_path ?? '',
        resolved: data.resolved
          ? {
              device: data.resolved.device ?? '',
              source: data.resolved.source ?? '',
              note: data.resolved.note ?? '',
            }
          : { device: '', source: '', note: '' },
        resolveError: data.resolve_error
          ? renderError(data.resolve_error.kind, data.resolve_error)
          : '',
        watch: {
          sendEnabled: Boolean(watch.send_enabled),
          checkEnabled: Boolean(watch.check_enabled),
          checkSource: watch.check_source ?? 'watch_only',
          command: watch.command ?? '',
          intervalMs: Number(watch.interval_ms) || 0,
          rules: (watch.rules ?? []).length,
          autoCommands: (watch.allowed_auto_commands ?? []).length,
        },
      };
    },
  });

  // ── serial_status ──────────────────────────────────────────────────────────
  register({
    name: 'serial_status',
    description:
      'Show who currently controls a scene\'s port (human / ai / paused) and the recent auto-recovery ' +
      'alerts the bridge recorded. Call this before taking autonomous action, and after the user says ' +
      'the hardware was handed over to you.',
    parameters: {
      scene: { ...sceneParam, required: true },
      alerts: {
        type: 'integer',
        description: 'How many recent alerts to include (default 10, max 100).',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['scene', 'state', 'alerts', 'aiMaySend', 'watched'],
        properties: {
          scene: { type: 'string' },
          state: { type: 'string', description: 'human | ai | paused' },
          changedBy: { type: 'string' },
          note: { type: 'string' },
          aiMaySend: {
            type: 'boolean',
            description: 'Whether the AI can send right now. True when state is ai, or when nobody is watching this scene in the web UI.',
          },
          watched: {
            type: 'boolean',
            description: 'Whether the web UI is currently watching this scene (a live heartbeat).',
          },
          alerts: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['time', 'rule', 'reason', 'action', 'source'],
              properties: {
                time: { type: 'string' },
                rule: { type: 'string' },
                reason: { type: 'string' },
                action: { type: 'string' },
                source: { type: 'string' },
              },
            },
          },
          alertsPath: { type: 'string' },
        },
      },
      render: (_args, value) => {
        const lines = [
          `<scene>${value.scene}</scene>`,
          `<control state="${value.state}" by="${value.changedBy}">${value.note || ''}</control>`,
          `<ai_may_send>${value.aiMaySend ? 'yes' : 'no'}</ai_may_send>`,
          `<web_watching>${value.watched ? 'yes' : 'no'}</web_watching>`,
        ];
        if (!value.aiMaySend && value.watched) {
          lines.push('<hint>网页正在监视本场景，AI 发送需要人类点[交给AI]或[继续AI]。</hint>');
        }
        if (value.alerts.length === 0) {
          lines.push('<alerts>（没有异常记录）</alerts>');
        } else {
          lines.push('<alerts>');
          for (const alert of value.alerts) {
            lines.push(`  [${alert.time}] 规则「${alert.rule}」${alert.reason} → ${alert.action}（${alert.source}）`);
          }
          lines.push('</alerts>');
        }
        return [{ type: 'text', text: lines.join('\n') }];
      },
      presentationMeta: (_args, value) => ({ path: value.alertsPath }),
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const scene = String(args?.scene ?? '').trim();
      if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
      const limit = Number.isFinite(args?.alerts)
        ? Math.min(100, Math.max(1, Math.trunc(args.alerts)))
        : 10;
      const data = await runCli(['status', '--scene', scene, '--alerts', String(limit)]);
      const current = data.state ?? {};
      return {
        scene: data.scene ?? scene,
        state: current.state ?? 'human',
        changedBy: current.changed_by ?? '',
        note: current.note ?? '',
        aiMaySend: Boolean(data.ai_may_send),
        watched: Boolean(data.watched),
        transport: data.transport ?? null,
        alerts: (data.alerts ?? []).map((alert) => ({
          time: alert.time ?? '',
          rule: alert.rule ?? '',
          reason: alert.reason ?? '',
          action: alert.action ?? '',
          source: alert.source ?? '',
        })),
        alertsPath: data.alerts_path ?? '',
      };
    },
  });

  // ── serial_watch ───────────────────────────────────────────────────────────
  register({
    name: 'serial_watch',
    description:
      'Read or change the two independent monitoring switches of a scene, without starting the web UI. ' +
      'send_enabled only controls timed auto-sending; check_enabled only controls rule evaluation. ' +
      'Either one works with the other off. Changes take effect immediately.',
    parameters: {
      scene: { ...sceneParam, required: true },
      sendEnabled: {
        type: 'boolean',
        description: 'Turn timed auto-sending on or off. Omit to leave unchanged.',
      },
      checkEnabled: {
        type: 'boolean',
        description: 'Turn rule evaluation on or off. Omit to leave unchanged.',
      },
      checkSource: {
        type: 'string',
        enum: ['watch_only', 'manual_only', 'both'],
        description: 'Which echoes the rules see. Default watch_only.',
      },
      command: { type: 'string', description: 'The command used by timed auto-sending.' },
      intervalMs: { type: 'integer', description: 'Timed auto-send interval in milliseconds.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['scene', 'sendEnabled', 'checkEnabled', 'checkSource', 'command', 'intervalMs'],
        properties: {
          scene: { type: 'string' },
          sendEnabled: { type: 'boolean' },
          checkEnabled: { type: 'boolean' },
          checkSource: { type: 'string' },
          command: { type: 'string' },
          intervalMs: { type: 'integer' },
          changed: {
            type: 'array',
            items: { type: 'string' },
          },
        },
      },
      render: (_args, value) => {
        const lines = [
          `<scene>${value.scene}</scene>`,
          `<watch>定时发送=${value.sendEnabled ? '开' : '关'}  异常判定=${value.checkEnabled ? '开' : '关'}` +
            `（来源 ${value.checkSource}）</watch>`,
          `<auto_send command="${value.command}" interval_ms="${value.intervalMs}"/>`,
        ];
        if (value.changed.length > 0) lines.push(`<changed>${value.changed.join(', ')}</changed>`);
        lines.push('<note>这两个开关只对常驻监视进程（网页监视模式）生效；serial_send 永远按调用即时收发。</note>');
        return [{ type: 'text', text: lines.join('\n') }];
      },
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const scene = String(args?.scene ?? '').trim();
      if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
      const sets = [];
      if (typeof args?.sendEnabled === 'boolean') sets.push(`send_enabled=${args.sendEnabled}`);
      if (typeof args?.checkEnabled === 'boolean') sets.push(`check_enabled=${args.checkEnabled}`);
      if (typeof args?.checkSource === 'string') sets.push(`check_source=${args.checkSource}`);
      if (typeof args?.command === 'string') sets.push(`command=${args.command}`);
      if (Number.isFinite(args?.intervalMs)) sets.push(`interval_ms=${Math.trunc(args.intervalMs)}`);

      const argv = ['watch', '--scene', scene, '--action', sets.length > 0 ? 'start' : 'status'];
      for (const item of sets) argv.push('--set', item);

      const data = await runCli(argv);
      const watch = data.watch ?? {};
      return {
        scene: data.scene ?? scene,
        sendEnabled: Boolean(watch.send_enabled),
        checkEnabled: Boolean(watch.check_enabled),
        checkSource: String(watch.check_source ?? 'watch_only'),
        command: String(watch.command ?? ''),
        intervalMs: Number(watch.interval_ms) || 0,
        changed: Object.keys(data.changed ?? {}),
      };
    },
  });

  // ── serial_control ─────────────────────────────────────────────────────────
  register({
    name: 'serial_control',
    description:
      'Hand a scene\'s port between the human and the AI (or pause the AI), without needing the web UI. ' +
      'Only meaningful while someone is watching the scene in the web UI: without it, the AI may send freely. ' +
      'Motion commands make real hardware move — never hand over control while the wheels touch the ground.',
    parameters: {
      scene: { ...sceneParam, required: true },
      action: {
        type: 'string',
        required: true,
        enum: ['ai', 'pause', 'resume', 'human'],
        description:
          'ai = hand control to the AI; pause = freeze the AI; resume = unfreeze the AI; ' +
          'human = take control back. Legality follows the button table: from human only "ai"; ' +
          'from ai only "pause"/"human"; from paused "ai"/"resume"/"human".',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        required: ['scene', 'state'],
        properties: {
          scene: { type: 'string' },
          state: { type: 'string', description: 'human | ai | paused' },
          changedBy: { type: 'string' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: [
          `<scene>${value.scene}</scene>`,
          `<control state="${value.state}" by="${value.changedBy}"/>`,
        ].join('\n'),
      }],
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const scene = String(args?.scene ?? '').trim();
      const action = String(args?.action ?? '').trim();
      if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
      if (!['ai', 'pause', 'resume', 'human'].includes(action)) {
        throw new Error(`action 只能是 ai / pause / resume / human，收到 ${action}`);
      }
      const data = await runCli(['control', '--scene', scene, '--action', action, '--by', 'ai']);
      const current = data.state ?? {};
      return {
        scene: data.scene ?? scene,
        state: current.state ?? 'human',
        changedBy: current.changed_by ?? '',
      };
    },
  });
}
