---
name: agentgc
description: Track files created or changed during agent work. Search the local index, preserve useful results, and prepare a reviewed, reversible cleanup. Use before long-running agent jobs or when investigating workspace leftovers.
---

# AgentGC

The program is `agcc`. The package is `agentgc`.

## Start tracking

Wrap a command before it starts:

```sh
agcc run --agent assistant --root . -- your-agent-command
```

For an already-running tool, ask the user to start `agcc watch --root .` in
another terminal. Do not promise monitoring after the watcher stops.
Add each output or temporary directory with another `--root`. Files outside
those roots are not tracked. Use `--exclude` only for paths the user agrees
to leave untracked. Add `--tag-files` only when file metadata writes are allowed.

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
