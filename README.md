# AgentGC

[中文](README.md) | [English](README.en.md)

追踪 AI Agent 创建和修改的文件，让持续运行的 Agent 留下的产物可查、可管、可清理。

项目名 **agentgc**，命令行工具名 **agcc**。产品形态是 **Skills + CLI**。

## 已实现

- **跟踪**：用 `agcc run -- <命令>` 包装任意命令，或用 `agcc watch` 观察指定目录。记录会话、路径、大小、时间和文件变化。
- **查询**：按时间、大小、活跃程度排序；搜索路径、备注和 Agent 名称；查询文件或会话的历史；支持 JSON 与分页。
- **标记**：`keep`（保留）、`temporary`（临时）、`review`（待确认）、`disposable`（已确认可清理）。
- **索引**：本地 SQLite 保存正式记录。文件扩展属性是可选提示，丢失或不可写不会使索引失效。
- **整理**：默认只显示清理预览。明确确认后，将文件移入同一文件系统中的隔离目录；可恢复，不覆盖已有文件。
- **Skill**：随安装包提供，可打印或安装到 Agent 的技能目录。

当前版本是可运行的 **0.1.0**，不是全系统审计器，也不是资源限制工具。

## 安装

需要 Python 3.11 或更高版本。运行时没有第三方依赖。

```sh
git clone https://github.com/LIghtJUNction/AgentGC.git
cd AgentGC
uv tool install .
agcc --version
```

仓库为私有时，克隆需要你自己的 GitHub 访问权限。也可以在已有的 Python 虚拟环境中执行 `python -m pip install .`。

## 日常最常用的几条

在项目目录中执行：

```sh
agcc run --agent assistant --root . -- your-agent-command
agcc list --root . --sort size --limit 20
agcc history --root . --limit 20
agcc sessions --root . --status running
agcc mark result.json keep --note "需要交付的结果"
```

`--root .` 将查询限定到当前项目；不传时仍查询共享索引中的所有项目。`agcc --help` 和 `agcc <命令> --help` 提供可复制的常用示例。直接编辑文件的 Agent 应使用下文的后台 `watch` 流程。

## 运行一个完整示例

在一个测试目录中执行：

```sh
agcc run --agent demo --root . -- python -c "from pathlib import Path; Path('scratch.txt').write_text('demo')"
agcc --json list --root . --sort size
agcc history scratch.txt
agcc mark scratch.txt disposable --note "演示已完成，文件没有剩余用途或引用"
agcc gc scratch.txt
agcc gc scratch.txt --apply --confirm-unused
agcc quarantine
agcc restore <上一步输出的隔离记录ID>
```

恢复后的文件自动标记为 `keep`。隔离只是移动文件，**不会释放磁盘空间**。本版本没有永久删除命令。

## 跟踪实际的 Agent

```sh
agcc run --agent assistant --root . -- your-agent-command
agcc run --agent assistant --root . --root /path/to/outputs -- your-agent-command
agcc watch --root . --agent existing-session
agcc watch --root . --duration 60 --backend poll --interval 2
agcc --json watch-start --root . --agent assistant --wait-ready 30
agcc watch --root . --agent assistant --control task-UNIQUE_ID --duration 3600
agcc watch-status task-UNIQUE_ID --wait-ready 30
agcc watch-stop task-UNIQUE_ID --wait 30
agcc scan --root .
```

`watch-start` 直接启动后台 watcher，并在同一次调用中等待基线成功和就绪验证，返回 `control`、会话 ID、覆盖目录、排除项、跟踪质量和私有日志路径。默认自动生成唯一控制名；可显式传 `--control`，但不能复用。启动超时或失败时只回收本次启动的直接子进程，不接管其他 watcher；回收失败会明确报告。默认有限运行 3600 秒。结束时执行 `agcc watch-stop <control>`，默认等待最多 30 秒完成最终扫描；只想发请求用 `--no-wait`。

原来的 `watch --control` 仍可用于执行工具自己管理后台进程。优先复用已确认存活、同一状态目录且覆盖本任务根目录与排除规则的跟踪，不停止其他任务的 watcher。控制名只允许 1–64 个 ASCII 字母、数字、下划线或连字符，不能复用。

基线成功后，完整就绪记录原子发布到 `<状态目录>/watches/<控制名>/ready.json`，包含会话 ID、PID、根目录、排除项和预计到期时间。状态目录可通过 `agcc --json doctor` 查询。`watch-status <控制名> --wait-ready 30` 在一个 CLI 调用内等待就绪，只输出一次结果，不需要 Agent 反复读取整个会话列表。失败或超时返回非零；开始写入前还须核对 `roots`、`excludes`、`coverage_known` 和 `gaps`。旧版就绪记录的排除范围未知，不能仅凭运行状态推定覆盖完整。心跳只是近期活动证据，不证明写入者身份。控制命令以非写入连接读取已有状态，不初始化或迁移数据库，也不因基线的写事务争抢写锁；等待预算包含打开状态的时间。状态尚未初始化时只等待并报告 `starting`，不创建新目录。

`watch-stop` 通过协作式请求让 watcher 自己最终扫描和结束，不发送记录中的 PID 信号，也不直接关闭会话。默认等待 30 秒，可用 `--wait` 调整、`--no-wait` 只发请求。就绪与停止等待最多 300 秒，内部有界退避，只输出一次结果。默认停止等待要求跟踪质量 `complete` 才返回零；`--no-strict-tracking` 可接受已结束的 `partial`，仍不会接受基线失败、中断或未知结果。停止跟踪不证明所有业务写入者停止，也不授权清理。

`run` 默认保留子进程的输入、输出和退出码，不使用额外的 shell。命令中需要 shell 运算符时，应显式调用 shell。会话信息写入 stderr，不混入子进程 stdout。包装器把 `AGENTGC_SESSION` 和 `AGENTGC_HOME` 传给子进程。

### 命令结果与跟踪质量

命令和跟踪分别显示。例如测试通过，但观察到未捕获元数据的短命文件时：

```text
command: succeeded; command_exit_code=0
tracking: partial; gap_count=4; unresolved_event_count=12
```

`run --strict-tracking` 会在命令成功但跟踪不完整时返回非零；`command_exit_code` 仍保留真实子进程结果。`watch` 和停止等待默认严格，`--no-strict-tracking` 可接受正常结束的部分跟踪。`complete` 只表示正常完成且没有已记录缺口或未解析事件，不证明所有中间历史都被捕获。未启动的命令退出码是 `null`，不伪装成 0；旧会话缺少独立结果时显示 `unknown`。旧生命周期 `session_status=failed` 不能用来判断测试是否失败。

```sh
agcc --json session SESSION_ID
agcc --json sessions --root . --limit 20
agcc diagnostics --session SESSION_ID
agcc --details diagnostics --session SESSION_ID
```

会话摘要 JSON 使用 `schema_version=1`，各入口统一提供 `command_exit_code`、`command_status`、`tracking_status`、`gap_count`、`unresolved_event_count` 和数组 `roots`。`gap_count` 是会话累计计数；`unresolved_event_count` 是尚保留的事件计数，`count_scope` 明确两者口径。缺失数值用 `null`。数据库内部仍保留原有生命周期字段，命令与跟踪结果在独立表保存，最终质量和生命周期同时提交。

缺口按链接保护边界 `expected-boundary`、元数据不可用 `metadata-unavailable`、覆盖缺失 `coverage-loss`、未知 `unknown` 分级；删除通知单列 `removal-notice`，不当作丢失文件。默认诊断按实际事件路径的根目录和一级相对父目录聚合；旧自由文本不猜路径，前缀保持未知。复合分类和 typed notice 可能与原缺口同因，不能相加当独立缺口数。即时提示不再打印整串路径，原文仍完整保存在历史中，`--details diagnostics` 展开原始分组。

根目录可以重复指定；相互包含的根目录会合并。默认跟踪当前目录。工作目录仍是启动 `agcc` 时的目录，`--root` 不会切换子进程的工作目录。用 `--exclude cache` 或 `--exclude '*.log'` 排除路径。匹配使用区分大小写的相对路径或路径分量，不是 `.gitignore` 语法。

默认跳过 `.git`、AgentGC 自己的状态目录、`.agentgc-quarantine`、符号链接、Windows 重解析点和非普通文件。不会默认忽略 `.venv`、构建目录或依赖目录，因为它们也可能是需要管理的产物。

### 跟踪边界

**只跟踪所选根目录。** 缓存、临时目录或产物位于根目录外时，必须添加对应的 `--root`。本版本没有系统范围的进程文件访问拦截。

| 平台 | 自动选择的跟踪方式 | 可选文件标记 |
| --- | --- | --- |
| Linux | inotify；无法使用时明确提示并退回扫描 | `user.agentgc` 扩展属性 |
| macOS | 定时扫描 | `io.agentgc.meta` 扩展属性 |
| Windows | 定时扫描 | 支持此能力的文件系统上的 `:agentgc` 数据流 |

局部目录权限不足、注册监听时路径消失或链接边界拒绝不会再让全部原生监听退回轮询；健康目录继续使用 inotify，受影响路径及缺口仍会记录，并在完整核对周期重试。临时不可读子树中已经注册的监听保留；链接替换和消失目录不保留旧监听。监听预算和内核资源耗尽仍属于整体故障，会按 backend 策略失败或退回轮询。启动基线必须完整，失败仍不启动子命令、不发布 readiness；运行中不完整扫描仍不会把不可读文件判为删除。结束时仍有局部原生覆盖缺口，会话标为失败，`watch` 返回非零；`run` 保留子命令退出码。此行为只在 Linux 测试。

`--backend native` 明确要求 Linux 原生事件，无法启用时会失败，不会静默改用扫描。Linux 默认最多注册 8192 个目录监听；超过限制可用 `--backend poll`。扫描间隔默认 1 秒，最低 0.1 秒。Linux 原生模式每 60 秒核对一次完整目录；可用 `--reconcile` 调整。

启动前建立基线，结束时再做一次完整核对。Linux 退出时分批排空已排队事件；排空预算为最多 32 批或 1 秒（每批结束后检查），仍有积压时记录 `gap`，不会无限等待持续写入者。最终事件读取失败也会继续核对仍存在的文件，并将会话标为 `failed`；无子进程的 `watch` 返回非零退出码，`run` 仍保留子进程退出码。Linux 会保留已收到的短命文件事件；文件已消失时，大小等字段保持未知。定时扫描可能漏掉两次扫描之间创建又删除的文件。原生事件也可能合并或丢失；新目录监听建立前、目录移动过程中和事件队列溢出时，都不能保证完整中间历史。溢出或读取失败会记录 `gap`；核对只能恢复仍存在的文件状态。

文件改名按旧路径消失、新路径出现记录。不会假装已经实现可靠的跨路径身份合并。记录的是元数据历史，**不保存文件内容、补丁或旧版本**。

### 来源不等于作者证明

`baseline` 表示建立基线时已存在的文件。`session-correlated` 表示文件在该会话观察期间发生了变化，**不证明是该 Agent 写入**。`filesystem-event` 表示收到了文件系统事件，也不包含写入者身份。`sessions.pid` 是包装器 PID，不是文件写入者 PID。

共享目录内的人类操作和其他进程也可能被观察到。本版本没有实现 Linux eBPF/fanotify、macOS Endpoint Security 或 Windows ETW 的进程级归属。Agent 名称是用户提供的会话标签。

## 输出格式

默认输出紧凑 Markdown：命令标题、摘要和项目符号明细，不再输出缩进 JSON。容量使用可读单位；内部签名、摘要和重复测量不在普通摘要中展开。普通列表默认最多显示 20 条已返回记录，明确省略数量；`--rows N` 调整展示条数，`--details` 显示全部已返回 Markdown 明细（不展开所有原始字段），均不扩大 `--limit` 查询范围。`list/search/history/sessions` 显示下一页 offset，需保留原筛选条件；会话保留查询顺序，使用 `--status running` 查看活跃会话，自动标记优先展示未知或活跃状态。JSON 也仍受查询上限约束，不等于完整索引。

```sh
agcc list --sort size --limit 20
agcc history path/to/file --limit 20 --offset 20
agcc --rows 5 sessions
agcc --details sync --dry-run           # 包括未变化的规则观察
agcc --json list --sort size --limit 100   # 脚本解析、原始字段与完整返回记录
```

`--json` 不做 Markdown 展示裁剪；不要用 Markdown 做机器解析。`session/sessions` 返回版本化摘要，`sessions` 不再直接序列化数据库行：使用 `session`、`session_status` 和独立命令／跟踪字段，`roots` 为数组。诊断 JSON 和历史仍保留原始证据。GC 拒绝、恢复、文件系统风险和错误不加展示层截断；默认诊断按类别和前缀汇总，`--details` 展开原始缺口。路径、备注中的控制字符、双向字符与 HTML 转义显示，JSON 保留原值。`run` 的子进程 stdout/stderr 不变，默认退出码跟随子进程，严格策略另行选择；摘要在 stderr。零计数不证明历史完整，命令成功不等于跟踪完整。

## 查询与标记

```sh
agcc list --sort time
agcc list --sort size --limit 50 --offset 0
agcc list --sort activity --label temporary
agcc --json search output
agcc history path/to/file
agcc history --session SESSION_ID
agcc list --session SESSION_ID
agcc list --root . --sort size --limit 20
agcc search output --root .
agcc history --root . --limit 20 --offset 20
agcc sessions --root . --status running
agcc sessions --root . --limit 20 --offset 20
agcc sessions
agcc stats
agcc mark path/to/result keep --note "需要交付的结果"
agcc mark path/to/scratch temporary --note "尚未确认可以清理"
agcc mark result.json receipt.json keep --note "本轮交付产物"
agcc mark temporary --files-from paths.txt --note "中间文件，未授权清理"
find ./outputs -type f -print0 | agcc mark review --files-from - --null
agcc diagnostics --session SESSION_ID
```

`list/search/history/sessions` 支持重复 `--root`，按目录并集筛选后再分页；与 `--label`、搜索文字或 `--session` 等已有条件同时生效。文件查询按已索引的实际路径筛选，`project` 不会匹配 `project-old`，路径中的 `%`、`_` 等字符没有通配含义。这只是查询本机记录，不扫描目录、不要求目录仍然存在，也不修改文件分类或会话。查询根目录不解析符号链接别名；使用索引显示的路径。

会话查询按跟踪根目录是否交叠筛选，因此覆盖当前项目的父目录 watcher 也会显示。`--status` 可选 `running/finished/failed/interrupted/closed-by-user`；`--limit` 默认 100，最多 10000，`--offset` 用于翻页。`history --root` 保留交叠会话的无路径缺口；这样的缺口可能也影响同会话的其他目录，不能视为当前项目的精确归属。完整会话诊断仍用 `diagnostics --session`。`stats` 保持共享索引的全局统计。

显式指定 `history <文件>` 时仍只返回该文件的路径事件；查看会话缺口请使用根目录历史或会话诊断。Markdown 中的历史、观察和会话时间显示为 UTC ISO 8601 时间，精确到秒，末尾 `Z` 表示 UTC；未知或无效时间显示 `unknown`。JSON 保留原始数值时间戳，便于脚本处理。

`mark` 支持多路径，以及 UTF-8 换行清单 `--files-from`（`-` 从 stdin 读取）；含换行的文件名使用 `--null` 的 NUL 分隔清单。清单中的相对路径相对于当前工作目录，不展开 glob、不递归目录。批量调用逐文件提交，失败不阻止其他路径；返回成功和失败数量、每个失败原因，部分失败退出码为 1。原来的单路径语法和 JSON 文件记录保持兼容。

新普通文件可立即标记：仅登记明确指定的文件元数据，不扫描父目录，也不补造此前历史。来源为 `manual-observation`，没有写入者或跟踪会话归属。拒绝链接、目录和内部状态；仍不允许直接重标缺失或隔离记录。`disposable` 的备注、活跃会话、Git、硬链接、摘要及已索引文件变化检查保持不变。

`diagnostics --session` 按类别和路径前缀展示紧凑诊断，`--details` 显示按相同原始详情合并的记录、次数、首末时间及影响说明。权限、链接边界、路径消失、事件溢出等文本分类仅是提示，不能断定正常卸载或临时文件。复合记录保留多个匹配分类及原文。未解析事件区分事件数和不同路径数：它们**不是丢失文件数**，删除通知可能是正常移除。最终扫描只能恢复仍存在的状态；历史清理后只汇总尚保留记录。`run/watch` 摘要显示相同的独立结果和紧凑诊断。不会删除原始缺口。

诊断还显示累计 `gaps`、尚保留的 `retained_gap_events`、缺少详情的 `missing_gap_details` 和分类计数 `categories`。复合记录可能计入多个分类，分类次数不能相加当作独立缺口数。累计计数大于保留记录时明确提示缺失历史；无法还原其分类或影响，也不能断定一定由 prune 导致。保留记录反而超过累计计数时显示不一致，缺失详情数为 unknown，不生成负数或假装正常。汇总在一个只读快照中完成，避免并发跟踪／历史裁剪混用不同时刻的计数；这些字段只描述记录，不证明当前覆盖完整。

时间排序使用最近观察到的变化时间。活跃程度是累计观察到的文件状态变化次数，不是读访问次数。`stats` 的字节数是现存普通文件的逻辑大小；硬链接可能重复计数，不等于实际磁盘占用。

`--json`、`--home`、`--rows` 和 `--details` 可以放在子命令前或后；`run --` 后的参数仍属于子命令。`list` 与 `history` 默认最多返回 100 条记录；查询上限为 10000。搜索按文本匹配，不把 `%` 或 `_` 当作用户输入的通配符。

加 `--tag-files` 可在 `run`、`watch` 或 `mark` 时写入文件标记。标签只包含小型索引引用、会话和分类，不包含文件内容。标签可能不受文件系统支持、变旧、被复制或被篡改，**不能据此授权清理**。正式记录始终在 SQLite 中。

## 项目规则 `.agcc` 与本机同步

将 TOML 格式的 `.agcc` 放在项目根目录并提交到 Git。其他成员执行 `agcc` 时使用同一套规则，各自的状态仍保存在本机 SQLite，不上传、不继承其他机器的 inode、会话或清理许可。

```toml
version = 1
extends = ["builtin:standard"]

[auto]
enabled = true
after = ["run", "watch", "scan"]

[limits]
max_entries = 200000
max_seconds = 30

[[rules]]
id = "build-pressure"
[rules.when]
any = [
  { size_gte = "2 GiB" },
  { files_gte = 100000 },
  { growth_gte = "256 MiB", growth_percent_gte = 25 },
]

[[rules]]
id = "python-cache"
[rules.match]
path = ["**/__pycache__", "**/.pytest_cache"]
kind = "directory"
[rules.then]
suggest = "temporary"
message = "可再生成缓存；不是清理许可"
```

同 ID 深度覆盖内置规则的表字段，数组整体替换；`enabled = false` 禁用提醒，`extends = []` 不继承默认提醒。新规则需要 `match` 和 `then`。匹配支持相对路径 `path` 数组、限定子目录 `under`、`kind = file/directory/any`；`*` 匹配单层、`**` 匹配零或多层，路径锚定 `.agcc` 所在目录。所有匹配字段须同时满足。禁止绝对路径、`..`、链接配置和任意脚本。未知字段、重复 ID、非法单位或版本直接报错。

`when.any` 中任一条件组成立即可，同一条件组内必须全部成立。支持 `size_gte`、`files_gte`、`growth_gte`、`growth_percent_gte`；容量单位为 B/KiB/MiB/GiB/KB/MB/GB。省略 `when` 表示路径匹配后直接提出用途建议。`then.attention` 产生注意事项；`then.suggest` 只能为 keep/temporary/review，两者均为独立自动记录，**不覆盖人工文件标签，也不能创建 disposable 或取消清理保护**。

```sh
agcc rules show
agcc rules validate
agcc rules explain ./target
agcc sync --dry-run
agcc sync                     # 默认保存本机自动判断
agcc auto-mark                # 默认预览
agcc auto-mark --apply
agcc auto-marks               # 查询自动记录；reviews 仍查询原目录提醒
agcc --no-project-rules sync   # 忽略项目配置，使用内置提醒；核心保护仍有效
```

默认从当前目录向上找最近 `.agcc`，不越过 Git 仓库根目录；显式 `--root` 时从第一个根目录发现配置。可在子命令前后使用 `--config /path/to/.agcc` 显式选择。`sync` 未指定根目录时检查项目根；显式根只能位于所选项目内。多项目应分别同步。`run/watch/scan` 仍只观察原来选定的根，配置不扩大跟踪范围；仅在发现 `.agcc` 且 `[auto]` 启用时，最终扫描后自动应用规则。外部根导致自动同步失败时会明确报告，`run` 保留子进程退出码。只读查询不扫描，`gc` 不受自动规则授权。无项目配置的 `pressure` 保持原行为，有配置时使用项目规则；仍默认预览、`--apply` 才保存，命令阈值覆盖不修改 `.agcc`。

判断有命中、未命中、未知三态。不完整扫描只能证明已观察下限超标，不能解除提醒或将未访问路径判为消失。完整扫描建立与项目、根目录、排除项及计量方式绑定的本机增长基线；缺少基线或基线为零时增长比例未知。`notify.cooldown = "24h"` 限制重复提醒；`notify.clear_below = 0.8` 在降到阈值的 80% 以下且完整检查后解除，避免临界值反复提醒。内置状态、Git 元数据和隔离内容按跟踪边界排除；扫描是元数据快照，不是原子文件系统快照或写入者证明。

规则指纹与状态变更历史存于独立 SQLite 表，`sync --dry-run` 不更新规则状态、增长基线和提醒时间。它不更新人工文件索引或伪造写入历史；需要刷新文件索引时仍使用 `scan`。本版支持内置及项目层和命令覆盖，不支持远程同步、用户级配置文件、插件或任意外部继承。

## 目录膨胀检查与主动标记

```sh
agcc --json pressure --root .
agcc --json pressure --root . --root /path/to/cache --threshold build=1024 --apply
agcc --json reviews
```

`pressure` 对显式选择的目录做有预算的元数据检查，默认最多 200000 个目录项、30 秒；可用 `--max-entries`、`--max-seconds` 调整。它不读取文件内容、不跟随链接、不跨文件系统；每个文件系统需另选根目录。预算耗尽、权限不足或跨挂载跳过时会返回缺口和退出码 1，字节数只表示已观察部分，不是完整占用。计量使用逻辑大小，硬链接与嵌套候选可能重复，不能将全部候选简单相加。

`measurement_basis` 标明统计来源：目录字节为普通文件的 `st_size` 之和；磁盘容量来自 `shutil.disk_usage`（POSIX 为 `statvfs`）。原来的 `used_percent` 使用 `used/total`，另给出 `df_style_used_percent=used/(used+当前用户可用空间)`，保留块等因素可使两者不同。阈值仍按原公式判断。没有测量 Btrfs 底层分配、压缩、共享 extent 或多设备布局，也没有将逻辑字节当成可回收空间；文件系统类型未知时明确标为未知。

任一候选目录达到 100000 个文件也会提醒（`--file-threshold` 可调整），避免漏掉占用不大但文件数暴涨的测试残留。常见包缓存、工具安装、数据库和模型存储也会作为审核候选；包缓存如独立挂载的 `/var/cache/pacman/pkg` 必须显式选择。

默认提醒阈值：`target/build/dist/.next` 为 512 MiB，依赖目录 `node_modules/.venv/venv` 和常见缓存为 1 GiB，`log/logs` 为 256 MiB，隔离目录为 1 MiB，`Backups/docker/containers` 受保护存储为 5 GiB。文件系统容量使用率达到 80% 提醒、90% 紧急。通用分区的余量阈值分别为 `min(5 GiB, 总容量的 10%)`、`min(2 GiB, 总容量的 5%)`；boot 分区分别为 `min(64 MiB, 总容量的 10%)`、`min(32 MiB, 总容量的 5%)`。因此约 1 GiB、使用 49%、剩余约 527 MiB 的分区不会仅因绝对余量被报为紧急。默认只在所选路径所属设备的常见挂载点 `/boot`、`/boot/efi`、`/efi` 检出 boot 用途，不仅凭目录名判断。自定义 EFI 挂载点可用 `agcc pressure --root /custom/efi --disk-role boot`；`sync`、`auto-mark` 也支持此选项，`--disk-role general` 显式选通用策略。选项作用于本次全部所选设备，不同用途建议分开调用。JSON 和 Markdown 均显示用途、判断来源、总容量及实际余量阈值；采样不可用或无效时显示 unknown，不视为零或安全。阈值不是行业标准；目录大小可按项目通过 `--threshold 类别=MiB` 覆盖，类别见 `agcc pressure --help`。

`pressure` 和 `sync` 还会对每个已选文件系统采样一次 inode（文件系统的文件／目录条目容量），不增加目录扫描。按当前用户可用的空闲数 `f_favail` 计算，使用率达到 80% 提醒、90% 紧急，与容量风险取并集。JSON 的 `disks` 增加 `inode_status`、`total_inodes`、`free_inodes`、`inode_used_percent`、`inode_attention`、`inode_urgent`、`inode_reason`；Markdown 同样展示且不受普通行数限制。没有此 API、Btrfs 等报告无固定总量的文件系统标为 `unsupported`；读取失败或计数无效标为 `unknown`。未知数量为 `null`，不是零或安全证明；它不会把完整目录计量误判成扫描失败，也不改变增长基线和清理许可。

**超过阈值只表示需要注意，不表示可以删。** 默认只预览；`--apply` 将超阈值目录的 `review` 备注存入 SQLite 的独立目录记录，不改变文件分类，不授予清理许可。`reviews` 显示历史观察，操作前必须重测。数据库、容器卷、实际依赖和唯一备份需要保护，不能因目录名判断用途。

技能要求 Agent 在写入前跟踪、任务结束主动分类产物，并在构建、测试、安装、下载后检查增长点。已知临时产物即使低于阈值也应标记。可靠前后测量出现至少 256 MiB 且 25% 的增长、已关闭日志超过 14 天，建议主动提醒；这两项目前由技能指导 Agent 检查，不是 CLI 自动检测。

用户明确要求整机检查时，按授权范围选择本地文件系统与常见存储目录，不再默认只看当前项目。仍不扫描 `/proc`、`/sys`、`/dev`、`/run`，不提权，不隐瞒不可读或未完成范围。这些是可解释的默认策略，不是准确识别所有分区用途的探测器，也不代替发布前预计构建量与实际余量的比较。它不是全系统实时监控，也不能证明文件写入者身份。

## 清理保护

文件被 Agent 创建或修改，不代表它可以删除。`temporary` 和 `review` 都不是清理许可。

`disposable` 必须有解释用途的 `--note`。标记时保存文件身份、大小、修改时间及 SHA-256；执行时重新核对。索引观察到修改或替换后会撤销清理许可；即使内容被改成同样大小并还原修改时间，清理检查仍会拒绝。

清理拒绝符号链接、重解析点、硬链接、Git 已跟踪文件、无法确认的 Git 状态、正在跟踪的目录，以及发生变化或尚未审核的文件。恢复绝不覆盖现有目标。默认预览最多 1000 个候选，达到上限时返回 `truncated`。

`--confirm-unused` 是操作者确认“已经停止所有写入者，检查用途和引用”。**它不是工具完成了全系统打开句柄或引用检查。** 必须先停止仍在使用文件的程序，以及在父进程退出后仍存活的后台子进程。相同状态目录中的 AgentGC 会话会互相检查；不同状态目录中的会话不会自动互通。

AgentGC 不是安全沙箱，不能阻止同一用户的恶意进程持续替换路径。请勿在仍有不受控写入的目录中执行隔离。隔离保存于源文件父目录下的 `.agentgc-quarantine/<ID>/payload`，不做跨文件系统复制后删除。

## 中断与恢复

```sh
agcc doctor
agcc recover
agcc recover --apply
agcc quarantine
agcc restore QUARANTINE_ID
agcc end-session SESSION_ID --confirm-stopped
```

SIGINT/SIGTERM 会结束直接子进程并做最终扫描；不会声称已结束所有后代进程。被强制结束的跟踪会话保留 `running` 状态，继续阻止清理。不要仅凭心跳过期认定它已经安全停止。确认包装器和后代进程均已结束后，才使用 `end-session`。

隔离前先提交 `prepared` 操作日志，再移动文件。`recover` 核对中断的操作；`--apply` 只修正记录，不删除内容。不一致的副本进入待人工检查状态。恢复使用不覆盖目标的硬链接操作；不支持硬链接的文件系统会拒绝恢复并保留隔离副本。进程中断可能留下重复链接或空隔离目录；本版本不提供断电条件下的完整文件系统事务保证。

## 数据和资源

状态目录可用 `--home` 或 `AGENTGC_HOME` 指定。默认位置：Linux `$XDG_STATE_HOME/agentgc`（否则 `~/.local/state/agentgc`）；macOS `~/Library/Application Support/agentgc`；Windows `%LOCALAPPDATA%/agentgc`。

同一项目的相关任务应复用固定的磁盘状态目录和已就绪 watcher。不要为每条命令、重试或阶段新建数据库：每个新库都会重新保存所选目录的基线。Linux 的 `scan`、`run`、`watch`、`watch-start` 默认拒绝把状态放在 tmpfs/ramfs（常见于 `/tmp`、`/dev/shm`），在创建数据库或启动命令前报错；确有必要的有限临时实验可显式加 `--allow-memory-state`，仍受预算约束。其他平台暂不检测内存文件系统。

跟踪默认每次扫描最多核对 **10000 个文件候选**，数据库及 SQLite 附属文件合计预算为 **256 MiB**。预算在写入批次前检查，最后一个批次和失败记录可能使实际大小超过预算；这不是精确的文件系统配额。超限时失败并保留缺口，基线超限不会启动子命令或发布就绪记录，也不会把未访问的文件记为删除。已有超预算库仍可查询和停止 watcher，但拒绝新增跟踪。不会自动删记录、轮换新库、迁移或备份。

```sh
# 优先只选择本次确实需要的目录，复用默认状态目录
agcc watch-start --root ./src --root ./tests --agent assistant
# 只有明确需要较大覆盖范围时才提高预算；不是失败后的自动重试办法
agcc run --root . --max-files 20000 --max-state-mib 512 -- your-command
```

SQLite 使用 WAL、写入事务和忙等待。目录扫描在写事务外遍历，每批最多核对 128 个文件后提交；消失文件核对也使用有界分页，避免整棵目录树长期占用共享写锁。打开完整的已有数据库只检查结构，不重复写入初始化记录；旧数据库缺少的结构仍会升级。不同的本地进程可使用同一状态目录。跟踪因异常退出会记录缺口；最终扫描不能补回丢失的中间历史。锁长时间不释放时仍可能失败，不保证无限等待。状态目录应放在本地文件系统，不建议放在网络共享盘。POSIX 上新建状态目录使用 `0700`、数据库使用 `0600`；Windows 继承目录访问权限。这不是加密存储。

索引包含绝对路径、Agent 标签和人工备注，请按私有数据管理。不保存完整命令参数，不上传记录，不接入远程服务。不要在 Agent 名称或备注中写入密钥。跟踪只检查文件元数据；仅在审核和隔离核对时流式读取文件内容，缓冲区为 1 MiB。

目录扫描采用流式遍历，不在内存中保存第二份完整文件索引；POSIX 的消失文件核对只查询所选路径的索引范围，不遍历同一 SQLite 中所有其他项目。Windows 暂时保留原核对查询，避免路径大小写变化导致漏记删除。Linux 事件每批最多处理 4096 个路径，超限后记录缺口并核对目录。

Linux 原生后台 `watch` 空闲时阻塞等待事件或下一维护期限，不按 `--interval` 反复醒来；仍保留 5 秒心跳、定期完整核对、时长限制和最终扫描。停止文件通过私有目录的原生事件唤醒等待，SIGINT/SIGTERM 通过唤醒管道及时处理；空事件不启动 SQLite 写事务。控制目录不作为用户文件入库，其监听也计入 8192 个默认目录监听预算。唤醒不可用时明确提示并保留短等待；命令包装器仍定期检查直接子进程退出，macOS/Windows 仍使用扫描。扫描和原生目录监听仍会随文件数、目录数增长；不承诺固定资源占用，也不会自动删除内容以释放空间。

```sh
agcc prune --days 30
agcc prune --days 30 --apply
agcc prune --days 30 --rule-history          # 额外预览旧规则变更历史
agcc prune --days 30 --rule-history --apply
```

`prune` 清理已结束会话的旧事件，保留文件索引、累计活跃次数、分类、会话和隔离日志。`--rule-history` 是显式可选项：同时预览或清理旧规则变更事件，不删除当前自动标记、增长基线或规则配置。规则事件没有会话归属，因此只要状态目录中还有 `running` 会话（包括心跳过期的会话），就跳过规则历史并说明原因；仍可处理已结束会话的普通事件。默认不清理规则历史，不自动执行保留任务。仍运行会话的事件不会被清理。被清理的详细历史无法再查询；按会话筛选文件也依赖保留下来的事件。规则历史清理也不可撤销，未知时间戳和保留期限内的记录不删除。SQLite 会重用空闲页面，不保证数据库文件立刻变小。

## 安装 Skill

```sh
agcc skill
agcc skill --install /path/to/your-agent/skills
# 用户级全局技能目录（Pi 支持）
agcc skill --install ~/.agents/skills
```

全局技能要求 Agent 在本地修改代码或文档、构建、测试、安装依赖、运行脚本和生成产物前主动跟踪，无需用户专门提到清理。只读问答和纯远程操作无需本地 watcher。已有覆盖本任务目录与排除规则的可信跟踪可复用；直接编辑文件时，执行一次 `watch-start` 等待就绪后再写入，让它跨编辑、构建、测试持续运行，结束时 `watch-stop <control>` 默认等待最终扫描，再检查双维度结果、历史与产物。原 `watch --control` 仍可用。不能把一次事后扫描说成完整跟踪。

Pi 安装后用 `/reload` 刷新技能，或显式输入 `/skill:agentgc`。技能是 Agent 的行为指引，不是自动执行钩子：不能保证所有客户端或模型都遵守，也不会开启全盘常驻监控。已有技能不会覆盖，更新安装副本前应保存自定义内容；仓库与安装副本不会自动同步。主动跟踪不等于主动删除，清理仍需用途与引用检查、用户许可和预览。

安装位置为 `<目标>/agentgc/SKILL.md`，已有文件不会被覆盖。技能规范与 CLI 一起打包，源文件在 `src/agentgc/skills/agentgc/SKILL.md`。Skill 要求 Agent 先确认用途、引用和用户许可，不能自行把“可能是垃圾”升级成清理许可。

## 开发与验证

```sh
python -m pip install --no-deps .
python -m unittest discover -s tests -v
python tools/benchmark.py --files 5000
```

开发时不安装也可运行：`PYTHONPATH=src python -m agentgc --help`（该写法适用于 POSIX shell）。CI 覆盖 Linux、macOS、Windows 的 Python 3.11 和 3.13。平台专用测试会明确跳过；CI 配置存在不代表尚未运行的平台已经验证。

测试覆盖文件变化、查询、标签失效、内容摘要检查、链接保护、Git 保护、会话冲突、隔离恢复、操作中断、命令退出码、真实子进程、Linux 原生事件和文件标记。复现方法及实现边界见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 许可证

项目中由权利人有权授权的原创材料采用 [Agent Upstream License（AUL）1.0-draft.1](LICENSE)。已独立授权的第三方材料仍适用其原有条款。Agent 开发流程见 [AGENTS.md](AGENTS.md)，上游声明见 [UPSTREAM.toml](UPSTREAM.toml)。这是自定义许可证草案，不属于 OSI 已批准的开源许可证。
