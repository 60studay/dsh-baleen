/* 鲸歌前端 · AI 通道（R2b 从 app.js 搬出来，纯搬移）。
 * 通道功能本身已冻结（见通讯板 13:40），这里只是把它从大文件里挪出来。
 */

import { $, api, post, errText, errLine, pushEvent } from './core.js';

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

export function pollChannel() {
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
