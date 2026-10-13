---
name: agentgc
description: Use proactively before editing code or documents and BEFORE any local file-writing work (even a tiny code/doc edit, build, test, dependency install, script, download or generated artifact) and AGAIN at task completion to classify outputs and flag disk growth. Also use for disk-space complaints, system-wide marking, cleanup, and swelling target, node_modules, .venv, build/dist, caches or logs, even without an explicit tracking request. Track first, preserve deliverables, mark uncertain size hotspots for review; thresholds never authorize deletion. Skip read-only explanations and remote-only operations.
---

# AgentGC

The program is `agcc`. The package is `agentgc`.

## Proactive default

Use this skill before local work that writes files, not only when the user asks
about garbage. This includes small source/document edits, builds, tests,
package installations, scripts, and generated deliverables. Read-only questions,
code inspection, and remote-only work do not need a local watcher.

Inspect `agcc --version` and `agcc --json sessions` first. If a known live
wrapper already covers the task's roots and state directory, reuse it rather
than starting nested watchers. `AGENTGC_SESSION` is a hint, not proof that a
wrapper is still alive. Do not stop watchers belonging to other tasks.

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

For edits made by the current agent's file tools, start a controlled watcher
yourself through the execution tool. Do not make the user open another terminal
when background execution is available. On POSIX shells, for example:

```sh
control="task-$(python -c 'import uuid; print(uuid.uuid4().hex)')"
log=$(mktemp "${TMPDIR:-/tmp}/agentgc-watch.XXXXXX")
nohup agcc watch --root "$PWD" --agent assistant --control "$control" \
  --duration 3600 > "$log" 2>&1 < /dev/null &
```

Retain the control name, log path, and state directory in task context. Use the
same `--home` for every related command if overriding the default. A controlled
watcher publishes `<state-home>/watches/<control>/ready.json` **after** the
baseline succeeds. Obtain the state-home from `agcc --json doctor`. Read and
parse readiness, then check that its session is running before writing files.
Wait with a short bounded timeout (for example 10 seconds); if readiness fails,
inspect the log and report the gap. Do not treat a background PID or a running
session row alone as proof that the baseline has completed.

Controlled watchers default to a 3600-second limit. Choose an appropriate
explicit duration; if it expires mid-task, report the gap and start a new
watcher before more writes. Never reuse a control name. On Windows or an
execution tool without persistent background jobs, use its supported background
mechanism with `watch --control`, or wrap writing commands with `run`. If neither
can cover direct file edits, explain the limitation rather than promising coverage.

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
raw metadata. Markdown defaults to 20 routine rows with explicit omission counts;
use `--rows N` to reduce output, or `--details` for all returned Markdown rows.
Details mode does not expand query limits or expose every raw field. Retain
original filters and paginate list/search/history using the displayed offset.
A page is not the whole index. GC/refusal details, recovery records and returned gaps
are not presentation-capped. Unknown is not zero; child exit success does not
prove gap-free tracking. `run` preserves child stdout/stderr; its own summary is
on stderr. Run/watch summaries include local status, recorded gaps and retained
unresolved-event counts when available; zero counts do not prove full history.
Sessions prioritize returned running records, not all system sessions.
Never scrape Markdown as a machine API.

```sh
agcc list --sort size --limit 20
agcc search output --limit 20
agcc history path/to/file --limit 20
agcc sessions
agcc stats
```

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
agcc watch-stop "$control"
agcc sessions
agcc list --session SESSION_ID --sort time --limit 20
agcc history --session SESSION_ID --limit 20
```

`watch-stop` is a cooperative request, not immediate completion and not cleanup
permission. Wait for that session to finish its final scan and leave `running`
with a short bounded timeout. If it fails, is interrupted, or remains running,
report the status and any gaps. Do not call `end-session` to manufacture success.
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
- Filesystem usage >=80% or free space <5 GiB: flag risk. >=90% or <2 GiB: urgent attention. Also compare expected build size against actual headroom.

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
```

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

Linux uses native events by default. macOS and Windows use polling. Polling
can miss a file created and removed between scans. Native events can also
have gaps; final scans recover surviving state, not every intermediate write.
No backend in this version proves process identity, records file contents,
checks every OS-level open handle, tracks RAM, or limits agent CPU usage.
File tags are untrusted hints. The SQLite index is the source of truth.
