/**
 * background.js 的集成测试（node 直接跑，不需要 Chrome）
 *
 * 用假的 chrome.* 与 fetch 把整个后台脚本加载起来，验证：
 *   - 能连上桥接并应用 delta
 *   - 弹窗消息 status / recent 能返回数据
 *   - 角标会显示待应用项数
 *
 * 运行：node extension/test_background.mjs
 */
import assert from 'node:assert';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));

// ---- 假的 chrome API ----
let seq = 100;
const nodes = new Map();
const put = (n) => { nodes.set(n.id, n); return n; };
const childList = (id) => [...nodes.values()].filter(n => n.parentId === id);
put({ id: '0', parentId: null, title: '', children: [] });
put({ id: '1', parentId: '0', title: 'Bookmarks bar', children: [] });   // 故意用英文标题
put({ id: '2', parentId: '0', title: 'Other bookmarks', children: [] });

const badge = { text: null };
const messageHandlers = [];
const listeners = { runtime: {}, action: {} };

globalThis.chrome = {
  bookmarks: {
    async getTree() {
      const build = (id) => {
        const n = nodes.get(id);
        const kids = childList(id).map(c => build(c.id));
        return Object.assign({}, n, { children: kids });
      };
      return [build('0')];
    },
    async getChildren(id) { return childList(id).map(n => Object.assign({}, n)); },
    async create({ parentId, title, url }) {
      return put({ id: String(seq++), parentId, title, url });
    },
    async remove(id) { nodes.delete(id); },
    async update(id, changes) { Object.assign(nodes.get(id), changes); return Object.assign({}, nodes.get(id)); },
  },
  runtime: {
    onStartup: { addListener() {} },
    onInstalled: { addListener() {} },
    onMessage: { addListener(fn) { messageHandlers.push(fn); } },
    lastError: undefined,
  },
  action: {
    async setBadgeText({ text }) { badge.text = text; },
    async setBadgeBackgroundColor() {},
  },
};

// ---- 假的桥接服务 ----
const DELTA = {
  add: [{ title: '来自 Safari', url: 'https://from-safari.example', container: 'bar', path: ['工具'] }],
  remove: [], rename: [],
  counts: { add: 1, remove: 0, rename: 0 },
};
const RECENT = {
  pending: { counts: { add: 1, remove: 0, rename: 0 }, add: DELTA.add, remove: [] },
  events: [
    { kind: 'sync', at: '2026-10-17 00:53:00', chrome_add: 2, safari_add: 1, chrome_del: 0, safari_del: 0 },
    { kind: 'applied', at: '2026-10-17 00:53:20', added: 2, removed: 0, renamed: 0 },
  ],
  counts: { add: 1, remove: 0, rename: 0 },
};

const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push((init && init.method ? init.method + ' ' : '') + url);
  const json = (obj) => ({ ok: true, status: 200, async json() { return obj; } });
  if (url.endsWith('/ping')) return json({ ok: true, app: 'bmsync' });
  if (url.endsWith('/delta')) return json(DELTA);
  if (url.endsWith('/recent')) return json(RECENT);
  if (url.endsWith('/applied')) return json({ ok: true });   // 与真实桥接一致
  throw new Error('unexpected url ' + url);
};

// ---- 加载被测脚本 ----
// 把真实源码拷到扩展目录下一个临时文件再 import：这样 background.js 里的
// `import * as BmsyncCore from './core.js'` 能按真实语义解析（data: URL 无法解析相对导入）。
const tmpName = `.tmp-background-test-${process.pid}.mjs`;
const tmpPath = path.join(here, tmpName);
writeFileSync(tmpPath, readFileSync(path.join(here, 'background.js'), 'utf8'));
let mod;
try {
  mod = await import('./' + tmpName);
} finally {
  try { unlinkSync(tmpPath); } catch (e) { /* 清理失败无所谓 */ }
}
void mod;

const send = (msg) => new Promise((resolve) => {
  const handler = messageHandlers[0];
  const ret = handler(msg, {}, resolve);
  if (ret !== true) resolve(undefined);
});

let failed = 0;
const check = async (name, fn) => {
  try { await fn(); console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + ' -> ' + e.message); }
};

await check('能连上桥接并应用到 Chrome（英文界面下落在 id=1）', async () => {
  const report = await send({ type: 'status' });
  assert.ok(report && report.online,
            'status 应返回在线：' + JSON.stringify(report) + ' | 访问记录 ' + calls.join(', '));
  const node = [...nodes.values()].find(n => n.url === 'https://from-safari.example');
  assert.ok(node, '书签应被写入');
  const tools = [...nodes.values()].find(n => !n.url && n.title === '工具');
  assert.ok(tools, '目录「工具」应被创建');
  assert.strictEqual(tools.parentId, '1', '目录应在书签栏(id=1)下');
  assert.strictEqual(node.parentId, tools.id);
});

await check('recent 消息返回待应用与历史记录', async () => {
  const recent = await send({ type: 'recent' });
  assert.ok(recent, '应返回数据');
  assert.ok(recent.events.length >= 2, '应有多条历史：' + JSON.stringify(recent.events));
  assert.strictEqual(recent.counts.add, 1);
});

await check('角标显示待应用项数', async () => {
  await send({ type: 'recent' });
  assert.strictEqual(String(badge.text), '1', '角标应为 1，实际 ' + badge.text);
});

await check('确实访问了 /ping /delta /recent，并回报 /applied', async () => {
  for (const suffix of ['/ping', '/delta', '/recent']) {
    assert.ok(calls.some(u => u.endsWith(suffix)), '未访问 ' + suffix + '：' + calls.join(','));
  }
  assert.ok(calls.some(u => u.startsWith('POST') && u.endsWith('/applied')),
            '应用后应回报 /applied：' + calls.join(', '));
});

console.log('\n扩展后台脚本：' + (failed ? '失败 ' + failed + ' 项' : '全部通过'));
process.exit(failed ? 1 : 0);
