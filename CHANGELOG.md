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

### 修复（实时同步的覆盖风险提示）

- **实时模式不再对"浏览器正在运行"保持沉默**：触发同步时如果检测到 Chrome/Safari 在运行，
  会在日志里明确提示"写入内容可能被浏览器用内存数据覆盖"。这一提示受
  `sync.require_browsers_closed` 控制（默认开启）。
- `watch --verify` 同样显示该风险，并给出"退出浏览器 → 同步 → 再打开"的可靠做法。
- README 补充排查方法：用 `stat` 观察 Safari 写盘延迟，区分"没同步"与"浏览器还没落盘"。

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

### 修复（严重：桥接会删掉用户刚在 Chrome 加的书签）

- **`/delta` 的删除判定改为"必须有墓碑"**：原先只要"Chrome 有、基线没有"就下令删除，
  而基线是上次同步结束时才写盘的 —— 用户在 Chrome 里刚加的书签会先落进文件，
  此时基线还不知道它，扩展就会把它删掉（实测发生过，日志表现为
  `同步 safari_del:1` → `扩展 remove:1`，两条 IT之家书签被删）
- 删除现在只针对"Safari 确实删除过（有墓碑记录）且被同步知道"的书签；
  没同步过的东西只会被"新增/保留"，永远不会被删
- 墓碑改为**记录 URL**（原先只记 id，而被删条目随后也会从基线移除，
  导致再也拼不回"被删的是哪个 URL"）；兼容旧格式

### 修复（扩展容器定位依赖界面语言）

- 桥接下发的条目改为携带**稳定容器标识**（`bar` / `other`），扩展据此用 Chrome 的
  固定文件夹 ID（`1` / `2`）定位，不再按标题查找 —— 标题随界面语言变化
  （英文版 `Bookmarks bar`），按标题会找不到容器、把书签塞错位置甚至静默失败
- 扩展拆出 `core.js`（与 chrome API 解耦）+ `test_core.js`：`node extension/test_core.js`
  即可自测，覆盖英文/中文界面、目录自动创建与复用、幂等、定向删除、改名、单项失败隔离
- Python 测试套件会自动调用 node 单测（没装 node 则明确跳过）

### 新增（Chrome 扩展桥接：浏览器不用退出）

- `extension/`：配套 Chrome 扩展（MV3，只申请 `bookmarks` 权限），通过官方书签 API 应用改动，
  解决"Chrome 运行时直接改文件会被内存内容覆盖"的固有问题
- `bmsync watch` 内置本地桥接服务（`127.0.0.1`，默认端口 8765，`watch.bridge_port` 可改/关闭），
  提供 `/ping`（心跳）、`/delta`（待应用的增删改）、`/applied`（扩展回报）
- **扩展在线时不再改写 Chrome 的 `Bookmarks` 文件**，交由扩展处理，避免两边互相覆盖
- 基线改为**按内容指纹取交集**：只有两个浏览器都确实拿到的条目才记为已同步。
  未落地的改动会在下一轮重试（新增是幂等的），绝不会被误判成删除

### 改进（Safari 侧噪音 + 诊断工具）

- 内容指纹进一步收窄为"用户可见内容"：不再包含 `WebBookmarkUUID` / `WebBookmarkSpecialID`
  （Safari 内部标识，会被系统重写时重新分配，但不代表书签变化）
- 新增 **`bmsync watch --why [秒]`**：观察一段时间，报告文件被重写时内容是否真的变化；
  内容真变时列出**具体差异字段**，用于把新的易变字段加入忽略列表
- 新增 `content_diff()` / `normalized_content()`：字段级差异比较，JSON 与 plist 通用

### 改进（不再因日常浏览而空转同步）

- 实时同步的触发条件从「文件 mtime 变化」改为「**书签实质内容变化**」：
  切标签页、Chrome 更新 checksum、系统重写 Safari plist 都会让文件变化但书签没变，
  这类噪音现在会被静默跳过（`-vv` 下显示累计次数），不再触发同步
- `content_fingerprint()` 同时支持 Chrome 的 JSON 与 Safari 的 plist，
  忽略 `checksum` / `date_last_used` / `WebBookmarkModificationDate` / `WebBookmarkSyncData` 等易变字段
- 带缓存的 `file_content_fingerprint()`：先比 `(size, mtime)`，未变则复用上次结果，避免每次轮询都解析整份文件
- 修复期间发现的一个自引入 bug：噪音分支提前更新了"已见指纹"，导致静默计时被反复重置、
  真正的变化永远触发不了（已加回归测试锁住"噪音不触发 + 真变化必触发"两个方向）

### 改进（备份策略）

- **按内容去重**：忽略 `checksum` / `date_last_used` 等易变字段，内容没变不再重复备份
- **最小间隔** `sync.backup_min_interval`（默认 300 秒）：连续小改动只留最近一份
- **分级保留**：24 小时内全留、7 天内每小时 1 份、30 天内每天 1 份、更早保留 `backup_keep` 份
- 实测：原先每秒级的实时同步会每 20 秒产生 2 份备份（≈200MB/天），现在同样的操作量只产生 2 份

### 新增（实时同步与工作模式）

- **三种工作模式**，用 `bmsync mode` 查看/切换并把选择写进 `config.json`：
  `manual`（手动）、`scheduled`（定时）、`watch`（实时）。切换时会自动清理另一种自动方式，
  避免两套机制同时写书签（`--keep-scheduler` 可保留）。
- **`bmsync watch` 实时同步**：轮询两个书签文件的大小 + mtime，检测到变化后等 `settle` 秒
  确认写完，再触发一次完整同步。参数（`interval` / `settle` / `resync_after`）可在配置里调。
- **`bmsync watch-daemon start|stop|status|restart`**：把实时同步装成 launchd 常驻服务
  （`RunAtLoad` + `KeepAlive`，崩溃后自动拉起）。
- `bmsync watch --verify` 自检；`--once N` 只监听 N 秒便于脚本化验证。
- 用 `flock` 保证单实例：已有 watch 在跑时拒绝启动第二个。
- 新增环境变量 `BMSYNC_LAUNCHD_DIR` 覆盖 LaunchAgents 目录（测试隔离，也方便非标准环境）。

### 新增

- `tests/test_bmsync.py` 加入合成 Chrome 书签夹具：没有真实 Chrome 书签文件时也能完整测试，
  CI 与贡献者可直接运行。
- `BMSYNC_TEST_CHROME` 环境变量，可指定任意书签文件做往返验证。
- 回归测试场景「全局选项的位置无关性」（16 种参数组合 + 9 条真实 CLI 调用）。
- 回归测试：`--quiet` 下报错可见、失败写入日志、任务日志为绝对路径且不落在仓库内。
- 回归测试：权限未就绪时 `install-scheduler` 返回 2、且不会先写 plist。
- 实时同步场景 18 项：双向实时同步、单实例锁、`--verify` / `--once`、
  常驻服务 plist（KeepAlive/RunAtLoad）、模式切换与自动清理（沙盒内验证不碰真实系统）。

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
