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
agcc watch-stop task-UNIQUE_ID
agcc scan --root .
```

`watch --control` lets an agent manage its own background watcher. Use a new control name for every task. After the baseline succeeds, it writes the session ID and PID to `<state-home>/watches/<control>/ready.json`. Find the state directory with `agcc --json doctor`. Read readiness and confirm the session is still running before editing; a launched background process does not prove the baseline is ready. Control names allow 1–64 ASCII letters, digits, underscores, or hyphens and cannot be reused.

`watch-stop` only requests a cooperative stop. It does not signal a PID or directly close a session record. The watcher performs its final scan and ends itself; wait for that session to leave `running`. Controlled watchers default to a 3600-second limit when no duration is supplied, not a permanent background service. Report readiness timeouts, early exits, and tracking gaps. Stopping a watcher does not prove that application writers have stopped or authorize cleanup.

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
```

Time sorting uses the most recently observed change. Activity is the cumulative count of observed file-state changes, not read access. Byte totals in `stats` are the logical sizes of existing regular files; hard links may be counted more than once. These totals are not physical disk usage.

`--json` and `--home` may appear before or after the subcommand. `list` and `history` return at most 100 records by default; the query limit is 10000. Search matches text and does not treat `%` or `_` in user input as wildcards.

Add `--tag-files` to `run`, `watch`, or `mark` to write file tags. Tags contain small index references, sessions, and classifications, not file contents. They may be unsupported, stale, copied, or tampered with, and **cannot authorize cleanup**. SQLite remains the authoritative record.

## Directory pressure and proactive marking

```sh
agcc --json pressure --root .
agcc --json pressure --root . --root /path/to/cache --threshold build=1024 --apply
agcc --json reviews
```

`pressure` inventories metadata under explicit roots with defaults of 200000 entries and 30 seconds. Override with `--max-entries` and `--max-seconds`. It does not read content, follow links, or cross filesystems; select each filesystem separately. Budget, permission, and mount gaps produce exit code 1 and incomplete lower-bound totals. Sizes are logical bytes; nested candidates overlap and hard links can repeat. Do not sum all candidates as unique disk usage.

Any candidate with >=100000 files also flags attention (`--file-threshold` overrides it), catching small-file test explosions below byte thresholds. Common package caches, tool installations, databases, and model stores are review candidates too. Explicitly select separately mounted stores such as `/var/cache/pacman/pkg`.

Review defaults: `target/build/dist/.next` 512 MiB; `node_modules/.venv/venv` and common caches 1 GiB; `log/logs` 256 MiB; quarantine 1 MiB; protected `Backups/docker/containers` storage 5 GiB. Filesystem usage >=80% or free space <5 GiB flags attention; >=90% or <2 GiB is urgent. These are adjustable heuristics, not industry standards. Use `--threshold CATEGORY=MIB`; see `agcc pressure --help` for categories.

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

Directory scans use streaming traversal without holding a second full file index in memory. Linux events process at most 4096 paths per batch; exceeding that limit records a gap and reconciles the directory. Scanning and native directory watches still scale with file and directory counts; resource use is not guaranteed to be constant.

```sh
agcc prune --days 30
agcc prune --days 30 --apply
```

`prune` removes old events from ended sessions while preserving the file index, cumulative activity counts, labels, sessions, and quarantine journal. Events for running sessions are retained. Pruned detailed history can no longer be queried; filtering files by session also depends on retained events. SQLite reuses free pages but does not guarantee that the database file immediately shrinks.

## Installing the skill

```sh
agcc skill
agcc skill --install /path/to/your-agent/skills
# User-level global skills directory (supported by Pi)
agcc skill --install ~/.agents/skills
```

The global skill instructs agents to proactively track before local code or document edits, builds, tests, dependency installs, scripts, and artifact generation, even without a cleanup request. Read-only questions and remote-only work do not need a local watcher. Reuse known live tracking that covers the task's roots. For direct file-tool edits in the current session, start `watch --control`, wait for readiness, then write. At completion, request a stop, wait for the final scan, and inspect session history and outputs. A post-task snapshot is not a substitute for tracking.

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
