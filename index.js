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

import { makeEnv, readJson } from './helpers/env.js';
import { renderError } from './helpers/errors.js';
import { register as registerSerialTools } from './tools/serial.js';
import { register as registerControlTools } from './tools/control.js';
import { register as registerDaemonTools } from './tools/daemon.js';
import { register as registerWebTools } from './tools/web.js';

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
    const env = makeEnv({ logRoot, protocolRoot, vendor: VENDOR });
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


  // 各工具组的注册都走同一个 ctx：它们只拿到 runCli / register / sceneParam / logRoot，
  // 互相之间不共享任何状态（每个 execute 独立跑一次 CLI）。
  const toolCtx = { register, runCli, sceneParam, logRoot };
  registerSerialTools(toolCtx);
  registerControlTools(toolCtx);
  registerDaemonTools(toolCtx);
  registerWebTools(toolCtx);
}
