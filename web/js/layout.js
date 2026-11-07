/* 鲸歌前端 · 布局与主题（R2b 从 app.js 搬出来，纯搬移）。
 * 配色（跟随/深/亮）、两条拖拽条、输入框自动增高 —— 都只跟"怎么看"有关，
 * 不碰串口也不碰规则，所以能第一个拆出来。
 */

import { $ } from './core.js';

/** 输入框随内容长高，最多 40vh（CSS 里也有 max-height 兜底）。 */
export function autoGrow() {
  var box = $('cmd');
  box.style.height = 'auto';
  var limit = Math.round(window.innerHeight * 0.4);
  box.style.height = Math.min(box.scrollHeight, limit) + 'px';
}


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

// 侧边栏收起/展开：属于布局，跟规则无关，所以放在这一层。
sidebarToggle.addEventListener('click', function () {
  sidebar.classList.toggle('collapsed');
});
