/**
 * bmsync 桥接扩展 —— 后台脚本
 *
 * 定时向本机桥接服务（bmsync watch 起的 127.0.0.1 服务）拉取"需要新增/删除/改名什么"，
 * 用官方书签 API 应用后回报结果。只连本机，不向任何外部服务器发送数据。
 *
 * 具体应用逻辑在 core.js（与 chrome API 解耦，可用 node 直接测试）。
 */

import * as BmsyncCore from './core.js';

const POLL_MS = 20000;              // 轮询间隔；bmsync 的桥接是本机回环，开销可忽略
const PORTS = [8765, 8766, 8767];   // 依次尝试，兼容用户改过端口的情况

let baseUrl = null;
let lastReport = null;
let currentRun = null;      // 正在进行的那一轮；重复调用应等它，而不是立刻返回上一轮结果

/** 探测桥接服务在哪个端口 */
async function findBridge() {
  if (baseUrl) {
    try {
      const r = await fetch(baseUrl + '/ping', { cache: 'no-store' });
      if (r.ok) return baseUrl;
    } catch (e) { /* 掉线了，重新探测 */ }
    baseUrl = null;
  }
  for (const port of PORTS) {
    const url = `http://127.0.0.1:${port}`;
    try {
      const r = await fetch(url + '/ping', { cache: 'no-store' });
      if (r.ok) { baseUrl = url; return url; }
    } catch (e) { /* 试下一个 */ }
  }
  return null;
}

async function reportApplied(info) {
  const base = await findBridge();
  if (!base) return;
  try {
    await fetch(base + '/applied', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(info),
    });
  } catch (e) { /* 报告失败不影响同步本身 */ }
}

async function setBadge(value, color) {
  try {
    if (color) await chrome.action.setBadgeBackgroundColor({ color });
    else await chrome.action.setBadgeBackgroundColor({ color: '#2e7d32' });
    await chrome.action.setBadgeText({ text: value === undefined ? '' : String(value) });
  } catch (e) { /* 某些环境下 action 不可用，忽略 */ }
}

/** Chrome 的书签 API 适配层，交给 core.js 使用 */
const chromeApi = {
  getTree: () => chrome.bookmarks.getTree(),
  getChildren: (id) => chrome.bookmarks.getChildren(id),
  create: (node) => chrome.bookmarks.create(node),
  remove: (id) => chrome.bookmarks.remove(id),
  update: (id, changes) => chrome.bookmarks.update(id, changes),
};

async function tick() {
  // 关键：上一轮还没跑完时要**等它**，不能直接返回 lastReport ——
  // 那会让调用方（弹窗、或紧随其后的定时器）拿到过期的结果（甚至是初始的 null）。
  if (currentRun) return currentRun;
  currentRun = (async () => {
      try {
      const base = await findBridge();
      if (!base) {
        lastReport = { at: Date.now(), online: false };
        await setBadge('off', '#9e9e9e');
        return lastReport;
      }
      const resp = await fetch(base + '/delta', { cache: 'no-store' });
      if (!resp.ok) throw new Error('delta 请求失败：' + resp.status);
      const delta = await resp.json();
      const counts = delta.counts || {};
      const nothing = !counts.add && !counts.remove && !counts.rename;

      let result = { added: 0, removed: 0, renamed: 0, errors: [] };
      if (!nothing) result = await BmsyncCore.applyDelta(chromeApi, delta);

      lastReport = { at: Date.now(), online: true, delta: counts, result };
      await setBadge((result.added + result.removed + result.renamed) || (nothing ? '' : 0));
      if (!nothing) await reportApplied(Object.assign({}, counts, result));
        return lastReport;
      } catch (e) {
        lastReport = { at: Date.now(), online: false, error: String(e) };
        await setBadge('err', '#d32f2f');
        return lastReport;
      } finally {
        currentRun = null;
      }
  })();
  return currentRun;
}

// 定时轮询（service worker 里 setInterval 可用；被回收后由事件重新唤醒）
setInterval(tick, POLL_MS);
chrome.runtime.onStartup.addListener(() => tick());
chrome.runtime.onInstalled.addListener(() => tick());

/** 向桥接要"最近变动"（待应用 + 同步历史 + 扩展回报），供弹窗展示 */
async function fetchRecent() {
  const base = await findBridge();
  if (!base) return null;
  try {
    const r = await fetch(base + '/recent', { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  }
}

// 供弹窗查询
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'status') {
    tick().then(sendResponse);
    return true;                                  // 异步响应
  }
  if (msg && msg.type === 'recent') {
    fetchRecent()
      .then((recent) => {
        // 待应用项数直接显示在图标角标上，不用点开也能看到有没有活要干
        const total = recent ? ((recent.counts && (recent.counts.add + recent.counts.remove
          + recent.counts.rename)) || 0) : 0;
        setBadge(total || '');
        sendResponse(recent);
      })
      .catch(() => sendResponse(null));
    return true;
  }
  return false;
});

tick();
