/* settings.js — 环境配置（由 static/app.js 拆分而来） */

import { $, el, labeled } from './dom.js';
import { api, apiKeyReady, post, toast, toolProxyUrl } from './api.js';
import { state } from './state.js';
import { loadBootstrap } from './loader.js';

/* ------------------------------------------------------------ 环境配置 */

/**
 * 模型 API：地址 / 模型 ID / Key。
 *
 * 三项配置（地址 / 模型 ID / Key）统一保存，命令行工具与面板共用同一份，不用两边各设一遍。
 * 地址与模型名不是密钥，直接回显当前值；Key 只显示"已配置"与长度。
 */
function buildModelCard(st) {
  const keys = st.status.keys || {};
  const baseUrl = keys.API_BASE_URL || {};
  const modelKey = keys.API_MODEL || {};
  const apiKey = keys.API_KEY || {};

  const urlInput = el('input', {
    type: 'text', value: baseUrl.value || '', style: { width: '100%' },
    placeholder: baseUrl.default || '',
  });
  const modelInput = el('input', {
    type: 'text', value: modelKey.value || '', style: { width: '100%' },
    placeholder: modelKey.default || '',
  });
  const keyInput = el('input', {
    type: 'password', style: { width: '100%' },
    placeholder: apiKey.configured ? `已配置（${apiKey.length} 字符）·留空则不修改` : 'sk-…',
  });
  const feedback = el('div', { class: 'small muted', style: { minHeight: '16px' } });

  const save = el('button', {
    class: 'sm primary', text: '保存模型配置',
    onclick: async (event) => {
      event.target.disabled = true;
      const payload = { API_BASE_URL: urlInput.value.trim(), API_MODEL: modelInput.value.trim() };
      if (keyInput.value.trim()) payload.API_KEY = keyInput.value.trim();
      const res = await post('/api/settings/credentials', payload);
      event.target.disabled = false;
      if (!res.ok) { toast(res.error || '保存失败', 'bad'); return; }
      toast('模型配置已保存', 'ok');
      settingsState.status = null;          // 强制重新拉取
      apiKeyReady = null;                   // 预检缓存也要失效
      renderSettings();
    },
  });

  const reset = el('button', {
    class: 'sm', text: '恢复默认',
    title: '清空这两项，回到内置默认值',
    onclick: async () => {
      const res = await post('/api/settings/credentials', { API_BASE_URL: '', API_MODEL: '' });
      if (!res.ok) { toast(res.error || '失败', 'bad'); return; }
      toast('已恢复默认', 'ok');
      settingsState.status = null;
      apiKeyReady = null;
      renderSettings();
    },
  });

  return el('div', { class: 'card' },
    el('h3', {}, '模型 API ', el('span', { class: 'sub', text: '全局配置' })),
    el('div', { class: 'small muted', text:
      '默认走 DeepSeek（https://api.deepseek.com）。任何 OpenAI 兼容服务都可以：'
      + '改这里的地址与模型 ID 即可，翻译与校对都使用这份配置。' }),
    el('div', { class: 'modal-grid', style: { marginTop: '10px' } },
      labeled('API 地址', urlInput),
      labeled('模型 ID', modelInput),
      labeled('API Key', keyInput)),
    el('div', { class: 'row mt' },
      save, reset,
      el('span', { class: 'spacer' }),
      el('span', { class: 'small muted', text:
        `内置默认：${baseUrl.default || ''} · ${modelKey.default || ''}` })),
    feedback);
}

const settingsState = { status: null, verify: null, tools: [], health: null };

/** 一条配置状态：整行横向铺满，左侧键名与说明，右侧状态与详情。 */
function buildStatusRow(name, info) {
  const tone = info.configured ? (info.optional ? 'info' : 'ok') : (info.optional ? 'info' : 'bad');
  const label = info.configured ? (info.optional ? '已自定义' : '已配置')
    : (info.optional ? '使用默认' : '未配置');
  const detail = info.optional
    ? (info.configured ? info.value : `默认：${info.default}`)
    : (info.configured ? `${info.length} 字符 · ${info.prefix}` : '尚未填入');
  return el('div', { class: 'status-row' },
    el('div', { class: 'status-text' },
      el('div', { class: 'status-name', text: name }),
      el('div', { class: 'small muted', text: info.description || '' })),
    el('div', { class: 'status-right' },
      el('span', { class: `pill ${tone}`, text: label }),
      el('div', { class: 'status-detail', text: detail })));
}

async function renderSettings() {
  const content = $('#content');
  if (!settingsState.status) settingsState.status = await api('/api/settings/credentials');
  const [toolsRes, healthRes] = await Promise.all([api('/api/tools'), api('/api/health')]);
  settingsState.tools = toolsRes.tools || [];
  settingsState.health = healthRes;
  const st = settingsState;
  const status = st.status || {};
  const keys = status.keys || {};

  const statusRows = Object.entries(keys).map(([name, info]) => buildStatusRow(name, info));

  const verifyBtn = el('button', {
    class: 'sm', text: '验证',
    title: '用当前 API Key 与地址向模型服务发一次最小请求，确认配置可用',
    onclick: async (event) => {
      event.target.disabled = true; event.target.textContent = '验证中…';
      st.verify = await post('/api/settings/credentials/verify', {});
      event.target.disabled = false; event.target.textContent = '验证';
      renderSettings();
    },
  });

  const logoutBtn = el('button', {
    class: 'sm danger', text: '退出登录',
    onclick: () => { location.href = '/logout'; },
  });

  const parts = [
    el('div', { class: 'banner info', text:
      '这里只显示"是否已配置"，绝不回显密钥内容。保存时只改动对应的一项，'
      + '其余配置原样保留，并自动备份。' }),
    el('div', { class: 'grid c2' },
      el('div', { class: 'card' },
        el('h3', {}, '当前状态 ', el('span', { class: 'sub',
          text: status.env_mtime ? `最近修改 ${status.env_mtime}` : '尚未配置' })),
        el('div', { class: 'status-list' }, ...statusRows),
        el('div', { class: 'row mt' }, verifyBtn,
          state.authRequired ? logoutBtn : null),
        st.verify ? el('div', { class: `banner ${st.verify.ok ? 'ok' : 'bad'} mt`,
          text: st.verify.ok ? st.verify.message : st.verify.error }) : null)),
    buildModelCard(st),
    el('div', { class: 'card' },
      el('h3', {}, '外部工具地址 ', el('span', { class: 'sub', text: '可选 · 第三方' })),
      el('div', { class: 'small muted', text:
        '这是可选的第三方工具，本项目不附带、不内嵌。请只用于你自己拥有版权或已获授权的作品；'
        + '把它用于下载、传播他人作品可能侵权，后果由使用者自负。安装方式见 ' }),
      el('div', { class: 'small', style: { marginTop: '2px' } },
        el('a', {
          href: 'https://github.com/zhongbai2333/Tomato-Novel-Downloader',
          target: '_blank', rel: 'noopener noreferrer',
          text: 'github.com/zhongbai2333/Tomato-Novel-Downloader',
        })),
      ...st.tools.map((tool) => {
        const input = el('input', { type: 'text', value: tool.url, style: { width: '300px' } });
        const dot = el('span', { class: `tool-dot ${tool.reachable ? 'on' : 'off'}` });
        return el('div', { class: 'row mt' },
          el('span', { class: 'small', style: { minWidth: '110px' }, text: tool.label }),
          dot,
          input,
          el('button', {
            class: 'sm', text: '保存',
            onclick: async (event) => {
              event.target.disabled = true;
              const res = await post('/api/tools', { id: tool.id, url: input.value });
              event.target.disabled = false;
              if (!res.ok) { toast(res.error || '保存失败', 'bad'); return; }
              toast('已保存', 'ok');
              await loadBootstrap();
              renderSettings();
            },
          }),
          el('button', {
            class: 'sm primary', text: state.toolProxy ? '打开（经面板）' : '打开',
            onclick: () => window.open(toolProxyUrl(tool, state.toolProxy), '_blank', 'noopener,noreferrer'),
          }),
          state.toolProxy ? el('button', {
            class: 'sm', text: '直连',
            onclick: () => window.open(tool.url, '_blank', 'noopener,noreferrer'),
          }) : null,
          el('span', { class: 'small muted', text: tool.reachable ? '服务可访问' : '服务未启动' }));
      })),
    !state.toolProxy ? el('div', { class: 'banner warn', text:
      '当前配置下，侧栏「工具」会直接访问上面填写的地址。' }) : null,
  ];

  content.className = 'content';
  content.replaceChildren(...parts.filter(Boolean));
  $('#view-title').textContent = state.authRequired ? '环境配置 · 远程模式' : '环境配置';
  $('#view-meta').textContent = status.all_set ? '全部已配置' : '存在未配置项';
}



export { buildModelCard, buildStatusRow, renderSettings, settingsState };
