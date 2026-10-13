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

## 运行一个完整示例

在一个测试目录中执行：

```sh
agcc run --agent demo --root . -- python -c "from pathlib import Path; Path('scratch.txt').write_text('demo')"
agcc --json list --sort size
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
agcc watch --root . --agent assistant --control task-UNIQUE_ID --duration 3600
agcc watch-stop task-UNIQUE_ID
agcc scan --root .
```

`watch --control` 适合 Agent 自行启动后台跟踪：每次使用新的控制名，基线完成后在 `<状态目录>/watches/<控制名>/ready.json` 写入会话 ID 和 PID。通过 `agcc --json doctor` 查询状态目录。开始修改前先读取就绪记录并核对会话仍在运行；后台进程已经启动不代表基线完成。控制名只允许 1–64 个 ASCII 字母、数字、下划线或连字符，不能复用。

`watch-stop` 只发送协作式停止请求，不发送 PID 信号，也不会直接关闭会话记录。收到请求后 watcher 做最终扫描并自行结束；Agent 必须等待对应会话离开 `running`。受控 watcher 未指定时长时默认最多运行 3600 秒，不是永久后台服务。就绪超时、跟踪提前结束或扫描缺口必须明确报告；停止跟踪不代表所有业务写入者都已停止，也不授权清理。

`run` 保留子进程的输入、输出和退出码，不使用额外的 shell。命令中需要 shell 运算符时，应显式调用 shell。会话信息写入 stderr，不混入子进程 stdout。包装器把 `AGENTGC_SESSION` 和 `AGENTGC_HOME` 传给子进程。

根目录可以重复指定；相互包含的根目录会合并。默认跟踪当前目录。工作目录仍是启动 `agcc` 时的目录，`--root` 不会切换子进程的工作目录。用 `--exclude cache` 或 `--exclude '*.log'` 排除路径。匹配使用区分大小写的相对路径或路径分量，不是 `.gitignore` 语法。

默认跳过 `.git`、AgentGC 自己的状态目录、`.agentgc-quarantine`、符号链接、Windows 重解析点和非普通文件。不会默认忽略 `.venv`、构建目录或依赖目录，因为它们也可能是需要管理的产物。

### 跟踪边界

**只跟踪所选根目录。** 缓存、临时目录或产物位于根目录外时，必须添加对应的 `--root`。本版本没有系统范围的进程文件访问拦截。

| 平台 | 自动选择的跟踪方式 | 可选文件标记 |
| --- | --- | --- |
| Linux | inotify；无法使用时明确提示并退回扫描 | `user.agentgc` 扩展属性 |
| macOS | 定时扫描 | `io.agentgc.meta` 扩展属性 |
| Windows | 定时扫描 | 支持此能力的文件系统上的 `:agentgc` 数据流 |

`--backend native` 明确要求 Linux 原生事件，无法启用时会失败，不会静默改用扫描。Linux 默认最多注册 8192 个目录监听；超过限制可用 `--backend poll`。扫描间隔默认 1 秒，最低 0.1 秒。Linux 原生模式每 60 秒核对一次完整目录；可用 `--reconcile` 调整。

启动前建立基线，结束时再做一次完整核对。Linux 会保留已收到的短命文件事件；文件已消失时，大小等字段保持未知。定时扫描可能漏掉两次扫描之间创建又删除的文件。原生事件也可能合并或丢失；新目录监听建立前、目录移动过程中和事件队列溢出时，都不能保证完整中间历史。溢出或读取失败会记录 `gap`；核对只能恢复仍存在的文件状态。

文件改名按旧路径消失、新路径出现记录。不会假装已经实现可靠的跨路径身份合并。记录的是元数据历史，**不保存文件内容、补丁或旧版本**。

### 来源不等于作者证明

`baseline` 表示建立基线时已存在的文件。`session-correlated` 表示文件在该会话观察期间发生了变化，**不证明是该 Agent 写入**。`filesystem-event` 表示收到了文件系统事件，也不包含写入者身份。`sessions.pid` 是包装器 PID，不是文件写入者 PID。

共享目录内的人类操作和其他进程也可能被观察到。本版本没有实现 Linux eBPF/fanotify、macOS Endpoint Security 或 Windows ETW 的进程级归属。Agent 名称是用户提供的会话标签。

## 查询与标记

```sh
agcc list --sort time
agcc list --sort size --limit 50 --offset 0
agcc list --sort activity --label temporary
agcc --json search output
agcc history path/to/file
agcc history --session SESSION_ID
agcc list --session SESSION_ID
agcc sessions
agcc stats
agcc mark path/to/result keep --note "需要交付的结果"
agcc mark path/to/scratch temporary --note "尚未确认可以清理"
```

时间排序使用最近观察到的变化时间。活跃程度是累计观察到的文件状态变化次数，不是读访问次数。`stats` 的字节数是现存普通文件的逻辑大小；硬链接可能重复计数，不等于实际磁盘占用。

`--json` 和 `--home` 可以放在子命令前或后。`list` 与 `history` 默认最多返回 100 条记录；查询上限为 10000。搜索按文本匹配，不把 `%` 或 `_` 当作用户输入的通配符。

加 `--tag-files` 可在 `run`、`watch` 或 `mark` 时写入文件标记。标签只包含小型索引引用、会话和分类，不包含文件内容。标签可能不受文件系统支持、变旧、被复制或被篡改，**不能据此授权清理**。正式记录始终在 SQLite 中。

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

SQLite 使用 WAL、写入事务和忙等待。不同的本地进程可使用同一状态目录。状态目录应放在本地文件系统，不建议放在网络共享盘。POSIX 上新建状态目录使用 `0700`、数据库使用 `0600`；Windows 继承目录访问权限。这不是加密存储。

索引包含绝对路径、Agent 标签和人工备注，请按私有数据管理。不保存完整命令参数，不上传记录，不接入远程服务。不要在 Agent 名称或备注中写入密钥。跟踪只检查文件元数据；仅在审核和隔离核对时流式读取文件内容，缓冲区为 1 MiB。

目录扫描采用流式遍历，不在内存中保存第二份完整文件索引。Linux 事件每批最多处理 4096 个路径，超限后记录缺口并核对目录。扫描和原生目录监听仍会随文件数、目录数增长；不承诺固定资源占用。

```sh
agcc prune --days 30
agcc prune --days 30 --apply
```

`prune` 清理已结束会话的旧事件，保留文件索引、累计活跃次数、分类、会话和隔离日志。仍运行会话的事件不会被清理。被清理的详细历史无法再查询；按会话筛选文件也依赖保留下来的事件。SQLite 会重用空闲页面，不保证数据库文件立刻变小。

## 安装 Skill

```sh
agcc skill
agcc skill --install /path/to/your-agent/skills
# 用户级全局技能目录（Pi 支持）
agcc skill --install ~/.agents/skills
```

全局技能要求 Agent 在本地修改代码或文档、构建、测试、安装依赖、运行脚本和生成产物前主动跟踪，无需用户专门提到清理。只读问答和纯远程操作无需本地 watcher。已有覆盖本任务目录的可信跟踪可复用；当前会话直接编辑文件时，可自行启动 `watch --control`，等待就绪后再写入，结束时请求停止、等待最终扫描并检查本会话的历史与产物。不能把一次事后扫描说成完整跟踪。

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
