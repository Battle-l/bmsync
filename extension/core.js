/**
 * bmsync 桥接扩展 —— 核心逻辑（与 chrome.* API 解耦，便于用 node 直接测试）
 *
 * 作用：Chrome 运行时把书签放在内存里，退出/保存时整份重写 Bookmarks 文件，
 * 所以「直接改文件」对运行中的 Chrome 无效。本模块用官方的 chrome.bookmarks API
 * 应用来自 bmsync 的改动，这样两个浏览器都开着也能同步。
 *
 * 关键点：容器一律用 **稳定的文件夹 ID**（Chrome 固定把书签栏设为 "1"、其他书签设为 "2"），
 * 不要用标题 —— 标题随界面语言变化（英文是 "Bookmarks bar"），按标题找会失败。
 *
 * 导出为 ES module：MV3 的 service worker 用 `type: "module"` 加载 background.js，
 * 它再 `import * as BmsyncCore from './core.js'`。不要用 UMD/CommonJS 包装 ——
 * ESM 导入只能看到 export，看不到 module.exports，会导致 applyDelta 未定义。
 */
'use strict';


const CONTAINER_IDS = { bar: '1', other: '2' };   // Chrome 的固定 ID，与语言无关

/** 在指定容器下按路径逐级查找/创建目录，返回可直接用作 parentId 的节点 id */
async function ensureFolder(api, container, pathParts) {
    const parentId = CONTAINER_IDS[container] || CONTAINER_IDS.other;
    let cursor = parentId;
    for (const part of pathParts || []) {
      if (!part) continue;
      const children = await api.getChildren(cursor);
      let next = children.find(c => !c.url && c.title === part);
      if (!next) next = await api.create({ parentId: cursor, title: part });
      cursor = next.id;
    }
    return cursor;
}

/** 按 URL 建立索引（同一 URL 只记第一个，避免重复） */
async function indexByUrl(api) {
    const tree = await api.getTree();
    const map = new Map();
    const walk = (node) => {
      if (node.url) {
        if (!map.has(node.url)) map.set(node.url, node);
        return;
      }
      (node.children || []).forEach(walk);
    };
    (tree || []).forEach(walk);
    return map;
}

/**
   * 应用一次 delta。返回 {added, removed, renamed, errors}。
   *
   * 安全约定（与 bmsync 的桥接保持一致）：
   *   - add 是幂等的：已存在同 URL 就跳过
   *   - remove 只删 delta 明确点名的 URL（bmsync 只在"基线知道它、且 Safari 已删除"时才下发）
   *   - 任何单项失败都记进 errors，不影响其它条目
   */
async function applyDelta(api, delta) {
    if (!delta || delta.error) return { added: 0, removed: 0, renamed: 0, errors: [delta && delta.error] };
    const index = await indexByUrl(api);
    const result = { added: 0, removed: 0, renamed: 0, errors: [] };

    for (const item of delta.add || []) {
      if (!item || !item.url) continue;
      if (index.has(item.url)) continue;                 // 已经有了
      try {
        const parentId = await ensureFolder(api, item.container, item.path);
        await api.create({ parentId, title: item.title || item.url, url: item.url });
        result.added++;
      } catch (e) {
        result.errors.push('add ' + item.url + ': ' + (e && e.message));
      }
    }

    for (const item of delta.remove || []) {
      if (!item || !item.url) continue;
      const node = index.get(item.url);
      if (!node) continue;                               // 已经不在了
      try {
        await api.remove(node.id);
        result.removed++;
      } catch (e) {
        result.errors.push('remove ' + item.url + ': ' + (e && e.message));
      }
    }

    for (const item of delta.rename || []) {
      if (!item || !item.url) continue;
      const node = [...index.values()].find(n => n.url === item.url && n.title === item.currentTitle);
      if (!node) continue;
      try {
        await api.update(node.id, { title: item.title });
        result.renamed++;
      } catch (e) {
        result.errors.push('rename ' + item.url + ': ' + (e && e.message));
      }
    }
    return result;
}

/**
   * 把桥接返回的"最近变动"渲染成可读文本行。
   * 与 DOM 解耦，便于在 node 里断言文案。
   */
function describeEvent(ev) {
    if (!ev) return null;
    if (ev.kind === 'applied') {
      const bits = [];
      if (ev.added) bits.push('新增 ' + ev.added);
      if (ev.removed) bits.push('删除 ' + ev.removed);
      if (ev.renamed) bits.push('改名 ' + ev.renamed);
      return { at: ev.at || '', text: bits.length ? '已应用到 Chrome：' + bits.join(' / ') : '已应用到 Chrome：无变化',
               tone: 'applied' };
    }
    const bits = [];
    const pair = (label, c, s, cls) => {
      const total = (c || 0) + (s || 0);
      if (total) bits.push({ label: label, count: total, cls: cls });
    };
    pair('新增', ev.chrome_add, ev.safari_add, 'plus');
    pair('修改', ev.chrome_upd, ev.safari_upd, 'tilde');
    pair('删除', ev.chrome_del, ev.safari_del, 'minus');
    if (!bits.length) return { at: ev.at || '', text: '无改动（仅对齐检查）', tone: 'none' };
    return { at: ev.at || '', bits: bits, tone: 'sync' };
}

/** 待应用改动的摘要文案 */
function describePending(pending) {
    const counts = (pending && pending.counts) || {};
    const total = (counts.add || 0) + (counts.remove || 0) + (counts.rename || 0);
    if (!total) return { total: 0, text: '暂无（Chrome 已是最新）' };
    const bits = [];
    if (counts.add) bits.push('新增 ' + counts.add);
    if (counts.remove) bits.push('删除 ' + counts.remove);
    if (counts.rename) bits.push('改名 ' + counts.rename);
    return { total: total, text: bits.join(' / '), items: pending };
}

export { CONTAINER_IDS, ensureFolder, indexByUrl, applyDelta, describeEvent, describePending };
