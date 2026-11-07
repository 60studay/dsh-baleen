import { $, api, post, errText, errLine, pushEvent } from '../js/core.js';
import { autoGrow } from '../js/layout.js';
import { loadRules, onRulesSaved } from './js/rules.js';
import { pollChannel } from './js/channel.js';

/* 鲸歌网页逻辑 · 收发区 + 规则 + 启动（R2b 第一步之后剩下的部分）。
 * 基础设施在 js/core.js、配色和拖动在 js/layout.js，这里靠 import 用它们。
 * 下一步（R2b 第二步）再把「通道」和「规则编辑」拆出去。
 */
  var consoleEl = $('console');
  var dispMode = 'text';
  var lastState = null;
  var pollTimer = null;
  var lastAlertsRaw = [];

  /* ── 告警条关闭状态（刷新后仍然生效）── */
  var ALERT_DISMISS_KEY = 'sb.alertDismissedTs';
  function getDismissedTs() {
    try { return parseFloat(localStorage.getItem(ALERT_DISMISS_KEY) || '0') || 0; }
    catch (e) { return 0; }
  }
  function setDismissedTs(value) {
    try { localStorage.setItem(ALERT_DISMISS_KEY, String(value)); } catch (e) { /* 隐私模式忽略 */ }
  }
  var lastStatus = { control: null, port: null, owner: null, alerts: 0, running: null };

  /* ── 回显区渲染 ─────────────────────────────────────────
   *  两列网格：左列徽章、右列内容。TX 和 RX 走同一套排版，
   *  所以"设备回的话"会自然对齐到"命令文字"的左边。
   *
   *  为什么不写 ">> 命令"：
   *    用户反馈 >> 容易和串口数据里的字符混淆。改成靠颜色 + 徽章区分。
   *  为什么回显要粘在一个块里：
   *    串口是字节流，半包/粘包都常见；逐字节换行会把 "OK PO" / "NG" 拆成两行。
   *  为什么用 grid 而不是给回显加 padding：
   *    徽章宽度随字体/缩放变，写死 padding 迟早错位。
   */
  var MAX_CHARS = 300000;
  var curRx = null;      // 当前正在追加的回显块（发完命令就置空，下一条回显新起一行）

  function trimConsole() {
    while (consoleEl.textContent.length > MAX_CHARS && consoleEl.firstChild) {
      consoleEl.removeChild(consoleEl.firstChild);
    }
  }

  /** 建一行：左列徽章、右列正文。返回正文节点供调用方追加。 */
  function newRow(tagClass, tagText) {
    var row = document.createElement('div');
    row.className = 'ln';
    var tag = document.createElement('span');
    tag.className = 'tag ' + tagClass;
    tag.textContent = tagText;
    var body = document.createElement('span');
    body.className = 'body';
    row.appendChild(tag);
    row.appendChild(body);
    consoleEl.appendChild(row);
    return body;
  }

  /** 追加回显：贴到当前回显行；没有就新起一行（设备徽章）。 */
  function append(text) {
    if (!text) return;
    if (!curRx) {
      curRx = newRow('dev', '设备');
      curRx.classList.add('rx');
    }
    curRx.appendChild(document.createTextNode(text));
    consoleEl.scrollTop = consoleEl.scrollHeight;
    trimConsole();
  }

  /* source → [徽章样式, 徽章文字]。web/manual 都是"人从网页点的"，算"我"。 */
  var WHO = {
    ai: ['ai', 'AI'],
    human: ['human', '我'],
    web: ['human', '我'],
    manual: ['human', '我'],
    auto: ['auto', '定时'],
    timer: ['auto', '定时'],
    watch: ['auto', '定时'],
    rule: ['rule', '规则'],
    dev: ['dev', '设备']
  };

  /** 打一行"谁发的 + 命令"，紧跟其后的回显会自动对齐到命令文字下面。 */
  function appendTx(who, command) {
    var meta = WHO[who] || WHO.dev;
    var body = newRow(meta[0], meta[1]);
    body.classList.add('tx');
    body.textContent = command;
    curRx = null;         // 下一条回显要新起一行（设备徽章）
    consoleEl.scrollTop = consoleEl.scrollHeight;
    trimConsole();
  }

  /* ── 显示 ─────────────────────────────────────────────── */
  function renderPorts(data) {
    var sel = $('portSelect');
    var current = data.port || '';
    sel.innerHTML = '';
    (data.ports || []).forEach(function (p) {
      var opt = document.createElement('option');
      opt.value = p.device;
      opt.textContent = p.device + '  ' + (p.description || '');
      if (p.device === current) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.options.length) {
      var opt = document.createElement('option');
      opt.textContent = '（没有检测到串口）';
      opt.value = '';
      sel.appendChild(opt);
    }
    var bauds = data.bauds || [];
    var bsel = $('baudSelect');
    var curBaud = data.baud || 115200;
    bsel.innerHTML = '';
    bauds.forEach(function (b) {
      var o = document.createElement('option');
      o.value = b; o.textContent = b;
      if (b === curBaud) o.selected = true;
      bsel.appendChild(o);
    });
    if (bauds.indexOf(curBaud) === -1) {
      var o2 = document.createElement('option');
      o2.value = curBaud; o2.textContent = curBaud;
      o2.selected = true;
      bsel.appendChild(o2);
    }
  }

  var STATE_LABEL = { human: '人工', ai: 'AI 托管', paused: '已暂停' };

  function renderStatus(data) {
    $('sceneName').textContent = '· ' + (data.scene || '');
    // 传输类型由后端给（transport），以后加 TCP / AI 通道时这里自动跟着变
    var tp = data.transport || {};
    var kindLabel = { serial: '串口', tcp: 'TCP', file: 'AI 通道' }[tp.type] || (tp.type || '串口');
    $('portPill').textContent = kindLabel + ' ' + (data.port || '未连接') + ' @ ' + (data.baud || '?');
    $('portPill').title = tp.frame ? (kindLabel + ' ' + (tp.frame || '')) : kindLabel;
    $('modePill').textContent = data.mode === 'watch' ? '监视模式（占口）' : '轻模式（不占口）';
    document.querySelector('input[name=wmode][value=' + (data.mode || 'light') + ']').checked = true;

    // 谁占着这个口。
    // 轻模式下后端**不探测**（打开 CH340 会拉 DTR/RTS，可能给板子来一次复位），
    // 只显示"状态未知"；想知道真答案就点这个胶囊，走 /port/probe 主动试一次。
    var owner = data.port_owner || {};
    var oe = $('portOwner');
    var OWNER_LABEL = { ours: '被本网页占用', other: '被其他程序占用', free: '可用', unknown: '状态未知' };
    var shown = owner;
    if (probeResult && (Date.now() - probeResult.at) < 8000) shown = probeResult;
    oe.dataset.owner = shown.owner || 'unknown';
    oe.textContent = OWNER_LABEL[shown.owner] || '—';
    oe.title = (shown.detail || '') + '（点一下检测占用）';
    oe.style.cursor = 'pointer';

    // 暂停/恢复按钮
    var running = !!data.watch_running;
    $('btnPausePort').textContent = running ? '暂停并释放串口' : '恢复占用';
    $('btnPausePort').disabled = false;
    $('pauseHint').textContent = data.hold_released_reason
      ? '已释放：' + data.hold_released_reason
      : (running ? '网页仍可用，只是把串口让给串口助手。' : '当前未占用串口，网页可正常使用。');

    var st = data.control || 'human';
    var el = $('controlstate');
    el.dataset.state = st;
    el.textContent = STATE_LABEL[st] || st;
    // 按钮规则：
    //   目标状态 ≠ 当前 → 可用（合法迁移）
    //   目标状态 = 当前 → 也可用，点了给出"已经在你手中"的反馈
    //   其余方向非法 → 禁用并说明原因（不能出现"点了没反应"）
    $('btnAi').disabled     = false;
    $('btnPause').disabled  = st !== 'ai';
    $('btnResume').disabled = st !== 'paused';
    $('btnHuman').disabled  = false;
    $('btnAi').title     = st === 'ai' ? '已经是 AI 托管' : '';
    $('btnHuman').title  = st === 'human' ? '已经是人工控制' : '';
    $('btnPause').title  = st === 'ai' ? '' : '只有 AI 托管时才能暂停 AI';
    $('btnResume').title = st === 'paused' ? '' : '只有已暂停时才能继续 AI';

    var CONTROL_HINT = {
      human: '人工控制：AI 不能发命令，你可以直接用串口助手',
      ai: 'AI 托管：AI 可以发命令，人类发任何命令会立刻收回控制权',
      paused: '已暂停：谁都不发，点[继续AI]或[人类接管]恢复'
    };
    var ch = $('controlhint');
    if (ch) ch.textContent = CONTROL_HINT[st] || '';

    var w = data.watch || {};
    // ⚠️ 用户正在编辑表单时**不许用轮询的值覆盖他**。
    // /status 每 700ms 拉一次，以前这里无条件写回，等于"根本改不了"：
    // 删掉一位数字，0.7 秒后它自己跳回来（用户实测报的 bug）。
    // 只跳过**表单控件**；规则列表、告警、状态胶囊这些只读展示照常刷新，
    // 否则用户改完规则看不到变化。
    var ae = document.activeElement;
    var editingForm = ae && /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName);
    if (!editingForm) {
      $('sendEnabled').checked = !!w.send_enabled;
      $('checkEnabled').checked = !!w.check_enabled;
      if (w.command && !$('watchCommand').value) $('watchCommand').value = w.command;
      if (w.interval_ms) $('watchInterval').value = w.interval_ms;
      if (w.check_source) $('checkSource').value = w.check_source;
    }

    var rs = $('ruleState');
    if (data.resolve_error) {
      rs.className = 'rule-state bad';
      rs.textContent = '✕ ' + (data.resolve_error.message || '串口不可用');
    } else if (w.check_enabled) {
      rs.className = 'rule-state ok';
      rs.textContent = '✓ 判定中（' + (w.check_source || 'watch_only') + '）';
    } else {
      rs.className = 'rule-state ok';
      rs.textContent = '— 判定未启用';
    }

    // 告警条：只显示"用户还没关掉"的记录。
    // alerts.jsonl 是历史累积文件，只要有一条就永久显示是不对的（Bug：关不掉）。
    // 关闭时把当前时间记进 localStorage，之后 ts 更旧的都不弹；文件本身不删。
    var rawAlerts = data.alerts || [];
    lastAlertsRaw = rawAlerts;
    var alerts = rawAlerts.filter(function (a) { return (a.ts || 0) > getDismissedTs(); });
    var bar = $('alertbar');
    if (alerts.length) {
      var a = alerts[alerts.length - 1];
      $('alertbarText').textContent = '⚠ ' + (a.time || '') + '  规则「' + (a.rule || '?') + '」触发：' +
        (a.reason || '') + (a.action ? '  → 已执行 ' + a.action : '');
      bar.classList.add('show');
    } else {
      bar.classList.remove('show');
    }

    /* ── 把状态变化写进时间轨道（不混进串口数据区）── */
    if (lastStatus.control !== null && lastStatus.control !== st) {
      pushEvent('ctrl', '控制权 → ' + (STATE_LABEL[st] || st));
    }
    if (lastStatus.port !== null && data.port && lastStatus.port !== data.port) {
      pushEvent('', '串口 → ' + data.port + ' @ ' + (data.baud || '?'));
    }
    var ow = owner.owner || 'unknown';
    if (lastStatus.owner !== null && lastStatus.owner !== ow) {
      var OWNER_TEXT = { ours: '被本网页占用', other: '被其他程序占用', free: '可用', unknown: '状态未知' };
      pushEvent(ow === 'other' ? 'bad' : '', '串口' + (OWNER_TEXT[ow] || ow));
    }
    var alertCount = alerts.length;
    if (alertCount > (lastStatus.alerts || 0)) {
      pushEvent('warn', '告警：' + (alerts[alertCount - 1].rule || '?'));
    }
    var runNow = !!data.watch_running;
    if (lastStatus.running !== null && lastStatus.running !== runNow) {
      pushEvent('', runNow ? '后台循环已启动' : '后台循环已停止');
    }
    lastStatus = { control: st, port: data.port || null, owner: ow,
                   alerts: alertCount, running: runNow };

    lastState = data;
  }

  /* ── 轮询 ─────────────────────────────────────────────── */
  function refreshPorts() {
    return api('/ports').then(function (data) {
      if (data.ok) renderPorts({ ports: data.ports, bauds: data.bauds,
                                 port: lastState && lastState.port,
                                 baud: lastState && lastState.baud });
    });
  }

  function tick() {
    api('/status').then(function (data) {
      if (data.ok) renderStatus(data);
    });
    api('/poll').then(function (data) {
      if (!data.ok) return;
      // 自动发送 / AI 发起的每一帧：先打来源+命令，再打回显（来源映射见 WHO）
      (data.frames || []).forEach(function (f) {
        appendTx(f.source || 'auto', f.tx);
        if (dispMode === 'hex') {
          if (f.hex) append(f.hex + '\n');
        } else if (f.rx) {
          append(f.rx);
        }
      });
      if (dispMode === 'hex') {
        if (data.rx_hex) append(data.rx_hex + '\n');
      } else if (data.rx_text) {
        append(data.rx_text);
      }
    });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(tick, 700);
  }

  /* ── 事件 ─────────────────────────────────────────────── */
  function send() {
    var raw = $('cmd').value;
    if (!raw.trim()) return;
    // 输入框是多行的，但**换行不原样发出去**：串口命令的结尾由场景的 newline
    // 统一加，手工塞进去的换行会变成命令的一部分。
    // hex 模式本来就允许用空格分隔字节，所以这里统一按空格合并。
    var text = raw.replace(/[\r\n]+/g, ' ').trim();
    $('cmd').value = '';
    autoGrow();
    // 先发，成功了才把这条命令打到回显区。
    // 否则被拒绝（控制权/被占用/格式错）时界面也显示成"发过了"，看起来像成功了（Bug 9b）。
    post('/send', { text: text, mode: dispMode, actor: 'human' }).then(function (data) {
      if (!data.ok) {
        pushEvent('bad', '发送失败：' + errLine(data.error));
        return;
      }
      appendTx('human', text);
      if (data.rx_text || data.rx_hex) {
        if (dispMode === 'hex') append(data.rx_hex + '\n');
        else if (data.rx_text) append(data.rx_text);
      }
    });
  }

  $('cmd').addEventListener('input', autoGrow);

  $('sendBtn').addEventListener('click', send);

  // 轻模式默认不主动读串口（保证不占口），需要时手动读一帧
  $('btnReadOnce').addEventListener('click', function () {
    api('/poll?force=1').then(function (data) {
      if (!data.ok) { pushEvent('bad', '读取失败：' + errText(data.error)); return; }
      if (data.rx_text) append(data.rx_text);
      else if (data.rx_hex && dispMode === 'hex') append(data.rx_hex + '\n');
      pushEvent('', '手动读取一帧（' + (data.bytes || 0) + ' 字节）');
    });
  });

  // 清空：回显 + 状态轨道 + 告警，一起清。
  // 用户测试时告警一直弹，"清空"只清回显等于没清（14:35 需求 3）。
  $('btnClear').addEventListener('click', function () {
    consoleEl.textContent = '';
    consoleEl.scrollTop = 0;
    curRx = null;
    $('timelineList').textContent = '';
    $('alertbar').classList.remove('show');
    post('/alerts/clear', {}).then(function (data) {
      if (!data.ok) { pushEvent('bad', '清空告警失败：' + errLine(data.error)); return; }
      pushEvent('', '已清空回显/状态轨道/告警' +
        (typeof data.cleared === 'number' ? '（告警 ' + data.cleared + ' 条）' : ''));
    });
  });

  // 关闭告警条：记下"已看到这里"，比它旧的不再弹（日志文件不动）
  $('alertbarClose').addEventListener('click', function () {
    var latestTs = 0;
    (lastAlertsRaw || []).forEach(function (a) { latestTs = Math.max(latestTs, a.ts || 0); });
    var nowTs = Date.now() / 1000;
    setDismissedTs(Math.max(latestTs, nowTs));
    $('alertbar').classList.remove('show');
    pushEvent('', '已关闭告警提示（日志仍保留）');
  });
  $('cmd').addEventListener('keydown', function (e) {
    // 回车发送、Shift+回车换行 —— 聊天软件的通用约定，不用教。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  document.querySelectorAll('#controlbar button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      post('/control', { action: btn.dataset.action }).then(function (data) {
        // 控制权变化本身由 renderStatus → 轨道统一显示，这里不再往数据区写
        if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
        if (data.note) pushEvent('', data.note);
        if (data.port_action === 'released') {
          pushEvent('', '已释放串口，可以打开串口助手了');
        } else if (data.port_action === 'acquired') {
          pushEvent('', '已重新占用串口（监视模式）');
        } else if (typeof data.port_action === 'string' && data.port_action.indexOf('failed') === 0) {
          pushEvent('bad', '串口未能重新占用：' + data.port_action);
        }
        tick();
      });
    });
  });

  document.querySelectorAll('input[name=disp]').forEach(function (r) {
    r.addEventListener('change', function () {
      dispMode = r.value;
      $('cmd').placeholder = dispMode === 'hex'
        ? '十六进制，如 AA 55 01 00，回车发送'
        : '输入指令后回车发送';
    });
  });

  document.querySelectorAll('input[name=wmode]').forEach(function (r) {
    r.addEventListener('change', function () {
      post('/mode', { mode: r.value }).then(function (data) {
        if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
        pushEvent('', '已切到' + (data.mode === 'watch' ? '监视模式（占口）' : '轻模式（不占口）'));
        tick();
      });
    });
  });

  function pushWatch() {
    post('/watch', {
      send_enabled: $('sendEnabled').checked,
      check_enabled: $('checkEnabled').checked,
      check_source: $('checkSource').value,
      command: $('watchCommand').value,
      interval_ms: parseInt($('watchInterval').value, 10) || 1000
    }).then(function (data) {
      if (!data.ok) pushEvent('bad', errLine(data.error));
      else tick();
    });
  }
  ['sendEnabled', 'checkEnabled'].forEach(function (id) {
    $(id).addEventListener('change', pushWatch);
  });
  $('checkSource').addEventListener('change', pushWatch);
  $('watchCommand').addEventListener('change', pushWatch);
  $('watchInterval').addEventListener('change', pushWatch);

  /* ── AI 自动选择 ─────────────────────────────────────────
   *  用户反馈"按下去特别没有实感"：原来按钮不变、下拉框不动，
   *  只有一行灰字，看不出到底选没选上。现在给三重反馈：
   *  1) 按钮自己变成"检测中…"并脉动（表示真的在干活）
   *  2) 结果文字变绿/变红（一眼分清成没成）
   *  3) 下拉框真的跳到选中的那一项并闪一下绿边（最关键的确认）
   */
  $('btnAuto').addEventListener('click', function () {
    var btn = $('btnAuto');
    var reason = $('autoReason');
    var original = btn.textContent;
    if (btn.disabled) return;

    btn.disabled = true;
    btn.classList.add('working');
    btn.textContent = '检测中…';
    reason.className = 'hint';
    reason.textContent = '正在按场景规则扫描串口…';

    function restore() {
      btn.disabled = false;
      btn.classList.remove('working');
      btn.textContent = original;
    }
    function fail(error) {
      restore();
      reason.className = 'hint bad';
      reason.textContent = '✗ ' + errLine(error);
      pushEvent('bad', '自动选择失败：' + errLine(error));
    }

    post('/autodetect', {}).then(function (data) {
      if (!data.ok) { fail(data.error); return; }
      var port = data.port;

      // 3) 让下拉框真的跳到这一项（先跳，用户马上看见变化）
      var sel = $('portSelect');
      var existed = false;
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === port) { sel.selectedIndex = i; existed = true; break; }
      }
      if (!existed && port) {   // 端口是新的：临时插进去，refreshPorts 会补全
        var o = document.createElement('option');
        o.value = port; o.textContent = port;
        sel.appendChild(o);
        sel.value = port;
      }
      sel.classList.remove('flash');
      void sel.offsetWidth;      // 强制重排，动画才能重放
      sel.classList.add('flash');

      reason.className = 'hint ok';
      reason.textContent = '✓ 选中 ' + port + '（' + data.reason + '）' +
        (data.note ? '；' + data.note : '');
      pushEvent('', '自动选择：' + port + '（' + data.reason + '）');

      post('/reconnect', { com: port, baud: parseInt($('baudSelect').value, 10) || 0 })
        .then(function (r) {
          restore();
          if (!r.ok) {
            reason.className = 'hint bad';
            reason.textContent = '✗ 已选中 ' + port + '，但连接失败：' + errLine(r.error);
            pushEvent('bad', '自动选择后连接失败：' + errLine(r.error));
            return;
          }
          refreshPorts(); tick();
        })
        .catch(function () { restore(); });
    }).catch(function (e) { fail({ message: '请求失败：' + (e && e.message || e) }); });
  });

  /* ── 显式探测串口占用（只有点了才真的 open 一次）────── */
  // 轻模式默认不碰串口，所以状态栏只能显示"状态未知"。
  // 用户想知道"是不是被串口助手占了"，点一下这里，后端才真去开一次。
  var probeResult = null;

  $('portOwner').addEventListener('click', function () {
    post('/port/probe', {}).then(function (data) {
      if (!data.ok) { pushEvent('bad', '检测占用失败：' + errLine(data.error)); return; }
      probeResult = { at: Date.now(), owner: data.owner, detail: data.detail };
      var label = { ours: '被本网页占用', other: '被其他程序占用',
                    free: '可用', unknown: '状态未知' }[data.owner] || data.owner;
      pushEvent('', '检测占用：' + label + (data.detail ? '（' + data.detail + '）' : ''));
      tick();
    });
  });

  ['portSelect', 'baudSelect'].forEach(function (id) {
    $(id).addEventListener('change', function () {
      post('/reconnect', {
        com: $('portSelect').value,
        baud: parseInt($('baudSelect').value, 10) || 0
      }).then(function (data) {
        if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
        pushEvent('', '已重连 ' + data.port + ' @ ' + data.baud);
        tick();
      });
    });
  });

  /* ── 暂停 / 恢复 与 彻底退出 分开 ─────────────────────── */
  $('btnPausePort').addEventListener('click', function () {
    var running = !!(lastState && lastState.watch_running);
    var path = running ? '/port/pause' : '/port/resume';
    post(path, {}).then(function (data) {
      if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
      if (running) {
        pushEvent('', '已暂停并释放串口（网页仍可用）');
      } else if (data.acquired) {
        pushEvent('', '已重新占用串口');
      } else {
        pushEvent('', '未占用串口：控制权不在 AI 或当前是轻模式');
      }
      tick();
    });
  });

  $('btnExit').addEventListener('click', function () {    if (!confirm('彻底退出？网页将关闭，串口会释放。\n（只想临时把串口让给别的程序，请用「暂停并释放串口」）')) return;
    post('/shutdown', {}).then(function () {
      document.body.innerHTML =
        '<div style="padding:40px;font:16px/1.8 Consolas,monospace;color:#d4d4d4">' +
        '<h2 style="color:#3fb950">鲸歌已退出</h2>' +
        '<p>HTTP 服务已停止，串口已释放。可以关闭这个标签页。</p>' +
        '<p style="color:#8a8a8a">需要时重新运行：python bridge.py web --scene &lt;场景&gt;</p></div>';
      if (pollTimer) clearInterval(pollTimer);
    });
  });

  /* ── 启动 ─────────────────────────────────────────────── */
  api('/status').then(function (data) {
    if (data.ok) renderStatus(data);
    return api('/ports');
  }).then(function (data) {
    if (data && data.ok) renderPorts({ ports: data.ports, bauds: data.bauds,
                                       port: lastState && lastState.port,
                                       baud: lastState && lastState.baud });
    onRulesSaved(tick);
    loadRules();
    tick();
    startPolling();
    pollChannel();
    setInterval(pollChannel, 2000);
  }).catch(function () {
    onRulesSaved(tick);
    loadRules();
    tick();
    startPolling();
    pollChannel();
    setInterval(pollChannel, 2000);
  });
