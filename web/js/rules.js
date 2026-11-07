/* 鲸歌前端 · 规则编辑（R2b 从 app.js 搬出来，纯搬移）。
 * 规则的读写走 /rules 接口，写回的是场景 config.json。
 *
 * 和后端的约定：保存成功后调 onSaved()，让外层刷新一次状态
 * ——不直接 import app.js 的 tick()，否则两个模块互相依赖。
 */

import { $, api, post, errText, errLine, pushEvent } from './core.js';

var onSaved = null;

/** 注册"保存成功后要做什么"（app.js 启动时把自己的 tick 传进来）。 */
export function onRulesSaved(fn) {
  onSaved = fn;
}

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

export function loadRules() {
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
    if (onSaved) onSaved();
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
