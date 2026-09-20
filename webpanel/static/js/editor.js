/* editor.js — 本地编辑与作品级操作（由 static/app.js 拆分而来） */

import { $, el, labeled } from './dom.js';
import { api, nf, post, toast } from './api.js';
import { state } from './state.js';
import { renderNav } from './nav.js';
import { lastChapterKey } from './reader.js';
import { route } from './router.js';
import { loadBootstrap } from './loader.js';

/* ------------------------------------------------------------ 本地编辑 */

/**
 * 本地章节编辑器：直接读写这部作品的原文与译文。
 *
 * 面板不再对接任何发布平台，这里只做三件事：看、改、新建。
 * 新建章节在按下「保存」之前**不会**创建文件；每次保存前后端都会把旧文件
 * 备份到 .backups/，所以改错了还能自己还原。
 */
const editorState = {
  work: null, num: null, side: 'origin', text: '', dirty: false, isNew: false,
  numbers: [], hasOrigin: new Set(), hasTranslated: new Set(), intro: new Set(),
  missingOrigin: [], missingTranslated: [], max: 0, nextNumber: 1,
  newNumber: 1, newSide: 'origin',
};

const EDITOR_SIDES = [['origin', '原文'], ['translated', '译文']];

const editorSideLabel = (side) => (side === 'origin' ? '原文' : '译文');

/** 与后端一致：只数非空行。 */
function editorLineCount(text) {
  return String(text || '').split(/\r?\n/).filter((line) => line.trim()).length;
}

function editorStatsText() {
  const text = editorState.text || '';
  return `${nf(text.length)} 字符 / ${nf(editorLineCount(text))} 行`;
}

/** 拉取章号清单与逐章状态（原文/译文是否存在、缺哪一侧）。 */
async function editorRefreshMeta() {
  const work = editorState.work;
  if (!work) return;
  const [inv, chapters] = await Promise.all([
    api(`/api/works/${encodeURIComponent(work)}/inventory`),
    api(`/api/works/${encodeURIComponent(work)}/chapters`),
  ]);
  editorState.numbers = inv.numbers || [];
  editorState.missingOrigin = inv.missing_origin || [];
  editorState.missingTranslated = inv.missing_translated || [];
  editorState.max = inv.max || 0;
  editorState.nextNumber = inv.next_number || 1;
  editorState.newNumber = editorState.nextNumber;

  const hasOrigin = new Set();
  const hasTranslated = new Set();
  const intro = new Set();
  for (const row of (chapters.chapters || [])) {
    if (row.has_origin) hasOrigin.add(row.num);
    if (row.has_translated) hasTranslated.add(row.num);
    if (row.is_intro) intro.add(row.num);
  }
  editorState.hasOrigin = hasOrigin;
  editorState.hasTranslated = hasTranslated;
  editorState.intro = intro;
}

/** 读取一章某一侧的纯文本，写入编辑器状态（不做脏数据确认）。 */
async function editorFetchInto(num, side) {
  const work = editorState.work;
  const res = await api(`/api/works/${encodeURIComponent(work)}/text/${num}?side=${side}`);
  if (!res.ok) { toast(res.error || '读取失败', 'bad'); return false; }
  editorState.num = num;
  editorState.side = side;
  editorState.text = res.text || '';
  editorState.dirty = false;
  editorState.isNew = false;
  return true;
}

/** 切换章节 / 文本类型：有未保存内容时先确认，绝不静默丢弃。 */
async function editorOpen(num, side) {
  if (editorState.dirty
      && !confirm('当前内容尚未保存，切换后会丢失这些修改。确定继续？')) {
    editorRender();
    return;
  }
  if (await editorFetchInto(num, side)) editorRender();
}

/** 新建：只把编辑器清空并指向目标章号，文件要等「保存」才创建。 */
function editorStartNew() {
  const num = Number(editorState.newNumber);
  if (!Number.isInteger(num) || num < 0) { toast('章号必须是不小于 0 的整数', 'bad'); return; }
  if (editorState.dirty
      && !confirm('当前内容尚未保存，新建会丢弃这些修改。确定继续？')) return;
  editorState.num = num;
  editorState.side = editorState.newSide;
  editorState.text = '';
  editorState.dirty = false;
  editorState.isNew = true;      // 首次保存要带 create:true，后端拒绝覆盖已存在的同侧文件
  editorRender();
  toast(`已切换到空编辑器：第 ${num} 章${editorSideLabel(editorState.side)}（按「保存」才会创建文件）`, '');
}

async function editorSave(button) {
  const work = editorState.work;
  const num = editorState.num;
  if (!work || num === null) return;
  button.disabled = true;
  button.textContent = '保存中…';
  const res = await post(`/api/works/${encodeURIComponent(work)}/text/${num}`, {
    side: editorState.side, text: editorState.text, create: editorState.isNew,
  });
  button.disabled = false;
  button.textContent = '保存';
  if (!res.ok) {
    // 失败时保留 dirty / isNew：内容还在缓冲区里，且这次仍算"尚未创建"
    toast(res.error || '保存失败', 'bad');
    return;
  }

  editorState.dirty = false;
  editorState.isNew = false;
  const label = res.label || editorSideLabel(res.side);
  const size = `${nf(res.chars)} 字符 / ${nf(res.lines)} 行`;
  if (res.changed === false) {
    toast(`第 ${res.num} 章${label}没有改动（${size}）`, '');
  } else {
    const head = res.created
      ? `已创建第 ${res.num} 章${label}`
      : `已保存第 ${res.num} 章${label}`;
    const tail = res.backup ? ` · 旧文件已备份到 ${res.backup}` : '';
    toast(`${head}（${size}）${tail}`, 'ok');
  }
  await editorRefreshMeta();
  editorRender();
}

/** 整个「编辑」页：选章 + 文本类型 + 保存 + 新建章节。 */
function editorRender() {
  const content = $('#content');
  const st = editorState;
  if (!st.work) return;

  const options = st.numbers.map((num) => el('option', {
    value: num, selected: num === st.num,
    text: `#${num}${st.intro.has(num) ? ' 简介' : ''} · `
      + `${st.hasOrigin.has(num) ? '原文✓' : '原文—'} ${st.hasTranslated.has(num) ? '译文✓' : '译文—'}`,
  }));
  // 刚新建、还没保存的章号不在清单里，补一个占位项，否则下拉会跳到别的章
  if (st.num !== null && !st.numbers.includes(st.num)) {
    options.unshift(el('option', { value: st.num, selected: true, text: `#${st.num} · 尚未创建` }));
  }

  const picker = el('select', { onchange: (e) => editorOpen(Number(e.target.value), st.side) }, options);

  const sideButtons = EDITOR_SIDES.map(([id, label]) => el('button', {
    class: `sm${st.side === id ? ' primary' : ''}`,
    text: label,
    onclick: () => { if (id !== st.side) editorOpen(st.num, id); },
  }));

  const dirtyPill = el('span', {
    class: `pill${st.dirty ? ' warn' : ''}`, text: st.dirty ? '未保存' : '已同步',
  });

  const feedback = el('span', { class: 'small muted', text: editorStatsText() });

  const saveBtn = el('button', { class: 'sm primary', text: '保存', onclick: (e) => editorSave(e.target) });

  const hints = [];
  if (st.missingTranslated.length) {
    hints.push(el('button', {
      class: 'sm', title: `缺译文：${st.missingTranslated.join('、')}`,
      text: `有 ${st.missingTranslated.length} 章缺译文`,
      onclick: () => editorOpen(st.missingTranslated[0], 'translated'),
    }));
  }
  if (st.missingOrigin.length) {
    hints.push(el('button', {
      class: 'sm', title: `缺原文：${st.missingOrigin.join('、')}`,
      text: `有 ${st.missingOrigin.length} 章缺原文`,
      onclick: () => editorOpen(st.missingOrigin[0], 'origin'),
    }));
  }

  const textarea = el('textarea', {
    class: 'mono',
    style: { width: '100%', minHeight: '60vh', lineHeight: '1.6', whiteSpace: 'pre', resize: 'vertical' },
    placeholder: '一行一段。这里的内容会原样写入文件。',
    oninput: (e) => {
      st.text = e.target.value;
      st.dirty = true;
      dirtyPill.className = 'pill warn';
      dirtyPill.textContent = '未保存';
      feedback.textContent = editorStatsText();
    },
  });
  textarea.value = st.text;

  const newNumInput = el('input', {
    type: 'number', min: '0', value: st.newNumber, style: { width: '90px' },
    oninput: (e) => { st.newNumber = Number(e.target.value); updateNewWarning(); },
  });
  const newSideSelect = el('select', {
    onchange: (e) => { st.newSide = e.target.value; updateNewWarning(); },
  }, EDITOR_SIDES.map(([id, label]) => el('option', {
    value: id, selected: st.newSide === id, text: label,
  })));
  const newWarning = el('div', { class: 'small muted' });

  function updateNewWarning() {
    const num = Number(st.newNumber);
    if (!Number.isInteger(num) || num < 0) {
      newWarning.className = 'small warn-text';
      newWarning.textContent = '章号必须是不小于 0 的整数。';
      return;
    }
    const exists = st.newSide === 'origin' ? st.hasOrigin.has(num) : st.hasTranslated.has(num);
    if (exists) {
      newWarning.className = 'small warn-text';
      newWarning.textContent = `⚠ 第 ${num} 章${editorSideLabel(st.newSide)}已存在，保存会覆盖它（旧文件会自动备份）。`;
    } else {
      newWarning.className = 'small muted';
      newWarning.textContent = `第 ${num} 章${editorSideLabel(st.newSide)}尚不存在，按「保存」时才会创建。`;
    }
  }

  content.className = 'content';
  content.replaceChildren(
    el('div', { class: 'banner info', text:
      '本地编辑：直接修改这部作品的原文与译文，不经过任何发布平台。'
      + '每次保存前都会自动备份旧内容；新建章节在按下保存之前不会创建文件。' }),
    // 作品级操作用得少、又是危险操作，放最上面一行，不占独立卡片
    el('div', { class: 'card' },
      el('div', { class: 'row' },
        el('b', { text: st.work }),
        el('span', { class: 'small muted', text: '作品操作' }),
        el('span', { class: 'spacer' }),
        el('button', { class: 'sm', text: '重命名作品', title: '给这部作品的目录改名',
          onclick: () => renameWork(st.work) }),
        el('button', { class: 'sm danger', text: '删除整本书',
          title: '把整部作品移入工作区根的 works/.trash/（可恢复）',
          onclick: () => deleteWork(st.work) })),
      el('div', { class: 'small muted mt', text:
        '重命名会改作品目录名；删除不是真删，整个目录会被移到 .trash/，随时可以捞回来。' })),
    el('div', { class: 'card' },
      el('h3', {}, '章节 ', el('span', { class: 'sub', text: `${st.numbers.length} 个章号 · 最大 #${st.max}` })),
      // 左右两栏：左边"读/改现有章节"，右边"新建章节"，中间一条竖分隔线
      el('div', { class: 'edit-split' },
        el('div', { class: 'split-col' },
          el('div', { class: 'split-title', text: '编辑现有章节' }),
          el('div', { class: 'row' },
            labeled('章节', picker),
            labeled('文本类型', el('div', { class: 'row' }, ...sideButtons)),
            el('span', { class: 'spacer' }),
            dirtyPill,
            saveBtn),
          hints.length ? el('div', { class: 'row small' }, ...hints) : null),
        el('div', { class: 'split-col' },
          el('div', { class: 'split-title', text: '新建章节' }),
          el('div', { class: 'row' },
            labeled('章号', newNumInput),
            labeled('文本类型', newSideSelect),
            el('span', { class: 'spacer' }),
            el('button', { class: 'sm', text: '新建空章节', onclick: editorStartNew })),
          newWarning))),
    el('div', { class: 'card' },
      el('h3', {}, '内容 ', el('span', { class: 'sub', text: st.num === null ? '' : `第 ${st.num} 章 · ${editorSideLabel(st.side)}` })),
      textarea,
      el('div', { class: 'row small', style: { marginTop: '6px' } }, feedback)));

  $('#view-title').textContent = st.work;
  $('#view-meta').textContent = `本地编辑 · 第 ${st.num} 章 · ${editorSideLabel(st.side)}`
    + (st.dirty ? ' · 未保存' : '');
  updateNewWarning();
}

/* ------------------------------------------------- 作品级操作（改名 / 删除） */

/** 记住的阅读位置是按作品名存的，改名后要把它一起搬过去。 */
function moveRememberedChapter(oldName, newName) {
  try {
    const last = localStorage.getItem(lastChapterKey(oldName));
    if (last !== null) {
      localStorage.setItem(lastChapterKey(newName), last);
      localStorage.removeItem(lastChapterKey(oldName));
    }
  } catch { /* 存储不可用就算了 */ }
}

/** 改作品目录名：成功后更新列表、hash 与阅读位置记忆，再重新渲染编辑页。 */
async function renameWork(currentName) {
  const input = window.prompt('新的作品名（就是目录名）', currentName);
  if (input === null) return;
  const name = input.trim();
  if (!name || name === currentName) return;

  const res = await post(`/api/works/${encodeURIComponent(currentName)}/rename`, { name });
  if (!res.ok) { toast(res.error || '重命名失败', 'bad'); return; }

  moveRememberedChapter(currentName, res.work);
  if (editorState.work === currentName) editorState.work = res.work;
  if (state.work === currentName) state.work = res.work;
  await loadBootstrap();
  renderNav();
  toast(`已改名为「${res.work}」`, 'ok');
  // 先跳转（hash 变了会走 route），再确保视图刷新
  const target = `#/work/${encodeURIComponent(res.work)}/edit`;
  if (location.hash === target) await route();
  else location.hash = target;
}

/** 删除整本书：移入 .trash/，然后清理视图与本地记忆。 */
async function deleteWork(name) {
  if (!confirm(`确定删除整部作品「${name}」？\n\n`
    + '它不是真删：整个目录会被移到工作区根的 works/.trash/ 目录，随时可以恢复。')) {
    return;
  }
  const res = await api(`/api/works/${encodeURIComponent(name)}`,
    { method: 'DELETE', body: '{}' });
  if (!res.ok) { toast(res.error || '删除失败', 'bad'); return; }

  try { localStorage.removeItem(lastChapterKey(name)); } catch { /* 忽略 */ }
  if (editorState.work === name) {
    editorState.work = null; editorState.num = null; editorState.text = '';
    editorState.dirty = false;
  }
  await loadBootstrap();
  renderNav();
  toast(`已把「${name}」移入 ${res.trash_path}（${res.files} 个文件）`, 'ok');
  // 作品没了，回到概览
  if (location.hash.startsWith(`#/work/${encodeURIComponent(name)}`)) location.hash = '#/overview';
  await route();
}

/** 路由入口：拉清单 → 决定默认章节 → 读取文本 → 渲染。 */
async function renderEditorView(work) {
  const changedWork = editorState.work !== work.name;
  editorState.work = work.name;
  if (changedWork) {
    editorState.num = null;
    editorState.side = 'origin';
    editorState.text = '';
    editorState.dirty = false;
  }
  await editorRefreshMeta();
  // 同一作品内重新进入时保留未保存的缓冲区，避免路由重绘把编辑内容冲掉
  if (!editorState.dirty) {
    let num = editorState.num;
    if (num === null || !editorState.numbers.includes(num)) {
      num = editorState.numbers.length ? editorState.numbers[0] : editorState.nextNumber;
    }
    await editorFetchInto(num, editorState.side);
  }
  editorRender();
}



export { EDITOR_SIDES, deleteWork, editorFetchInto, editorLineCount, editorOpen, editorRefreshMeta, editorRender, editorSave, editorSideLabel, editorStartNew, editorState, editorStatsText, moveRememberedChapter, renameWork, renderEditorView };
