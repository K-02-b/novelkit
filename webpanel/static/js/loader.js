/* loader.js — 启动数据加载（由 static/app.js 拆分而来） */

import { api } from './api.js';
import { state } from './state.js';

/* ---------------------------------------------------------------- 启动 */

async function loadBootstrap() {
  const [data, tools, health] = await Promise.all([
    api('/api/works'), api('/api/tools'), api('/api/health'),
  ]);
  state.works = data.works || [];
  state.tools = tools.tools || [];
  state.authRequired = !!health.auth_required;
  state.toolProxy = health.tool_proxy !== false;
}

export { loadBootstrap };
