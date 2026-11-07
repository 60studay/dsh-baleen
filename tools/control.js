/* 鲸歌工具层 · 监视与控制权（serial_watch / serial_control）。
 * 控制权是人类与 AI 之间的门禁，单独放一个文件，改动时好找。
 */

import { renderError } from '../helpers/errors.js';

export function register({ register, runCli, sceneParam, logRoot }) {
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
        selfGranted: {
          type: 'boolean',
          description: 'true when the AI took control itself with nobody watching the web UI',
        },
        note: { type: 'string' },
      },
    },
    render: (_args, value) => [{
      type: 'text',
      text: [
        `<scene>${value.scene}</scene>`,
        `<control state="${value.state}" by="${value.changedBy}"/>`,
        value.selfGranted
          ? '<audit self_granted="true">你这次是以 AI 身份自授权的（无人在看网页），已记入 changed_by=ai</audit>'
          : '',
        value.note && !value.selfGranted ? `<note>${value.note}</note>` : '',
      ].filter(Boolean).join('\n'),
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
    const out = {
      scene: data.scene ?? scene,
      state: current.state ?? 'human',
      changedBy: current.changed_by ?? '',
    };
    if (data.self_granted) out.selfGranted = true;
    if (data.note) out.note = String(data.note);
    return out;
  },
});

}
