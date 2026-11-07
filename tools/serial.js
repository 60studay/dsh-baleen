/* 鲸歌工具层 · 串口读写与状态（serial_list / send / read / info / status）。
 * 这五个工具互相不共享状态：每次 execute 都独立跑一次 CLI。
 */

import { renderError } from '../helpers/errors.js';

export function register({ register, runCli, sceneParam, logRoot }) {
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
    wait: {
      type: 'boolean',
      description:
        'true (default) = wait for the device reply and return it. ' +
        'false = return as soon as the bytes are out, for batch work: fire many commands, ' +
        'then read the replies with serial_read. Nothing is lost — the reply is still logged ' +
        'and kept for /poll.',
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
        submitted: { type: 'boolean', description: 'True when wait=false: the bytes are out, the reply was not waited for.' },
        submitMs: { type: 'integer', description: 'Time until the bytes were on the wire.' },
      },
    },
    render: (_args, value) => {
      const head = [`<scene>${value.scene}</scene>`, `<tx>${value.command}</tx>`, `<port>${value.port}</port>`];
      if (value.note) head.push(`<note>${value.note}</note>`);
      const body = value.submitted
        ? '（已发出，未等回显；用 serial_read 取回显）'
        : (value.empty ? '(无回显)' : `${value.response}\n【hex】${value.responseHex}`);
      const lines = [...head, '<rx>', body, '</rx>'];
      if (Number.isFinite(value.submitMs)) lines.push(`<submit_ms>${value.submitMs}</submit_ms>`);
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

    const wait = args?.wait !== false;      // 默认 true
    const argv = ['send', '--scene', scene, '--command', command, '--as', 'ai'];
    if (!wait) argv.push('--no-wait');
    const data = await runCli(argv);
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
      ...(data.submitted ? { submitted: true } : {}),
      ...(Number.isFinite(data.submit_ms) ? { submitMs: data.submit_ms } : {}),
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
        transport: {
          type: 'object',
          additionalProperties: true,
          description:
            'How bytes physically leave this scene: serial (type/device/baud/frame) or a future ' +
            'tcp/file transport. The field set depends on the transport kind, so it is not enumerated.',
        },
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
      // 只有拿到描述时才带上：schema 里它是 object，塞 null 会被校验拒掉
      ...(data.transport ? { transport: data.transport } : {}),
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

}
