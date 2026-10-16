# bmsync —— Chrome ↔ Safari 书签双向同步

[![tests](https://github.com/Battle-l/bmsync/actions/workflows/tests.yml/badge.svg)](https://github.com/Battle-l/bmsync/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

macOS 上的命令行工具，把 Chrome 和 Safari 的书签互相同步。纯 Python 标准库实现，**不需要安装任何依赖**。

思路就是最直白的那套：**分别读出两边 → 比对出各自的改动 → 合并 → 分别写回**。区别在于它不生成中间文件再导入（那样每次都会重复导入），而是直接读写两个浏览器的原生书签库，并用一份"上次同步后的基线"记录状态，所以第二次同步只处理真正的增量，重复执行结果稳定。

```
Chrome Bookmarks (JSON) ─┐                        ┌─→ 写回 Chrome
                         ├─→ 三方比对/合并 ─→ 统一树 ┤
Safari Bookmarks.plist ──┘   (基线 state.json)    └─→ 写回 Safari
```

- 支持 macOS 自带 Python 3.9+，无第三方依赖
- 冲突按修改时间判定，并逐条打印冲突说明
- 备份按内容去重 + 分级保留，不会因实时同步而爆量
- 三种工作模式：手动 / 定时（launchd）/ **实时（监听文件变化）**
- **浏览器不用退出**：配一个 Chrome 扩展，通过官方书签 API 落地改动（见第 4 节）
- 99 项端到端测试（含对真实 Chrome 书签的无损往返验证）

---

## 快速开始

```bash
git clone https://github.com/Battle-l/bmsync.git
cd bmsync

./bin/bmsync doctor                  # 1. 先看权限是否就绪
./bin/bmsync sync --dry-run -v       # 2. 演练，逐条看将要做的改动
./bin/bmsync sync                    # 3. 正式同步
```

想直接用 `bmsync` 命令：

```bash
ln -s "$(pwd)/bin/bmsync" /usr/local/bin/bmsync
bmsync status
```

## 1. 先做这一步：给终端"完全磁盘访问权限"

Safari 的书签文件受 macOS 的 TCC 保护，`chmod`、`sudo` 都没用，必须手动授权，否则读都读不到：

1. 打开 **系统设置 → 隐私与安全性 → 完全磁盘访问权限**
2. 把你运行本脚本的程序加进去并打开开关（Terminal / iTerm / VS Code 等）
3. **完全退出并重新启动**该程序（只关窗口不算）
4. 验证：

```bash
./bin/bmsync doctor
```

看到 `Chrome 读取/写入 正常`、`Safari 读取/写入 正常` 就可以继续了。若 Safari 一栏仍失败，说明授权没生效，重做第 2–3 步。

> 也可以直接把 `/usr/bin/python3` 加进完全磁盘访问权限列表，这样任何终端调用它都能读到 Safari 书签。

## 2. 命令一览

```bash
./bin/bmsync doctor                    # 环境自检：权限、路径、进程、定时任务
./bin/bmsync status                    # 看两边各有多少书签、上次同步情况

./bin/bmsync sync --dry-run -v         # 演练：只打印"将要做的改动"，一个字节都不写
./bin/bmsync sync                      # 正式同步（两边原文件会自动备份）
./bin/bmsync sync --list-changes -v    # 逐条列出改动明细
./bin/bmsync sync --prefer safari      # 冲突时以 Safari 为准
./bin/bmsync sync --direction chrome2safari   # 单向：只把 Chrome 的改动应用到 Safari

./bin/bmsync export --format html      # 导出成可手动导入的 HTML（放到 state/export/）
./bin/bmsync import ~/Downloads/bookmarks.html   # 把外部导出的书签并进来，再同步到两边
./bin/bmsync init-config               # 生成可编辑的 config.json

./bin/bmsync mode                      # 查看当前工作模式
./bin/bmsync mode watch                # 切到实时模式（会自动清理定时任务，避免两套机制打架）
./bin/bmsync watch                     # 前台运行实时同步（Ctrl+C 退出）
./bin/bmsync watch-daemon start        # 实时同步装成常驻服务（登录自启、掉线拉起）
./bin/bmsync watch-daemon status       # 看常驻服务状态
./bin/bmsync watch --verify            # 实时模式自检：权限、指纹、参数
```

全局选项（`-v`、`-vv`、`--json` / `-t`、`--config`）写在**子命令前后都有效**，下面这些写法等价：

```bash
./bin/bmsync sync --dry-run -v
./bin/bmsync -v sync --dry-run
./bin/bmsync sync --dry-run --config ~/bm.json -v
```

`-v` 让输出更详细、`-vv` 在报错时额外打印 Python 调用栈（提 Issue 时附上它很有用）。

**同步前请退出 Chrome 和 Safari。** 浏览器运行时会把内存里的旧数据写回磁盘，覆盖掉我们的改动。脚本会检测并拒绝执行（确认无碍时可加 `--force`）。

## 3. 三种工作模式

用 `bmsync mode` 查看、`bmsync mode <模式>` 切换（会写进 `config.json` 的 `mode` 字段，切换时会自动清理另一种自动方式，避免两套机制同时写书签）。

| 模式 | 触发方式 | 适合谁 |
| --- | --- | --- |
| `manual` | 只有你手动跑 `bmsync sync` 才同步 | 想完全掌控、偶尔同步一次 |
| `scheduled` | launchd 每 N 分钟同步一次 | 想要"无感"，能接受分钟级延迟 |
| `watch` | 常驻进程盯着书签文件，一变就同步 | 想要接近实时 |

```bash
./bin/bmsync mode              # 看当前模式（同时显示定时任务/常驻服务是否在跑）
./bin/bmsync mode watch        # 切到实时
./bin/bmsync mode scheduled    # 切到定时
./bin/bmsync mode manual       # 关掉所有自动同步
```

### 实时模式（watch）

```bash
./bin/bmsync watch                      # 前台运行，Ctrl+C 退出（想先观察就用这个）
./bin/bmsync watch-daemon start         # 后台常驻：登录自启、进程掉了自动拉起
./bin/bmsync watch-daemon status        # 看状态（PID / 上次退出码）
./bin/bmsync watch-daemon stop          # 停止并移除
./bin/bmsync watch --once 10            # 只监听 10 秒（自测用）
./bin/bmsync watch --verify             # 自检：权限、文件指纹、监听参数
./bin/bmsync watch --why 60             # 观察 60 秒：文件被重写时书签内容是否真的变了
```

**排查"我没动书签，为什么一直提示写入"**：

```bash
./bin/bmsync watch --why 60
```

它会盯着两个文件 60 秒，每次文件被重写就报告是"内容未变（噪音）"还是"内容也变了"，后者还会列出**具体哪个字段变了**。把差异发出来即可把该字段加入忽略列表。

**它的工作方式**：每 5 秒检查一次两个书签文件，**只有书签的实质内容变了才同步**：

1. `大小 + mtime` 变了 → 说明文件被重写过（浏览器切标签页、Chrome 更新 checksum、系统重写 plist 都会这样）
2. 再比对**实质内容指纹**：只算"用户看得见的内容"（URL、标题、层级、容器类型），
   忽略 `checksum`、`date_last_used`、`date_added`、`WebBookmarkModificationDate`、
   `WebBookmarkSyncData`，以及 Safari 内部会被重新分配的 `WebBookmarkUUID` / `WebBookmarkSpecialID`
3. 内容确实变了 → 等 `settle` 秒确认浏览器写完（避免读到写了一半的文件）→ 触发一次完整同步
4. 内容没变 → 静默跳过，只在 `-vv` 下留一行"跳过同步（累计 N 次）"

所以日常浏览时不会因为切标签页就频繁同步。日志里能看到真正的同步：

```
[2026-10-16 21:36:17] watch: Chrome 书签文件有变化，开始同步
[2026-10-16 21:36:17] {"chrome_add": 0, ..., "safari_add": 1, ...}
[2026-10-16 21:36:17] watch: 同步结束（退出码 0）
```

参数可以在 `config.json` 里调：

```json
"watch": {
  "interval": 5.0,       // 轮询间隔（秒）
  "settle": 3.0,         // 文件连续多久没变化才算写完了（秒）
  "resync_after": 60.0,  // 长时间无变化时也兜底同步一次（秒）
  "bridge_port": 8765,   // 给 Chrome 扩展用的本地端口（0 = 关闭）
  "bridge_timeout": 90.0 // 扩展多久没心跳就认为离线（秒）
}
```

**必须知道的两条限制**（任何同类工具都绕不过去，因为都只能操作文件）：

1. **浏览器把书签放在内存里，只在落盘时才写文件。** 所以"实时"的粒度是**浏览器保存书签的频率**，不是你点收藏的瞬间。Safari 尤其倾向于延后写盘（有时几十秒），表现为"Chrome 的改动很快到 Safari，Safari 的改动却迟迟不过去"——这时 `bmsync` 还没机会看到文件变化。
2. **运行中的浏览器会整份重写书签文件。** 它用内存里的内容覆盖磁盘，所以本工具写进去的内容可能在几秒后被它抹掉——页面上看不到，关闭浏览器时还可能被彻底覆盖。

第 2 条的应对方式由 `sync.require_browsers_closed` 决定（默认 `true`）：

| 取值 | 行为 |
| --- | --- |
| `true`（默认） | 实时模式照常同步，但每次触发都会在日志里提示"浏览器正在运行，内容可能被覆盖"；手动 `sync` 则直接拒绝执行 |
| `false` | 明确接受覆盖风险，不再提示 |

**想要双向都可靠落地**，推荐这样用：退出 Chrome 和 Safari → `bmsync sync`（或让实时模式自己触发一次）→ 再打开浏览器。这样两边都是"退出时写盘、启动时读盘"，不会有内存覆盖。

### 让 Chrome 开着也能同步：配套扩展（推荐）

上面第 2 条限制的根治办法是用 Chrome **官方的书签 API**——也就是一个扩展。仓库里的 `extension/` 就是干这个的：

```
extension/
├── manifest.json    # MV3，只申请 bookmarks 权限
├── core.js          # 应用改动的核心逻辑（与 chrome API 解耦）
├── test_core.mjs    # node extension/test_core.mjs 自测核心逻辑
├── test_background.mjs  # node extension/test_background.mjs 自测后台脚本
├── test_popup.mjs       # node extension/test_popup.mjs 自测弹窗（module 加载/元素 id/渲染）
├── background.js    # 定时向本地桥接拉取"要新增/删除什么"，用官方 API 应用
├── popup.html/js    # 点扩展图标看：连接状态、待应用改动、最近变动
```

> 容器定位用的是 Chrome 的**固定文件夹 ID**（书签栏 = `1`、其他书签 = `2`），
> 而不是文件夹标题——标题会随界面语言变化（英文版是 `Bookmarks bar`），按标题找会失败。

**安装**（一次性，约 1 分钟）：

1. Chrome 地址栏打开 `chrome://extensions`
2. 右上角打开 **开发者模式**
3. 点 **加载已解压的扩展程序**，选择仓库里的 `extension` 目录
4. 在终端运行 `./bin/bmsync watch`（或 `watch-daemon start`）
5. 点扩展图标，看到 **已连接** 就生效了

> 更新过扩展代码后，需要到 `chrome://extensions` 点一下这个扩展的**刷新**按钮重新加载
> —— 弹窗和后台脚本都是 ES module，改动后不刷新会继续跑旧代码（表现为弹窗报
> `BmsyncCore is not defined` 之类的错误）。

**弹窗里能看到什么**（点扩展图标）：

- **连接状态**：已连接 / 未连接，以及上次检查时间
- **待应用到 Chrome**：还没落地到 Chrome 的改动清单（最多列 4 条）
- **最近变动**：每次同步的增删改数量 + 扩展"已应用到 Chrome"的回报，按时间倒序
- 图标角标显示待应用项数（没有待办时不显示）

需要更细的内容时看 `state/bmsync.log`（逐条改动明细）。

**它如何工作**：`bmsync watch` 会在 `127.0.0.1:8765` 起一个只监听本机的桥接服务（可用 `watch.bridge_port` 改端口，设 `0` 关闭）。扩展每 20 秒拉一次差异，用 `chrome.bookmarks` 应用新增/删除/改名。

**装上之后的行为变化**：

| | 扩展未安装/未连接 | 扩展已连接 |
| --- | --- | --- |
| Chrome 落盘方式 | bmsync 直接改 `Bookmarks` 文件（浏览器运行时可能被覆盖） | **扩展通过官方 API 应用，立即生效且不会被覆盖** |
| Chrome 文件 | 会被改写 | **完全不动**（避免和扩展互相覆盖） |
| 可靠性 | Chrome 运行时可能丢改动 | 双向可靠 |

**安全说明**：桥接只绑定 `127.0.0.1`，不对外暴露；扩展只申请 `bookmarks` 权限，且只访问 `127.0.0.1`，不向任何外部服务器发送数据。所有代码都在 `extension/` 里，可以直接审阅。

**扩展不在线时会怎样**：bmsync 会退回"直接改文件"的方式，并且只在**两边都确实拿到**某条书签后才把它记进基线——所以未落地的改动会在下一轮自动重试，不会丢，也不会被误判成删除。

> 判断"到底是没同步、还是浏览器延后写盘"，看一行就知道：
> ```bash
> while :; do stat -f "%Sm %N" -t "%H:%M:%S" ~/Library/Safari/Bookmarks.plist; sleep 1; done
> ```
> 在 Safari 里加个书签，看这行时间**多久才变**。如果它迟迟不变，说明 Safari 还没落盘，跟同步无关。

其他行为：

- 用 `flock` 保证**只有一个实例**在跑；已有实例时会拒绝启动（避免两个进程互相踩）
- 同步时用 `--force`：即使浏览器开着也会写（浏览器退出时可能用内存数据覆盖，建议还是让它自己落盘后再同步）
- 常驻服务带 `KeepAlive`，被系统杀掉或崩溃后会自动拉起（`ThrottleInterval` 10 秒防抖）
- 服务日志在 `~/Library/Logs/bmsync/watch.{out,err}.log`，同步明细仍在 `state/bmsync.log`

## 4. 定时同步

```bash
./bin/bmsync init-config                       # 先生成配置文件
./bin/bmsync install-scheduler --interval 60   # 每 60 分钟一次（最小 5 分钟）
./bin/bmsync install-scheduler --dry-run       # 只看看会写入什么，不真装
./bin/bmsync install-scheduler --no-verify     # 装完不立即触发验证
```

安装命令做完三件事，尽量不留悬念：

1. **先做权限前置检查** —— 读不到 Safari 或写不了 Chrome 就直接拒绝安装，并告诉你缺哪一项（避免装了个注定失败的任务）；
2. **先卸掉内存里的旧定义再装新的**（`bootout` + `bootstrap`）—— 只改 plist 文件而不重新加载，launchd 里跑的还是旧配置，这正是"`launchctl start` 立刻失败、退出码 78、连日志都没有"的常见原因；
3. **立刻触发一次并回报退出码** —— 不用等一个同步周期就知道行不行。

装好后，**务必按下面的方式确认它真的生效了**（`launchctl start` 是异步的，敲完不代表跑成功）：

```bash
launchctl list | grep bmsync        # 1. 确认已加载（能看到 com.bmsync.sync）
launchctl start com.bmsync.sync     # 2. 触发一次

# 3. 看退出码——这一步最关键
launchctl print gui/$(id -u)/com.bmsync.sync | grep -E 'last exit code|state'

# 4. 看这次同步到底做了什么
tail -20 state/bmsync.log
```

退出码的含义：

| 退出码 | 含义 | 处理 |
| --- | --- | --- |
| `0` | 成功 | 没问题，改动明细在 `state/bmsync.log` |
| `2` | 文件不可读写 | 多半是定时任务没有磁盘访问权限，见下面「launchd 的权限」 |
| `3` | 浏览器没退出 | 退出 Chrome/Safari 后重试（任务用 `--force` 时不会出现这个码） |
| `78` | launchd 根本没能启动进程 | 程序路径失效或不可执行，重新运行一次 `install-scheduler` 并重新加载 |
| 其它 | 运行中出错 | 看 `~/Library/Logs/bmsync/scheduler.err.log` |

任务日志固定放在 **`~/Library/Logs/bmsync/`**（`scheduler.out.log` / `scheduler.err.log`），不放仓库里——否则仓库在外部卷或受保护目录时，launchd 可能连日志文件都创建不了，失败时一点线索都没有。也可以换位置：

```bash
./bin/bmsync install-scheduler --interval 60 --log-dir ~/Documents/bmsync-logs
./bin/bmsync uninstall-scheduler    # 卸载
```

### launchd 的权限（失败时的头号原因）

launchd 拉起的进程**不会继承终端的权限**，需要单独授权：

1. 系统设置 → 隐私与安全性 → **完全磁盘访问权限** → `+`
2. 按 `Cmd+Shift+G`，输入 `/usr/bin/python3` 并添加（这是定时任务实际使用的解释器；用 `./bin/bmsync install-scheduler` 装完会打印确切路径）
3. 到 **隐私与安全性 → 文件与文件夹 / 完全磁盘访问权限** 里确认没有"可移除卷"相关的拦截——如果仓库放在外部磁盘（`/Volumes/...`），macOS 会额外要求"可移除卷"访问权限

改完权限后重新加载一次任务：

```bash
launchctl bootout gui/$(id -u)/com.bmsync.sync 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bmsync.sync.plist
launchctl start com.bmsync.sync
```

也可以用 cron（注意换成你自己的路径）：

```cron
*/60 * * * * /usr/bin/python3 "/path/to/bmsync/bmsync.py" sync --force --quiet
```

> launchd/cron 拉起的进程同样受 TCC 限制：如果它报权限错误，需要把 `/usr/bin/python3` 单独加进"完全磁盘访问权限"。

## 5. 它到底怎么判断"谁改了什么"

关键在于 `state/state.json` 里保存的**基线**（上次同步结束时的统一状态）。每次同步做三方比对：

| 情况 | 处理 |
| --- | --- |
| 只有一边新增 | 加到另一边 |
| 只有一边改名 / 换地址 / 移动 | 同步过去（移动 = 删除 + 新增） |
| 只有一边删除 | 另一边也删除 |
| 两边都改了同一个书签 | 按修改时间取较新的，并在输出里列出冲突（可用 `--prefer` 指定） |
| 一边删除、另一边修改 | 比较时间，较新的赢 |
| 两边都新增同一个书签 | 只保留一份 |

书签的"身份"按 URL 认定（规范化后比较：大小写、末尾斜杠、`%20` 与空格、默认端口等差异视为同一个），文件夹按"父目录 + 名字"认定。

**顺序不作为同步目标**——两个浏览器的排序机制不同，强行对齐只会每次都被判定成改动。内容与层级保证一致，顺序各自保留。

## 6. 特别说明（重要）

- **首次同步会把两个浏览器的书签并成一份**（不推断删除，避免误删）。之后才进入增量模式。
- **Chrome 的"移动设备书签"**：统一树里它就是 `其他书签/移动设备书签` 这个普通目录；写回 Chrome 时会还原到 `synced → Mobile bookmarks` 容器。若 Chrome 把书签直接挂在 `synced` 下，会被整理进 `Mobile bookmarks` 文件夹（保留原有 guid，只影响结构，不丢数据）。
- **Safari 的阅读列表（Reading List）默认不参与同步**，按 `WebBookmarkSpecialID` 识别，容器及其内容原样保留。因为阅读列表条目看起来就是普通书签，一旦导入会在 Chrome 里变成重复书签。要改就编辑 `config.json` 的 `sync.skip_folders`。
- **Safari 的"个人收藏"= Chrome 的"书签栏"**，Safari 的"书签菜单"= Chrome 的"其他书签"。
- **不会动 guid / UUID**：能对上的节点沿用原有 id 和 guid，减少 Chrome 云端同步的额外动作。
- **写文件用"先写临时文件再替换"**，并在写入前把两边原文件备份进 `state/backups/`。
  备份做了三层控制，实时同步也不会爆量：
  1. **按内容去重**：忽略 `checksum`、`date_last_used` 等每次都会变的字段，内容没变就不新增备份
  2. **最小间隔**：`sync.backup_min_interval`（默认 300 秒）内的连续变化只留最近一份
  3. **分级保留**：24 小时内全留、1 周内每小时 1 份、1 个月内每天 1 份、更早只留 `backup_keep` 份
- 只同步**书签**，不碰历史记录、扩展、密码等其他数据。

## 7. 配置文件

`./bin/bmsync init-config` 会在仓库目录生成 `config.json`：

```json
{
  "chrome": {
    "user_data_dir": "~/Library/Application Support/Google/Chrome",
    "profile": "Default"
  },
  "safari": {
    "bookmarks_path": "~/Library/Safari/Bookmarks.plist"
  },
  "mode": "manual",
  "watch": {
    "interval": 5.0,
    "settle": 3.0,
    "resync_after": 60.0,
    "bridge_port": 8765,
    "bridge_timeout": 90.0
  },
  "sync": {
    "direction": "both",
    "prefer": "newest",
    "skip_folders": ["com.apple.ReadingList", "Reading List", "阅读列表"],
    "backup_keep": 20,
    "backup_min_interval": 300,
    "require_browsers_closed": true
  },
  "state_dir": "/path/to/bmsync/state",
  "log_file": "/path/to/bmsync/state/bmsync.log"
}
```

- `mode`：`manual` / `scheduled` / `watch`（用 `bmsync mode` 切换更省事）
- `watch`：实时模式的节奏参数，见第 3 节；`bridge_port` 是给 Chrome 扩展用的本地端口（0 = 关闭）
- `backup_keep` / `backup_min_interval`：备份保留份数与最小间隔，见第 6 节
- `direction`：`both` 双向；`chrome2safari` / `safari2chrome` 单向（只把源的变化应用到目标，目标自己的书签不受影响，冲突一律以源为准）
- `prefer`：`newest`（默认，按修改时间）/ `chrome` / `safari`
- 其他 Chromium 系浏览器（Edge、Brave、Vivaldi）改 `user_data_dir` 即可

多配置文件：

```bash
./bin/bmsync --config ~/bm-work.json sync
```

## 8. 出问题时

| 现象 | 原因 / 处理 |
| --- | --- |
| `[错误] 无法读取 Safari 书签` | 没给完全磁盘访问权限，见第 1 节（`chmod`/`sudo` 无效） |
| `[错误] 请先退出：Chrome` | 退出浏览器；确实需要时加 `--force` |
| 同步后浏览器里看不到变化 | 浏览器还开着，被内存数据覆盖了；退出后重跑 |
| 想知道到底改了什么 | `state/bmsync.log` 里有每次同步的逐条记录 |
| 改坏了想回滚 | 从 `state/backups/` 取对应时间的备份覆盖回去 |
| 想从头再来 | 删掉 `state/state.json`（下次同步会按"首次同步"重新建立基线） |

回滚 Chrome 备份（**退出 Chrome 后**执行）：

```bash
cp state/backups/chrome.20261016-195845.bak \
   ~/Library/Application\ Support/Google/Chrome/Default/Bookmarks
```

## 9. 项目结构

```
bmsync/
├── bmsync.py                    # 全部实现，单文件，纯标准库
├── bin/bmsync                   # 启动器（可直接 ./bin/bmsync sync）
├── tests/test_bmsync.py         # 99 项端到端测试
├── .github/
│   ├── workflows/tests.yml      # CI：macOS 上跑测试 + lint
│   └── ISSUE_TEMPLATE/          # Issue 模板（引导附带 doctor 输出）
├── extension/                   # 配套 Chrome 扩展（让浏览器开着也能同步）
├── CHANGELOG.md                 # 变更记录
├── pyproject.toml               # 元数据（运行不需要安装）
└── state/                       # 运行时生成（已在 .gitignore 中）
    ├── state.json               # 同步基线（谁改了什么全靠它）
    ├── bmsync.log               # 改动日志
    ├── backups/                 # 每次写入前的原文件备份
    └── export/                  # export 命令的输出
```

## 10. 开发与贡献

欢迎提 Issue 和 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md)。跑测试：

```bash
python3 tests/test_bmsync.py
```

- 本机有 Chrome 书签文件时，往返测试会用它做**真实数据的无损验证**
- 没有时会自动回退到内置合成夹具（覆盖重复 URL、特殊协议、NBSP、emoji、5 层嵌套等情况）
- 也可以用环境变量指定别的书签文件：`BMSYNC_TEST_CHROME=~/Downloads/Bookmarks python3 tests/test_bmsync.py`

提交前建议顺手跑一下 lint：

```bash
pip install ruff
ruff check .
```

## 11. 已知限制

- 顺序不同步（理由见第 5 节）。
- Safari 里"收藏"和书签栏是同一份数据；在 Safari 收藏一个页面会同时出现在书签栏，反向同步时也会进 Chrome 书签栏，这符合预期。
- 极短时间内的双向改动仍以时间戳判定，可能无法完全反映你的真实意图；脚本会把冲突列出来供你核对。
- **实时模式的延迟取决于浏览器**：书签先写在内存里，落盘后才能被看到（见第 3 节）。真正的"零延迟"做不到。
- 实时模式用轮询而非 FSEvents：浏览器是「写临时文件 + 原子替换」，路径级事件对这种方式不友好；mtime+size 轮询在各文件系统上都可靠，开销也可忽略（每秒不到 1ms）。
- 只在 macOS 上有意义；Linux/Windows 上跑不起来（没有 Safari）。

## License

[MIT](LICENSE)
