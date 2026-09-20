/* terms.js — 术语高亮匹配（由 static/app.js 拆分而来） */

import { $, el } from './dom.js';
import { state } from './state.js';

function highlight(text, query) {
  if (!query) return document.createTextNode(text);
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const frag = document.createDocumentFragment();
  let index = 0;
  for (;;) {
    const found = lower.indexOf(q, index);
    if (found < 0) { frag.append(text.slice(index)); break; }
    if (found > index) frag.append(text.slice(index, found));
    frag.append(el('mark', { text: text.slice(found, found + q.length) }));
    index = found + q.length;
  }
  return frag;
}

/** 术语配色：用黄金角在色相环上取样，词条之间颜色差异最大化，
 *  而且词条再多也不会像固定调色板那样很快撞色。 */
const GOLDEN_ANGLE = 137.508;

function termColorVars(index) {
  const hue = ((index * GOLDEN_ANGLE) % 360 + 360) % 360;
  const h = hue.toFixed(1);
  return {
    '--tc': `hsl(${h}, 80%, 70%)`,
    '--tcbg': `hsla(${h}, 80%, 58%, 0.30)`,   // 中文侧底色重一些
    '--tcbg-en': `hsla(${h}, 80%, 58%, 0.18)`, // 英文侧更淡，避免影响阅读
  };
}

/** 一侧的匹配器：长词优先，匹配到的文本映射回术语下标。 */
function buildSide(entries, caseInsensitive) {
  const map = new Map();
  for (const entry of entries) {
    const key = caseInsensitive ? entry.text.toLowerCase() : entry.text;
    if (key && !map.has(key)) map.set(key, entry.index);
  }
  const keys = [...map.keys()].sort((a, b) => b.length - a.length).slice(0, 500);
  if (!keys.length) return null;
  const escape = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  try {
    return { re: new RegExp(`(${keys.map(escape).join('|')})`, caseInsensitive ? 'gi' : 'g'), map };
  } catch {
    return null;
  }
}

/** 预扫描英文正文：哪些术语的译法真的按记录写了。
 *  没命中的术语会在中文侧用虚线标出，提示"术语库记的译法没有出现在译文里"。 */
function computeEnglishPresence(matcher, text) {
  const present = new Set();
  if (!matcher || !matcher.en || !text) return present;
  matcher.en.re.lastIndex = 0;
  let match;
  while ((match = matcher.en.re.exec(text)) !== null) {
    const hit = match[0];
    if (!hit.length) { matcher.en.re.lastIndex += 1; continue; }
    const index = matcher.en.map.get(hit.toLowerCase());
    if (index !== undefined) present.add(index);
  }
  return present;
}

/** 为本章术语建立"中文词条 + 英文译法"两侧匹配器，同一术语共用一个颜色。 */
function buildTermMatcher(terms) {
  const list = terms || [];
  const zh = [];
  const en = [];
  list.forEach((item, index) => {
    if (!item) return;
    if (item.term) zh.push({ text: item.term, index });
    (item.translations || []).forEach((text) => { if (text) en.push({ text, index }); });
  });
  const matcher = { terms: list, zh: buildSide(zh, false), en: buildSide(en, true), englishPresent: new Set() };
  return matcher;
}

/** 同时渲染"搜索命中"与"术语命中"：搜索优先，其余片段再按术语切分。
 *  side 决定用中文词条还是英文译法匹配，命中的术语带同一个颜色。 */
function decorate(text, query, matcher, side) {
  const frag = document.createDocumentFragment();
  const sideMatcher = matcher ? (side === 'en' ? matcher.en : matcher.zh) : null;

  const emitTerms = (segment) => {
    if (!segment) return;
    if (!sideMatcher) { frag.append(segment); return; }
    sideMatcher.re.lastIndex = 0;
    let last = 0;
    let match;
    while ((match = sideMatcher.re.exec(segment)) !== null) {
      const hit = match[0];
      if (!hit.length) { sideMatcher.re.lastIndex += 1; continue; }
      if (match.index > last) frag.append(segment.slice(last, match.index));

      const key = side === 'en' ? hit.toLowerCase() : hit;
      const index = sideMatcher.map.has(key) ? sideMatcher.map.get(key) : -1;
      const info = index >= 0 ? matcher.terms[index] : null;
      const missingEn = side === 'zh' && index >= 0
        && matcher.englishPresent && matcher.englishPresent.size >= 0
        && !matcher.englishPresent.has(index);
      frag.append(el('span', {
        class: `term-hl${side === 'en' ? ' en-side' : ''}${missingEn ? ' no-en' : ''}`,
        style: termColorVars(index),
        'data-term': info ? info.term : hit,
        'data-translation': info ? (info.translation || '') : '',
        text: hit,
      }));
      last = match.index + hit.length;
    }
    if (last < segment.length) frag.append(segment.slice(last));
  };

  if (!query) { emitTerms(text); return frag; }

  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  let index = 0;
  for (;;) {
    const found = lower.indexOf(q, index);
    if (found < 0) { emitTerms(text.slice(index)); break; }
    if (found > index) emitTerms(text.slice(index, found));
    frag.append(el('mark', { text: text.slice(found, found + q.length) }));
    index = found + q.length;
  }
  return frag;
}

/** 重新装饰阅读区所有段落（搜索词或术语开关变化时调用）。 */
function redecorateReader() {
  const body = $('#reader-body');
  if (!body) return;
  const matcher = state.reader.termsOn ? state.reader.matcher : null;
  const query = state.reader.query;
  const paint = (cell, side) => {
    const raw = cell.dataset.raw !== undefined ? cell.dataset.raw : cell.textContent;
    cell.dataset.raw = raw;
    cell.replaceChildren(decorate(raw, query, matcher, side));
  };
  body.querySelectorAll('.zh .para').forEach((cell) => paint(cell, 'zh'));
  body.querySelectorAll('.en .para').forEach((cell) => paint(cell, 'en'));
}


export { GOLDEN_ANGLE, buildSide, buildTermMatcher, computeEnglishPresence, decorate, highlight, redecorateReader, termColorVars };
