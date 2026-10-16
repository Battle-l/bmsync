/**
 * 弹窗（popup.html + popup.js）的静态与加载测试
 *
 * 背景：popup.js 曾在普通 <script> 下直接使用全局 BmsyncCore，而 core.js 已改为
 * ES module（只 export、不挂全局），于是报 "BmsyncCore is not defined"；
 * 紧接着 DOM 取值失败还会报 "Cannot set properties of null"。
 * 这类问题不跑一次弹窗就发现不了，所以这里把弹窗脚本真的加载一遍。
 *
 * 运行：node extension/test_popup.mjs
 */
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, 'popup.html'), 'utf8');
const js = readFileSync(path.join(here, 'popup.js'), 'utf8');

let failed = 0;
const check = async (name, fn) => {
  try { await fn(); console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + ' -> ' + e.message); }
};

// ---- 1) 弹窗脚本必须是 module，并 import core.js ----
await check('popup.html 以 type=module 加载 popup.js', () => {
  assert.match(html, /<script[^>]+type=["']module["'][^>]+src=["']popup\.js["']/,
               '脚本标签：' + (html.match(/<script[^>]*>/) || ['(无)'])[0]);
});

await check('popup.js import 了 core.js', () => {
  assert.match(js, /import\s+\*\s+as\s+BmsyncCore\s+from\s+['"]\.\/core\.js['"]/,
               '未找到 import 语句');
});

// ---- 2) js 里引用的元素 id 必须在 html 中存在 ----
await check('popup.js 引用的元素 id 都存在于 popup.html', () => {
  const htmlIds = new Set([...html.matchAll(/id=["']([^"']+)["']/g)].map(m => m[1]));
  const used = new Set();
  for (const m of js.matchAll(/\$\(['"]([^'"]+)['"]\)/g)) used.add(m[1]);
  for (const m of js.matchAll(/set(?:Text|HTML)\(['"]([^'"]+)['"]/g)) used.add(m[1]);
  const missing = [...used].filter(id => !htmlIds.has(id));
  assert.deepStrictEqual(missing, [], 'HTML 里缺少这些 id：' + missing.join(', '));
});

// ---- 3) 真的把弹窗脚本加载起来（用最小 DOM/chrome 模拟）----
function makeEnv() {
  const elements = new Map();
  const mkEl = (id) => ({
    id, textContent: '', innerHTML: '', className: '', style: {},
  });
  for (const m of html.matchAll(/id=["']([^"']+)["']/g)) elements.set(m[1], mkEl(m[1]));
  globalThis.document = {
    readyState: 'complete',
    getElementById: (id) => elements.get(id) || null,
    addEventListener: () => {},
  };
  globalThis.chrome = {
    runtime: {
      lastError: undefined,
      sendMessage: (msg, cb) => {
        if (msg.type === 'status') cb({ at: Date.now(), online: true });
        else if (msg.type === 'recent') {
          cb({
            pending: { counts: { add: 1, remove: 0, rename: 0 },
                       add: [{ title: '来自 Safari', url: 'https://x.example', container: 'bar', path: [] }],
                       remove: [] },
            events: [{ kind: 'sync', at: '2026-10-17 01:02:03', chrome_add: 2, safari_add: 1 },
                     { kind: 'applied', at: '2026-10-17 01:02:20', added: 2, removed: 1, renamed: 0 }],
            counts: { add: 1, remove: 0, rename: 0 },
          });
        } else cb(null);
      },
    },
  };
  return elements;
}

await check('popup.js 能在最小 DOM 下加载且不抛 ReferenceError', async () => {
  const elements = makeEnv();
  const src = readFileSync(path.join(here, 'popup.js'), 'utf8');
  // popup.js 是 ES module，这里用 data URL 加载（其内部只 import ./core.js，
  // 但 data: 无法解析相对路径 —— 所以改成把 core 的导出先挂到全局再注入源码）
  const patched = src.replace(/^import .*$/m, 'const BmsyncCore = globalThis.__CORE__;');
  globalThis.__CORE__ = await import('./core.js');
  await import('data:text/javascript;base64,' + Buffer.from(patched).toString('base64'));
  // 等微任务把 refresh() 里的两个 await 走完
  await new Promise((r) => setTimeout(r, 60));
  assert.strictEqual(elements.get('state').textContent, '已连接', '状态未渲染');
  assert.ok(elements.get('pending').innerHTML.includes('来自 Safari'), '待应用清单未渲染');
  assert.ok(elements.get('events').innerHTML.includes('新增'), '最近变动未渲染');
});

console.log('\n弹窗：' + (failed ? '失败 ' + failed + ' 项' : '全部通过'));
process.exit(failed ? 1 : 0);
