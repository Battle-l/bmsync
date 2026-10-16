/**
 * bmsync 桥接扩展 —— 核心逻辑（与 chrome.* API 解耦，便于用 node 直接测试）
 *
 * 作用：Chrome 运行时把书签放在内存里，退出/保存时整份重写 Bookmarks 文件，
 * 所以「直接改文件」对运行中的 Chrome 无效。本模块用官方的 chrome.bookmarks API
 * 应用来自 bmsync 的改动，这样两个浏览器都开着也能同步。
 *
 * 关键点：容器一律用 **稳定的文件夹 ID**（Chrome 固定把书签栏设为 "1"、其他书签设为 "2"），
 * 不要用标题 —— 标题随界面语言变化（英文是 "Bookmarks bar"），按标题找会失败。
 */

/* eslint-disable no-undef */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;   // node 测试用
  root.BmsyncCore = api;                                                    // 扩展里用
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
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

  return { CONTAINER_IDS, ensureFolder, indexByUrl, applyDelta };
});
