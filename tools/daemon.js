/* 鲸歌工具层 · 常驻管家（serial_daemon）。
 * 管家让命令不用每次开关串口，代价是它占着口。
 */

import { renderError } from '../helpers/errors.js';

export function register({ register, runCli, sceneParam, logRoot }) {
// ── serial_daemon ──────────────────────────────────────────────────────────
register({
  name: 'serial_daemon',
  description:
    'Start or stop the resident daemon that holds the port once, so commands no longer open/close ' +
    'the serial port every time. While it runs the port is occupied (a serial terminal cannot open it) ' +
    'and it releases automatically after an idle timeout. Idempotent: starting twice returns the same pid. ' +
    'Use it before a multi-step task; stop it when you are done so a human can use the port.',
  parameters: {
    scene: { ...sceneParam, required: true },
    action: { type: 'string', required: true, enum: ['start', 'stop', 'status'] },
    port: { type: 'number', description: 'HTTP port for the daemon (default 8898).' },
    idle: { type: 'number', description: 'Idle seconds before it exits and releases the port (default 600).' },
  },
  output: {
    schema: {
      type: 'object',
      additionalProperties: false,
      required: ['scene', 'action', 'running'],
      properties: {
        scene: { type: 'string' },
        action: { type: 'string' },
        running: { type: 'boolean' },
        pid: { type: 'number' },
        port: { type: 'string' },
        url: { type: 'string' },
        note: { type: 'string' },
        uptimeS: { type: 'number' },
      },
    },
    render: (_args, value) => [{
      type: 'text',
      text: [
        `<daemon scene="${value.scene}" action="${value.action}" running="${value.running}"/>`,
        value.pid ? `<pid>${value.pid}</pid>` : '',
        value.port ? `<port>${value.port}</port>` : '',
        value.note ? `<note>${value.note}</note>` : '',
      ].filter(Boolean).join('\n'),
    }],
  },
  isConcurrencySafe: () => false,
  async execute(args) {
    const scene = String(args?.scene ?? '').trim();
    const action = String(args?.action ?? '').trim();
    if (!scene) throw new Error('scene 不能为空，先用 serial_list 看有哪些场景');
    if (!['start', 'stop', 'status'].includes(action)) {
      throw new Error(`action 只能是 start / stop / status，收到 ${action}`);
    }
    const argv = ['daemon', '--scene', scene, '--action', action];
    if (Number.isFinite(args?.port)) argv.push('--port', String(Math.trunc(args.port)));
    if (Number.isFinite(args?.idle)) argv.push('--idle', String(Math.trunc(args.idle)));
    const data = await runCli(argv);
    // 只放有值的字段：undefined 会让 schema 校验报 "not lossless JSON"
    const result = { scene, action, running: Boolean(data.running) };
    if (data.pid !== undefined) result.pid = data.pid;
    if (data.port) result.port = data.port;
    if (data.url) result.url = data.url;
    if (data.note) result.note = data.note;
    if (data.uptime_s !== undefined) result.uptimeS = data.uptime_s;
    return result;
  },
});

}
