/* import-view.js — 导入 EPUB（由 static/app.js 拆分而来） */

import { $, el, labeled } from './dom.js';
import { api, nf, post, toast } from './api.js';
import { renderNav } from './nav.js';
import { route } from './router.js';
import { loadBootstrap } from './loader.js';

/* ------------------------------------------------------------ 导入 EPUB */

const importState = {
  fileName: '', scan: null, selected: new Set(), includeIntro: true,
  introIndex: null, work: '', startNumber: 1, overwrite: false,
  error: '', busy: false, previewIndex: null,
};

function renderImport() {
  const st = importState;
  const content = $('#content');
  const parts = [];

  parts.push(el('div', { class: 'banner info', text:
    '上传 EPUB，勾选要导入的章节，再写入作品。解析阶段只读，不会改动任何文件；'
    + '默认勾选简介之后的全部正文条目。' }));

  if (st.error) parts.push(el('div', { class: 'banner bad', text: st.error }));

  // 上传区只在"还没有解析出文件"时出现；已经有扫描结果就收起它，
  // 页面焦点交给章节表（要换文件就先「取消导入」）。
  if (!st.scan) {
    const fileInput = el('input', { type: 'file', accept: '.epub', style: { display: 'none' },
      onchange: (e) => { if (e.target.files[0]) uploadEpub(e.target.files[0]); } });

    const dropZone = el('div', {
      class: 'dropzone',
      onclick: () => fileInput.click(),
      ondragover: (e) => { e.preventDefault(); e.currentTarget.classList.add('over'); },
      ondragleave: (e) => e.currentTarget.classList.remove('over'),
      ondrop: (e) => {
        e.preventDefault(); e.currentTarget.classList.remove('over');
        const file = e.dataTransfer.files[0];
        if (file) uploadEpub(file);
      },
    },
    el('div', { style: { fontSize: '22px', marginBottom: '6px' }, text: '⊕' }),
    el('div', { text: st.busy ? '解析中…' : '点击选择 EPUB，或把文件拖到这里' }),
    el('div', { class: 'small muted', text: '只在本机解析，不会上传到任何服务器' }));
    parts.push(el('div', { class: 'card' }, fileInput, dropZone));
  }

  if (st.scan) {
    const items = st.scan.items;
    const bodyCandidates = items.filter((i) => i.index >= 0);

    const header = el('div', { class: 'row' },
      el('b', { text: st.fileName }),
      el('span', { class: 'pill', text: `${items.length} 个文档条目` }),
      el('span', { class: 'pill info', text: `已选 ${st.selected.size} 章` }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'sm', text: '全选', onclick: () => {
        st.selected = new Set(bodyCandidates.map((i) => i.index));
        renderImport();
      } }),
      el('button', { class: 'sm', text: '全不选', onclick: () => { st.selected = new Set(); renderImport(); } }),
      el('button', { class: 'sm', text: '仅非空', onclick: () => {
        st.selected = new Set(bodyCandidates.filter((i) => !i.empty).map((i) => i.index));
        renderImport();
      } }),
      el('button', { class: 'sm', text: '取消导入', title: '放弃本次解析结果，重新选择 EPUB',
        onclick: () => cancelImport() }));

    const rows = items.map((item) => {
      const isIntro = st.includeIntro && st.introIndex === item.index;
      const checkbox = el('input', {
        type: 'checkbox',
        checked: !isIntro && st.selected.has(item.index),
        disabled: isIntro,
        title: isIntro ? '已作为简介章节单独导入' : '',
        onchange: (e) => {
          if (e.target.checked) st.selected.add(item.index); else st.selected.delete(item.index);
          renderImport();
        },
      });
      // 整行可点即可勾选（预览按钮除外），与中英对照页保持同一种交互；
      // 但"作为简介导入"的那一条不参与正文勾选，置灰且不可点。
      const toggleRow = (event) => {
        if (isIntro) return;
        if (event.target.closest('button') || event.target.closest('input')) return;
        checkbox.checked = !checkbox.checked;
        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
      };
      return el('tr', {
        class: `${item.empty || isIntro ? 'dim ' : ''}${isIntro ? '' : 'clickable-row'}`,
        title: isIntro ? '已作为简介章节单独导入，不再计入正文' : '',
        onclick: toggleRow,
      },
        el('td', {}, checkbox),
        el('td', { class: 'num', text: item.index }),
        el('td', {}, item.title,
          isIntro ? el('span', { class: 'pill ok', style: { marginLeft: '6px' }, text: '简介' }) : null,
          !isIntro && item.toc ? el('span', { class: 'pill', style: { marginLeft: '6px' }, text: '目录' }) : null,
          !isIntro && item.chapter_number !== null && item.chapter_number !== undefined
            ? el('span', { class: 'pill info', style: { marginLeft: '6px' },
                text: `第 ${item.chapter_number} 章` }) : null,
          item.empty ? el('span', { class: 'pill', style: { marginLeft: '6px' }, text: '空' }) : null),
        el('td', { class: 'num', text: nf(item.chars) }),
        el('td', { class: 'num', text: nf(item.paragraphs) }),
        el('td', {}, el('button', { class: 'sm', text: '预览',
          onclick: () => { st.previewIndex = st.previewIndex === item.index ? null : item.index; renderImport(); } })));
    });

    const table = el('div', { class: 'table-wrap', style: { maxHeight: '42vh' } }, el('table', {},
      el('thead', {}, el('tr', {}, ...['', '下标', '标题', '字数', '段数', ''].map((h) => el('th', { text: h })))),
      el('tbody', {}, ...rows)));

    const previewItem = items.find((i) => i.index === st.previewIndex);
    const previewBox = previewItem
      ? el('div', { class: 'card' },
          el('h3', {}, `预览 · 下标 ${previewItem.index} · ${previewItem.title}`),
          el('div', { class: 'preview-text', text: previewItem.preview }))
      : null;

    const introToggle = el('label', { class: 'check' },
      el('input', { type: 'checkbox', checked: st.includeIntro,
        onchange: (e) => { st.includeIntro = e.target.checked; renderImport(); } }),
      el('span', { text: '作为简介章节导入' }));

    const introSelect = el('select', {
      disabled: !st.includeIntro,
      onchange: (e) => { st.introIndex = Number(e.target.value); renderImport(); },
    }, items.map((i) => el('option', { value: i.index, selected: i.index === st.introIndex,
      text: `[${i.index}] ${i.title}` })));

    const workInput = el('input', { type: 'text', placeholder: '作品名，如 three', value: st.work,
      style: { width: '160px' }, oninput: (e) => { st.work = e.target.value.trim(); } });
    const startInput = el('input', { type: 'number', min: '0', value: st.startNumber,
      style: { width: '80px' }, oninput: (e) => { st.startNumber = Number(e.target.value) || 0; } });
    const overwriteBox = el('label', { class: 'check' },
      el('input', { type: 'checkbox', checked: st.overwrite,
        onchange: (e) => { st.overwrite = e.target.checked; } }),
      el('span', { text: '覆盖已有文件' }));

    const commitBtn = el('button', {
      class: 'sm primary', text: '导入选中的章节',
      onclick: async (event) => {
        const button = event.target;
        button.disabled = true; button.textContent = '写入中…';
        const res = await post('/api/import/epub/commit', {
          scan_id: st.scan.scan_id, work: st.work,
          include_intro: st.includeIntro, intro_index: st.introIndex,
          body_indices: [...st.selected].sort((a, b) => a - b),
          start_number: st.startNumber, overwrite: st.overwrite,
        });
        button.disabled = false; button.textContent = '导入选中的章节';
        if (res.ok) {
          // 导入完成就进这部作品：结果页没有额外价值，用户要的是接着翻译/校对。
          // 先把作品列表刷新出来（侧栏是独立的渲染入口），再跳转。
          st.error = '';
          await loadBootstrap();
          renderNav();
          toast(`已导入 ${res.count} 个文件：${res.work}`, 'ok');
          if (res.missing_chapters && res.missing_chapters.length) {
            toast(`注意：漏选了第 ${res.missing_chapters.slice(0, 8).join('、')} 章`
              + (res.missing_chapters.length > 8 ? ' …' : '') + '，请确认不是误勾', 'bad');
          }
          const target = `#/work/${encodeURIComponent(res.work)}/reader`;
          resetImportState();          // 导入页内容清空：下次进来是干净的上传界面
          if (location.hash === target) await route();
          else location.hash = target;
          return;
        }
        st.error = res.error || '导入失败';
        toast(st.error, 'bad');
        renderImport();
      },
    });

    parts.push(el('div', { class: 'card' },
      header,
      el('div', { class: 'small muted', text:
        '勾选要作为正文章节的条目。默认勾选简介之后的全部非空条目（目录文档除外），'
        + '番外、后日谈之类的特殊标题不会被漏掉；预览只读取前 320 字。' }),
      table));
    parts.push(previewBox);
    parts.push(el('div', { class: 'card' },
      el('h3', {}, '导入设置'),
      el('div', { class: 'row' }, introToggle, labeled('简介条目', introSelect)),
      el('div', { class: 'row mt' },
        labeled('作品名', workInput),
        labeled('正文起始章号', startInput),
        overwriteBox,
        el('span', { class: 'spacer' }),
        commitBtn),
      el('div', { class: 'small muted mt', text:
        '勾选「作为简介章节导入」后，该条目会作为作品的开头一章单独导入，'
        + '不再计入正文；正文章节按勾选顺序依次编号。' })));
  }

  content.className = 'content';
  content.replaceChildren(...parts.filter(Boolean));
  $('#view-title').textContent = '导入 EPUB';
  $('#view-meta').textContent = st.scan ? `${st.selected.size} / ${st.scan.items.length} 章已选` : '选择文件开始';
}

/** 取消导入：丢弃解析结果与勾选，回到重新选择文件的状态。 */
/** 把导入页恢复到"还没选文件"的状态（取消导入、导入成功之后都要清）。 */
function resetImportState() {
  const st = importState;
  st.fileName = ''; st.scan = null; st.selected = new Set();
  st.error = ''; st.previewIndex = null; st.busy = false;
  st.includeIntro = true; st.introIndex = null;
  st.work = ''; st.startNumber = 1; st.overwrite = false; st.onlyNumber = null;
}

function cancelImport() {
  resetImportState();
  toast('已取消导入', '');
  renderImport();
}

/** 文件名 → 作品名：保留中文等文字，只清掉路径与文件系统不允许的字符。 */
function sanitizeWorkName(fileName) {
  const base = String(fileName || '').replace(/\.epub$/i, '');
  // 去掉路径分隔符与控制字符，并把 Windows/Unix 的保留字符换成 ·
  const cleaned = base
    .replace(/[\\/]/g, ' ')
    .replace(/[:*?"<>|]/g, '·')
    .replace(/[\x00-\x1f]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\.+$/, '');
  const name = (cleaned || 'imported').slice(0, 40).replace(/[. ]+$/, '');
  return name || 'imported';
}

async function uploadEpub(file) {
  const st = importState;
  st.busy = true; st.error = '';
  renderImport();
  try {
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
      reader.onerror = () => reject(new Error('读取文件失败'));
      reader.readAsDataURL(file);
    });
    const res = await post('/api/import/epub/scan', { filename: file.name, data: base64 });
    if (!res.ok) {
      st.error = res.error || '解析失败';
      st.scan = null;
    } else {
      st.scan = res;
      st.fileName = file.name;
      st.includeIntro = res.suggested_intro !== null && res.suggested_intro !== undefined;
      st.introIndex = res.suggested_intro;
      // 默认勾选所有"看起来是正文"的条目：非空、不是目录/导航文档、排在简介之后。
      // 不按"标题里有没有数字"来筛 —— 番外、后日谈、尾声这类标题会被误伤。
      st.selected = new Set(res.items
        .filter((i) => !i.empty && !i.toc)
        .filter((i) => !st.includeIntro || i.index > st.introIndex)
        .map((i) => i.index));
      st.previewIndex = null;
      if (!st.work) st.work = sanitizeWorkName(file.name);
    }
  } catch (error) {
    st.error = error.message;
  }
  st.busy = false;
  renderImport();
}


export { cancelImport, importState, renderImport, resetImportState, sanitizeWorkName, uploadEpub };
