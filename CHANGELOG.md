# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- **全局选项（`-v` / `-vv` / `--json` / `--config`）写在子命令后面会报
  `unrecognized arguments`**：argparse 解析完子命令后不会再回主解析器找参数，而 README 里的
  示例恰好都写在后面（如 `bmsync sync --dry-run -v`）。现在这几个选项由
  `preprocess_globals()` 在解析前统一抽取，写在子命令前后都有效；`--config=路径` 形式和
  `--` 之后的参数也做了正确处理。
- 顺带修正 `-v` 的计数语义：默认级别为 1，每个 `-v` 累加一级（`sync -v` 与 `-v sync` 结果一致）。
- 修复 `build_native_map` 认领原生节点时未按"同 URL 第二个副本"顺序编号，导致重复 URL 书签
  在往返中丢一条（由新增的合成夹具测试暴露）。
- 修复整理 Chrome `synced` 结构中直接挂载的书签时会给书签换新 guid/id 的问题（现在复用原节点）。
- 修复 `find_id` 把"分配新 id"的语义当"查找"用，导致移动设备书签目录被重复创建。
- 显式指定的配置文件不存在时，原先会静默回退到默认配置（可能在错误的浏览器配置上跑同步），
  现在会打印明确警告。

### 修复（定时任务相关的可观测性）

- **`--quiet` 不再吞掉报错**：定时任务用 `--quiet` 运行时，失败信息仍会打到 stderr。
  原先 `--quiet` 把 verbose 置 0，导致 launchd 里跑失败时完全没有任何输出。
- **失败也会写进 `state/bmsync.log`**：记录时间、退出码和原因，定时任务失败后有据可查。
- **任务日志移到 `~/Library/Logs/bmsync/`**：原先放在仓库的 `state/` 下，若仓库位于外部卷或
  受保护目录，launchd 可能连日志文件都创建不了，表现为 `scheduler.err.log` 不存在、毫无线索。
  新增 `install-scheduler --log-dir` 可自定义，且强制转成绝对路径。
- `install-scheduler` 安装时会打印任务日志位置和验证命令（含 `launchctl print` 查退出码）。
- 权限失败时的提示明确了 launchd/cron 场景（读不到 Safari、写不了 Chrome 都指向同一处授权）。
- **`install-scheduler` 改用 `bootout` + `bootstrap` + `kickstart`**（原先用已废弃的
  `load`/`unload`，只改 plist 不重新加载会留下内存里的旧定义，导致 `launchctl start`
  立刻失败、退出码 78、日志文件都不创建）。加载失败时回退 `load -w` 并打印两条命令的报错。
- **安装前做权限前置检查**：读不到 Safari / 写不了 Chrome 时拒绝安装并说明缺什么，
  不再装出一个注定失败的任务。
- **安装后立即触发一次并回报退出码**（`--no-verify` 可关闭），按退出码给出处置建议，
  并自动带出日志最后几行。

### 新增

- `tests/test_bmsync.py` 加入合成 Chrome 书签夹具：没有真实 Chrome 书签文件时也能完整测试，
  CI 与贡献者可直接运行。
- `BMSYNC_TEST_CHROME` 环境变量，可指定任意书签文件做往返验证。
- 回归测试场景「全局选项的位置无关性」（16 种参数组合 + 9 条真实 CLI 调用）。
- 回归测试：`--quiet` 下报错可见、失败写入日志、任务日志为绝对路径且不落在仓库内。
- 回归测试：权限未就绪时 `install-scheduler` 返回 2、且不会先写 plist。

## [1.0.0] - 2026-10-16

首个版本。

### 功能

- 直接读写 Chrome（`Bookmarks` JSON）与 Safari（`Bookmarks.plist`）的原生书签库
- 基于"上次同步基线"的三方合并，幂等：无改动时重复执行不重写文件
- 冲突按修改时间判定，并逐条输出冲突说明（可用 `--prefer` 指定）
- 首次同步取并集、不推断删除，避免误删
- 双向 / 单向（`chrome2safari`、`safari2chrome`）同步
- 每次写入前自动备份两边原文件，支持回滚
- `doctor` / `status` / `export` / `import` / `init-config` 等子命令
- launchd 定时同步（`install-scheduler` / `uninstall-scheduler`）
- 67 项端到端测试（当前已增至 73 项），GitHub Actions 在 macOS 上跑测试 + ruff lint
