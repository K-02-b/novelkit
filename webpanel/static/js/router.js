/* router.js — hash 路由（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { SUBVIEWS, state, workByName } from './state.js';
import { renderNav, renderOverview } from './nav.js';
import { rememberChapter, renderReader } from './reader.js';
import { renderGlobalGlossary, renderGlossaryView } from './glossary-view.js';
import { editorState, renderEditorView } from './editor.js';
import { renderImport } from './import-view.js';
import { renderSettings } from './settings.js';

/* ---------------------------------------------------------------- 路由 */

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const parts = raw.split('/').filter(Boolean);
  if (parts[0] === 'import' || parts[0] === 'settings' || parts[0] === 'global-glossary') {
    return { view: parts[0], work: null, param: null };
  }
  if (parts[0] === 'work' && parts[1]) {
    // location.hash 对非 ASCII 是**已编码**的（#/work/%E8%B5%9B…），
    // 而作品名要用未编码的形式去匹配 state.works —— 中文作品名曾经因此打不开。
    let work = parts[1];
    try { work = decodeURIComponent(work); } catch { /* 非法转义就用原串 */ }
    const third = parts[2];
    const isView = third && SUBVIEWS.some((s) => s.id === third);
    const view = isView ? third : 'reader';
    const param = isView ? (parts[3] || null) : (third || null);
    return { view, work, param };
  }
  return { view: parts[0] || 'overview', work: null, param: null };
}

async function route() {
  const { view, work, param } = parseHash();
  state.work = null;
  const content = $('#content');
  content.className = 'content';
  content.replaceChildren(el('div', { class: 'empty' }, el('span', { class: 'spin' }), ' 加载中…'));

  try {
    if (view === 'import') {
      renderNav();
      renderImport();
      return;
    }
    if (view === 'settings') {
      renderNav();
      await renderSettings();
      return;
    }
    if (view === 'global-glossary') {
      renderNav();
      await renderGlobalGlossary();
      return;
    }
    if (view === 'overview' || !work) {
      renderNav();
      renderOverview();
      return;
    }

    const summary = workByName(work);
    if (!summary) {
      renderNav();
      content.replaceChildren(el('div', { class: 'empty', text: `作品不存在：${work}` }));
      return;
    }
    state.work = summary.name;

    if (view === 'reader') {
      if (param && /^\d+$/.test(param)) {
        state.reader.num = Number(param);
        rememberChapter(summary.name, state.reader.num);
      } else {
        // 不带章号时不能沿用上一部作品的章号，交给 renderReader 按记忆/简介决定
        state.reader.num = null;
      }
      renderNav();
      await renderReader(summary);
    } else if (view === 'glossary') {
      renderNav();
      await renderGlossaryView(summary);
    } else if (view === 'edit') {
      if (param && /^\d+$/.test(param)) editorState.num = Number(param);
      renderNav();
      await renderEditorView(summary);
    } else {
      state.reader.num = null;
      renderNav();
      await renderReader(summary);
    }
  } catch (error) {
    renderNav();
    content.replaceChildren(el('div', { class: 'empty', text: `加载失败：${error.message}` }));
  }
}


export { parseHash, route };
