/* 鲸歌前端 · 基础设施（R2b 从 app.js 搬出来，纯搬移）。
 * 这一层不认识业务：不知道串口、不知道规则，只提供取元素、发请求、报错、写轨道。
 */

export var $ = function (id) { return document.getElementById(id); };

/* ── 状态轨道：系统事件与串口数据分开 ─────────────────── */
export function nowText() {
  var d = new Date();
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

export function pushEvent(kind, text) {
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


export function api(path, options) {
  return fetch(path, options).then(function (r) {
    return r.json().catch(function () { return { ok: false, error: { message: 'HTTP ' + r.status } }; });
  });
}

export function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
}

export function errText(error) {
  if (!error) return '未知错误';
  var lines = [error.message || String(error)];
  if (error.hint && error.hint.length) {
    error.hint.forEach(function (h, i) { lines.push((i + 1) + '. ' + h); });
  }
  return lines.join('\n');
}

/** 压成一行给状态轨道用：保留 hint，不要只留第一句。
 *  只显示第一句会丢掉"该怎么解决"（比如"点[交给AI]"），等于报了错但没给路。 */
export function errLine(error) {
  if (!error) return '未知错误';
  var parts = [String(error.message || error)];
  (error.hint || []).forEach(function (h) {
    var text = String(h || '').replace(/^\s*\d+\.\s*/, '').trim();
    if (text) parts.push(text);
  });
  return parts.join(' · ');
}
