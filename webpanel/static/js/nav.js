/* nav.js — 侧栏与概览（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { nf, toolProxyUrl } from './api.js';
import { SUBVIEWS, state } from './state.js';
import { parseHash } from './router.js';

/* ---------------------------------------------------------------- 侧栏 */

function renderNav() {
  const { view, work } = parseHash();
  const nodes = [];

  nodes.push(el('div', {
    class: `nav-item${view === 'overview' ? ' active' : ''}`,
    onclick: () => { location.hash = '#/overview'; },
  }, el('span', { class: 'ico', text: '◈' }), el('span', { text: '概览' })));

  nodes.push(el('div', {
    class: `nav-item${view === 'global-glossary' ? ' active' : ''}`,
    title: '所有作品共用的术语基准（写入 config/glossary.json）',
    onclick: () => { location.hash = '#/global-glossary'; },
  }, el('span', { class: 'ico', text: '⌘' }), el('span', { text: '全局术语库' })));

  nodes.push(el('div', { class: 'side-section', text: '作品' }));

  if (!state.works.length) {
    nodes.push(el('div', { class: 'muted small', style: { padding: '8px 10px' }, text: '还没有作品' }));
  }

  for (const item of state.works) {
    const open = item.name === work;
    nodes.push(el('div', {
      class: `work-chip${open ? ' active' : ''}`,
      title: `${item.path}\n已译 ${item.translated}/${item.chapters} 章`,
      onclick: () => { location.hash = `#/work/${item.name}`; },
    },
    el('span', { class: 'name', text: item.name }),
    el('span', { class: 'count', text: `${item.translated}/${item.chapters}` })));

    if (!open) continue;
    nodes.push(el('div', { class: 'subnav' }, ...SUBVIEWS.map((sub) => el('div', {
      class: `subnav-item${view === sub.id ? ' active' : ''}`,
      onclick: () => { location.hash = `#/work/${item.name}/${sub.id}`; },
    }, el('span', { class: 'ico', text: sub.icon }), el('span', { text: sub.label })))));
  }

  nodes.push(el('div', { class: 'side-section', text: '全局' }));
  nodes.push(el('div', {
    class: `nav-item${view === 'import' ? ' active' : ''}`,
    onclick: () => { location.hash = '#/import'; },
  }, el('span', { class: 'ico', text: '⊕' }), el('span', { text: '导入 EPUB' })));
  nodes.push(el('div', {
    class: `nav-item${view === 'settings' ? ' active' : ''}`,
    onclick: () => { location.hash = '#/settings'; },
  }, el('span', { class: 'ico', text: '⚙' }), el('span', { text: '环境配置' })));

  if (state.tools.length) {
    nodes.push(el('div', { class: 'side-section', text: '工具' }));
    for (const tool of state.tools) {
      nodes.push(el('div', {
        class: 'nav-item tool-item',
        onclick: () => window.open(toolProxyUrl(tool, state.toolProxy), '_blank', 'noopener,noreferrer'),
      },
      el('span', { class: 'ico', text: '↗' }),
      el('span', { class: 'tool-name', text: tool.label }),
      el('span', {
        class: `tool-dot ${tool.reachable ? 'on' : 'off'}`,
        title: tool.reachable ? '服务可访问' : '服务未启动（点击仍会尝试打开）',
      })));
    }
  }

  $('#nav').replaceChildren(...nodes);
}

/* ---------------------------------------------------------------- 概览 */

/** 工作区里一个作品都没有时的上手引导（按顺序告诉用户先做什么）。 */
function buildStartHereCard() {
  const step = (title, detail, action) => el('li', {},
    el('b', { text: title }),
    el('div', { class: 'small muted', text: detail }),
    action || null);

  return el('div', { class: 'card' },
    el('h3', {}, '开始使用 ', el('span', { class: 'sub', text: '还没有作品' })),
    el('div', { class: 'small muted', text:
      '一部作品对应一本小说。从 EPUB 导入章节，或直接打开已有作品。' }),
    el('ol', { class: 'steps' },
      step('导入原文',
        '上传 EPUB，勾选要翻译的章节，导出成一部新作品。',
        el('div', { class: 'row mt' },
          el('button', { class: 'sm primary', onclick: () => { location.hash = '#/import'; } }, '导入 EPUB'))),
      step('配置模型 API',
        '在「环境配置」里填入模型 API 地址、模型 ID 与 API Key（默认 DeepSeek）。',
        el('div', { class: 'row mt' },
          el('button', { class: 'sm', onclick: () => { location.hash = '#/settings'; } }, '环境配置'))),
      step('翻译',
        '进入作品的「中英对照」，底部「本章操作」可直接翻译本章、批量推进，并实时看到日志。'),
      step('校对与精修',
        '用术语库统一译名、用分段处理做局部重译与 Refine 精修；译文有问题随时回炉。'),
      step('本地编辑',
        '在作品「编辑」页直接查看、修改原文与译文，或新建章节；保存时自动备份旧文件。')),
    el('div', { class: 'banner info mt', text:
      '还没有作品时，用「导入 EPUB」建立第一部；导入或打开后回到「概览」即可看到。' }));
}

function renderOverview() {
  const content = $('#content');
  const cards = state.works.map((w) => {
    const pct = w.chapters ? Math.round((w.translated / w.chapters) * 100) : 0;
    const openSub = (sub) => { location.hash = `#/work/${w.name}/${sub}`; };
    return el('div', { class: 'card' },
      el('h3', {},
        w.name,
        el('span', { class: 'sub', text: `${w.translated} / ${w.chapters} 章已译 · ${pct}%` })),
      el('div', { class: 'bar' }, el('i', { style: { width: `${pct}%` } })),
      el('div', { class: 'row mt small dim' },
        el('span', {}, '待翻译 ', el('b', { text: nf(w.pending) })),
        el('span', {}, '术语库 ', el('b', { text: nf(w.glossaries) })),
        el('span', {}, '冲突 ', el('b', { text: nf(w.conflicts) })),
        el('span', {}, '下一章 ', el('b', { text: w.next_pending === null ? '已完结' : w.next_pending }))),
      el('div', { class: 'row mt' },
        el('button', { class: 'sm', onclick: () => openSub('reader') }, '中英对照'),
        el('button', { class: 'sm', onclick: () => openSub('glossary') }, '术语库'),
        el('button', { class: 'sm', onclick: () => openSub('edit') }, '编辑')));
  });

  content.className = 'content';
  if (!cards.length) {
    content.replaceChildren(buildStartHereCard());
    $('#view-title').textContent = '概览';
    $('#view-meta').textContent = '尚未导入作品';
    return;
  }

  content.replaceChildren(el('div', { class: 'grid c2' }, ...cards));
  $('#view-title').textContent = '概览';
  $('#view-meta').textContent = `${state.works.length} 个作品 · 点左侧作品进入`;
}


export { buildStartHereCard, renderNav, renderOverview };
