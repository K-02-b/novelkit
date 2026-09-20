/* jobs.js — 后台任务日志（SSE）（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { api, post, toast } from './api.js';
import { state } from './state.js';
import { refreshChapterInPlace } from './reader.js';
import { route } from './router.js';
import { loadBootstrap } from './loader.js';


/** 自更新的任务日志（SSE 推送）。
 *
 * 默认**收起**，只留一行状态 + 最后一条输出——展开的终端会吃掉大半个阅读区。
 * 失败时自动展开（这时你正需要看报错），成功后自动收起。
 *
 * 两个必须小心的地方（都踩过坑）：
 *  1. 任务进入终态后**只能触发一次页面刷新**。否则每次重绘都会重新发现"任务已完成"，
 *     又排一次刷新，形成无限重载。用 refreshedJobs 记录已消费的任务。
 *  2. 每次重绘都会新建组件，必须先把上一个同 id 的连接关掉，否则 EventSource 会越堆越多。
 */
const jobWatchers = new Map();     // jobId -> EventSource
const refreshedJobs = new Set();   // 已经因它刷新过页面的任务

function makeJobLog(jobId) {
  let expanded = false;
  let autoExpanded = false;
  let finished = false;      // 终态只处理一次（见上方注释 1）

  const statePill = el('span', { class: 'pill info', text: '运行中…' });
  const preview = el('span', { class: 'log-preview mono', text: '' });
  const toggle = el('button', {
    class: 'sm', text: '展开日志',
    onclick: () => { expanded = !expanded; applyExpanded(); },
  });
  const cancel = el('button', {
    class: 'sm', text: '取消任务',
    onclick: async (event) => {
      event.target.disabled = true;
      await post(`/api/jobs/${jobId}/cancel`, {});
      toast('已请求取消', '');
    },
  });

  const box = el('div', { class: 'log small', text: '等待输出…' });
  const body = el('div', { class: 'job-log-body', style: { display: 'none' } }, box);

  function applyExpanded() {
    body.style.display = expanded ? '' : 'none';
    toggle.textContent = expanded ? '收起日志' : '展开日志';
  }

  const head = el('div', { class: 'row small job-log-head' },
    statePill, preview, el('span', { class: 'spacer' }), toggle, cancel);

  function updatePreview() {
    const lines = box.textContent.split('\n').map((line) => line.trim()).filter(Boolean);
    const last = lines.length ? lines[lines.length - 1] : '';
    preview.textContent = last.length > 90 ? last.slice(0, 90) + '…' : last;
  }

  function setLog(text) {
    box.textContent = text || '（暂无输出）';
    if (expanded) box.scrollTop = box.scrollHeight;
    updatePreview();
  }

  function appendLog(chunk) {
    const current = box.textContent;
    box.textContent = (current && current !== '等待输出…' && current !== '（暂无输出）')
      ? current + chunk
      : chunk;
    if (expanded) box.scrollTop = box.scrollHeight;
    updatePreview();
  }

  const stop = () => {
    const source = jobWatchers.get(jobId);
    if (source) { source.close(); jobWatchers.delete(jobId); }
  };

  /** 任务进入终态：更新徽标、必要时自动展开，并只触发一次内容刷新。 */
  function finish(job) {
    if (finished || !job) return;
    finished = true;

    statePill.className = job.state === 'done' ? 'pill ok' : 'pill warn';
    statePill.textContent = job.state === 'done'
      ? '完成'
      : (job.state === 'cancelled' ? '已取消' : `失败（退出码 ${job.returncode}）`);
    cancel.remove();
    stop();

    if (job.state === 'failed' && !autoExpanded) {
      autoExpanded = true; expanded = true; applyExpanded();
    }
    // 只在第一次看到终态时更新内容，避免无限重载。
    // 注意：这里**不能** route()/loadBootstrap() ——那会重建整页，
    // 导致阅读区闪烁并把用户的滚动位置清零。只重取本章、按行最小化更新。
    if (!refreshedJobs.has(jobId)) {
      refreshedJobs.add(jobId);
      toast(job.state === 'done' ? '任务完成，已更新译文' : '任务已结束',
            job.state === 'done' ? 'ok' : 'bad');
      // 侧栏的"已译/总数"也要更新；loadBootstrap 只重画侧栏与徽标，不碰正文
      loadBootstrap().then(() => refreshChapterInPlace({ animateChanges: job.state === 'done' }));
    }
  }

  /** 打开 SSE：服务端推 init / append / end，浏览器断线会自己重连。 */
  const startStream = () => {
    stop();  // 清掉可能残留的同 id 连接
    const source = new EventSource(`/api/jobs/${jobId}/stream`);
    jobWatchers.set(jobId, source);

    source.addEventListener('init', (event) => {
      let payload = null;
      try { payload = JSON.parse(event.data); } catch { return; }
      setLog(payload.log || '');
      const job = payload.job;
      if (job && job.state !== 'running') return finish(job);
      statePill.className = 'pill info';
      statePill.textContent = `运行中 · ${job ? job.label : ''}`;
    });

    source.addEventListener('append', (event) => {
      let payload = null;
      try { payload = JSON.parse(event.data); } catch { return; }
      appendLog(payload.chunk || '');
    });

    source.addEventListener('end', (event) => {
      let payload = null;
      try { payload = JSON.parse(event.data); } catch { /* 忽略坏帧 */ }
      const job = payload && payload.job;
      if (!job) {
        statePill.className = 'pill warn';
        statePill.textContent = '任务已丢失';
        stop();
        return;
      }
      finish(job);
    });

    // EventSource 断线会自动重连；只有任务真的不在了才收手（否则会无限重连）
    source.onerror = async () => {
      if (finished || source.readyState === EventSource.CLOSED) return;
      const res = await api(`/api/jobs/${jobId}`).catch(() => null);
      if (!res || !res.ok) {
        statePill.className = 'pill warn';
        statePill.textContent = '任务已丢失';
        stop();
      }
    };
  };

  applyExpanded();
  startStream();
  return el('div', { class: 'mt job-log' }, head, body);
}

/** 统一的"启动任务"入口：挂上日志，但不重绘页面。 */
function mountJob(job) {
  const slot = document.querySelector('#job-slot');
  if (!slot) return;
  slot.replaceChildren(makeJobLog(job.id));
  // 故意不 scrollIntoView：启动任务不该把用户正在读的位置拽走，
  // 日志出现的提示交给 toast，想看日志自己滚下去就行。
}

export { jobWatchers, makeJobLog, mountJob, refreshedJobs };
