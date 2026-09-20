/* api.js — HTTP 调用与轻提示（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';

const nf = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString('en-US'));

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
  if (res.status === 401) {
    // 令牌缺失/过期（远程部署时）：跳登录页，登录后回到当前地址。
    const next = encodeURIComponent(location.pathname + location.hash);
    location.href = `/login?next=${next}`;
    return { ok: false, error: '需要登录', auth_required: true };
  }
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: `HTTP ${res.status}` }; }
  if (!res.ok && data && data.ok === undefined) data.ok = false;
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

/** 外部工具入口地址：面板开启反代时走**同源** `/tools/<id>/`。
 *  云服务器上直连 `127.0.0.1:18423` 指的是用户自己的电脑，必然打不开；
 *  反代由服务端转发到 tools.json 里配置的地址，所以不用对外开放该端口。 */
function toolProxyUrl(tool, proxied) {
  if (proxied === false) return tool.url;
  return `/tools/${encodeURIComponent(tool.id)}/`;
}

function toast(message, kind = '') {
  const host = $('#toast-host');
  if (!host) return;
  const node = el('div', { class: `toast ${kind}`, text: message });
  // append 到末尾：容器是自下而上的列，所以新提示出现在最下方，旧的依次上移
  host.append(node);

  const life = kind === 'bad' ? 9000 : 4500;
  const fade = 280;                       // 与 .toast.leaving 的动画时长一致
  setTimeout(() => {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), fade);
  }, life - fade);
}

/** 模型 API Key 是否已配置（结果缓存，改配置后由设置页清空）。 */
let apiKeyReady = null;

async function ensureModelApiKey() {
  if (apiKeyReady === null) {
    const res = await api('/api/settings/credentials');
    const key = (res && res.keys && res.keys.API_KEY) || {};
    apiKeyReady = !!key.configured;
  }
  if (apiKeyReady) return true;
  toast('还没有配置模型 API Key，请先到「环境配置」填入并保存。', 'bad');
  return false;
}


export { api, apiKeyReady, ensureModelApiKey, nf, post, toast, toolProxyUrl };
