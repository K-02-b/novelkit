/* glossary-view.js — 术语库视图（由 static/app.js 拆分而来） */

import { $, el, labeled } from './dom.js';
import { api } from './api.js';
import { state } from './state.js';
import { chapterScopeLabel } from './reader.js';
import { openGlossaryModal } from './glossary-modal.js';
import { route } from './router.js';

/* ------------------------------------------------------------ 术语库 */

async function renderGlossaryView(work) {
  const data = await api(`/api/works/${encodeURIComponent(work.name)}/glossary`);
  const tracker = data.tracker || {};
  const conflicts = data.conflicts || [];
  const perChapter = data.chapters || {};

  const flat = [];
  for (const [category, items] of Object.entries(tracker)) {
    for (const [term, info] of Object.entries(items || {})) {
      flat.push({ category, term, chapter: info.chapter, value: info.value });
    }
  }
  flat.sort((a, b) => (a.chapter - b.chapter) || a.term.localeCompare(b.term));

  const tabs = el('div', { class: 'tabs' },
    ...[['tracker', `术语总表 (${flat.length})`],
        ['chapters', `按章 (${Object.keys(perChapter).length})`],
        ['conflicts', `冲突 (${conflicts.length})`]]
      .map(([id, label]) => el('div', {
        class: `tab${state.glossary.tab === id ? ' active' : ''}`, text: label,
        onclick: () => { state.glossary.tab = id; route(); },
      })));

  const search = el('input', {
    type: 'text', placeholder: '词条 / 译法 / 语境…', value: state.glossary.query, style: { width: '240px' },
    oninput: (e) => { state.glossary.query = e.target.value.trim(); route(); },
  });
  const addBtn = el('button', {
    class: 'sm primary', text: '＋ 新增术语',
    onclick: () => openGlossaryModal(work.name),
  });
  const controls = el('div', { class: 'row', style: { marginBottom: '12px' } },
    labeled('搜索', search), el('span', { class: 'spacer' }), addBtn);

  const query = state.glossary.query.toLowerCase();
  // tracker 里 chapter = -1 的条目来自全局术语库：它有自己的独立视图，
  // 这里不混进来，只用一行提示说明"另有 N 条全局基准在这个作品里生效"。
  const globalCount = flat.filter((r) => r.chapter === -1).length;
  const own = flat.filter((r) => r.chapter !== -1);
  const body = state.glossary.tab === 'conflicts'
    ? renderConflicts(conflicts, query)
    : state.glossary.tab === 'chapters'
      ? renderGlossaryByChapter(perChapter, query)
      : renderGlossaryTracker(own, query);

  const globalNote = globalCount
    ? el('div', { class: 'row small muted', style: { marginTop: '10px' } },
        el('span', { text: `另有 ${globalCount} 条全局术语库的词条对所有作品生效：` }),
        el('button', { class: 'sm', text: '打开全局术语库',
          onclick: () => { location.hash = '#/global-glossary'; } }))
    : null;

  const content = $('#content');
  content.className = 'content';
  // 注意过滤掉 null（globalNote 在没有全局条目时是 null，直接传进去会被渲染成文本 "null"）
  content.replaceChildren(...[controls, tabs, body, globalNote].filter(Boolean));
  $('#view-title').textContent = work.name;
  $('#view-meta').textContent = `术语库 · ${own.length} 条术语 · ${conflicts.length} 条冲突`;
}

function renderGlossaryTracker(flat, query) {
  const shown = flat.filter((r) => !query
    || r.term.toLowerCase().includes(query)
    || JSON.stringify(r.value).toLowerCase().includes(query));
  return glossaryTable(shown, { scopeHeading: '章', onRow: (r) =>
    openGlossaryModal(state.glossary.work, { term: r.term, chapter: r.chapter }) });
}

/** 术语表格：作品视图与全局视图共用（列名略有不同）。 */
function glossaryTable(shown, { scopeHeading = '范围', onRow = null, rowMode = 'work' } = {}) {
  if (!shown.length) return el('div', { class: 'empty', text: '没有匹配的术语' });

  const rows = shown.slice(0, 3000).map((r) => el('tr', {
    class: onRow ? 'clickable-row' : '',
    title: onRow ? '点击可修改这条术语' : '',
    onclick: onRow ? () => onRow(r) : null,
  },
  el('td', { class: 'num', text: rowMode === 'global' ? '全局' : chapterScopeLabel(r.chapter) }),
  el('td', { class: 'small muted', text: categoryLabel(r.category) }),
  el('td', { text: r.term }),
  el('td', { class: 'small', style: { color: 'var(--cyan)' },
    text: typeof r.value === 'string' ? r.value : JSON.stringify(r.value) })));

  return el('div', { class: 'table-wrap' }, el('table', { class: 'glossary' },
    el('thead', {}, el('tr', {}, ...[scopeHeading, '类别', '词条', '译法 / 说明'].map((h) => el('th', { text: h })))),
    el('tbody', {}, ...rows)));
}

const CATEGORY_NAMES = {
  fixed_terms: '固定术语',
  contextual_terms: '语境术语',
  aesthetic_sentences: '美学表达',
  cultural_nuances: '文化虚指',
};

const categoryLabel = (id) => CATEGORY_NAMES[id] || String(id || '').replace('_terms', '').replace('_', ' ');

/** 全局术语库：跟「概览」并列的独立视图，不属于任何作品。 */
async function renderGlobalGlossary() {
  const data = await api('/api/glossary/global');
  const content = $('#content');

  if (!data.ok) {
    content.replaceChildren(el('div', { class: 'empty', text: data.error || '读取失败' }));
    return;
  }

  const query = state.glossary.query.toLowerCase();
  const entries = data.entries || [];
  const categories = data.categories || [];

  const search = el('input', {
    type: 'text', placeholder: '词条 / 译法 / 说明…', value: state.glossary.query,
    style: { width: '240px' },
    oninput: (e) => { state.glossary.query = e.target.value.trim(); route(); },
  });
  const addBtn = el('button', {
    class: 'sm primary', text: '＋ 新增术语',
    onclick: () => openGlossaryModal(null, { mode: 'global' }),
  });

  const controls = el('div', { class: 'row', style: { marginBottom: '12px' } },
    labeled('搜索', search), el('span', { class: 'spacer' }), addBtn);

  const banner = el('div', { class: 'banner info', text:
    '全局术语库对所有作品生效，是译名的最高基准；自动翻译永远不会覆盖它。'
    + '在这里新增或修改的词条，写进 config/glossary.json。' });

  const counts = data.counts || {};
  const chips = el('div', { class: 'row small muted', style: { margin: '4px 0 10px' } },
    ...categories.map((c) => el('span', { class: 'pill info',
      text: `${c.label} ${counts[c.id] || 0}` })));

  const shown = entries.filter((e) => !query
    || e.term.toLowerCase().includes(query)
    || JSON.stringify(e.value).toLowerCase().includes(query));

  const table = glossaryTable(shown, {
    scopeHeading: '范围', rowMode: 'global',
    onRow: (r) => openGlossaryModal(null, { term: r.term, mode: 'global' }),
  });

  content.className = 'content';
  content.replaceChildren(banner, controls, chips, table);
  $('#view-title').textContent = '全局术语库';
  $('#view-meta').textContent = `${data.total} 条术语 · 所有作品共用`;
}

function renderGlossaryByChapter(perChapter, query) {
  const entries = Object.entries(perChapter).sort((a, b) => Number(a[0]) - Number(b[0]));
  if (!entries.length) return el('div', { class: 'empty', text: '还没有章节术语库' });

  const cards = entries.map(([num, gloss]) => {
    const items = Object.entries(gloss).flatMap(([category, terms]) =>
      Object.entries(terms || {}).map(([term, value]) => ({ category, term, value })));
    const shown = items.filter((i) => !query
      || i.term.toLowerCase().includes(query)
      || JSON.stringify(i.value).toLowerCase().includes(query));
    if (!shown.length) return null;

    // 一章一张卡片（占满整行），卡片内术语按三列排布
    const cells = shown.map((i) => el('div', { class: 'term-cell', title: i.category },
      el('span', { class: 'term-name', text: i.term }),
      el('span', { class: 'term-arrow', text: '→' }),
      el('span', { class: 'term-value',
        text: typeof i.value === 'string' ? i.value : JSON.stringify(i.value) })));

    return el('div', { class: 'card' },
      el('h3', {}, `第 ${num} 章`, el('span', { class: 'sub', text: `${shown.length} 条` })),
      el('div', { class: 'term-grid' }, ...cells));
  }).filter(Boolean);

  return cards.length
    ? el('div', { class: 'cards-col' }, ...cards)
    : el('div', { class: 'empty', text: '没有匹配的术语' });
}

function renderConflicts(conflicts, query) {
  const records = conflicts.filter((r) => !query
    || String(r.term || '').toLowerCase().includes(query)
    || String(r.old || '').toLowerCase().includes(query)
    || String(r.new || '').toLowerCase().includes(query));
  if (!records.length) return el('div', { class: 'empty', text: '没有冲突记录（新规则下冲突会被拦截并记录在这里）' });

  const rows = records.slice(-400).reverse().map((r) => el('tr', {},
    el('td', { class: 'small muted nowrap', text: (r.time || '').slice(5, 19) }),
    el('td', { text: r.term }),
    el('td', { class: 'small dim', text: r.context || '—' }),
    el('td', { class: 'small muted', text: r.category }),
    el('td', { class: 'small', style: { color: 'var(--green)' }, text: r.old }),
    el('td', { class: 'small', style: { color: 'var(--red)' }, text: r.new }),
    el('td', { class: 'small muted', text: r.origin === 'global' ? '全局' : `ch${r.origin_chapter}` }),
    el('td', {}, el('span', { class: `pill ${r.severity === 'minor' ? 'warn' : 'bad'}`, text: r.severity })),
    el('td', {}, el('span', { class: `pill ${r.action === 'ignored' ? 'ok' : 'warn'}`,
      text: r.action === 'ignored' ? '已拦截' : '已覆盖' }))));

  return el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {}, ...['时间', '词条', '语境', '类别', '既定译法', '模型新给', '来源', '严重度', '处理']
      .map((h) => el('th', { text: h })))),
    el('tbody', {}, ...rows)));
}


export { CATEGORY_NAMES, categoryLabel, glossaryTable, renderConflicts, renderGlobalGlossary, renderGlossaryByChapter, renderGlossaryTracker, renderGlossaryView };
