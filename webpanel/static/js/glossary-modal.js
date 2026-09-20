/* glossary-modal.js — 术语录入模态框（由 static/app.js 拆分而来） */

import { $, el, labeled } from './dom.js';
import { api, post, toast } from './api.js';
import { route } from './router.js';
import { loadBootstrap } from './loader.js';

/* ------------------------------------------------- 术语录入模态框 */

const glossaryModalState = { work: null, mode: 'work', chapter: '', busy: false, lookup: null, force: false };

function openGlossaryModal(work, { term = '', chapter = null, mode = 'work' } = {}) {
  const st = glossaryModalState;
  st.work = work;
  st.mode = mode;
  st.chapter = chapter === null ? '' : String(chapter);
  st.lookup = null;
  st.force = false;
  renderGlossaryModal(term);
}

function closeGlossaryModal() {
  const host = document.querySelector('#modal-host');
  if (host) host.replaceChildren();
}

function renderGlossaryModal(termValue) {
  const work = glossaryModalState.work;
  const isGlobal = glossaryModalState.mode === 'global';
  const host = document.querySelector('#modal-host');
  if (!host || (!work && !isGlobal)) return;

  const termInput = el('input', { type: 'text', value: termValue || glossaryModalState.term || '',
    placeholder: '中文词条，如 苍穹神木' });
  const transInput = el('input', { type: 'text', placeholder: '英文译法，如 Skywood' });
  const catSelect = el('select', {},
    [['fixed_terms', '固定术语（全篇统一译名）'],
     ['contextual_terms', '语境术语（按语境给不同译法）'],
     ['aesthetic_sentences', '美学表达（修辞 / 句子处理的说明）'],
     ['cultural_nuances', '文化虚指（数字、称谓等文化含义说明）']]
      .map(([v, t]) => el('option', { value: v, text: t })));
  const ctxInput = el('input', { type: 'text', placeholder: '语境说明（语境术语必填）' });
  const ctxField = labeled('语境说明', ctxInput);
  const scopeSelect = el('select', {},
    (isGlobal
      ? [['global', '全局术语库（所有作品共用）']]
      : [['chapter', '仅本章（默认，先到先得）'],
         ['work', '整本书（这部作品最高优先级）'],
         ['global', '全局术语库（所有作品共用）']])
      .map(([v, t]) => el('option', { value: v, text: t })));
  const chapterInput = el('input', { type: 'text', value: glossaryModalState.chapter,
    placeholder: '留空 = 自动（新词写入最新章）', style: { width: '230px' } });
  const chapterField = labeled('写入章节', chapterInput);
  // 选「整本书」/「全局」时章节号没有意义；只有语境术语才需要语境说明
  const syncFields = () => {
    chapterField.classList.toggle('field-hidden', scopeSelect.value !== 'chapter');
    ctxField.classList.toggle('field-hidden', catSelect.value !== 'contextual_terms');
  };
  scopeSelect.addEventListener('change', syncFields);
  catSelect.addEventListener('change', syncFields);
  const forceBox = el('input', { type: 'checkbox', id: 'gl-force' });
  const feedback = el('div', { class: 'small muted', style: { minHeight: '18px' } });

  const doLookup = async () => {
    const term = termInput.value.trim();
    if (!term) { feedback.textContent = ''; glossaryModalState.lookup = null; return; }
    const res = isGlobal
      ? await api(`/api/glossary/global/lookup?term=${encodeURIComponent(term)}`)
      : await api(`/api/works/${encodeURIComponent(work)}/glossary/lookup?term=${encodeURIComponent(term)}`);
    glossaryModalState.lookup = res;
    if (!res.found) {
      feedback.className = 'small muted';
      feedback.textContent = '术语库里还没有这个词条，提交后会新增。';
      return;
    }
    if (res.source === 'work') {
      feedback.className = 'small';
      feedback.textContent = `来自作品术语库（整本书最高优先级）：${res.value}。`
        + '要改它请把「作用范围」选成「整本书」并勾选强制覆盖。';
    } else if (res.source === 'global') {
      feedback.className = 'small warn-text';
      feedback.textContent = `⚠ 该词条来自全局术语库：${res.value}`
        + '（所有作品共用）。要改它请把「作用范围」选成「全局术语库」并勾选强制覆盖。';
    } else {
      feedback.className = 'small';
      feedback.textContent = `已存在于第 ${res.chapter} 章：${res.value}（如需改成别的译法，请勾选「强制覆盖」）`;
    }
  };
  termInput.addEventListener('blur', doLookup);

  const submit = el('button', {
    class: 'sm primary', text: '保存',
    onclick: async (event) => {
      const button = event.target;
      button.disabled = true; button.textContent = '保存中…';
      const chapter = chapterInput.value.trim();
      const url = isGlobal
        ? '/api/glossary/global/term'
        : `/api/works/${encodeURIComponent(work)}/glossary/term`;
      const res = await post(url, {
        term: termInput.value.trim(),
        translation: transInput.value.trim(),
        category: catSelect.value,
        context: ctxInput.value.trim(),
        chapter: scopeSelect.value === 'chapter' && chapter !== '' ? Number(chapter) : null,
        force: forceBox.checked,
        scope: scopeSelect.value,
      });
      button.disabled = false; button.textContent = '保存';
      if (res.ok) {
        toast(res.message || '已保存', 'ok');
        closeGlossaryModal();
        await loadBootstrap();
        route();
      } else {
        feedback.className = 'small warn-text';
        feedback.textContent = res.error || '保存失败';
        toast(res.error || '保存失败', 'bad');
        if (res.conflict) { glossaryModalState.force = true; }
      }
    },
  });

  host.replaceChildren(el('div', { class: 'modal-backdrop', onclick: (e) => {
    if (e.target.classList.contains('modal-backdrop')) closeGlossaryModal();
  } },
  el('div', { class: 'modal' },
    el('h3', {}, '指定术语 ', el('span', { class: 'sub', text: isGlobal ? '全局术语库（所有作品共用）' : work })),
    el('div', { class: 'modal-grid' },
      labeled(isGlobal ? '词条' : '词条（中文）', termInput),
      labeled('译法 / 说明', transInput),
      labeled('作用范围', scopeSelect),
      labeled('类别', catSelect),
      ctxField,
      chapterField,
      el('label', { class: 'check', style: { alignSelf: 'end' } }, forceBox,
        el('span', { text: '强制覆盖已有译法' }))),
    feedback,
    el('div', { class: 'row mt' },
      el('span', { class: 'spacer' }),
      el('button', { class: 'sm', text: '取消', onclick: closeGlossaryModal }),
      submit),
    el('div', { class: 'small muted mt', text:
      '优先级：作品术语库（整本书）＞ 全局术语库（所有作品）＞ 章节术语库。\n'
      + '「仅本章」遵循先到先得：新词写入指定章节，已存在的词条回到它最早确立的那一章改写。\n'
      + '「整本书」作用于这部作品的所有章节，用来登记这本书的原著术语与关键设定。\n'
      + '「全局术语库」对所有作品生效（术语总表里标为"全局"），适合跨作品统一的写法；'
      + '自动翻译永远不会覆盖它。' }))));
  syncFields();
  setTimeout(() => termInput.focus(), 30);
  if (termValue) doLookup();
}


export { closeGlossaryModal, glossaryModalState, openGlossaryModal, renderGlossaryModal };
