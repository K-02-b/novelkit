/* tips.js — 悬浮提示卡与逐字显现（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { chapterActionState } from './chapter-actions.js';

/* ---------------------------------------------------------------- 悬浮卡片
 *
 * 原生 title 提示是浏览器画的：样式不可控、只支持纯文本、还有延迟。
 * 这里自绘一张卡片，视觉与面板其余部分一致（标题 / 时间 / 状态点 / 正文）。
 * 用事件委托挂载，避免为一章上百个高亮各绑一次监听。
 */

let tipTimer = null;
let tipNode = null;
let tipPinned = false;      // 点击图标后固定，点别处才收起
const revealTimers = new Set();   // 正在逐字显现的计时器

const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/**
 * 把"刚改好的译文"像写上去一样逐字显示出来——**只在任务刚结束时做一次**，
 * 作用是提示用户"这段刚刚被改过"。刷新页面不会重放（没有对比基准，就不会触发）。
 *
 * 逐字期间会临时用纯文本覆盖，结束后再恢复术语高亮等装饰节点。
 */
function revealParagraph(node, text) {
  if (!node || !text) return;
  if (prefersReducedMotion()) return;
  const decorated = [...node.childNodes].map((child) => child.cloneNode(true));
  const locked = Math.ceil(node.getBoundingClientRect().height);
  node.style.minHeight = `${locked}px`;
  node.textContent = '';

  const duration = Math.min(1100, Math.max(240, text.length * 15));
  const step = Math.max(1, Math.ceil(text.length / (duration / 16)));
  let shown = 0;
  const finish = () => {
    node.replaceChildren(...decorated.map((child) => child.cloneNode(true)));
    node.style.minHeight = '';
  };
  const timer = setInterval(() => {
    shown = Math.min(text.length, shown + step);
    node.textContent = text.slice(0, shown);
    if (shown >= text.length) { clearInterval(timer); revealTimers.delete(timer); finish(); }
  }, 16);
  revealTimers.add(timer);
}

function stopReveals() {
  revealTimers.forEach((timer) => clearInterval(timer));
  revealTimers.clear();
}

function tipHost() {
  let host = document.querySelector('#tip-host');
  if (!host) {
    host = el('div', { id: 'tip-host' });
    document.body.append(host);
  }
  return host;
}

function hideTip() {
  if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
  if (tipNode) { tipNode.remove(); tipNode = null; }
  tipPinned = false;
  document.querySelectorAll('.tip-active').forEach((n) => n.classList.remove('tip-active'));
}

function showTip(anchor, content, { pinned = false } = {}) {
  hideTip();
  tipPinned = pinned;
  if (pinned) anchor.classList.add('tip-active');
  const card = el('div', { class: `tip-card${pinned ? ' pinned' : ''}` }, content);
  card.dataset.anchor = anchor.dataset.tipKey || '';
  tipNode = card;
  tipHost().append(card);

  const rect = anchor.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  const margin = 8;
  let left = rect.left;
  if (left + cardRect.width > window.innerWidth - margin) {
    left = Math.max(margin, window.innerWidth - cardRect.width - margin);
  }
  let top = rect.bottom + 6;
  if (top + cardRect.height > window.innerHeight - margin) {
    top = Math.max(margin, rect.top - cardRect.height - 6);   // 放不下就翻到上方
  }
  card.style.left = `${Math.round(left)}px`;
  card.style.top = `${Math.round(top)}px`;
}

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / 绝对时间。 */
function timeAgo(stamp) {
  if (!stamp) return '';
  const parsed = new Date(String(stamp).replace(' ', 'T'));
  if (Number.isNaN(parsed.getTime())) return String(stamp);
  const seconds = Math.max(0, (Date.now() - parsed.getTime()) / 1000);
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
  return String(stamp);
}

function tipLine(text, cls) {
  return text ? el('div', { class: cls, text }) : null;
}

/** Refine 批注卡片：标题 / 时间 / 状态点 / 理由，样式对齐 dsh 的悬浮卡。 */
function noteTipCard(payload) {
  const changed = !!payload.changed;
  return el('div', {},
    el('div', { class: 'tip-title', text: `第 ${payload.row} 段 · ${changed ? 'Refine 修改理由' : 'Refine 评审意见'}` }),
    tipLine(payload.updated ? timeAgo(payload.updated) : '', 'tip-meta'),
    el('div', { class: 'tip-status' },
      el('span', { class: `tip-dot ${changed ? 'changed' : 'unchanged'}` }),
      el('span', { text: changed ? '已修改' : '判定无需修改' })),
    el('div', { class: 'tip-sep' }),
    el('div', { class: 'tip-body', text: payload.reason || '（模型未给出理由）' }));
}

/** 术语卡片：中文词条 / 颜色点 / 译法 / 是否落地。 */
function termTipCard(anchor) {
  const term = anchor.dataset.term || anchor.textContent;
  const translation = anchor.dataset.translation || '（无记录译法）';
  const missing = anchor.classList.contains('no-en');
  const side = anchor.classList.contains('en-side') ? '英文译法' : '中文词条';
  return el('div', {},
    el('div', { class: 'tip-title' }, el('span', {
      class: 'tip-swatch', style: { background: anchor.style.getPropertyValue('--tc') },
    }), term),
    tipLine(side, 'tip-meta'),
    el('div', { class: 'tip-status' },
      el('span', { class: `tip-dot ${missing ? 'warn' : 'ok'}` }),
      el('span', { text: missing ? '本章英文未按此译法书写' : '已在英文中出现' })),
    el('div', { class: 'tip-sep' }),
    el('div', { class: 'tip-body', text: translation }));
}

/** 按元素类型构造卡片内容；不是可提示元素则返回 null。 */
function tipContentFor(anchor) {
  if (!anchor) return null;
  if (anchor.classList.contains('note-icon') || anchor.classList.contains('merge-badge')) {
    let payload = {};
    try { payload = JSON.parse(anchor.dataset.note || '{}'); } catch { payload = {}; }
    return noteTipCard(payload);
  }
  if (anchor.classList.contains('term-hl')) return termTipCard(anchor);
  return null;
}

const TIP_SELECTOR = '.note-icon, .term-hl, .merge-badge';

/** 在阅读区启用事件委托的悬浮卡片：悬停预览，点击固定。 */
function enableReaderTips(body) {
  if (!body || body.dataset.tipsReady === '1') return;
  body.dataset.tipsReady = '1';

  body.addEventListener('mouseover', (event) => {
    if (tipPinned) return;                       // 已固定时不被悬停打扰
    const anchor = event.target.closest(TIP_SELECTOR);
    if (!anchor) return;
    if (tipNode && tipNode.dataset.anchor === anchor.dataset.tipKey) return;
    if (tipTimer) clearTimeout(tipTimer);
    tipTimer = setTimeout(() => {
      const content = tipContentFor(anchor);
      if (content) showTip(anchor, content);
    }, 110);
  });

  body.addEventListener('mouseout', (event) => {
    if (tipPinned) return;                       // 固定后移到别处也不收
    if (event.target.closest(TIP_SELECTOR)) hideTip();
  });

  // 点击注释图标：固定住卡片，直到点击其它地方
  body.addEventListener('click', (event) => {
    const anchor = event.target.closest(TIP_SELECTOR);
    if (!anchor) return;
    event.stopPropagation();
    const same = tipPinned && tipNode && tipNode.dataset.anchor === anchor.dataset.tipKey;
    if (same) { hideTip(); return; }
    const content = tipContentFor(anchor);
    if (content) showTip(anchor, content, { pinned: true });
  });

  // 卡片按视口坐标 fixed 定位，锚点一滚就对不上了，所以任何滚动都收起（包括固定住的）
  body.addEventListener('scroll', hideTip, { passive: true });

  // 分段选择：挑选模式下，点段落任意一侧（中文或英文）都能勾选/取消。
  // 注意三件事：勾选框本身交给原生行为；注释图标/术语高亮只弹卡片不切换选择；
  // 正在拖选文字时不切换，免得复制正文时误勾。
  body.addEventListener('click', (event) => {
    if (!chapterActionState.picking) return;
    if (event.target.closest('.rowpick-wrap')) return;      // 原生 checkbox 自己处理
    if (event.target.closest(TIP_SELECTOR)) return;         // 悬浮卡片元素不参与选择
    const row = event.target.closest('.rowpair');
    if (!row) return;
    if (String(window.getSelection() || '').trim()) return; // 正在选文字
    const box = row.querySelector('.rowpick');
    if (!box) return;
    box.checked = !box.checked;
    box.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

// 点击别处 / 按 Esc 收起固定的卡片
document.addEventListener('click', (event) => {
  if (!tipPinned) return;
  if (event.target.closest('.tip-card') || event.target.closest(TIP_SELECTOR)) return;
  hideTip();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') hideTip();
});

/**
 * 框选复制时只输出干净的正文。
 *
 * 光靠 user-select:none 把装饰元素排除掉还不够——两栏栅格是块级元素，
 * 选区里会夹杂空行，粘到富文本编辑器里就变成一个个空段落。
 * 这里统一规整：逐行去空白、丢掉空行，再写进剪贴板。
 * 只有当选区确实落在正文里时才接管，别处的复制不受影响。
 */
document.addEventListener('copy', (event) => {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return;
  const body = document.querySelector('#reader-body');
  if (!body) return;
  const range = selection.getRangeAt(0);
  if (!body.contains(range.commonAncestorContainer)) return;

  const cleaned = selection.toString()
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .join('\n');
  if (!cleaned) return;
  event.clipboardData.setData('text/plain', cleaned);
  event.preventDefault();
});

// 捕获阶段监听：页面上任何容器滚动（阅读区、日志、下拉…）都要把提示卡收起来，
// 否则 fixed 定位的卡片会停在原处、盖住正文。
document.addEventListener('scroll', hideTip, true);
window.addEventListener('resize', hideTip);


/**
 * 复制本章英文全文。
 *
 * 直接按对齐行的顺序拼接 en_parts，得到的**就是文件本身的段落顺序**——
 * 对齐只决定中英怎么配对，从不重排或丢弃段落（有"不丢不重"的不变量测试守着）。
 * 用纯文本写入剪贴板，方便直接粘到别的平台；顺带报一下段数与词数。
 */

export { TIP_SELECTOR, enableReaderTips, hideTip, noteTipCard, prefersReducedMotion, revealParagraph, revealTimers, showTip, stopReveals, termTipCard, timeAgo, tipContentFor, tipHost, tipLine, tipNode, tipPinned, tipTimer };
