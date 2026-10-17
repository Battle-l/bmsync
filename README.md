> GitHub 归档分支：`first-answer-20261017-1356`。这是一个独立的历史快照，不是主分支。

# bmsync 快照：会话第一次回答完成后的版本

这个目录是 **2026-10-17 本次会话中，第一次回答完成后的 `bmsync.py` 版本**。
它刻意停在 Safari iCloud 功能开发之前，用来对照/回退。

## 版本依据

- 脚本来源：`/tmp/bmsync.py.before-icloud`（mtime 2026-10-17 13:56）
- 测试来源：`/tmp/test_bmsync.py.before-icloud`（mtime 2026-10-17 13:56）
- 这两个备份是开始实现 Safari `Sync.Changes` / iCloud 触发之前留下的。
- 脚本 `bmsync 1.0.0`
- SHA256：
  - `bmsync.py`：`beca6c309b019fc558774d033fcf0f95f3e6f6f2331c565d88498092483c0f58`
  - `tests/test_bmsync.py`：`a02d93db908854f875a63781bbe35140086c4272e169fd833f91f370009cd5e4`

## 这个版本包含什么

- Chrome → Safari 的实时 AppleScript 写入（`live_mode=auto`）
- Chrome 155 `make new` 崩溃的规避写法（`tell <parent> to make ...`）
- 「改动汇总」按**实际目标操作**统计（移动算删除+新增）
- 反复活 tombstone（`suppress_resurrection=true`）
- 本机 Chrome ↔ Safari 双向同步、备份、幂等校验、launchd 定时任务
- **不包含**任何 Safari iCloud 代码：
  - 没有 `Sync.Changes`
  - 没有 `safari_icloud*` 配置
  - 没有阅读列表触发项
  - 没有 `safari-push`
  - 没有 `safari_icloud_register_unsynced`

也就是说，这个版本对 Safari 只做本机 plist 写入，不碰 iCloud。

## 验证

在临时副本里运行了完整测试：

```text
通过 128 项，失败 0 项
```

## 怎么运行

```bash
cd <克隆下来的仓库目录>
./bin/bmsync --version
./bin/bmsync doctor
./bin/bmsync sync --dry-run -v
```

仓库里**不包含 `config.json`**（`.gitignore` 排除了带个人路径的配置）。克隆后可以运行
`./bin/bmsync init-config` 生成，`state_dir` 会指向当前目录下的 `state/`。
它默认仍会读取真实的 Chrome / Safari 书签；运行 `sync` 会修改真实浏览器，先 dry-run。

## 注意

- 这个仓库只保存代码/测试/文档，**没有包含主项目的 `state/`、日志、备份或 recovery 文件**。
  第一次运行会自己创建 `state/`。
- 没有保存当时那一版 README/CHANGELOG 的独立副本；当前主项目的文档描述的是后续（含 iCloud）
  的行为，不要拿它当这个快照的说明。以 `./bin/bmsync --help` 和本文件为准。
- 这个版本仍然会在 Safari iCloud 开启时直接写 `Bookmarks.plist`。后来已经验证这会和 iCloud
  打架；如果要用它，请先关闭 Safari iCloud，或只做只读/dry-run。
