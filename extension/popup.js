/**
 * 弹窗逻辑：显示桥接连接状态、待应用改动，以及最近的变动记录。
 *
 * 注意：本文件是 <script type="module">，BmsyncCore 通过 import 取得。
 * 之前它是普通 script 而 core.js 已改成 ES module（只 export、不挂全局），
 * 于是报 "BmsyncCore is not defined"。凡是访问它的页面都必须 import。
 */
import * as BmsyncCore from './core.js';

const $ = (id) => document.getElementById(id);

/** 空安全的文本设置：元素缺失时静默跳过，避免整个弹窗因一个节点报错而空白 */
function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function renderState(report) {
  const badge = $('state');
  if (badge) {
    if (report && report.online) {
      badge.textContent = '已连接';
      badge.className = 'badge ok';
    } else {
      badge.textContent = '未连接';
      badge.className = 'badge bad';
    }
  }
  setText('at', report && report.at ? new Date(report.at).toLocaleTimeString() : '-');
  const err = $('err');
  if (err) {
    if (report && report.error) {
      err.style.display = '';
      err.textContent = '错误：' + report.error;
    } else {
      err.style.display = 'none';
    }
  }
}

function renderPending(pending) {
  const d = BmsyncCore.describePending(pending);
  const rows = [`<li><div class="row"><span>${esc(d.text)}</span>` +
                (d.total ? `<span class="badge">${d.total} 项</span>` : '') + '</div></li>'];
  const adds = (pending && pending.add) || [];
  const removes = (pending && pending.remove) || [];
  const list = adds.concat(removes);
  const show = list.slice(0, 4);
  for (const it of show) {
    const tag = adds.includes(it) ? '<span class="plus">＋</span>' : '<span class="minus">－</span>';
    rows.push(`<li class="dim">${tag} <span class="url" title="${esc(it.url)}">` +
              `${esc(it.title || it.url)}</span></li>`);
  }
  if (list.length > show.length) {
    rows.push(`<li class="dim">…其余 ${list.length - show.length} 项</li>`);
  }
  setHTML('pending', rows.join(''));
}

function renderEvents(events) {
  if (!events || !events.length) {
    setHTML('events', '<li class="dim empty">还没有同步记录</li>');
    return;
  }
  const rows = events.slice(0, 12).map((ev) => {
    const d = BmsyncCore.describeEvent(ev);
    if (!d) return '';
    let what;
    if (d.bits) {
      what = d.bits.map((b) => `<span class="${b.cls}">${esc(b.label)} ${b.count}</span>`).join('　');
    } else {
      what = esc(d.text);
    }
    const time = esc(String(d.at).replace(/^\d{4}-\d{2}-\d{2}\s*/, ''));
    return `<li class="ev"><div class="when"><span class="dim">${time}</span>` +
           `<span class="dim">${ev.kind === 'applied' ? 'Chrome' : '同步'}</span></div>` +
           `<div class="what">${what}</div></li>`;
  }).filter(Boolean);
  setHTML('events', rows.length ? rows.join('') : '<li class="dim empty">还没有同步记录</li>');
}

function ask(message) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(message, (resp) => {
        if (chrome.runtime.lastError) resolve(null);
        else resolve(resp);
      });
    } catch (e) {
      resolve(null);
    }
  });
}

async function refresh() {
  const report = await ask({ type: 'status' });
  renderState(report);
  const recent = await ask({ type: 'recent' });
  if (recent) {
    renderPending(recent.pending);
    renderEvents(recent.events);
  } else {
    renderPending(null);
    renderEvents(null);
  }
}

// 等 DOM 就绪再操作（脚本在 body 末尾，但显式等待更稳）
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', refresh);
} else {
  refresh();
}
