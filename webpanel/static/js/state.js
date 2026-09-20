/* state.js — 全局状态（由 static/app.js 拆分而来） */

/* ---------------------------------------------------------------- 状态 */

const SUBVIEWS = [
  { id: 'reader', label: '中英对照', icon: '⇄' },
  { id: 'glossary', label: '术语库', icon: '⌗' },
  { id: 'edit', label: '编辑', icon: '✎' },
];

const state = {
  works: [],
  tools: [],
  authRequired: false,
  toolProxy: true,
  work: null,
  reader: { num: 1, mode: 'both', size: 15, query: '', matchIndex: -1,
            terms: [], matcher: null, termsOn: true, paragraphs: null },
  glossary: { tab: 'tracker', query: '' },
};

const workByName = (name) => state.works.find((w) => w.name === name) || null;


export { SUBVIEWS, state, workByName };
