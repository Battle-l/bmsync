/**
 * core.js 的单元测试（node 直接跑，不需要 Chrome）
 *
 * 重点验证：容器定位用的是 Chrome 的稳定文件夹 ID（"1"/"2"），
 * 而不是会随界面语言变化的标题 —— 拿英文界面的 Chrome（"Bookmarks bar"）
 * 也能正确落位。这正是之前"从 Safari 改的书签同步不到 Chrome"的根因之一。
 *
 * 运行：node extension/test_core.js
 */
'use strict';

const assert = require('assert');
const core = require('./core.js');

/** 一个最小可用的书签树模拟器，容器标题可由参数指定（模拟不同界面语言） */
function makeChromeMock({ barTitle = 'Bookmarks bar', otherTitle = 'Other bookmarks' } = {}) {
  let seq = 100;
  const nodes = new Map();          // id -> {id, parentId, title, url, children?}

  const put = (node) => { nodes.set(node.id, node); return node; };
  const childList = (id) => [...nodes.values()].filter(n => n.parentId === id);

  put({ id: '0', parentId: null, title: '', children: [] });                    // 根
  put({ id: '1', parentId: '0', title: barTitle, children: [] });               // 书签栏
  put({ id: '2', parentId: '0', title: otherTitle, children: [] });             // 其他书签
  put({ id: '3', parentId: '0', title: 'Mobile bookmarks', children: [] });

  return {
    nodes,
    api: {
      async getTree() {
        const build = (id) => {
          const n = nodes.get(id);
          const kids = childList(id).map(c => build(c.id));
          return Object.assign({}, n, kids.length || !n.url ? { children: kids } : {});
        };
        return [build('0')];
      },
      async getChildren(id) { return childList(id).map(n => Object.assign({}, n)); },
      async create({ parentId, title, url }) {
        const id = String(seq++);
        return put({ id, parentId, title, url, children: url ? undefined : [] });
      },
      async remove(id) {
        const n = nodes.get(id);
        assert.ok(n, '被删节点应存在');
        nodes.delete(id);
      },
      async update(id, changes) {
        const n = nodes.get(id);
        Object.assign(n, changes);
        return Object.assign({}, n);
      },
    },
    find(url) { return [...nodes.values()].find(n => n.url === url); },
    findFolder(title) { return [...nodes.values()].find(n => !n.url && n.title === title); },
  };
}

const checks = [];
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => checks.push([true, name]))
    .catch((e) => checks.push([false, name + ' -> ' + e.message]));
}

(async () => {
  // 1) 容器定位使用稳定 ID：即使书签栏标题是英文，也要落进 id=1
  await check('英文界面下也能落进书签栏（不依赖标题）', async () => {
    const mock = makeChromeMock({ barTitle: 'Bookmarks bar' });
    const r = await core.applyDelta(mock.api, {
      add: [{ title: 'A', url: 'https://a.example', container: 'bar', path: [] }],
      remove: [], rename: [],
    });
    assert.strictEqual(r.added, 1, JSON.stringify(r));
    const node = mock.find('https://a.example');
    assert.strictEqual(node.parentId, '1', '应挂在书签栏(id=1)下，实际 ' + node.parentId);
  });

  // 2) other 容器 → id=2
  await check('落进「其他书签」(id=2)', async () => {
    const mock = makeChromeMock();
    await core.applyDelta(mock.api, {
      add: [{ title: 'B', url: 'https://b.example', container: 'other', path: [] }],
      remove: [], rename: [],
    });
    assert.strictEqual(mock.find('https://b.example').parentId, '2');
  });

  // 3) 按路径逐级创建目录（目录不存在时应新建）
  await check('按路径自动创建缺失的目录', async () => {
    const mock = makeChromeMock();
    await core.applyDelta(mock.api, {
      add: [{ title: 'C', url: 'https://c.example', container: 'other', path: ['工具', '子目录'] }],
      remove: [], rename: [],
    });
    const tool = mock.findFolder('工具');
    const sub = mock.findFolder('子目录');
    assert.ok(tool && sub, '两级目录都应被创建');
    assert.strictEqual(tool.parentId, '2');
    assert.strictEqual(sub.parentId, tool.id);
    assert.strictEqual(mock.find('https://c.example').parentId, sub.id);
  });

  // 4) 已存在的目录要复用，不能重复创建
  await check('已存在的目录复用而不重复创建', async () => {
    const mock = makeChromeMock();
    const delta = (url) => ({ add: [{ title: 'x', url, container: 'other', path: ['工具'] }], remove: [], rename: [] });
    await core.applyDelta(mock.api, delta('https://d1.example'));
    await core.applyDelta(mock.api, delta('https://d2.example'));
    const tools = [...mock.nodes.values()].filter(n => !n.url && n.title === '工具');
    assert.strictEqual(tools.length, 1, '目录应只有一个，实际 ' + tools.length);
    assert.strictEqual(mock.find('https://d2.example').parentId, tools[0].id);
  });

  // 5) 幂等：同 URL 已存在就不再加（避免扩展与文件同步互相打架时产生重复）
  await check('同 URL 已存在时跳过（幂等）', async () => {
    const mock = makeChromeMock();
    const item = { add: [{ title: 'A', url: 'https://dup.example', container: 'bar', path: [] }], remove: [], rename: [] };
    await core.applyDelta(mock.api, item);
    const r2 = await core.applyDelta(mock.api, item);
    assert.strictEqual(r2.added, 0, '第二次不该再加');
    const count = [...mock.nodes.values()].filter(n => n.url === 'https://dup.example').length;
    assert.strictEqual(count, 1);
  });

  // 6) 删除只删 delta 点名的 URL
  await check('删除只针对点名 URL，不动其它书签', async () => {
    const mock = makeChromeMock();
    await core.applyDelta(mock.api, {
      add: [{ title: 'A', url: 'https://keep.example', container: 'bar', path: [] },
            { title: 'B', url: 'https://drop.example', container: 'bar', path: [] }],
      remove: [], rename: [],
    });
    const r = await core.applyDelta(mock.api, { add: [], rename: [], remove: [{ url: 'https://drop.example' }] });
    assert.strictEqual(r.removed, 1);
    assert.ok(mock.find('https://keep.example'), '未点名的书签必须保留');
    assert.ok(!mock.find('https://drop.example'));
  });

  // 7) 改名
  await check('改名按 URL + 原标题定位', async () => {
    const mock = makeChromeMock();
    await core.applyDelta(mock.api, {
      add: [{ title: '旧名', url: 'https://r.example', container: 'bar', path: [] }], remove: [], rename: [],
    });
    const r = await core.applyDelta(mock.api, {
      add: [], remove: [],
      rename: [{ url: 'https://r.example', title: '新名', currentTitle: '旧名' }],
    });
    assert.strictEqual(r.renamed, 1);
    assert.strictEqual(mock.find('https://r.example').title, '新名');
  });

  // 8) 单项失败不影响其它条目
  await check('单项报错不中断整批', async () => {
    const mock = makeChromeMock();
    const bad = { create: mock.api.create };
    const brokenApi = Object.assign({}, mock.api, {
      create: async (n) => {
        if (n.url === 'https://bad.example') throw new Error('模拟失败');
        return bad.create(n);
      },
    });
    const r = await core.applyDelta(brokenApi, {
      add: [{ title: 'bad', url: 'https://bad.example', container: 'bar', path: [] },
            { title: 'good', url: 'https://good.example', container: 'bar', path: [] }],
      remove: [], rename: [],
    });
    assert.strictEqual(r.added, 1);
    assert.strictEqual(r.errors.length, 1);
    assert.ok(mock.find('https://good.example'), '好的一条仍应写入');
  });

  // 9) 中文界面同样正确（说明既不依赖英文也不依赖中文）
  await check('中文界面下同样正确', async () => {
    const mock = makeChromeMock({ barTitle: '书签栏', otherTitle: '其他书签' });
    await core.applyDelta(mock.api, {
      add: [{ title: 'A', url: 'https://cn.example', container: 'bar', path: [] }], remove: [], rename: [],
    });
    assert.strictEqual(mock.find('https://cn.example').parentId, '1');
  });

  let failed = 0;
  for (const [ok, name] of checks) {
    console.log((ok ? '  ✓ ' : '  ✗ ') + name);
    if (!ok) failed++;
  }
  console.log('\n扩展逻辑：通过 ' + (checks.length - failed) + ' 项，失败 ' + failed + ' 项');
  process.exit(failed ? 1 : 0);
})();
