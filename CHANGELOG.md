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

### 新增

- `tests/test_bmsync.py` 加入合成 Chrome 书签夹具：没有真实 Chrome 书签文件时也能完整测试，
  CI 与贡献者可直接运行。
- `BMSYNC_TEST_CHROME` 环境变量，可指定任意书签文件做往返验证。
- 回归测试场景「全局选项的位置无关性」（16 种参数组合 + 9 条真实 CLI 调用）。

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
- 67 项端到端测试，GitHub Actions 在 macOS 上跑测试 + ruff lint
