/* chapter-actions.js — 本章操作与分段选择（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { api, ensureModelApiKey, nf, post, toast } from './api.js';
import { highlight, redecorateReader } from './terms.js';
import { state } from './state.js';
import { renderNav } from './nav.js';
import { highlightLabelText, rebuildText } from './reader.js';
import { jobWatchers, mountJob, refreshedJobs } from './jobs.js';
import { openGlossaryModal } from './glossary-modal.js';
import { route } from './router.js';
import { loadBootstrap } from './loader.js';

/* ------------------------------------------ 本章操作（翻译 / 重译 / 精修） */

const ACTIONS_OPEN_KEY = 'novel.panel.actionsOpen';
const RAG_KEY = 'novel.panel.rag';

/**
 * 带记忆的布尔开关：localStorage 里没有记录时用 defaultValue，
 * 用户手动改过之后就一直沿用他的选择（跨会话保留）。
 * 存储不可用（隐私模式/被禁用）时静默退回默认值，不影响功能。
 */
function persistedFlag(key, defaultValue) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === '1') return true;
    if (raw === '0') return false;
  } catch { /* 忽略：存储不可用时用默认值 */ }
  return defaultValue;
}

function rememberFlag(key, value) {
  try { localStorage.setItem(key, value ? '1' : '0'); } catch { /* 忽略 */ }
}
const SNIPPET_KEYS = ['translate', 'retranslate', 'refine'];
const SNIPPET_META = {
  translate: { label: '整章翻译提示词', hint: '翻译整章时注入（<user_supplement>）' },
  retranslate: { label: '局部重译提示词', hint: '只重译选中段落时注入' },
  refine: { label: 'Refine 提示词', hint: '点评与修订时注入' },
};

/** 一个可折叠的补充要求输入框；失焦即自动保存，内容跨会话保留。 */
function snippetBox(work, kind, { rows = 3 } = {}) {
  const meta = SNIPPET_META[kind];
  const value = (state.snippets && state.snippets[kind]) || '';
  const area = el('textarea', {
    rows: String(rows),
    placeholder: (state.snippetMeta[kind] && state.snippetMeta[kind].placeholder) || '',
    style: { width: '100%' },
    oninput: (e) => { state.snippets[kind] = e.target.value; },
  });
  area.value = value;

  const status = el('span', { class: 'small muted', 'data-snippet-status': kind,
    text: value.trim() ? `已保存 ${value.trim().length} 字符` : '未填写' });

  const save = async () => {
    const res = await post(`/api/works/${encodeURIComponent(work.name)}/snippets`,
      { kind, text: state.snippets[kind] || '' });
    if (!res.ok) { status.textContent = res.error || '保存失败'; return; }
    state.snippets = res.snippets;
    status.textContent = (state.snippets[kind] || '').trim()
      ? `已保存 ${state.snippets[kind].trim().length} 字符` : '未填写';
  };
  area.addEventListener('blur', save);
  area.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); }
  });

  const body = el('div', { class: 'snippet-body', style: { display: 'none' } },
    el('div', { class: 'small muted', text: `${meta.hint}。失焦自动保存（Ctrl+S 立即保存）。` }),
    area,
    el('div', { class: 'row small', style: { marginTop: '4px' } }, status));

  const toggle = el('button', {
    class: 'sm ghost snippet-toggle',
    text: '▸ 提示词',
    title: meta.hint,
    onclick: () => {
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : '';
      toggle.textContent = open ? '▸ 提示词' : '▾ 提示词';
      if (!open) area.focus();
    },
  });

  return { toggle, body };
}

const chapterActionState = {
  // RAG 默认开启（本机性能足够），但用户改过就一直记住上次的选择
  rag: persistedFlag(RAG_KEY, true),
  job: null, error: '', busy: false,
  selectedRows: new Set(), picking: false, ignoreDraft: false, rebuildGlossary: false,
};

function actionsOpenByDefault() {
  try { return localStorage.getItem(ACTIONS_OPEN_KEY) !== '0'; } catch { return true; }
}
function buildChapterActions(work, data, chapters) {
  const st = chapterActionState;
  const number = data.num;
  const translated = data.has_translated;
  const rows = [];      // 全宽内容（任务日志、错误、结果）
  const left = [];      // 左栏：翻译 / 高亮 / 局部重译
  const right = [];     // 右栏：术语高亮与指定术语

  // --- 翻译 ---
  const ragBox = el('label', { class: 'check', title: '翻译前回全书检索候选词的上下文（默认开启，会记住上次选择）' },
    el('input', { type: 'checkbox', checked: st.rag, onchange: (e) => {
      st.rag = e.target.checked;
      rememberFlag(RAG_KEY, st.rag);
    } }),
    el('span', { text: 'RAG 术语检索' }));

  const translateBtn = el('button', {
    class: 'sm primary', text: translated ? '重新翻译本章' : '翻译本章',
    onclick: async (event) => {
      if (translated && !confirm(`第 ${number} 章已有译文，重新翻译会覆盖它。继续？`)) return;
      if (!await ensureModelApiKey()) return;
      const button = event.target;
      button.disabled = true; button.textContent = '启动中…';
      const res = await post(`/api/works/${encodeURIComponent(work.name)}/translate`, {
        chapter: String(number), rag: st.rag, force: translated,
        rebuild_glossary: st.rebuildGlossary,
      });
      button.disabled = false; button.textContent = translated ? '重新翻译本章' : '翻译本章';
      if (!res.ok) { toast(res.error || '启动失败', 'bad'); return; }
      if (st.job) { const old = jobWatchers.get(st.job); if (old) { old.close(); jobWatchers.delete(st.job); } }
      refreshedJobs.delete(res.job.id);
      st.job = res.job.id;
      toast(`已启动：${res.job.label}`, 'ok');
      mountJob(res.job);
    },
  });

  const highlightBox = el('label', { class: 'check', title: '在正文里用底色标出本章出现的术语' },
    el('input', { type: 'checkbox', checked: state.reader.termsOn,
      onchange: (e) => { state.reader.termsOn = e.target.checked; redecorateReader(); } }),
    el('span', { id: 'highlight-label', text: highlightLabelText() }));

  const rebuildLabel = el('span', { text: rebuildText() });
  const rebuildBox = el('label', {
    class: 'check',
    title: '忽略本章已有的术语库，按本次译文整体重建。'
      + '默认是「合并写入」：只更新同名术语，人工预登记和旧译文留下的词条都会保留。',
  },
  el('input', { type: 'checkbox', checked: st.rebuildGlossary,
    onchange: (e) => {
      st.rebuildGlossary = e.target.checked;
      rebuildLabel.textContent = rebuildText();   // 勾上要提示会丢多少条
    } }),
  rebuildLabel);

  const translateSnippet = snippetBox(work, 'translate');
  left.push(el('div', { class: 'row' }, translateBtn, ragBox, rebuildBox, translateSnippet.toggle));
  left.push(translateSnippet.body);

  // --- 分段重译：勾选若干中文段落，只重译这些段落 ---
  if (translated) {
    const pickToggle = el('label', {
      class: 'check',
      title: '勾选正文里的段落（点任意一侧都行），只处理这几段。'
        + '上下文、术语库与 RAG 检索和整章翻译一样注入，未勾选的段落保持原样；'
        + '一个对齐行含多段中文时整行一起处理。',
    },
    el('input', { type: 'checkbox', checked: st.picking,
      onchange: (e) => { st.picking = e.target.checked; applyPicking(); } }),
    el('span', { text: '分段选择' }));

    const ignoreDraftBox = el('label', {
      class: 'check',
      title: '不把现有译文交给 AI（等价于重新翻译），适合现译错得离谱、怕被带偏的段落',
    },
    el('input', { type: 'checkbox', checked: st.ignoreDraft,
      onchange: (e) => { st.ignoreDraft = e.target.checked; } }),
    el('span', { text: '忽略现有译文' }));

    const countLabel = el('span', { class: 'pill', 'data-pick-count': '1',
      text: st.selectedRows.size ? `已选 ${st.selectedRows.size} 段` : '未选段落' });

    const clearBtn = el('button', {
      class: 'sm', text: '清空选择',
      onclick: () => {
        st.selectedRows.clear();
        document.querySelectorAll('.rowpick').forEach((box) => { box.checked = false; });
        syncPickCount();
      },
    });

    const refineBtn = el('button', {
      class: 'sm primary', 'data-refine-submit': '1',
      text: st.selectedRows.size ? `处理选中的 ${st.selectedRows.size} 段` : '处理选中段落',
      disabled: st.selectedRows.size === 0,
      title: '让 AI 逐段评审并修订选中的段落，改动理由会显示在段落角上',
      onclick: async (event) => {
        const list = [...st.selectedRows].sort((a, b) => a - b);
        if (!list.length) return;
        if (!await ensureModelApiKey()) return;
        const button = event.target;
        button.disabled = true; button.textContent = '启动中…';
        const res = await post(`/api/works/${encodeURIComponent(work.name)}/refine`, {
          chapter: number, rows: list, rag: st.rag, ignore_draft: st.ignoreDraft,
        });
        button.disabled = false;
        if (!res.ok) { toast(res.error || '启动失败', 'bad'); syncPickCount(); return; }
        if (st.job) { const old = jobWatchers.get(st.job); if (old) { old.close(); jobWatchers.delete(st.job); } }
        refreshedJobs.delete(res.job.id);
        st.job = res.job.id;
        st.selectedRows.clear();
        toast(`已启动：${res.job.label}`, 'ok');
        mountJob(res.job);
      },
    });

    const refineSnippet = snippetBox(work, 'refine');
    left.push(el('div', { class: 'row small' },
      pickToggle, countLabel, clearBtn, refineBtn, ignoreDraftBox, refineSnippet.toggle));
    left.push(refineSnippet.body);
  }

  right.unshift(el('div', { class: 'row small' }, highlightBox,
    el('button', {
      class: 'sm', text: '＋ 指定术语',
      title: '为本章指定一个术语译法（写入术语库）',
      onclick: () => openGlossaryModal(work.name, { chapter: number }),
    })));

  // 任务日志有独立槽位 #job-slot（由 mountJob 负责挂），这里**不能**再放一份，
  // 否则同一份日志会同时出现在卡片里和槽位里（页面上看到两条一模一样的）。
  if (st.error) rows.push(el('div', { class: 'banner bad mt', text: st.error }));

  const summaryText = translated
    ? `已翻译 · ${nf(data.en_words)} 词`
    : '未翻译';

  const body = el('div', { class: 'actions-body' },
    el('div', { class: 'act-grid' },
      el('div', { class: 'act-col' }, ...left),
      el('div', { class: 'act-col' }, ...right)),
    ...rows);
  const details = el('details', {
    class: 'card collapsible',
    open: actionsOpenByDefault(),
  },
  el('summary', {}, '本章操作 ',
    el('span', { id: 'actions-summary', class: `sub${translated ? '' : ' warn-text'}`, text: summaryText }),
    chapterActionState.selectedRows.size
      ? el('span', { class: 'pill info', style: { marginLeft: '8px' },
          text: `已选 ${chapterActionState.selectedRows.size} 段` })
      : null),
  body);

  details.addEventListener('toggle', () => {
    try { localStorage.setItem(ACTIONS_OPEN_KEY, details.open ? '1' : '0'); } catch { /* 忽略 */ }
  });
  return details;
}

function applyPicking() {
  const body = $('#reader-body');
  if (body) body.classList.toggle('picking', chapterActionState.picking);
}

/** 更新"已选 N 段"的显示与重译按钮文案（勾选时不重绘，避免滚动位置丢失）。 */
function syncPickCount() {
  const count = chapterActionState.selectedRows.size;
  document.querySelectorAll('[data-pick-count]').forEach((node) => {
    node.textContent = count ? `已选 ${count} 段` : '未选段落';
  });
  document.querySelectorAll('[data-refine-submit]').forEach((button) => {
    button.disabled = count === 0;
    button.textContent = count ? `处理选中的 ${count} 段` : '处理选中段落';
  });
}

function setReaderMode(mode) { state.reader.mode = mode; applyReaderMode(); }

function markMode(button) {
  button.parentElement.querySelectorAll('button').forEach((b) => b.classList.remove('primary'));
  button.classList.add('primary');
}

function applyReaderMode() {
  const body = $('#reader-body');
  if (!body) return;
  body.classList.remove('solo-zh', 'solo-en');
  if (state.reader.mode === 'zh') body.classList.add('solo-zh');
  if (state.reader.mode === 'en') body.classList.add('solo-en');
}

function applyReaderSize() {
  document.documentElement.style.setProperty('--reader-size', `${state.reader.size}px`);
}

function applyReaderHighlight() {
  redecorateReader();
  // 重绘后旧的 <mark> 全被替换了，之前定位到的"当前命中"失去意义，一律从头数
  state.reader.matchIndex = -1;
  updateMatchCount();
}

/** 当前高亮命中数（0 时给个明确反馈）。 */
function readerMatches() {
  const body = $('#reader-body');
  return body ? [...body.querySelectorAll('mark')] : [];
}

function updateMatchCount() {
  const badge = $('#match-count');
  if (!badge) return;
  const marks = readerMatches();
  const query = state.reader.query;
  if (!query) { badge.textContent = ''; return; }
  if (!marks.length) { badge.textContent = '无匹配'; return; }
  const index = state.reader.matchIndex >= 0 && state.reader.matchIndex < marks.length
    ? state.reader.matchIndex : 0;
  badge.textContent = `${index + 1}/${marks.length}`;
}

/** 跳到上一个 / 下一个搜索命中，并把它标成"当前"以便一眼看到。 */
function gotoMatch(step) {
  const marks = readerMatches();
  const badge = $('#match-count');
  if (!marks.length) {
    if (badge && state.reader.query) badge.textContent = '无匹配';
    if (state.reader.query) toast('本章没有匹配的内容', '');
    return;
  }
  const current = state.reader.matchIndex;
  let next = current < 0 ? (step > 0 ? 0 : marks.length - 1) : current + step;
  if (next < 0) next = marks.length - 1;              // 循环
  if (next >= marks.length) next = 0;
  state.reader.matchIndex = next;

  marks.forEach((m) => m.classList.remove('mark-current'));
  const target = marks[next];
  target.classList.add('mark-current');
  target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  updateMatchCount();
}

/** 删除当前章：确认后删掉这一章的文件（先备份），并跳到相邻章。 */
async function deleteCurrentChapter(work, chapters, index) {
  const num = state.reader.num;
  const label = num === 0 ? '简介' : `第 ${num} 章`;
  const next = chapters[index + 1] || chapters[index - 1];
  const tail = next ? `删除后会跳到第 ${next.num} 章。` : '这是最后一章，删除后作品里就没有章节了。';
  if (!confirm(`确定删除${label}？\n\n原文件会先备份到作品的 .backups/ 目录，可自行还原。\n${tail}`)) {
    return;
  }
  const res = await fetch(
    `/api/works/${encodeURIComponent(work.name)}/chapter/${num}`,
    { method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ include_glossary: true }) })
    .then((r) => r.json()).catch(() => ({ ok: false, error: '请求失败' }));

  if (!res.ok) { toast(res.error || '删除失败', 'bad'); return; }
  toast(`已删除${label}（${res.count} 个文件，已备份）`, 'ok');
  await loadBootstrap();
  renderNav();
  if (next) location.hash = `#/work/${encodeURIComponent(work.name)}/reader/${next.num}`;
  else await route();
}


export { ACTIONS_OPEN_KEY, RAG_KEY, SNIPPET_KEYS, SNIPPET_META, actionsOpenByDefault, applyPicking, applyReaderHighlight, applyReaderMode, applyReaderSize, buildChapterActions, chapterActionState, deleteCurrentChapter, gotoMatch, markMode, persistedFlag, readerMatches, rememberFlag, setReaderMode, snippetBox, syncPickCount, updateMatchCount };
