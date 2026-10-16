/**
 * bmsync 桥接扩展
 *
 * 作用：Chrome 运行时把书签放在内存里，退出/保存时整份重写 Bookmarks 文件，
 * 所以「直接改文件」对运行中的 Chrome 无效（几秒后就被覆盖）。本扩展用官方的
 * chrome.bookmarks API 应用来自 bmsync 的改动，这样两个浏览器都开着也能同步。
 *
 * 工作方式：每 POLL_MS 向本机桥接服务拉一次「需要新增/删除/改名什么」，应用后回报。
 * 只连 127.0.0.1，不发送任何数据到外部。
 */

const POLL_MS = 20000;          // 轮询间隔；bmsync 的桥接是本地回环，开销可忽略
const PORTS = [8765, 8766, 8767];   // 依次尝试，兼容用户改过端口的情况
const BAR = '书签栏';
const OTHER = '其他书签';

let baseUrl = null;
let lastReport = null;

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

async function getDelta() {
  const base = await findBridge();
  if (!base) return null;
  const r = await fetch(base + '/delta', { cache: 'no-store' });
  if (!r.ok) throw new Error('delta 请求失败：' + r.status);
  return r.json();
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

/** 找到（必要时创建）某个容器下的目录路径，例如 ["其他书签","工具","子目录"] */
async function ensureFolder(containerTitle, pathParts) {
  const roots = await chrome.bookmarks.getTree();
  const root = roots[0];
  const wanted = containerTitle === BAR ? BAR : OTHER;
  let node = (root.children || []).find(c => c.title === wanted) || root.children[0];
  for (const part of pathParts || []) {
    if (!part) continue;
    const children = await chrome.bookmarks.getChildren(node.id);
    let next = children.find(c => !c.url && c.title === part);
    if (!next) next = await chrome.bookmarks.create({ parentId: node.id, title: part });
    node = next;
  }
  return node.id;
}

/** 按 URL 建立索引，便于查找与去重 */
async function indexByUrl() {
  const tree = await chrome.bookmarks.getTree();
  const map = new Map();
  const walk = (node) => {
    if (node.url) {
      if (!map.has(node.url)) map.set(node.url, node);
      return;
    }
    (node.children || []).forEach(walk);
  };
  tree.forEach(walk);
  return map;
}

/** 把 bmsync 的差异应用到 Chrome */
async function applyDelta(delta) {
  if (!delta || delta.error) return { ok: false, error: delta && delta.error };
  const index = await indexByUrl();
  const result = { added: 0, removed: 0, renamed: 0, errors: [] };

  for (const item of delta.add || []) {
    if (index.has(item.url)) continue;              // 已经有了（可能是另一方向带过来的）
    try {
      const parentId = await ensureFolder(item.path && item.path[0] === BAR ? BAR : OTHER,
                                         (item.path || []).slice(1));
      await chrome.bookmarks.create({ parentId, title: item.title || item.url, url: item.url });
      result.added++;
    } catch (e) {
      result.errors.push(`add ${item.url}: ${e.message}`);
    }
  }

  for (const item of delta.remove || []) {
    const node = index.get(item.url);
    if (!node) continue;                            // 已经不在了
    try {
      await chrome.bookmarks.remove(node.id);
      result.removed++;
    } catch (e) {
      result.errors.push(`remove ${item.url}: ${e.message}`);
    }
  }

  for (const item of delta.rename || []) {
    const node = [...index.values()].find(n => n.url === item.url && n.title === item.currentTitle);
    if (!node) continue;
    try {
      await chrome.bookmarks.update(node.id, { title: item.title });
      result.renamed++;
    } catch (e) {
      result.errors.push(`rename ${item.url}: ${e.message}`);
    }
  }
  return result;
}

let running = false;

async function tick(force) {
  if (running) return lastReport;                   // 避免上一次还没跑完就重入
  running = true;
  try {
    const delta = await getDelta();
    if (!delta) {
      lastReport = { at: Date.now(), online: false };
      await setBadge('off', '#9e9e9e');
      return lastReport;
    }
    const counts = delta.counts || {};
    const nothing = !counts.add && !counts.remove && !counts.rename;
    let result = { added: 0, removed: 0, renamed: 0, errors: [] };
    if (!nothing) result = await applyDelta(delta);
    lastReport = { at: Date.now(), online: true, delta: counts, result };
    await setBadge(result.added + result.removed + result.renamed);
    if (!nothing || force) await reportApplied({ ...counts, ...result });
    return lastReport;
  } catch (e) {
    lastReport = { at: Date.now(), online: false, error: String(e) };
    await setBadge('err', '#d32f2f');
    return lastReport;
  } finally {
    running = false;
  }
}

async function setBadge(value, color) {
  try {
    if (color) await chrome.action.setBadgeBackgroundColor({ color });
    else await chrome.action.setBadgeBackgroundColor({ color: '#2e7d32' });
    await chrome.action.setBadgeText({ text: value === undefined ? '' : String(value) });
  } catch (e) { /* 某些环境下 action 不可用，忽略 */ }
}

// 定时轮询（service worker 里 setInterval 可用；被回收后由事件重新唤醒）
setInterval(() => tick(false), POLL_MS);
chrome.runtime.onStartup.addListener(() => tick(true));
chrome.runtime.onInstalled.addListener(() => tick(true));

// 供 popup 查询
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'status') {
    tick(true).then(sendResponse);
    return true;                                    // 异步响应
  }
  return false;
});

tick(true);
