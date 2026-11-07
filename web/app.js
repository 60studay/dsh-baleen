/* 鲸歌网页逻辑（R2：从 index.html 搬出来的，纯搬移）。
 * 注意：整段仍是一个 IIFE，作用域和搬之前完全一样；
 * 现在 900 多行还该再拆（R2b 按模块拆），但这一步只做搬移。
 */
(function () {
  var $ = function (id) { return document.getElementById(id); };
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

  /* ── 状态轨道：系统事件与串口数据分开 ─────────────────── */
  function nowText() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function pushEvent(kind, text) {
    var box = $('timelineList');
    if (!box) return;
    var row = document.createElement('div');
    row.className = 'tl-item ' + (kind || '');
    var t = document.createElement('span');
    t.className = 't';
    t.textContent = nowText();
    row.appendChild(t);
    row.appendChild(document.createTextNode(text));
    box.appendChild(row);
    while (box.childNodes.length > 200) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

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

  function api(path, options) {
    return fetch(path, options).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: { message: 'HTTP ' + r.status } }; });
    });
  }

  function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function errText(error) {
    if (!error) return '未知错误';
    var lines = [error.message || String(error)];
    if (error.hint && error.hint.length) {
      error.hint.forEach(function (h, i) { lines.push((i + 1) + '. ' + h); });
    }
    return lines.join('\n');
  }

  /** 压成一行给状态轨道用：保留 hint，不要只留第一句。
   *  只显示第一句会丢掉"该怎么解决"（比如"点[交给AI]"），等于报了错但没给路。 */
  function errLine(error) {
    if (!error) return '未知错误';
    var parts = [String(error.message || error)];
    (error.hint || []).forEach(function (h) {
      var text = String(h || '').replace(/^\s*\d+\.\s*/, '').trim();
      if (text) parts.push(text);
    });
    return parts.join(' · ');
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

  /** 输入框随内容长高，最多 40vh（CSS 里也有 max-height 兜底）。 */
  function autoGrow() {
    var box = $('cmd');
    box.style.height = 'auto';
    var limit = Math.round(window.innerHeight * 0.4);
    box.style.height = Math.min(box.scrollHeight, limit) + 'px';
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

  /* ── 主题（一个图标循环切换）──────────────────────────────
   *  用户："占 3 个栏的位置还是太大了，可以改成一个图标，
   *        先是太阳，点一下变成月亮，再点一下变成自动"。
   *  图标用纯字符而不是 emoji：emoji 带变体选择符，不同系统渲染差别大。
   */
  var THEME_KEY = 'whale.theme';
  var THEME_ORDER = ['light', 'dark', 'auto'];
  var THEME_ICON = { light: '☀', dark: '☾', auto: '◐' };
  var THEME_NAME = { light: '亮色', dark: '深色', auto: '跟随系统' };

  function applyTheme(theme) {
    var name = THEME_ORDER.indexOf(theme) >= 0 ? theme : 'auto';
    var html = document.documentElement;
    if (name === 'dark' || name === 'light') html.setAttribute('data-theme', name);
    else html.removeAttribute('data-theme');
    var icon = $('themeIcon');
    if (icon) icon.textContent = THEME_ICON[name];
    var btn = $('themeBtn');
    if (btn) {
      // 只有图标没有文字：title 和 aria-label 都写**当前**状态，
      // 否则用户记不住这个符号现在代表什么。
      btn.title = '配色：' + THEME_NAME[name] + '（点击切换）';
      btn.setAttribute('aria-label', '配色：' + THEME_NAME[name] + '，点击切换');
    }
  }

  $('themeBtn').addEventListener('click', function () {
    var cur = 'auto';
    try { cur = localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { /* 忽略 */ }
    var next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 隐私模式忽略 */ }
  });

  var savedTheme = 'auto';
  try { savedTheme = localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { /* 忽略 */ }
  applyTheme(savedTheme);

  /* ── 可拖动布局 ──────────────────────────────────────────
   *  用户从第一天就想要这个："侧边栏和下边栏不能自由拖动改变大小"。
   *  自己写（不引 split.js）：项目零依赖，两条分隔线不值一个库。
   *  尺寸存 localStorage，否则刷新就还原，等于没做；双击恢复默认。
   */
  var LAYOUT_KEY = 'whale.layout';
  var SIDEW_MIN = 180, SIDEW_MAX = 620;
  var INPUT_MIN = 38, INPUT_MAX = 400;

  function readLayout() {
    try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}') || {}; }
    catch (e) { return {}; }
  }

  function saveLayout(next) {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(next)); } catch (e) { /* 忽略 */ }
  }

  function applyLayout(layout) {
    var root = document.documentElement;
    if (layout.sidew) root.style.setProperty('--sidew', layout.sidew + 'px');
    else root.style.removeProperty('--sidew');
    if (layout.inputH) root.style.setProperty('--input-h', layout.inputH + 'px');
    else root.style.removeProperty('--input-h');
    autoGrow();
  }

  function bindSplitter(el, onDrag, onReset) {
    el.addEventListener('mousedown', function (e) {
      e.preventDefault();
      el.classList.add('dragging');
      document.body.classList.add('dragging');
      function move(ev) { onDrag(ev); }
      function up() {
        el.classList.remove('dragging');
        document.body.classList.remove('dragging');
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
    el.addEventListener('dblclick', onReset);
  }

  var layout = readLayout();
  applyLayout(layout);

  bindSplitter($('vsplit'), function (e) {
    // 侧边栏在右边：拖到哪儿，宽度就是"窗口右边到鼠标"的距离
    var width = Math.round(window.innerWidth - e.clientX);
    width = Math.max(SIDEW_MIN, Math.min(SIDEW_MAX, width));
    layout.sidew = width;
    applyLayout(layout);
  }, function () {
    layout.sidew = 0;
    applyLayout(layout);
    saveLayout(layout);
  });

  bindSplitter($('hsplit'), function (e) {
    // 往上拖 = 输入框变高
    var composerTop = $('cmd').getBoundingClientRect().bottom;
    var height = Math.round(composerTop - e.clientY);
    height = Math.max(INPUT_MIN, Math.min(INPUT_MAX, height));
    layout.inputH = height;
    applyLayout(layout);
  }, function () {
    layout.inputH = 0;
    applyLayout(layout);
    saveLayout(layout);
  });

  document.addEventListener('mouseup', function () {
    if (layout) saveLayout(layout);
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

  /* ── 规则编辑 ─────────────────────────────────────────── */
  var rulesCache = [];
  var allowCache = [];
  var editingIndex = -1;

  var TYPE_LABEL = {
    contains: '包含', equals: '等于', regex: '正则', no_response: '无响应',
    hex: '字节', hex_regex: '字节正则'
  };

  function describeRule(r) {
    var when = r.when || {};
    var t = when.type || '?';
    var target = t === 'no_response'
      ? ('连续 ' + (when.consecutive || 3) + ' 次无回显')
      : (TYPE_LABEL[t] || t) + ' "' + (when.value || when.pattern || '') + '"';
    var send = (r.then && r.then.send) ? (' → 发 "' + r.then.send + '"') : ' → 仅通知';
    return when.type ? target + send : '(无效规则)';
  }

  function renderRules() {
    var box = $('ruleList');
    box.innerHTML = '';
    if (!rulesCache.length) {
      var empty = document.createElement('div');
      empty.className = 'hint';
      empty.textContent = '还没有规则，点上面「+ 添加规则」。';
      box.appendChild(empty);
    }
    rulesCache.forEach(function (r, i) {
      var row = document.createElement('div');
      row.className = 'rule-item';
      var txt = document.createElement('span');
      txt.className = 'txt';
      txt.textContent = (r.name || '未命名');
      txt.title = describeRule(r);
      row.appendChild(txt);

      var edit = document.createElement('button');
      edit.className = 'edit';
      edit.textContent = '改';
      edit.title = '编辑';
      edit.addEventListener('click', function () { openRuleModal(i); });
      row.appendChild(edit);

      var del = document.createElement('button');
      del.className = 'del';
      del.textContent = '×';
      del.title = '删除';
      del.addEventListener('click', function () {
        if (!confirm('删除规则「' + (r.name || '') + '」？')) return;
        var next = rulesCache.slice();
        next.splice(i, 1);
        saveRules(next, allowCache);
      });
      row.appendChild(del);
      box.appendChild(row);
    });
    $('allowList').value = allowCache.join(', ');
  }

  function loadRules() {
    api('/rules').then(function (data) {
      if (!data.ok) return;
      rulesCache = data.rules || [];
      allowCache = data.allowed_auto_commands || [];
      renderRules();
    });
  }

  function saveRules(rules, allowed) {
    post('/rules', { rules: rules, allowed_auto_commands: allowed }).then(function (data) {
      if (!data.ok) {
        $('ruleError').textContent = errText(data.error);
        pushEvent('bad', '规则保存失败：' + errLine(data.error));
        return;
      }
      rulesCache = data.rules || [];
      allowCache = data.allowed_auto_commands || [];
      renderRules();
      closeRuleModal();
      pushEvent('', '规则已保存到 ' + (data.config_path || 'config.json'));
      tick();
    });
  }

  function openRuleModal(index) {
    editingIndex = (typeof index === 'number') ? index : -1;
    var r = editingIndex >= 0 ? rulesCache[editingIndex] : null;
    $('modalTitle').textContent = r ? '编辑规则' : '添加规则';
    $('rName').value = r ? (r.name || '') : '';
    var when = (r && r.when) || {};
    $('rType').value = when.type || 'contains';
    $('rValue').value = when.value || when.pattern ||
      (when.type === 'no_response' ? String(when.consecutive || 3) : '');
    $('rSend').value = (r && r.then && r.then.send) || '';
    $('rRetries').value = (r && r.then && r.then.max_retries) || 3;
    $('ruleError').textContent = '';
    $('ruleModal').classList.add('show');
    $('rName').focus();
  }

  function closeRuleModal() {
    $('ruleModal').classList.remove('show');
    $('ruleError').textContent = '';
  }

  $('btnAddRule').addEventListener('click', function () { openRuleModal(-1); });
  $('btnRuleCancel').addEventListener('click', closeRuleModal);
  $('ruleModal').addEventListener('click', function (e) {
    if (e.target === $('ruleModal')) closeRuleModal();
  });

  $('btnRuleSave').addEventListener('click', function () {
    var name = $('rName').value.trim();
    var type = $('rType').value;
    var value = $('rValue').value.trim();
    var send = $('rSend').value.trim();
    var retries = parseInt($('rRetries').value, 10) || 3;

    if (!name) { $('ruleError').textContent = '请填名称'; return; }
    if (!value) { $('ruleError').textContent = '请填匹配内容'; return; }

    var when = { type: type };
    if (type === 'no_response') {
      when.consecutive = parseInt(value, 10) || 3;
    } else if (type === 'regex' || type === 'hex_regex') {
      when.pattern = value;
    } else {
      when.value = value;
    }

    var rule = { name: name, when: when };
    if (send) rule.then = { send: send, max_retries: retries };
    rule.notify = true;

    var next = rulesCache.slice();
    if (editingIndex >= 0) next[editingIndex] = rule; else next.push(rule);
    saveRules(next, allowCache);
  });

  $('allowList').addEventListener('change', function () {
    var list = $('allowList').value.split(',').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    saveRules(rulesCache, list);
  });

  $('sidebarToggle').addEventListener('click', function () {
    $('sidebar').classList.toggle('collapsed');
  });

  /* ── AI 通道 ─────────────────────────────────────────────
   *  和串口收发是两个页签，但逻辑完全独立：它不碰串口，只读写
   *  <log_root>/channel/board.jsonl（两个 AI 共用的留言板）。
   *
   *  为什么每条消息都要显示「证据」和「没验过的部分」：
   *  两个 AI 都会错，所以权威不在任何一方嘴上，而在"别人能不能照着复现"里。
   *  not_verified 是最该看的一栏——它直接告诉你"这条别急着信"。
   */
  var chanSince = 0;
  var chanSeen = [];
  var chanUnread = 0;
  var chanPaneActive = false;

  var TYPE_LABEL = {
    note: '对话', claim: '声明', verify_req: '请复验',
    verify_res: '复验结果', reject: '驳回'
  };

  /** 谁发的 → 徽章样式。A 蓝、B 绿、人 黄、其他 灰（和收发区语义一致）。 */
  function whoClass(name) {
    var n = String(name || '').toLowerCase();
    if (n === 'human' || n === '人') return 'auto';
    if (n.indexOf('b') >= 0) return 'human';
    if (n.indexOf('a') >= 0) return 'ai';
    return 'dev';
  }

  function chanEmpty(text) {
    var box = document.createElement('div');
    box.className = 'chan-empty';
    box.textContent = text;
    return box;
  }

  function renderMessage(m) {
    var row = document.createElement('div');
    row.className = 'msg';

    var who = document.createElement('span');
    who.className = 'tag ' + whoClass(m.from);
    who.textContent = m.from;

    var body = document.createElement('div');
    var head = document.createElement('div');
    head.className = 'head';
    head.textContent = new Date((m.ts || 0) * 1000).toLocaleTimeString() +
      ' · ' + (TYPE_LABEL[m.type] || m.type) + ' · 信任 ' + (m.trust || 'none');
    body.appendChild(head);

    var claim = document.createElement('div');
    claim.className = 'claim';
    claim.textContent = m.claim || '';
    body.appendChild(claim);

    var ev = m.evidence || {};
    if (ev.cmd) {
      var evBox = document.createElement('div');
      evBox.className = 'ev';
      var cmdLine = document.createElement('div');
      cmdLine.appendChild(document.createTextNode('$ '));
      var code = document.createElement('code');
      code.textContent = ev.cmd;
      cmdLine.appendChild(code);
      if (typeof ev.exit === 'number') {
        cmdLine.appendChild(document.createTextNode('   (exit ' + ev.exit + ')'));
      }
      evBox.appendChild(cmdLine);
      if (ev.stdout) {
        var pre = document.createElement('pre');
        pre.textContent = ev.stdout;
        evBox.appendChild(pre);
      }
      body.appendChild(evBox);
    }

    if (m.not_verified && m.not_verified.length) {
      var nv = document.createElement('div');
      nv.className = 'nv';
      var b = document.createElement('b');
      b.textContent = '未验证：';
      nv.appendChild(b);
      nv.appendChild(document.createTextNode(m.not_verified.join('、')));
      body.appendChild(nv);
    }

    if (ev.cmd) {
      var acts = document.createElement('div');
      acts.className = 'acts';
      var btn = document.createElement('button');
      btn.className = 'tiny ghost';
      btn.textContent = '一键复验';
      btn.title = '重跑上面那条命令，把结果作为新消息贴回通道';
      btn.addEventListener('click', function () { verifyMessage(m.seq, btn); });
      acts.appendChild(btn);
      body.appendChild(acts);
    }

    row.appendChild(who);
    row.appendChild(body);
    return row;
  }

  function appendMessages(list) {
    var box = $('chanList');
    var placeholder = box.querySelector('.chan-empty');
    if (placeholder && list.length) box.removeChild(placeholder);
    list.forEach(function (m) {
      chanSeen.push(m);
      box.appendChild(renderMessage(m));
      if (m.seq > chanSince) chanSince = m.seq;
    });
    box.scrollTop = box.scrollHeight;
  }

  function pollChannel() {
    api('/channel?since=' + chanSince).then(function (data) {
      if (!data.ok) return;
      var list = data.messages || [];
      if (list.length) {
        appendMessages(list);
        if (!chanPaneActive) {
          chanUnread += list.length;
          var badge = $('chanBadge');
          badge.hidden = false;
          badge.textContent = String(chanUnread);
        }
      }
    });
  }

  function verifyMessage(seq, btn) {
    btn.disabled = true;
    btn.textContent = '复验中…';
    post('/channel/verify', { seq: seq }).then(function (data) {
      btn.disabled = false;
      btn.textContent = '一键复验';
      if (!data.ok) {
        pushEvent('bad', '复验失败：' + errLine(data.error));
        return;
      }
      var r = data.result || {};
      pushEvent(r.exit === 0 ? '' : 'warn',
                '复验 #' + seq + '：exit=' + r.exit + '，' + r.elapsed_ms + 'ms');
      pollChannel();   // 结论会作为新消息回到通道里
    });
  }

  function sendChannel() {
    var body = {
      // 网页上打字的人只能是「人」。以前这里是个下拉框（AI-A / AI-B / 人），
      // 等于让用户随手冒充 AI，写进去的记录也就不可信了。
      from: 'human',
      type: $('chanType').value,
      claim: $('chanClaim').value.trim(),
      not_verified: $('chanNotVerified').value
    };
    var cmd = $('chanCmd').value.trim();
    if (cmd) {
      body.evidence = {
        cmd: cmd,
        stdout: $('chanStdout').value,
        exit: 0
      };
    }
    if (!body.claim) { pushEvent('bad', '通道消息不能是空的'); return; }
    $('chanError').hidden = true;
    post('/channel', body).then(function (data) {
      if (!data.ok) {
        // 最常见的一种拒绝：声明类消息没带证据。就地显示，别让人去另一个页签找。
        $('chanError').hidden = false;
        $('chanError').textContent = '发送失败：' + errText(data.error);
        return;
      }
      $('chanClaim').value = '';
      $('chanCmd').value = '';
      $('chanStdout').value = '';
      $('chanNotVerified').value = '';
      pollChannel();
    });
  }

  function switchPane(name) {
    chanPaneActive = name === 'channel';
    [].forEach.call(document.querySelectorAll('#tabs .tab'), function (tab) {
      tab.classList.toggle('active', tab.getAttribute('data-pane') === name);
    });
    $('paneSerial').hidden = chanPaneActive;
    $('paneChannel').hidden = !chanPaneActive;
    if (chanPaneActive) {
      chanUnread = 0;
      $('chanBadge').hidden = true;
      var box = $('chanList');
      box.scrollTop = box.scrollHeight;
    }
  }

  [].forEach.call(document.querySelectorAll('#tabs .tab'), function (tab) {
    tab.addEventListener('click', function () {
      switchPane(tab.getAttribute('data-pane'));
    });
  });
  $('chanSend').addEventListener('click', sendChannel);

  /* ── 启动 ─────────────────────────────────────────────── */
  api('/status').then(function (data) {
    if (data.ok) renderStatus(data);
    return api('/ports');
  }).then(function (data) {
    if (data && data.ok) renderPorts({ ports: data.ports, bauds: data.bauds,
                                       port: lastState && lastState.port,
                                       baud: lastState && lastState.baud });
    loadRules();
    tick();
    startPolling();
    pollChannel();
    setInterval(pollChannel, 2000);
  }).catch(function () {
    loadRules();
    tick();
    startPolling();
    pollChannel();
    setInterval(pollChannel, 2000);
  });
})();
