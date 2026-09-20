/* reader.js — 中英对照与阅读区刷新（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { api, nf, toast } from './api.js';
import { buildTermMatcher, computeEnglishPresence, decorate } from './terms.js';
import { state, workByName } from './state.js';
import { enableReaderTips, revealParagraph, stopReveals } from './tips.js';
import { applyPicking, applyReaderHighlight, applyReaderMode, applyReaderSize, buildChapterActions, chapterActionState, deleteCurrentChapter, gotoMatch, markMode, setReaderMode, syncPickCount } from './chapter-actions.js';
import { route } from './router.js';

/* ------------------------------------------------------------ 中英对照 */

function buildReaderRows(work, data) {
  // 术语高亮的两侧匹配器在这里初始化，初次渲染与原地刷新都走同一条路径
  state.reader.terms = data.terms || [];
  state.reader.matcher = buildTermMatcher(state.reader.terms);
  const englishText = (data.rows || []).flatMap((r) => r.en_parts || []).join('\n');
  state.reader.matcher.englishPresent = computeEnglishPresence(state.reader.matcher, englishText);
  const termMatcher = state.reader.termsOn ? state.reader.matcher : null;

  const rows = data.rows || [];
const header = buildReaderHeader(data);

const paraCell = (parts, cls, emptyText) => el('div', { class: cls },
  (parts && parts.length)
    ? parts.map((text) => el('div', { class: 'para' },
        decorate(text, state.reader.query, termMatcher, cls === 'en' ? 'en' : 'zh')))
    : el('span', { class: 'gapmark', text: emptyText }));

const rowNodes = rows.map((row, rowIndex) =>
  buildRowNode(work, row, rowIndex + 1, data, { paraCell, termMatcher }));

  rememberParagraphs(rows);
  return el('div', { class: 'reader', id: 'reader-body' }, header, ...rowNodes);
}


/** 一行的"内容指纹"：只有指纹变了才需要重建这个 DOM 节点。 */
function rowFingerprint(row) {
  return JSON.stringify([
    row.zh_parts || [], row.en_parts || [], row.gap || null,
    row.note ? [row.note.reason || '', !!row.note.changed] : null,
  ]);
}

function buildRowNode(work, row, rowNumber, data, ctx) {
  const termMatcher = ctx.termMatcher;
  const paraCell = ctx.paraCell || ((parts, cls, emptyText) => el('div', { class: cls },
    (parts && parts.length)
      ? parts.map((text) => el('div', { class: 'para' },
          decorate(text, state.reader.query, termMatcher, cls === 'en' ? 'en' : 'zh')))
      : el('span', { class: 'gapmark', text: emptyText })));

  const zhParts = row.zh_parts || (row.zh ? [row.zh] : []);
  const enParts = row.en_parts || (row.en ? [row.en] : []);
  const merged = zhParts.length !== enParts.length && zhParts.length && enParts.length;
  const classes = ['rowpair'];
  if (row.gap === 'en') classes.push('only-zh');
  if (row.gap === 'zh') classes.push('only-en');
  if (merged) classes.push('merged');

  const badge = merged
    ? el('span', {
        class: 'merge-badge',
        'data-tip-key': `merge-${rowNumber}`,
        'data-note': JSON.stringify({ row: rowNumber, changed: false,
          reason: `原文 ${zhParts.length} 段合并对应译文 ${enParts.length} 段；系统按长度比例做了对齐，这一行不是 1:1。` }),
        text: `${zhParts.length}:${enParts.length}`,
      })
    : null;

  const checkbox = el('input', {
    type: 'checkbox', class: 'rowpick', 'data-row': rowNumber,
    checked: chapterActionState.selectedRows.has(rowNumber),
    onchange: (event) => {
      if (event.target.checked) chapterActionState.selectedRows.add(rowNumber);
      else chapterActionState.selectedRows.delete(rowNumber);
      const host = event.target.closest('.rowpair');
      if (host) host.classList.toggle('picked', event.target.checked);
      syncPickCount();
    },
  });

  const note = row.note;
  const noteIcon = note
    ? el('span', {
        class: `note-icon${note.changed ? '' : ' unchanged'}`,
        'data-tip-key': `note-${rowNumber}`,
        'data-note': JSON.stringify({ row: rowNumber, reason: note.reason || '',
                                      changed: !!note.changed, updated: data.refine_updated || '' }),
        'aria-label': `第 ${rowNumber} 段 Refine 意见`,
        text: '✎',
      })
    : null;

  const node = el('div', { class: classes.join(' ') },
    el('label', { class: 'rowpick-wrap', title: '勾选后可重译这一段' }, checkbox),
    paraCell(zhParts, 'zh', '（译文此段无对应原文）'),
    paraCell(enParts, 'en', '（此段尚未翻译）'),
    badge, noteIcon);
  node.dataset.rowKey = rowFingerprint(row);
  if (chapterActionState.selectedRows.has(rowNumber)) node.classList.add('picked');
  return node;
}

/** 记住本次渲染的英文段落（归一化），下次刷新时用来判断"哪些段落刚被改动"。 */
function rememberParagraphs(rows) {
  state.reader.paragraphs = new Set(
    (rows || []).flatMap((row) => (row.en_parts || []).map(normalizeForDiff)));
}

function normalizeForDiff(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

/* ------------------------------------------------- 阅读位置记忆 */

/** 每部作品记住上次读到哪一章（按作品名分开存，互不影响）。 */
function lastChapterKey(workName) {
  return `novelkit:last-chapter:${workName}`;
}

function rememberChapter(workName, num) {
  try { localStorage.setItem(lastChapterKey(workName), String(num)); } catch { /* 忽略 */ }
}

function lastChapterOf(workName) {
  try {
    const raw = localStorage.getItem(lastChapterKey(workName));
    const num = Number(raw);
    return Number.isInteger(num) && num >= 0 ? num : null;
  } catch {
    return null;                 // 存储不可用（隐私模式等）时退回默认
  }
}

/** 没指定章节时的落点：上次读到的章（还在就用它），否则有简介就进简介。 */
function defaultChapterNum(chapters) {
  const remembered = lastChapterOf(state.work || '');
  if (remembered !== null && chapters.some((c) => c.num === remembered)) return remembered;
  const intro = chapters.find((c) => c.is_intro);
  return (intro || chapters[0]).num;
}

async function renderReader(work) {
  stopReveals();          // 切章时清掉上一章可能还在跑的逐字计时器
  const content = $('#content');
  const inv = await api(`/api/works/${encodeURIComponent(work.name)}/chapters`);
  const chapters = inv.chapters || [];
  if (!chapters.length) {
    content.replaceChildren(el('div', { class: 'empty', text: '该作品没有章节文件' }));
    return;
  }
  if (!chapters.some((c) => c.num === state.reader.num)) {
    state.reader.num = defaultChapterNum(chapters);
  }

  const options = chapters.map((c) => chapterOption(c));
  const index = chapters.findIndex((c) => c.num === state.reader.num);
  const pick = (num) => {
    state.reader.num = num;
    rememberChapter(work.name, num);
    location.hash = `#/work/${work.name}/reader/${num}`;
  };

  const toolbar = el('div', { class: 'reader-toolbar' },
    el('div', { class: 'chapter-nav' },
      el('button', { class: 'sm', disabled: index <= 0, onclick: () => pick(chapters[index - 1].num) }, '‹ 上一章'),
      el('select', { onchange: (e) => pick(Number(e.target.value)) }, options),
      el('button', { class: 'sm', disabled: index >= chapters.length - 1, onclick: () => pick(chapters[index + 1].num) }, '下一章 ›')),
    el('span', { class: 'spacer' }),
    el('div', { class: 'search-box' },
      el('input', {
        type: 'text', placeholder: '章内搜索…', value: state.reader.query, style: { width: '150px' },
        oninput: (e) => { state.reader.query = e.target.value; state.reader.matchIndex = -1; applyReaderHighlight(); },
        onkeydown: (e) => {
          // 回车（或 ↓）= 下一个，Shift+回车（或 ↑）= 上一个
          if (e.key === 'Enter') { e.preventDefault(); gotoMatch(e.shiftKey ? -1 : 1); }
          if (e.key === 'ArrowDown') { e.preventDefault(); gotoMatch(1); }
          if (e.key === 'ArrowUp') { e.preventDefault(); gotoMatch(-1); }
          if (e.key === 'Escape') { e.target.value = ''; state.reader.query = ''; applyReaderHighlight(); }
        },
      }),
      el('button', { class: 'sm', title: '上一个匹配（Shift+Enter）',
        onclick: () => gotoMatch(-1) }, '↑'),
      el('button', { class: 'sm', title: '下一个匹配（回车）',
        onclick: () => gotoMatch(1) }, '↓'),
      el('span', { class: 'small muted', id: 'match-count' })),
    el('button', { class: 'sm', text: '并排', onclick: (e) => { setReaderMode('both'); markMode(e.target); } }),
    el('button', { class: 'sm', text: '仅中文', onclick: (e) => { setReaderMode('zh'); markMode(e.target); } }),
    el('button', { class: 'sm', text: '仅英文', onclick: (e) => { setReaderMode('en'); markMode(e.target); } }),
    el('button', { class: 'sm', text: '复制英文', title: '把本章英文全文复制到剪贴板（适合投稿到别的平台）',
      onclick: copyChapterEnglish }),
    el('button', { class: 'sm', onclick: () => { state.reader.size = Math.max(11, state.reader.size - 1); applyReaderSize(); } }, 'A−'),
    el('button', { class: 'sm', onclick: () => { state.reader.size = Math.min(26, state.reader.size + 1); applyReaderSize(); } }, 'A+'),
    el('button', { class: 'sm danger', text: '删除本章', title: '删除这一章（先备份到 .backups/）',
      onclick: () => deleteCurrentChapter(work, chapters, index) }));

  if (!state.snippetsLoadedFor || state.snippetsLoadedFor !== work.name) {
    const sn = await api(`/api/works/${encodeURIComponent(work.name)}/snippets`);
    state.snippets = sn.snippets || { translate: '', retranslate: '', refine: '' };
    state.snippetMeta = Object.fromEntries((sn.kinds || []).map((k) => [k.id, k]));
    state.snippetsLoadedFor = work.name;
  }

  state.reader.chapters = chapters;
  const data = await fetchChapter(work.name, state.reader.num);
  const rows = data.rows || [];
  const gapsZh = rows.filter((r) => r.gap === 'zh').length;
  const gapsEn = rows.filter((r) => r.gap === 'en').length;

  const body = buildReaderRows(work, data);

  const glossaryCard = buildGlossaryCard(data);

  const actionsCard = buildChapterActions(work, data, chapters);

  content.className = 'content flush';
  content.replaceChildren(toolbar, body, el('div', { style: { padding: '12px 14px' } },
    el('div', { id: 'job-slot' }),
    el('div', { id: 'reader-meta' }, buildReaderMeta(work, data, rows, gapsZh, gapsEn)),
    el('div', { id: 'actions-slot' }, actionsCard),
    el('div', { id: 'glossary-slot' }, glossaryCard || el('span'))));   // 空卡片用空节点占位

  applyReaderMode();
  applyReaderSize();
  enableReaderTips(body);
  applyPicking();
  syncPickCount();
  const wanted = state.reader.mode === 'zh' ? '仅中文' : state.reader.mode === 'en' ? '仅英文' : '并排';
  toolbar.querySelectorAll('button').forEach((b) => { if (b.textContent === wanted) b.classList.add('primary'); });

  $('#view-title').textContent = work.name;
  $('#view-meta').textContent = `中英对照 · 第 ${state.reader.num} 章 / 共 ${chapters.length} 章`;
}



async function copyChapterEnglish() {
  const workName = state.work;
  if (!workName) return;
  let data;
  try {
    data = await fetchChapter(workName, state.reader.num);
  } catch {
    toast('读取本章失败', 'bad');
    return;
  }
  const paragraphs = (data.rows || []).flatMap((row) => row.en_parts || [])
    .map((text) => text.trim())
    .filter(Boolean);
  if (!paragraphs.length) {
    toast('本章还没有译文', 'bad');
    return;
  }
  const text = paragraphs.join('\n\n') + '\n';
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 剪贴板 API 在非安全上下文/无权限时会失败，退回 textarea + execCommand
    const holder = el('textarea', { style: { position: 'fixed', left: '-9999px' } });
    holder.value = text;
    document.body.append(holder);
    holder.select();
    const ok = document.execCommand('copy');
    holder.remove();
    if (!ok) { toast('复制失败，请手动选中正文复制', 'bad'); return; }
  }
  const words = paragraphs.join(' ').split(/\s+/).filter(Boolean).length;
  toast(`已复制 ${paragraphs.length} 段 · ${nf(words)} 词`, 'ok');
}

/** 阅读区顶部那两行统计；译完后段数/词数会变，刷新时要重建。 */
function buildReaderHeader(data) {
  return el('div', { class: 'reader-head' },
    el('div', { text: `中文原文 · ${data.zh_paragraphs} 段 · ${nf(data.zh_chars)} 字` }),
    el('div', { text: `英文译文 · ${data.en_paragraphs} 段 · ${nf(data.en_words)} 词` }));
}

/** 术语来源列的文案：-2 = 作品级（整本书最高优先），-1 = 全局（跨作品）。 */
function chapterScopeLabel(chapter) {
  if (chapter === -2) return '作品';
  if (chapter === -1) return '全局';
  return chapter;
}

/** 章节下拉里的一项。翻译完成后要重建它，否则会一直显示"未译"。 */
function chapterOption(c) {
  return el('option', {
    value: c.num, selected: c.num === state.reader.num,
    text: `#${c.num}${c.is_intro ? ' 简介' : ''} · ${c.has_translated ? `${nf(c.en_words)}w` : '未译'}`,
  });
}

/** 重新拉章节清单并就地更新下拉：译完一章后"未译"要变成词数。 */
async function refreshChapterList(workName) {
  let chapters = [];
  try {
    chapters = (await api(`/api/works/${encodeURIComponent(workName)}/chapters`)).chapters || [];
  } catch {
    return;
  }
  if (!chapters.length) return;
  state.reader.chapters = chapters;
  const select = document.querySelector('.chapter-nav select');
  if (!select) return;
  const index = chapters.findIndex((c) => c.num === state.reader.num);
  select.replaceChildren(...chapters.map((c) => chapterOption(c)));
  const buttons = document.querySelectorAll('.chapter-nav button');
  if (buttons.length >= 2) {
    buttons[0].disabled = index <= 0;
    buttons[1].disabled = index >= chapters.length - 1 || index < 0;
  }
}

async function fetchChapter(workName, num) {
  return api(`/api/works/${encodeURIComponent(workName)}/chapter/${num}`);
}

/** 底部那排统计小标签。 */
function buildReaderMeta(work, data, rows, gapsZh, gapsEn) {
  return el('div', { class: 'row small dim' },
    el('span', { class: 'pill', text: `${work.name} 第 ${state.reader.num} 章` }),
    el('span', { class: 'pill', text: `对齐 ${rows.length} 行` }),
    gapsEn ? el('span', { class: 'pill warn', text: `仅中文 ${gapsEn} 行` }) : null,
    gapsZh ? el('span', { class: 'pill info', text: `仅英文 ${gapsZh} 行` }) : null,
    (data.conflicts && data.conflicts.length)
      ? el('span', { class: 'pill bad', text: `本章冲突 ${data.conflicts.length}` }) : null);
}

/** "重建本章术语" 勾选框的文案；勾上时带字数提示，让人知道会丢东西。 */
function rebuildText() {
  if (!chapterActionState.rebuildGlossary) return '重建本章术语';
  const count = (state.reader.glossaryCount || 0);
  return count ? `重建本章术语（将丢弃现有 ${count} 条）` : '重建本章术语';
}

/** "术语高亮 · N 词（M 词译法未在正文出现）" 这句的文案。 */
function highlightLabelText() {
  const terms = state.reader.terms || [];
  const m = state.reader.matcher;
  let miss = 0;
  if (m) {
    miss = (m.terms || []).filter((t, i) => t.translations && t.translations.length
      && !m.englishPresent.has(i)).length;
  }
  return `术语高亮 · ${terms.length} 词` + (miss ? `（${miss} 词译法未在正文出现）` : '');
}

function buildGlossaryCard(data) {
  const entries = Object.entries(data.glossary || {});
  const count = entries.reduce((sum, [, items]) => sum + Object.keys(items || {}).length, 0);
  state.reader.glossaryCount = count;
  if (!count) return null;
  return el('details', { class: 'card collapsible' },
    el('summary', {}, '本章新术语 ', el('span', { class: 'sub', text: `${count} 条（点击展开）` })),
    el('div', { class: 'row small mt' }, ...entries.flatMap(([category, items]) =>
      Object.entries(items || {}).slice(0, 60).map(([term, value]) => el('span', { class: 'pill info' },
        `${term} → ${typeof value === 'string' ? value : Object.values(value || {}).join(' / ')}`)))));
}

/** 只刷新"译出来的东西"，不整页重绘：滚动位置、筛选状态、任务日志都保留。 */

/**
 * 最小化更新正文：**只重建内容真的变了的行**，其余 DOM 原样保留。
 *
 * 整块 replaceChildren 虽然简单，但会丢掉框选中的文字、让每个批注图标重播渐显动画、
 * 也会把滚动位置和悬停状态一起清掉。所以这里按"行内容指纹"逐行比对：
 *   * 指纹相同 → 一个字都不动
 *   * 指纹不同 → 只替换这一行
 *   * 行数变了（段落结构变化，例如一次修订跨段）→ 才整块重建
 */
function patchReaderBody(body, work, data, freshTexts) {
  const rows = data.rows || [];
  const existing = [...body.querySelectorAll('.rowpair')];
  const ctx = { paraCell: null, termMatcher: state.reader.termsOn ? state.reader.matcher : null };

  if (existing.length !== rows.length) {
    const scroll = body.scrollTop;
    body.replaceChildren(...buildReaderRows(work, data).childNodes);
    body.scrollTop = scroll;
    return;
  }

  const fresh = [];
  rows.forEach((row, index) => {
    const node = existing[index];
    const key = rowFingerprint(row);
    if (node.dataset.rowKey === key) return;          // 没变，跳过
    const replacement = buildRowNode(work, row, index + 1, data, ctx);
    node.replaceWith(replacement);
    fresh.push(replacement);
  });

  if (!freshTexts || !freshTexts.size) return;
  // "段落不多"才逐字写出；整章翻译这类大面积改动只闪一下，避免长时间爬字。
  // 注意：逐字会把段落文本临时清空，所以必须先判断、再开始写。
  const typeOut = freshTexts.size <= 24;
  for (const node of fresh) {
    const hit = [...node.querySelectorAll('.en .para')]
      .some((para) => freshTexts.has(normalizeForDiff(para.textContent)));
    if (!hit) continue;
    node.classList.add('just-updated');
    setTimeout(() => node.classList.remove('just-updated'), 2400);
    if (!typeOut) continue;
    for (const para of node.querySelectorAll('.en .para')) {
      if (freshTexts.has(normalizeForDiff(para.textContent))) {
        revealParagraph(para, para.textContent);
      }
    }
  }
}

async function refreshChapterInPlace({ animateChanges = false } = {}) {
  const workName = state.work;
  if (!workName) { route(); return; }
  let data;
  try {
    data = await fetchChapter(workName, state.reader.num);
  } catch {
    route();
    return;
  }
  const body = $('#reader-body');
  const work = workByName(workName) || { name: workName };
  const rows = data.rows || [];

  // 与上次渲染对比，找出"刚刚才出现的译文段落"。
  // 用段落级集合做 diff（而不是按行号）——一次修订可能跨越对齐行，
  // 按行号比对会把变化记到错误的行上。
  const before = state.reader.paragraphs;
  const freshTexts = new Set();
  if (animateChanges && before) {
    for (const row of rows) {
      for (const part of row.en_parts || []) {
        const key = normalizeForDiff(part);
        if (key && !before.has(key)) freshTexts.add(key);
      }
    }
  }
  const gapsZh = rows.filter((r) => r.gap === 'zh').length;
  const gapsEn = rows.filter((r) => r.gap === 'en').length;

  if (body) {
    patchReaderBody(body, work, data, freshTexts);
  }
  rememberParagraphs(rows);   // 更新对比基准（patch 路径不会走 buildReaderRows）
  refreshChapterList(workName);   // 下拉里的"未译 / Nw"也要跟着变

  const head = body && body.querySelector('.reader-head');
  if (head) head.replaceWith(buildReaderHeader(data));

  const meta = $('#reader-meta');
  if (meta) meta.replaceChildren(...buildReaderMeta(work, data, rows, gapsZh, gapsEn).childNodes);
  const slot = $('#glossary-slot');
  if (slot) slot.replaceChildren(buildGlossaryCard(data) || el('span'));   // null 会被渲染成文本 "null"

  const summary = document.querySelector('#actions-summary');
  if (summary) {
    summary.textContent = data.has_translated ? `已翻译 · ${nf(data.en_words)} 词` : '未翻译';
    summary.className = `sub${data.has_translated ? '' : ' warn-text'}`;
  }
  // 本章操作整块重建：首次翻译完成后「翻译本章」要变成「重新翻译本章」，
  // 并出现分段选择等控件。任务日志在 #job-slot（同级），不会被这次重建清掉。
  const actionsSlot = document.querySelector('#actions-slot');
  if (actionsSlot) {
    actionsSlot.replaceChildren(buildChapterActions(work, data, state.reader.chapters || []));
  }

  chapterActionState.selectedRows.clear();
  syncPickCount();
}


export { buildGlossaryCard, buildReaderHeader, buildReaderMeta, buildReaderRows, buildRowNode, chapterOption, chapterScopeLabel, copyChapterEnglish, defaultChapterNum, fetchChapter, highlightLabelText, lastChapterKey, lastChapterOf, normalizeForDiff, patchReaderBody, rebuildText, refreshChapterInPlace, refreshChapterList, rememberChapter, rememberParagraphs, renderReader, rowFingerprint };
