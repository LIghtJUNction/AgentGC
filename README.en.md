# AgentGC

[中文](README.md) | [English](README.en.md)

Track files created or changed during AI agent work, so artifacts left behind by long-running agents can be found, reviewed, and managed.

The project is **agentgc**; the command-line tool is **agcc**. It ships as **Skills + CLI**.

## Features

- **Tracking**: wrap any command with `agcc run -- <command>`, or observe selected directories with `agcc watch`. Record sessions, paths, sizes, timestamps, and file changes.
- **Queries**: sort by time, size, or activity; search paths, notes, and agent labels; inspect file or session history. JSON output and pagination are supported.
- **Labels**: `keep`, `temporary`, `review`, and `disposable` (explicitly approved for cleanup).
- **Index**: local SQLite is the authoritative record. Optional file metadata tags are hints; missing or unwritable tags do not invalidate the index.
- **Cleanup**: preview by default. After explicit confirmation, move files into quarantine on the same filesystem. Restore never overwrites existing files.
- **Skill**: bundled with the package; print it or install it into your agent's skills directory.

The current version, **0.1.0**, is runnable. It is not a system-wide auditor or a resource limiter.

## Installation

Requires Python 3.11 or later. There are no third-party runtime dependencies.

```sh
git clone https://github.com/LIghtJUNction/AgentGC.git
cd AgentGC
uv tool install .
agcc --version
```

Cloning a private repository requires your own GitHub access. Alternatively, run `python -m pip install .` in an existing Python virtual environment.

## A complete example

Run this in a test directory:

```sh
agcc run --agent demo --root . -- python -c "from pathlib import Path; Path('scratch.txt').write_text('demo')"
agcc --json list --sort size
agcc history scratch.txt
agcc mark scratch.txt disposable --note "Demo complete; no remaining use or references"
agcc gc scratch.txt
agcc gc scratch.txt --apply --confirm-unused
agcc quarantine
agcc restore <QUARANTINE_ID_FROM_THE_PREVIOUS_OUTPUT>
```

A restored file is automatically marked `keep`. Quarantine only moves files; **it does not free disk space**. This version has no permanent-delete command.

## Tracking an agent

```sh
agcc run --agent assistant --root . -- your-agent-command
agcc run --agent assistant --root . --root /path/to/outputs -- your-agent-command
agcc watch --root . --agent existing-session
agcc watch --root . --duration 60 --backend poll --interval 2
agcc watch --root . --agent assistant --control task-UNIQUE_ID --duration 3600
agcc watch-status task-UNIQUE_ID --wait-ready 30
agcc watch-stop task-UNIQUE_ID --wait 30
agcc scan --root .
```

Start `watch --control` once through the execution tool's background mechanism, then keep working on the engineering task while it tracks edits, builds and tests. Do not restart it for each operation. Reuse only confirmed live tracking with the same state home, correct roots and compatible exclusions; never stop another task's watcher. New controls require unique names of 1–64 ASCII letters, digits, underscores or hyphens.

After a successful baseline, the complete readiness record is published atomically to `<state-home>/watches/<control>/ready.json`, including session, PID, roots, exclusions and expected expiry. Find the state directory with `agcc --json doctor`. `watch-status CONTROL --wait-ready 30` waits locally in one CLI call and emits one result, rather than requiring repeated agent calls or full session dumps. Failure or timeout returns nonzero. Before writing, also check `roots`, `excludes`, `coverage_known` and `gaps`. Legacy records lack exclusion evidence. Recent heartbeat evidence alone does not prove writer identity or complete coverage. Control commands read existing state through a non-writing connection, without schema initialization/migration or contention for the baseline's write lock. Store opening counts toward the wait budget; missing or initializing state remains `starting`, without creating directories.

`watch-stop` only requests a cooperative stop, without signaling a PID or directly closing a session. `--wait 30` waits for final reconciliation, emits one result, and returns zero only for normal completion. Both wait options accept up to 300 seconds and use bounded local backoff queries. Controlled watchers default to a finite 3600-second duration; choose a sufficient explicit duration for longer tasks. They are not permanent daemons. Report readiness timeouts, early exits and gaps. Watcher completion does not prove application writers stopped or authorize cleanup.

`run` preserves the child's input, output, and exit code without adding a shell. If a command needs shell operators, invoke a shell explicitly. Session information goes to stderr, not the child's stdout. The wrapper passes `AGENTGC_SESSION` and `AGENTGC_HOME` to the child.

Repeat `--root` to select multiple directories; nested roots are merged. The default root is the current directory. The child still runs in the directory where you started `agcc`; `--root` does not change its working directory. Use `--exclude cache` or `--exclude '*.log'` to exclude paths. Matching uses case-sensitive relative paths or path components, not `.gitignore` syntax.

By default, tracking skips `.git`, AgentGC's own state directory, `.agentgc-quarantine`, symbolic links, Windows reparse points, and non-regular files. It does not automatically ignore `.venv`, build directories, or dependency directories, since these may also contain artifacts you want to manage.

### Tracking boundaries

**Only selected roots are tracked.** Add another `--root` for caches, temporary directories, or outputs outside those roots. This version does not intercept system-wide process file access.

| Platform | Automatically selected backend | Optional file tags |
| --- | --- | --- |
| Linux | inotify; explicit warning and fallback to polling if unavailable | `user.agentgc` extended attribute |
| macOS | Polling | `io.agentgc.meta` extended attribute |
| Windows | Polling | `:agentgc` alternate data stream on supporting filesystems |

`--backend native` explicitly requires Linux native events and fails if they cannot be enabled; it does not silently fall back to polling. Linux defaults to at most 8192 directory watches; use `--backend poll` if you exceed this limit. The default polling interval is 1 second, with a minimum of 0.1 seconds. Linux native mode reconciles the full directory tree every 60 seconds; adjust this with `--reconcile`.

Tracking establishes a baseline before starting and performs a full reconciliation at the end. Linux retains received events for short-lived files; if a file has already disappeared, its size and other unavailable fields remain unknown. Polling can miss a file created and deleted between scans. Native events can also be merged or lost: intermediate history is not guaranteed before watches are registered for new directories, during directory moves, or when the event queue overflows. Overflow or read failures record a `gap`; reconciliation can recover the state of surviving files only.

Renames are recorded as the old path disappearing and the new path appearing. Reliable identity merging across paths is not implemented. Records contain metadata history, **not file contents, patches, or previous versions**.

### Observations are not proof of authorship

`baseline` means a file existed when the baseline was established. `session-correlated` means a file changed during the observed session; **it does not prove that the agent wrote it**. `filesystem-event` means a filesystem event was received, without writer identity. `sessions.pid` is the wrapper PID, not the file writer's PID.

Human actions and other processes in a shared directory may also be observed. This version does not implement process-level attribution through Linux eBPF/fanotify, macOS Endpoint Security, or Windows ETW. Agent names are user-provided session labels.

## Output format

Default output is compact Markdown: a command heading, summary and bullet rows, rather than indented JSON. Sizes use readable units; routine summaries omit internal hashes/signatures and repeated measurements. Ordinary lists default to 20 returned rows with explicit omission counts. `--rows N` changes the display cap; `--details` shows all returned Markdown rows (not every raw field); neither expands query limits. `list/search/history` show the next offset; preserve your filters. Returned running sessions and uncertain/active automatic marks are prioritized. JSON is still subject to query limits, not the entire index.

```sh
agcc list --sort size --limit 20
agcc history path/to/file --limit 20 --offset 20
agcc --rows 5 sessions
agcc --details sync --dry-run           # includes unchanged observations
agcc --json list --sort size --limit 100   # machine parsing and raw returned records
```

JSON retains raw command fields without Markdown presentation caps; do not scrape Markdown as an API. GC candidates/refusal reasons, recovery records and returned errors/gaps have no second presentation cap. Sync counts unknown across all observations, even unchanged states. Incomplete scans remain explicit. Control characters, bidi controls and HTML in paths/notes are escaped for display; JSON retains original values. `run` preserves child stdout/stderr and exit codes, with the wrapper Markdown summary on stderr. The summary reads local session status, gap counts and retained unresolved-event counts; read failures are explicitly unavailable. Zero counts do not prove complete history; successful child exit does not imply gap-free tracking. Filesystem risks, GC refusals, recovery and gap details bypass `--rows`.

## Queries and labels

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
agcc mark path/to/result keep --note "Final result needed for delivery"
agcc mark path/to/scratch temporary --note "Not yet approved for cleanup"
agcc mark result.json receipt.json keep --note "Deliverables"
agcc mark temporary --files-from paths.txt --note "Intermediate files; not cleanup permission"
find ./outputs -type f -print0 | agcc mark review --files-from - --null
agcc diagnostics --session SESSION_ID
```

`mark` accepts multiple explicit paths or a UTF-8 newline list (`--files-from -` reads stdin; `--null` uses NUL delimiters for names containing newlines). Relative paths use the current directory; no glob expansion or recursive traversal is performed. Each file commits independently; failures do not stop other files, are reported individually, and cause exit code 1. Single-path syntax and JSON file records remain compatible.

New regular files are indexed immediately using metadata only, with source `manual-observation` and no writer/session attribution. This does not scan parents or recover past history. Links, directories and internal state are refused; missing/quarantined index entries still require reconciliation. Disposable notes, live-session/Git/hard-link protections, hashes and changed-index checks remain intact.

`diagnostics --session` groups identical retained gap details, showing counts, first/last occurrence and impact. Permission, link-boundary, disappeared-path and event-loss-risk categories are text-derived hints, not verified causes; compound records retain all matching categories and original detail. It never guesses that a disappearance was a normal uninstall. Unresolved notices are grouped by kind, with event counts and distinct path counts: **neither is a lost-file count**. Deletion notices can reflect normal removal; creation/modification notices can lack metadata after paths vanish. Final scans only recover surviving state. Pruned history is not included. Default run/watch summaries include these diagnostics without changing their raw JSON payloads or removing original evidence.

Time sorting uses the most recently observed change. Activity is the cumulative count of observed file-state changes, not read access. Byte totals in `stats` are the logical sizes of existing regular files; hard links may be counted more than once. These totals are not physical disk usage.

`--json`, `--home`, `--rows` and `--details` may appear before or after the subcommand; arguments after `run --` still belong to the child. `list` and `history` return at most 100 records by default; the query limit is 10000. Search matches text and does not treat `%` or `_` in user input as wildcards.

Add `--tag-files` to `run`, `watch`, or `mark` to write file tags. Tags contain small index references, sessions, and classifications, not file contents. They may be unsupported, stale, copied, or tampered with, and **cannot authorize cleanup**. SQLite remains the authoritative record.

## Shared `.agcc` rules and local sync

Commit a TOML `.agcc` in the project root. Team members share policy through Git, not local SQLite state, inode identities, sessions, or cleanup permissions. See the repository's `.agcc` for a runnable example.

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
any = [{size_gte = "2 GiB"}, {files_gte = 100000},
       {growth_gte = "256 MiB", growth_percent_gte = 25}]
[[rules]]
id = "python-cache"
[rules.match]
path = ["**/__pycache__", "**/.pytest_cache"]
kind = "directory"
[rules.then]
suggest = "temporary"
message = "Regenerable intermediate; not cleanup permission"
```

Same-ID tables merge recursively; arrays replace wholesale. `enabled = false` disables a reminder; `extends = []` disables builtin inheritance. New rules need `match` and `then`. Relative `path` patterns, literal `under`, and `kind = file/directory/any` combine with AND. Paths are anchored to the configuration directory; `*` matches one segment and `**` zero or more. Absolute paths, `..`, linked configuration, executable hooks, unknown fields, duplicate IDs, invalid units and unsupported versions are rejected.

`when.any` groups combine with OR; conditions inside a group combine with AND. Supported conditions: size_gte, files_gte, growth_gte, growth_percent_gte. Units: B/KiB/MiB/GiB/KB/MB/GB. Without `when`, matching produces an unconditional suggestion. `then.attention` flags attention; `then.suggest` allows keep/temporary/review only. Automatic records never overwrite manual file labels or authorize disposal.

```sh
agcc rules show
agcc rules validate
agcc rules explain ./target
agcc sync --dry-run
agcc sync
agcc auto-mark
agcc auto-mark --apply
agcc auto-marks
agcc --no-project-rules sync
```

Discovery starts at the current directory, or first explicit root, and stops at the Git repository boundary. `--config` selects an explicit file; global options also work after subcommands. `sync` defaults to the project root; explicit roots must stay within that project. Sync separate projects separately. With discovered configuration and enabled `[auto]`, run/watch/scan automatically reconcile after their final scan without expanding their original roots. External roots are reported as an automatic-sync failure; run preserves the child exit code. Queries do not scan; gc gains no authorization. Without project policy, pressure keeps its previous behavior; with policy, it uses project rules, remains preview-first, and saves only with `--apply`. Command overrides never edit configuration. `reviews` retains its existing directory-note meaning; `auto-marks` queries automatic rule states.

Evaluation is matched/not-matched/unknown. Incomplete lower bounds may prove a hit, never resolution or disappearance. Complete scans establish local growth baselines keyed by project, roots, exclusions and measurement. Missing or zero baselines make growth percentage unknown. `notify.cooldown = "24h"` throttles alerts; `notify.clear_below = 0.8` requires a complete measurement below 80% before resolution. Internal state, Git metadata and quarantine contents remain excluded. Metadata scans are not atomic filesystem snapshots or proof of writer identity.

Separate SQLite tables retain effective-policy fingerprints and transition history. Dry runs do not update rule states, baselines or notification times. Sync does not update the manual file index or manufacture writer history; use scan to refresh that index. This version supports builtin/project layers and command overrides, not remote state sync, user-level configuration files, plugins or external inheritance.

## Directory pressure and proactive marking

```sh
agcc --json pressure --root .
agcc --json pressure --root . --root /path/to/cache --threshold build=1024 --apply
agcc --json reviews
```

`pressure` inventories metadata under explicit roots with defaults of 200000 entries and 30 seconds. Override with `--max-entries` and `--max-seconds`. It does not read content, follow links, or cross filesystems; select each filesystem separately. Budget, permission, and mount gaps produce exit code 1 and incomplete lower-bound totals. Sizes are logical bytes; nested candidates overlap and hard links can repeat. Do not sum all candidates as unique disk usage.

Any candidate with >=100000 files also flags attention (`--file-threshold` overrides it), catching small-file test explosions below byte thresholds. Common package caches, tool installations, databases, and model stores are review candidates too. Explicitly select separately mounted stores such as `/var/cache/pacman/pkg`.

Review defaults: `target/build/dist/.next` 512 MiB; `node_modules/.venv/venv` and common caches 1 GiB; `log/logs` 256 MiB; quarantine 1 MiB; protected `Backups/docker/containers` storage 5 GiB. Filesystem byte usage >=80% or free space <5 GiB flags attention; >=90% or <2 GiB is urgent. These are heuristics, not industry standards. Directory size thresholds are adjustable with `--threshold CATEGORY=MIB`; see `agcc pressure --help` for categories.

`pressure` and `sync` also sample inode capacity once per selected filesystem, without extra traversal. Inodes represent filesystem file/directory entry capacity. Usage is based on `f_favail`, the free count available to the current user; >=80% flags attention and >=90% is urgent, combined with byte pressure. Additive `disks` fields are `inode_status`, `total_inodes`, `free_inodes`, `inode_used_percent`, `inode_attention`, `inode_urgent` and `inode_reason`; Markdown shows them without a routine-row cap. Missing APIs or no fixed capacity (for example Btrfs reporting zero total) are `unsupported`; read failures or invalid counters are `unknown`. Unknown numbers are `null`, not zero or evidence of safety. This does not invalidate complete directory measurements, alter growth baselines, or grant cleanup permission.

Exceeding a threshold is never cleanup permission. Default mode is preview. `--apply` saves separate SQLite directory `review` notes, never changes file labels or authorizes cleanup. `reviews` are historical observations: remeasure before acting. Protect databases, container volumes, live dependencies, and unique backups. Directory names do not prove their purpose.

The skill requires tracking before writes, classifying outputs at completion, and checking growth after builds, tests, installs, and downloads. Known intermediate outputs should be marked even below thresholds. Growth >=256 MiB AND >=25% between reliable measurements, or closed logs older than 14 days, merits attention; those two checks are agent guidance, not automatic CLI calculations.

An explicit system-wide request expands discovery beyond the current project to authorized local filesystems and common stores. Runtime roots `/proc`, `/sys`, `/dev`, `/run` remain excluded. Do not elevate privileges or hide unreadable or unfinished scopes. This is not whole-system real-time monitoring or proof of writer identity.

## Cleanup protections

A file being created or changed during agent work does not mean it can be deleted. Neither `temporary` nor `review` grants cleanup permission.

Marking a file `disposable` requires a `--note` explaining its disposition. Review saves file identity, size, modification time, and SHA-256; execution checks them again. An observed modification or replacement revokes cleanup permission. Even if content is changed to the same size and its modification time is restored, cleanup checks still reject it.

Cleanup rejects symbolic links, reparse points, hard links, Git-tracked files, uncertain Git status, files in actively tracked directories, and changed or unreviewed files. Restore never overwrites an existing destination. By default, preview returns at most 1000 candidates and reports `truncated` when the limit is reached.

`--confirm-unused` is the operator's confirmation that **all writers have stopped and uses and references have been checked**. It is not a claim that the tool has performed system-wide open-handle or reference checks. Stop programs still using the files, including background children that survive their parent. AgentGC sessions sharing a state directory check one another; sessions using different state directories do not automatically coordinate.

AgentGC is not a security sandbox and cannot prevent a malicious process with the same user permissions from repeatedly replacing paths. Do not quarantine files in a directory with uncontrolled ongoing writes. Quarantine is stored at `.agentgc-quarantine/<ID>/payload` beneath the source file's parent directory; it does not copy across filesystems and then delete the source.

## Interruptions and recovery

```sh
agcc doctor
agcc recover
agcc recover --apply
agcc quarantine
agcc restore QUARANTINE_ID
agcc end-session SESSION_ID --confirm-stopped
```

SIGINT/SIGTERM stops the direct child and triggers a final scan; it does not guarantee termination of all descendants. A forcibly terminated tracking session remains `running` and continues to block cleanup. A stale heartbeat alone does not prove the session has stopped safely. Use `end-session` only after confirming that the wrapper and its descendants have stopped.

Before quarantine, the tool commits a `prepared` operation journal, then moves the file. `recover` checks interrupted operations; `--apply` only repairs records and does not delete content. Inconsistent copies require manual review. Restore uses a non-overwriting hard-link operation; filesystems without hard-link support reject restoration and retain the quarantined copy. Interruptions can leave duplicate links or empty quarantine directories. This version does not provide full filesystem transaction guarantees under power loss.

## Data and resources

Set the state directory with `--home` or `AGENTGC_HOME`. Defaults: Linux `$XDG_STATE_HOME/agentgc`, or `~/.local/state/agentgc` if unset; macOS `~/Library/Application Support/agentgc`; Windows `%LOCALAPPDATA%/agentgc`.

SQLite uses WAL, write transactions, and a busy timeout. Multiple local processes may share a state directory. Keep state on a local filesystem; network shares are not recommended. On POSIX, new state directories use `0700` and databases use `0600`; on Windows, directory permissions are inherited. Storage is not encrypted.

The index contains absolute paths, agent labels, and manual notes; treat it as private data. It does not save full command arguments, upload records, or connect to remote services. Do not put secrets in agent names or notes. Tracking checks file metadata only; review and quarantine validation stream file contents with a 1 MiB buffer.

Directory scans stream without holding a second full file index. POSIX missing-file reconciliation queries only selected path-index ranges, not every other workspace in a shared database. Windows retains its previous query to preserve case-insensitive path matching. Linux events process at most 4096 paths per batch; exceeding the limit records a gap and reconciles surviving files.

Idle Linux native `watch` blocks until events or the next maintenance deadline instead of waking on every `--interval`. It retains 5-second heartbeats, periodic reconciliation, duration limits and final scanning. Private control events wake cooperative stops; a signal pipe wakes SIGINT/SIGTERM; empty event batches do not start SQLite write transactions. Control files are not indexed as user files, and the control watch counts toward the default 8192-watch budget. Wakeup failures produce a warning and short waits. Command wrappers still check direct-child exit; macOS/Windows still poll. Resource use scales with file/directory counts, is not constant, and never triggers automatic deletion.

```sh
agcc prune --days 30
agcc prune --days 30 --apply
agcc prune --days 30 --rule-history          # Also preview old rule transitions
agcc prune --days 30 --rule-history --apply
```

`prune` removes old events from ended sessions while preserving the file index, cumulative activity counts, labels, sessions, and quarantine journal. Events for running sessions are retained. `--rule-history` explicitly opts into previewing or pruning old rule-transition events; current automatic marks, growth baselines and configuration are retained. Because transitions have no session identity, rule history is skipped with a reason whenever any session is still `running`, including stale sessions. Default pruning does not touch rule history or schedule automatic retention. Unknown timestamps and events within the retention window are preserved. Rule-history pruning is irreversible too. Pruned detailed history can no longer be queried; filtering files by session also depends on retained events. SQLite reuses free pages but does not guarantee that the database file immediately shrinks.

## Installing the skill

```sh
agcc skill
agcc skill --install /path/to/your-agent/skills
# User-level global skills directory (supported by Pi)
agcc skill --install ~/.agents/skills
```

The global skill instructs agents to proactively track before local code or document edits, builds, tests, dependency installs, scripts, and artifact generation, even without a cleanup request. Read-only questions and remote-only work do not need a local watcher. Reuse known live tracking with compatible roots and exclusions. For direct file-tool edits, start one background `watch --control`, use `watch-status --wait-ready`, then keep it running across edits, builds and tests. At completion, use `watch-stop --wait` and inspect session history and outputs. A post-task snapshot is not a substitute for tracking.

In Pi, run `/reload` after installation, or invoke `/skill:agentgc` explicitly. Skills are agent instructions, not automatic execution hooks: they cannot guarantee compliance by every client or model and do not enable persistent whole-system monitoring. Existing skills are not overwritten; preserve customizations before updating an installed copy. Repository and installed copies do not synchronize automatically. Proactive tracking does not mean automatic deletion; cleanup still requires use and reference checks, user permission, and a preview.

The installed path is `<destination>/agentgc/SKILL.md`; existing files are not overwritten. The skill ships with the CLI, with its source at `src/agentgc/skills/agentgc/SKILL.md`. It requires the agent to check use, references, and user permission first; suspected clutter must not be treated as authorized for cleanup.

## Development and verification

```sh
python -m pip install --no-deps .
python -m unittest discover -s tests -v
python tools/benchmark.py --files 5000
```

For development without installation: `PYTHONPATH=src python -m agentgc --help` (POSIX shells). CI is configured for Python 3.11 and 3.13 on Linux, macOS, and Windows. Platform-specific tests explicitly skip where unsupported; a configured CI matrix does not mean an untested platform has been verified.

Tests cover file changes, queries, label invalidation, content-digest checks, link protection, Git protection, session conflicts, quarantine and restore, interrupted operations, command exit codes, real child processes, Linux native events, and file tags. See [DEVELOPMENT.md](DEVELOPMENT.md) for reproduction steps and implementation boundaries.

## License

Original materials that their rights holders can license are offered under the [Agent Upstream License (AUL) 1.0-draft.1](LICENSE). Separately licensed third-party material retains its original terms. See [AGENTS.md](AGENTS.md) and [UPSTREAM.toml](UPSTREAM.toml). AUL is a custom draft, not an OSI-approved open-source license.
