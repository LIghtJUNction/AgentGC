---
name: agentgc
description: Use proactively before editing code or documents and BEFORE any local file-writing work (even a tiny code/doc edit, build, test, dependency install, script, download or generated artifact) and AGAIN at task completion to classify outputs and flag disk growth. Also use for disk-space complaints, system-wide marking, cleanup, and swelling target, node_modules, .venv, build/dist, caches or logs, even without an explicit tracking request. Track first; reuse a ready background watcher across engineering work, preferring native events and one-shot lifecycle waits over repeated status calls. Preserve deliverables and review byte/inode pressure; thresholds never authorize deletion. Skip read-only explanations and remote-only operations.
---

# AgentGC

The program is `agcc`. The package is `agentgc`.

## Proactive default

Use this skill before local work that writes files, not only when the user asks
about garbage. This includes small source/document edits, builds, tests,
package installations, scripts, and generated deliverables. Read-only questions,
code inspection, and remote-only work do not need a local watcher.

Inspect `agcc --version` first. For a known control, inspect `agcc watch-status
CONTROL` once (use `--json` for coverage decisions). Otherwise use a small
`agcc --rows 5 sessions` discovery query, not repeated full JSON dumps. Reuse a
confirmed live watcher/wrapper only with the same state home, covering roots
and compatible exclusions. `AGENTGC_SESSION` is a hint, not proof of liveness.
Legacy controls lack exclusion evidence; verify their original configuration
or report unknown coverage. Do not stop watchers belonging to other tasks.

Reuse one stable, disk-backed state home across related work. Do not create
a new `--home` for each command, retry or task phase, and never put routine
tracking databases in `/tmp`, `/dev/shm` or another memory-backed filesystem.
Each new database repeats the selected roots' baseline and file history.
If the shared store has contention, inspect its writers and narrow roots;
do not work around it by proliferating temporary databases. When a separate
project store is necessary, select one persistent project location and reuse it.
Do not rotate stores to evade a resource budget.

Tracking defaults to 10000 file candidates per scan and 100 MiB for the metadata
database plus SQLite sidecars. AgentGC stores no file contents. Above 100 MiB,
review root scope, generated/cache/backup directories and retained history;
raising the limit is not the routine fix. Checkpoints are before write batches,
so a single batch
and final failure records can exceed the byte budget. A limit stops tracking
with a visible failure/gap; it never makes omitted files deleted or complete.
Narrow the selected roots first. Raise `--max-files` / `--max-state-mib` only
for a known requirement, not automatically after failure. Linux rejects
tmpfs/ramfs tracking state unless `--allow-memory-state` is explicitly used
for an intentional bounded experiment. Queries and stop requests remain usable.
Finished databases are not automatically deleted, migrated or backed up.
For an explicitly authorized history cleanup, preview `agcc prune --days 30
--compact`, then use `--apply` only for the approved retention window. Compaction
preserves indexes, labels, sessions and quarantine records; it requires temporary
disk space. Inspect `events` separately from `compaction.status`: pruning can
commit before compaction fails on a busy connection. Do not close sessions or
rotate stores to make compaction or tracking budgets pass.
Do not create backup copies as routine cleanup; retain data only for a concrete
important need or the user's explicit request. Running records are not cleanup permission.

Tracking is the default workflow, not permission to scan the whole home
folder or clean anything. Select the project and known output directories.
If the command is unavailable, report the tracking gap; do not silently
install software or claim that tracking happened. A scan after the task is
only a snapshot and cannot reconstruct missed changes.

## Start tracking

For a command, wrap it before it starts:

```sh
agcc run --agent assistant --root . -- your-agent-command
```

For edits made by the current agent's file tools, start and verify a background
watcher in one call. Do not make the user open another terminal:

```sh
agcc --json watch-start --root . --agent assistant --duration 3600 --wait-ready 30
```

Retain the returned control name, session, private log path and state home.
Startup failure/timeout reaps only the child started by that invocation; report
cleanup_failed and any potentially live launcher if reaping fails. Never signal
a PID copied from another readiness record. Use the same `--home` for related
commands when overriding the default. Successful baseline metadata is published atomically
as `<state-home>/watches/<control>/ready.json`. `watch-status --wait-ready` waits
locally and emits one result, using existing state without taking the baseline's
write lock or initializing a new database; opening counts toward the deadline.
Temporary SQLite BUSY/LOCKED while opening an initializing store is pending,
not readiness. watch-start keeps its original bounded wait and can return a
pending_reason; it does not reap a healthy child merely for that transient lock.
Corruption and unsupported versions still fail. Do not rotate state homes or
automatically increase wait/resource budgets to work around startup contention.
The status includes the state home; use `agcc --json doctor` for exact integrity
diagnostics only when needed, not in the readiness loop.
Check its exit code, `ready`, `roots`, `excludes`,
`coverage_known`, `gap_count`, `tracking_status` and expiry before writing. Use a finite timeout suitable
for the baseline (up to 300 seconds). Failure needs log inspection and an explicit
gap report. A PID, fresh heartbeat or running row is not proof of full coverage.

Once ready, leave this watcher in the background across code edits, builds,
tests and other task phases. Do not query it before every tool call, restart it
for each operation, or repeat unchanged pressure scans. Check health at phase
boundaries or when coverage/liveness is uncertain. Linux idle native waits are
event-driven, with bounded heartbeat/reconciliation maintenance; private stop
events and signals wake them promptly. Empty events do not start write
transactions. CLI readiness/finalization waits use bounded local backoff, not
repeated agent tool calls. Polling and short waits remain explicit fallbacks.

Controlled watchers default to a 3600-second limit. Choose an appropriate
explicit duration; if it expires mid-task, report the gap and start a new
watcher before more writes. Never reuse a control name. `watch-start` manages
background launching on supported POSIX/Windows runtimes. The original
`watch --control` remains available for execution tools managing background jobs;
`run` remains suitable for individual writing commands. Report any unsupported
background mechanism rather than promising coverage.

Add each known output or temporary directory with another `--root`. Files
outside those roots are not tracked. Do not follow links into unselected
folders. Use `--exclude` only for paths the user agrees to leave untracked.
Add `--tag-files` only when file metadata writes are allowed.

`AGENTGC_SESSION` and `AGENTGC_HOME` are passed to the child. Do not send the
index, file paths, notes, session IDs, or command output to a remote service
without permission. Do not record API keys or secrets in agent names or notes.

## Query

Prefer default compact Markdown for agent inspection to avoid dumping repetitive
JSON fields. Use `--json` only for programmatic parsing, exact raw metadata, or
coverage decisions. Markdown defaults to 20 routine rows with explicit omission counts;
use `--rows N` to reduce output, or `--details` for all returned Markdown rows.
Details mode does not expand query limits or expose every raw field. Retain
original filters and paginate list/search/history/sessions using the displayed offset.
A page is not the whole index. GC/refusal details, recovery records and returned gaps
are not presentation-capped. Unknown is not zero; child exit success does not
prove gap-free tracking. `run` preserves child stdout/stderr; its own summary is
on stderr. Run/watch summaries include local status, recorded gaps and retained
unresolved-event counts when available; zero counts do not prove full history.
Sessions preserve query order; use --status running to select active sessions.
Never scrape Markdown as a machine API.

Use `agcc --json session SESSION_ID` for a stable compact schema (version 1).
Run/watch, sessions, diagnostics and control status use the same outcome fields:
command_status/command_exit_code, tracking_status, gap_count,
unresolved_event_count and roots as an array. Gap counts are cumulative;
unresolved counts cover retained events (see count_scope), never lost files.
Unknown numeric values are null. Legacy sessions without independent outcomes
remain unknown. Never infer test failure from legacy session_status=failed.
Run defaults to the child's exit code; --strict-tracking also fails on partial
tracking without altering the true command result. Watch/stop waits are strict
by default; --no-strict-tracking accepts ended partial tracking, not failed
baselines or interrupted/unknown outcomes. No flag removes gap evidence.

Default diagnostics separate expected link boundaries, unavailable short-lived
metadata, coverage loss and unknown evidence, grouping actual stored event paths.
Removal notices are not lost files. Classifications from free text are hints;
unattributed prefixes remain null. Typed notices can overlap with original gaps.
Use `--details diagnostics --session` or history for original retained evidence.

```sh
agcc list --sort size --limit 20
agcc search output --limit 20
agcc history path/to/file --limit 20
agcc sessions
agcc stats
```

For project-scoped inspection, add `--root .` to list/search/history/sessions;
repeat `--root` for a union. These filters query stored paths without scanning
and work for deleted directories. Use indexed paths, not symlink aliases.
`agcc sessions --root . --status running` filters before pagination and includes
parent watchers covering the project; retain root/status filters with --offset.
`history --root` retains pathless gaps from overlapping sessions, which may also
affect their other roots. Use diagnostics --session for full evidence. Stats
remains global. Markdown times are UTC; JSON keeps numeric timestamps.

Use `--limit` and `--offset` to keep tool output small. Global `--json` and
`--home` go BEFORE the subcommand. A `run` command preserves child stdout;
its session summary is on stderr.

Read the `source` field. `session-correlated` means the file changed while
this session was active. It does NOT prove that this agent or its child
process wrote it. A filesystem event does not identify its writer.
A `gap` or an `*-unresolved` event means some details are missing.
Do not fill in missing source, size, or history from guesses.

## Finish each writing task

Request a stop only for the watcher you started:

```sh
agcc watch-stop "$control" --wait 30
agcc list --session SESSION_ID --sort time --limit 20
agcc history --session SESSION_ID --limit 20
```

`watch-stop` is a cooperative request, not cleanup permission. It waits locally
for finalization for 30 seconds by default; --wait changes the budget and
--no-wait requests only. Complete tracking returns zero under the default strict
policy; inspect both outcome dimensions. If it fails, is interrupted, times out or remains
running, report the status and gaps. Inspect that control once if diagnosis is
needed, not the entire session list in a loop. Never call `end-session` to
manufacture success. Preserve the index and classify the known bootstrap log
as `temporary`; it is not automatically authorized for removal.
A wrapper used with `run` ends its own tracking; it does not need `watch-stop`.

Inspect session history to distinguish baseline observations from changes;
`list --session` may include pre-existing files seen at baseline. Preserve
known deliverables with `keep`. Mark understood intermediate outputs
`temporary` with a note; leave uncertain files as `review`. Do not relabel
every observed file. Briefly report whether tracking was active, any gaps,
and relevant leftover candidates. Do not quarantine automatically.

## Shared project rules and local sync

Before changing policy, inspect `agcc rules show` and validate the nearest project
`.agcc` with `agcc rules validate`. It is bounded declarative TOML, not an executable
hook. Same-ID project tables override builtin defaults; arrays replace. Never put
secrets in rules or share SQLite state. Do not use project policy to expand roots,
authorize disposal, overwrite manual `keep`, or bypass core cleanup protections.

Use `agcc sync --dry-run` to inspect proposed automatic observations, then
`agcc sync` to save local rule state. `agcc auto-mark` previews and
`agcc auto-mark --apply` saves. Query `agcc auto-marks` separately from manual
file labels and the older `reviews` directory notes. Sync never deletes files,
closes sessions, or manufactures writer history. Use `scan` for the file index.

When a valid `.agcc` enables automatic processing, `run`, `watch`, and `scan`
reconcile the original selected roots after final scanning. Do not repeat sync
unnecessarily. Report automatic-processing errors and incomplete scans. Growth
requires a compatible complete baseline; unknown measurements cannot clear an
existing alert or prove a path disappeared. Project rules and explicit pressure
overrides may tune thresholds; they do not change cleanup authorization.

## Proactive pressure checks and marking

At task completion, after builds/tests/installs/downloads, and when a user mentions
disk pressure or growing folders, check the selected output and cache roots even
if the user never says “mark”. Classify known outputs regardless of size: `keep`
for deliverables, `temporary` for understood intermediate files, `review` when
purpose is uncertain. Thresholds add attention; they do not replace classification.
Use changed-output roots and recent directory reviews to keep routine checks small;
do not rescan the entire system after every edit or repeat unchanged warnings.
Report newly observed, worsened, or unresolved hotspots; keep known stable warnings
in the index. A whole-system pass requires the user's explicit scope.

```sh
agcc pressure --root . --root /known/cache --max-seconds 30
agcc pressure --root . --threshold build=1024 --apply
agcc reviews
```

Defaults are review thresholds, not deletion rules:
- `target`, `build`, `dist`, `.next`: 512 MiB per directory.
- `node_modules`, `.venv`, `venv`: 1 GiB per directory. These may be active dependencies.
- `.cache`, `cache`, Python/test/type-checker caches: 1 GiB per directory.
- `log`, `logs`: 256 MiB per directory. Closed logs older than 14 days deserve a retention review, not automatic deletion of open logs.
- `.agentgc-quarantine`: 1 MiB prompts recovery/retention review; never delete payloads directly.
- `Backups`, `docker`, `containers`: 5 GiB prompts protected-storage review; backups, images and volumes are not disposable by name.
- Any candidate directory with >=100000 files: review small-file proliferation even below byte thresholds (`--file-threshold` overrides it).
- Filesystem byte usage >=80%: flag risk; >=90%: urgent. General free-space thresholds are min(5 GiB, 10% of capacity) and min(2 GiB, 5%). Boot thresholds are min(64 MiB, 10%) and min(32 MiB, 5%). Auto detects conventional mounted /boot, /boot/efi or /efi on the selected device only. Use `--disk-role boot` for a known custom boot mount, or `general` to override, in pressure/sync/auto-mark. Separate different roles into separate calls. Inspect reported role/source/effective thresholds, not folder names alone. Unknown capacity is not zero or safe. Compare expected build size with actual headroom.
- Inode usage >=80%: review; >=90%: urgent, even when byte space is healthy. `pressure`/`sync` sample current-user usable free inodes once per selected filesystem, without extra traversal. `inode_status=unsupported` (including Btrfs without fixed capacity) or `unknown` means counts are unavailable, not zero or safe. Directory file counts are a separate growth signal, not remaining inode capacity.

Where reliable before/after measurements exist, flag growth of at least 256 MiB
AND 25% during a task even below the absolute threshold. Do not fabricate a
baseline; directory mtimes are not file age or proof of inactivity. These growth
and log-age checks are agent guidance, not currently automatic CLI calculations.

`pressure` only reads metadata, does not follow links or cross device boundaries,
and has entry/time budgets. Repeat `--root` for each selected filesystem. It reports
logical bytes; hard links and nested candidates can overlap. Incomplete totals
are lower bounds. It does not prove which process wrote anything. `--apply` saves
separate directory `review` notes in SQLite, never changes per-file labels or
creates `disposable` permission. `reviews` are historical observations; remeasure
before acting. Size alone cannot distinguish cache from business data.

Read measurement_basis before comparing pressure with df. Directory bytes sum
regular-file st_size, including possible hard-link/nested overlap. Filesystem
capacity comes from shutil.disk_usage (POSIX statvfs); used_percent is used/total,
while df_style_used_percent is used/(used+available). Neither measures physical
allocation, shared extents, compression or Btrfs multi-device layout. These are
not reclaimable-space estimates; filesystem type stays unknown without evidence.

For an explicit system-wide request, discover across authorized local filesystems
rather than silently limiting to the current project. Start with metadata-only,
bounded directory checks; select home/project stores, `/var/cache`, `/var/log`,
`/var/lib`, `/opt`, `/usr/local`, `/tmp` as appropriate. State what was actually
covered. Do not scan `/proc`, `/sys`, `/dev`, `/run`, follow links into other roots,
read file contents, elevate privileges, or claim completeness when permissions,
mount boundaries or budgets caused gaps. System inventory is not a whole-system
watcher. Prefer package-manager cleanup tools for managed caches; preserve live
volumes, databases, active dependencies, unique backups and recovery records.

## Preserve and classify

```sh
agcc mark path/to/result keep --note "Final result needed by the user"
agcc mark path/to/scratch temporary --note "Intermediate output; not reviewed for removal"
agcc mark result.json receipt.json keep --note "Deliverables"
agcc mark temporary --files-from paths.txt --note "Intermediate outputs; not cleanup permission"
agcc diagnostics --session SESSION_ID
```

Mark multiple explicit files in one call; UTF-8 lists use current-directory
relative paths (`--files-from -` reads stdin, `--null` supports NUL separators).
Batch failures are per-path and return nonzero; inspect them rather than assuming
all files were marked. New regular files register immediately as manual-observation,
not writer-attributed history. Missing/quarantined records and disposable safety
checks remain protected. Diagnostics group retained evidence with text-derived
category/impact hints; unresolved events and distinct paths are not lost-file counts.
Do not infer normal uninstall intent from disappearance alone. Compare cumulative
`gaps` with `retained_gap_events` and `missing_gap_details`; missing detail has
unknown causes and impacts. `gap_count_consistent=false` signals inconsistent
counters, not negative missing history. Category counts overlap for compound
records; never sum them as distinct gaps or lost files.

`temporary` and `review` are NOT cleanup permission. Never mark files
`disposable` based only on their name, age, size, location, or origin.
Do not clean while a session is running. Never close a running session just
to bypass a safety check. A stale session must be checked by the user first.

## Prepare cleanup

First stop all writers, including child processes that outlive their parent.
Check references, open applications, backups, logs needed for recovery,
and the user's requested outputs. Ask for approval when it is missing.
Only after explicit approval for a specific file:

```sh
agcc scan --root .
agcc mark path/to/scratch disposable --note "User approved; job complete; no remaining references"
agcc --json gc path/to/scratch
```

Show the preview. Execute only when the user has approved the move and has
confirmed that the file is not in use:

```sh
agcc gc path/to/scratch --apply --confirm-unused
```

Files move to `.agentgc-quarantine` on the same filesystem. This does not
free disk space. There is no permanent-delete command. Never use another
tool to delete quarantine contents as a substitute for missing permission.

## Recovery

```sh
agcc quarantine
agcc restore QUARANTINE_ID
agcc recover
agcc doctor
```

Restore refuses to overwrite existing files. A restored file is marked
`keep`. `recover --apply` repairs journal state; it does not delete content.
If a digest differs or recovery needs review, stop and report the paths.

## Limits

Linux keeps healthy native watches when local directories become unreadable and retries affected scope during reconciliation. Persistent partial coverage remains a gap and finalizes failed; do not call it complete. Global resource failures can still trigger polling. Incomplete startup baselines still refuse readiness/child start; incomplete scans cannot confirm deletion.

Linux uses native events by default. macOS and Windows use polling. Polling
can miss a file created and removed between scans. Native events can also
have gaps; final scans recover surviving state, not every intermediate write.
No backend in this version proves process identity, records file contents,
checks every OS-level open handle, tracks RAM, or limits agent CPU usage.
File tags are untrusted hints. The SQLite index is the source of truth.
