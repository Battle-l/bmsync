# 贡献指南

感谢愿意一起改进 bmsync！这个项目只针对 macOS，代码刻意保持"单文件 + 纯标准库"，方便任何人 clone 下来就能跑、能读、能改。

## 快速开始

```bash
git clone https://github.com/Battle-l/bmsync.git
cd bmsync

python3 tests/test_bmsync.py     # 跑测试（不需要任何依赖）
./bin/bmsync doctor              # 看一眼本机环境
./bin/bmsync sync --dry-run -v   # 演练
```

运行环境要求：macOS + Python 3.9 或更高。**不需要 pip install 任何东西**（`pyproject.toml` 只是元数据）。

## 目录结构

| 文件 | 说明 |
| --- | --- |
| `bmsync.py` | 全部实现：两个浏览器后端、统一书签树、三方合并、CLI |
| `bin/bmsync` | 启动器（找到仓库里的 `bmsync.py` 并转发参数） |
| `tests/test_bmsync.py` | 端到端测试（子进程调用真实 CLI + 直接调库校验） |
| `.github/workflows/tests.yml` | CI：macOS 上跑测试，Ubuntu 上跑 ruff |

`bmsync.py` 的主要分区（按顺序读下来就能理解全貌）：

1. 常量与时间戳转换（Chrome 用"1601 起的微秒"，Safari 用"2001 起的秒"）
2. `Item` / `Tree` —— 与浏览器无关的统一书签树。**id 的约定很关键**：书签是 `u:<规范化URL>`，目录是 `f:<父id>/<名字>`，重复项加 `#2`、`#3` 后缀
3. `Mid` —— 中间格式，用来把两边的原生结构映射成同一棵树
4. `ChromeBackend` / `SafariBackend` —— 各自的读、写、原生节点认领（`build_native_map`）
5. `merge()` —— 三方合并与冲突判定
6. 配置、状态、备份、权限检测
7. 导出/导入、CLI 命令

## 提交前请确认

1. **测试全绿**：

   ```bash
   python3 tests/test_bmsync.py          # 期望 "通过 79 项，失败 0 项"
   BMSYNC_TEST_CHROME=/nonexistent python3 tests/test_bmsync.py   # 合成夹具路径也要绿
   ```

2. **lint 通过**：

   ```bash
   pip install ruff && ruff check .
   ```

3. **新增行为请补测试**。改合并逻辑、URL 规范化、解析器时尤其重要——这个项目踩过的坑基本都是这三处的。

## 测试约定

- 测试跑在临时沙盒里（`state/bmsync-test-*`），**不会碰你真实的浏览器数据**。
- 有真实 Chrome 书签文件时，往返测试会用它验证"读进来再写回去不丢数据"；没有就自动用 `synthetic_chrome_bookmarks()` 合成夹具。合成夹具请保持覆盖这些易踩坑的情况：重复 URL、`chrome://` / `file://` / 带空格 / 中文 / 带 fragment 的 URL、NBSP、emoji、深层嵌套、`synced` 结构。
- 断言里比较 URL 时请用 `NU()`（即 `bmsync.norm_url`）包一层——写盘时 URL 会被规范化，"看似不相等的两个 URL 其实是同一个"是这里最常见的误判来源。

## 千万别提交的东西

- **`state/` 目录**：里面有同步基线、日志和**真实书签备份**，属于个人数据。`.gitignore` 已忽略，请别用 `git add -f` 加进来。
- **`config.json`**：可能含个人路径。已在 `.gitignore` 中。
- 任何从真实浏览器导出的书签文件、`Bookmarks.plist`、`Bookmarks` JSON。

如果不小心提交了，除了 `git rm --cached` 之外还要清理历史（`git filter-repo`），并视情况撤销远端——书签里往往有内网地址、私人链接，值得当敏感数据处理。

## 代码风格

- Python 3.9 兼容：不要用 `match`、`X | Y` 类型注解、`dict | dict` 等 3.10+ 语法；文件顶部已 `from __future__ import annotations`。
- 只依赖标准库。想引入第三方库的 PR 需要先讨论必要性。
- 注释和输出信息用中文（面向当前用户群），代码标识符用英文。
- 提交信息用中文或英文都行，但请写清楚"为什么改"，而不只是"改了什么"。

## 适合上手的方向

- **Windows / Linux 支持**：把 `SafariBackend` 抽象成可选后端，接 Firefox 的 `places.sqlite`
- **更聪明的冲突处理**：现在纯按时间戳，可以做"两边都保留（生成副本）"的策略
- **顺序同步（可选开关）**：现在故意不碰顺序，有兴趣可以做一个 `sync_order` 选项
- **FSEvents 实时同步**：监听书签文件变化，配合防抖
- **更多浏览器**：Edge / Brave / Vivaldi 目前只需改配置，但缺少自动探测
- **图形界面**：`bmsync.py` 里的逻辑已经和后端解耦，套一层菜单栏 App 或 Web UI 都可行

## 提交 PR

1. Fork 并从 `main` 开分支
2. 改动尽量聚焦，一个 PR 做一件事
3. 说明你的验证方式（跑了哪些测试、是否用真实书签验证过）
4. 涉及写盘的改动，请务必说明**回滚方式**

## 行为准则

保持友善、就事论事。这是个解决个人痛点的工具，欢迎不同意见和更简单的方案。
