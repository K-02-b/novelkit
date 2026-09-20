/* main.js — 入口（由 static/app.js 拆分而来） */

import { applyReaderSize } from './chapter-actions.js';
import { route } from './router.js';
import { loadBootstrap } from './loader.js';

window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', async () => {
  applyReaderSize();
  await loadBootstrap();
  await route();
});
