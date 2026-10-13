# Development and verification

## Modules

`paths.py` controls roots, exclusions, link checks, and streaming digests.
`store.py` owns the SQLite schema, transactions, sessions, queries, and events.
`native.py` implements Linux inotify with the system C library.
`tracker.py` records observations and runs the child command.
`watch_control.py` provides task-scoped readiness and cooperative stop requests.
`tags.py` writes optional platform metadata. Tags are never trusted input.
`cleanup.py` handles review, quarantine, restore, and recovery records.
`cli.py` parses commands and formats output. The bundled skill is package data.
`pressure.py` inventories directory growth candidates with explicit roots and
entry/time/depth budgets. Optional SQLite `directory_reviews` records are
historical attention notes, separate from file labels and cleanup authorization.
Thresholds never create disposable reviews. Incomplete byte counts are lower
bounds; nested candidates and hard links may overlap. Skip symlinks, runtime
pseudo-filesystems, and unselected mount boundaries; do not read file contents.

The package needs Python 3.11 or later. Runtime dependencies: none.
Build dependency: setuptools. The CLI is `agcc`, not `agentgc`.

## Run tests

Install the package, then run:

```sh
python -m unittest discover -s tests -v
agcc --version
agcc skill --json
```

On POSIX, source-only tests can use:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

The tests create isolated temporary workspaces. They never clean user files.
Linux tests exercise actual inotify, xattr writes when supported, SIGTERM,
and a killed watcher. Windows skips POSIX-only tests. Tests that require
symlinks skip when the operating system does not grant that capability.
The cross-platform CI matrix does not test every filesystem or ACL policy.

## Resource check

```sh
python tools/benchmark.py --files 5000 --idle-seconds 1
```

This creates and removes a private temporary directory. Output includes
initial and unchanged scan times, the number of new events on an unchanged
scan, Python allocation peak during a separate scan, index bytes, and the
CPU time of an idle native event wait when available. It is not a full
harness benchmark. Allocation peak excludes the interpreter baseline and
native/SQLite allocations. Timing is machine-dependent.

Do not claim that filesystem events prove which process wrote a file.
Do not claim that quarantine frees disk space. File byte totals are logical
sizes, not unique physical blocks. Do not report CI success without reading
the actual checks for the submitted commit.

## Safety model

The SQLite store is local, private application state. It is not a security
boundary against another process with the same user permissions. Read-only
queries are safe during tracking. Cleanup requires stopped writers and a
user review. Sessions in separate state directories cannot guard each other.

Controlled watchers publish readiness only after a successful baseline. Their
unique control directories live beneath the private state home and cannot be
reused. Stop requests never signal recorded PIDs or close session records:
only the watcher ends its session after final reconciliation. A stop request
is not evidence that application writers have stopped. Reject linked control
paths and malformed readiness records. Readiness timeouts and early watcher
exits are tracking gaps, not permission to bypass cleanup protections.

An unchanged scan writes scan tokens but creates no file change events.
A failed scan must not treat unreadable files as deleted. Native queues
and reconciliation may miss intermediate states; retain explicit gap records.
Tags can fail, become stale, or be forged; do not use them as cleanup permits.

Disposable review binds to file identity, metadata, and a SHA-256 digest.
Cleanup revalidates under a SQLite write lock. A prepared operation is
committed before rename. If a later step fails, keep the journal and data.
Restoration creates the target without overwrite, then commits the restored
record before unlinking the extra quarantine link. A crash can leave two
links. Never delete an inconsistent copy merely to make a test pass.

The filesystem and SQLite do not share one transaction. This version has
no full power-loss guarantee and no automatic cleanup of every interrupted
restore, empty operation directory, or duplicate link. Recovery reports
ambiguous states rather than guessing. Permanent deletion is intentionally
absent until a separate retention and reference-checking design is reviewed.

## Primary API references

- Python filesystem interfaces: https://docs.python.org/3/library/os.html
- Python SQLite interfaces: https://docs.python.org/3/library/sqlite3.html
- Linux inotify interface: https://man7.org/linux/man-pages/man7/inotify.7.html

Do not add a dependency or claim a new platform backend merely because an
API exists. Implement it, test it, and update the platform table first.
