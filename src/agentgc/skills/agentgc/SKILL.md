---
name: agentgc
description: Proactively track local file changes before editing code or documents, running builds, tests, installs, scripts, or generating artifacts, even when the user does not mention tracking or cleanup. Use for workspace leftovers too. Preserve useful outputs and preview reversible cleanup; never treat observed files as automatically disposable.
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

```sh
agcc --json list --sort size
agcc --json search output
agcc --json history path/to/file
agcc --json sessions
agcc --json stats
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
agcc --json watch-stop "$control"
agcc --json sessions
agcc --json list --session SESSION_ID --sort time --limit 50
agcc --json history --session SESSION_ID --limit 50
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
