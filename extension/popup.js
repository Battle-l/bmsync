/** 弹窗：显示桥接连接状态、待应用改动，以及最近的变动记录 */
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function renderState(report, recent) {
  const badge = $('state');
  if (report && report.online) {
    badge.textContent = '已连接';
    badge.className = 'badge ok';
  } else {
    badge.textContent = '未连接';
    badge.className = 'badge bad';
  }
  $('at').textContent = report && report.at ? new Date(report.at).toLocaleTimeString() : '-';
  if (report && report.error) {
    $('err').style.display = '';
    $('err').textContent = '错误：' + report.error;
  }
  if (!recent) return;
}

function renderPending(pending) {
  const el = $('pending');
  const d = BmsyncCore.describePending(pending);
  const rows = [];
  rows.push(`<li><div class="row"><span>${esc(d.text)}</span>` +
            (d.total ? `<span class="badge">${d.total} 项</span>` : '') + '</div></li>');
  const list = (pending && (pending.add || pending.remove)) || [];
  const show = list.slice(0, 4);
  for (const it of show) {
    const tag = (pending.add || []).includes(it)
      ? '<span class="plus">＋</span>' : '<span class="minus">－</span>';
    rows.push(`<li class="dim">${tag} <span class="url" title="${esc(it.url)}">${esc(it.title || it.url)}</span></li>`);
  }
  if (list.length > show.length) {
    rows.push(`<li class="dim">…其余 ${list.length - show.length} 项</li>`);
  }
  el.innerHTML = rows.join('');
}

function renderEvents(events) {
  const el = $('events');
  if (!events || !events.length) {
    el.innerHTML = '<li class="dim empty">还没有同步记录</li>';
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
  el.innerHTML = rows.length ? rows.join('') : '<li class="dim empty">还没有同步记录</li>';
}

function refresh() {
  chrome.runtime.sendMessage({ type: 'status' }, (report) => {
    if (chrome.runtime.lastError) {
      renderState(null, null);
      renderPending(null);
      renderEvents(null);
      return;
    }
    renderState(report, null);
    // 直接向桥接要"最近变动"（含待应用项），拿到更细的信息
    chrome.runtime.sendMessage({ type: 'recent' }, (recent) => {
      if (chrome.runtime.lastError || !recent) {
        renderPending(null);
        renderEvents(null);
        return;
      }
      $('err').style.display = 'none';
      renderPending(recent.pending);
      renderEvents(recent.events);
    });
  });
}

refresh();
