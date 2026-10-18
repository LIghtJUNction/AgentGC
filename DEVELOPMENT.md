# Development and verification

## Modules

`paths.py` controls roots, exclusions, link checks, and streaming digests.
`store.py` owns the SQLite schema, transactions, sessions, queries, and events.
Opening a complete initialized WAL store checks schema metadata without acquiring
an initialization writer lock. Missing schema-v1 objects still receive additive
upgrades; fresh initialization publishes its version in the schema transaction.
Initialization errors close the connection.
Nonblocking read-only opening maps SQLite BUSY/LOCKED during initialization to
StoreNotReady, so startup polling uses its existing deadline instead of reaping
a healthy child on a transient read lock. Other database errors still propagate.
Real exclusive-lock regressions verify pending status, eventual readiness and
corruption refusal without increasing timeouts or creating readiness evidence.
Control clients use its existing-database read-only mode, with no schema writes,
migration, directory creation or writer-lock wait. Database opening counts toward
client wait deadlines; missing/version-zero initialization remains pending.
`native.py` implements Linux inotify with the system C library.
Path-local permission/disappearance/link registration failures expose partial
coverage instead of discarding healthy watches. Scope walks report inaccessible
paths separately so previously registered descendant watches can be retained;
links and vanished paths do not retain old registrations. Retained registrations
and private control watches still count against the global budget. IN_IGNORED
removes stale registrations and requests reconciliation. Partial coverage retries
on reconciliation even without dirty directory events, remains a durable gap,
and finalizes failed if still present. Global resource failures retain fallback
behavior. Incomplete startup baselines still refuse child start/readiness.
`tests/test_partial_native.py` exercises Linux inotify with injected local errors,
healthy events, retry, retained budget, link replacement and final status.
`tracker.py` records observations and runs the child command.
`limits.py` bounds tracking to 10000 file candidates per scan (across selected
roots and missing-file reconciliation) and 256 MiB of SQLite database/sidecar
logical bytes by default. Check byte budgets before each scan/native-event write
batch; one batch and final failure evidence can overshoot. Do not claim an exact
filesystem quota. A reached limit stops tracking, preserves a gap and skips the
otherwise repeated final scan/drain. Baseline failure cannot launch the child
or publish readiness. Interrupted scans never reconcile their unvisited tail as
missing. Explicit CLI budgets propagate to background children. Preflight rejects
known Linux tmpfs/ramfs state before creating store or launch directories, unless
explicitly opted in. Mount detection is metadata-only, boundary-aware and selects
the deepest mount, including escaped mount paths; unsupported platforms retain
their previous location behavior. Queries/control requests remain available for
oversized or memory-backed historical stores. No automatic retention, migration
or backup is added. `tests/test_tracking_limits.py` uses isolated stores and small
fixtures for resource limits, child/readiness refusal, retained labels/missing
state, baseline reuse and Linux mount detection.
`session_state.py` provides schema-versioned independent command/tracking outcomes.
Additive `session_results` retains the actual child returncode (negative values
mean POSIX signals), command status and durable tracking quality. Old read-only
stores never migrate and return unknown outcomes when these records are absent.
Final tracking quality and legacy lifecycle commit in one short transaction;
no scan is enlarged to hold that writer lock. Command results are persisted as
soon as observed and are not replaced by wrapper failures or strict CLI codes.
`tests/test_session_quality.py` and `tests/test_quality_cli.py` cover this contract.
`watch_lifecycle.py` launches finite detached watchers without a shell, waits for
baseline/readiness, validates its own direct-child PID and coverage, and performs
bounded failure cleanup only on that child. Cleanup failures remain explicit
without serializing Popen command arguments. `tests/test_watch_lifecycle.py`
uses isolated directories and reaps its own children; no recorded foreign PID
is signalled. watch-stop defaults to a 30-second finalization wait; --no-wait
still returns the same typed outcomes without changing lifecycle records.
`watch_control.py` provides task-scoped readiness, compact health queries and
cooperative stop requests. Atomic readiness publication uses a non-overwriting
hard link in the private local state directory; unsupported filesystems fail
visibly rather than exposing partial JSON. Records are bounded to 4096 bytes.
`tags.py` writes optional platform metadata. Tags are never trusted input.
`cleanup.py` handles review, quarantine, restore, and recovery records.
Manual marks register only explicitly selected new regular files in the same
transaction as the label, using manual-observation without session attribution.
Batch CLI marks commit independently and report partial failures; single-file
payload compatibility is preserved. No refresh bypasses disposable digest or
changed-index checks. `diagnostics.py` groups identical retained gap texts and
unresolved notice kinds, with distinct path counts and conservative text-derived
impact hints. It never equates notices with lost files or infers uninstall intent.
Category rollups count compound records in each matching category, without
inflating retained gap totals. Cumulative counters are compared with retained
records to expose missing detail, not infer its cause. Excess retained records
are inconsistent, with a null missing-detail count. A read savepoint keeps all
counts in one snapshot, preserves outer transactions, and works on read-only
connections. `tests/test_diagnostic_retention.py` covers compound counts, missing
and inconsistent history, concurrent writers, read-only use and nested transactions.
`tests/test_usability.py` covers immediate registration, batch/list/stdin input,
partial failures, transactional rollback, link/internal/session protections,
changed disposable refusal and grouped evidence in isolated workspaces.
`cli.py` parses commands and dispatches output. The bundled skill is package data.
Project query roots on list/search/history/sessions are index-only filters,
not filesystem scopes or cleanup permissions. Match stored paths at directory
boundaries, union repeated roots, and apply all filters before pagination.
Missing directories remain queryable without scanning or resolving link aliases.
Session roots overlap in either direction to include ancestor watchers. Project
history retains pathless gaps from those sessions without claiming precise path
attribution. Session status filtering never closes records. Stats stays global.
`tests/test_query_scope.py` covers these boundaries in isolated temporary stores.
`output.py` renders compact, escaped Markdown without mutating command results.
Time fields use readable UTC in Markdown; numeric JSON timestamps are unchanged.
Session APIs use normalized roots arrays and schema-versioned summaries rather
than raw database rows. JSON preserves command result payloads and query limits. Routine rows have a visible
20-row presentation cap, configurable with --rows or bypassed with --details.
History and sessions support bounded offset pagination. Sessions preserve query
order so display caps cannot skip or repeat rows; unknown/active automatic marks
are prioritized only for display. Run/watch summaries read local
retained session evidence; strict CLI policies never overwrite child exit codes.
Zero counts do not prove complete history. GC decisions, filesystem risks,
recovery and errors bypass routine display caps. Original gap text is available
in details/history. Count unknown evaluations over all sync observations, not only changes.
Formatting never manufactures gap-free tracking or changes child streams/codes.
Default diagnostics show compact classification/path-prefix groups. Original
gap text remains in JSON/history and --details; text-derived classes are hints,
typed notices may overlap with gaps, and missing detail remains unknown.
`tests/test_diagnostic_groups.py` covers path attribution and coherent snapshots.
`pressure.py` inventories directory growth candidates with explicit roots and
entry/time/depth budgets. It reports measurement_basis: st_size logical directory bytes, shutil.disk_usage
filesystem capacity, used/total and df-style used/(used+available). Physical
allocation, shared extents, compression and Btrfs multi-device layout are not
measured. Original thresholds remain unchanged. `tests/test_measurement_basis.py`
uses mocked counters and isolated hardlinks to verify the distinction.
Windows directory entries omit device IDs, so refresh zero-device directory
metadata with lstat before enforcing mount boundaries. Skip all reparse points
before traversal. The pressure regressions simulate missing device IDs, real
cross-device directories and reparse entries without relaxing these protections.
Test fixtures canonicalize platform temporary-directory aliases before direct
SQL comparisons, and bundled UTF-8 skill checks specify their encoding.
Optional SQLite `directory_reviews` records are
historical attention notes, separate from file labels and cleanup authorization.
Thresholds never create disposable reviews. Incomplete byte counts are lower
bounds; nested candidates and hard links may overlap. Skip symlinks, runtime
pseudo-filesystems, and unselected mount boundaries; do not read file contents.
Per-device inode samples use `statvfs.f_files` and current-user usable
`statvfs.f_favail`, never directory counts or `f_ffree` as a capacity substitute.
Usage >=80%/90% adds attention/urgency to byte pressure. Missing API or zero fixed
capacity is unsupported; failures/invalid counters are unknown and all unknown
numeric/boolean fields stay null. These are additive `disks` fields, forwarded by
sync and rendered without row truncation. They do not change rules, file labels,
cleanup authorization, or complete directory measurement/growth baselines.

Byte headroom uses capacity-scaled floors: general min(5 GiB, 10%) /
min(2 GiB, 5%); boot min(64 MiB, 10%) / min(32 MiB, 5%), plus exact 80%/90%
usage thresholds. Auto role selection requires a conventional mounted boot path
on the selected device; explicit disk-role overrides are per invocation, not
persistent rule changes. Byte sample failure/invalid counters remain unknown,
without invalidating directory measurement baselines. Report policy source,
capacity and effective floors in JSON/Markdown; inode pressure remains independent.
`tests/test_partition_policy.py` uses mocked counters and mount metadata, checking
small EFI examples, large-volume headroom, exact thresholds, unknown samples,
device boundaries and direct/project-rule CLI paths. No real partitions are changed.

`rules.py` loads bounded, non-linked TOML `.agcc` files, validates the closed
schema, merges builtin policy, and matches anchored relative paths.
`sync.py` reconciles separate automatic observations and compatible complete-scan
baselines. Its optional tables are auto_marks, auto_events and sync_baselines.
No automatic policy changes manual file labels, file history, quarantine or
cleanup permissions. `prune --rule-history` explicitly opts into preview/apply
retention for old `auto_events`, without changing current marks or baselines.
Because those transitions have no session identity, skip all rule-history pruning
while any session remains running, even with an expired heartbeat. Count, guard
and delete share the same immediate transaction. Default prune behavior and its
JSON shape remain unchanged; no optional tables are created by pruning. Incomplete observations cannot resolve attention or missing
paths. Rule updates carry fingerprints; transitions retain local history.

The package needs Python 3.11 or later. Runtime dependencies: none.
Build dependency: setuptools. The CLI is `agcc`, not `agentgc`.

Release discovery explicitly selects the `agentgc` runtime package, not implicit
namespace directories. Package data is limited to the bundled skill. `MANIFEST.in`
resets source-tree entries from stale `SOURCES.txt` manifests, then includes only
runtime modules and the skill. If adding a runtime subpackage or resource, update
both the discovery/data configuration and manifest allowlist deliberately.
Never ship local quarantine contents, caches or SQLite state.

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
`tests/process_helpers.py` provides bounded direct-child cleanup: terminate, wait
up to 3 seconds, then kill and wait up to 3 seconds if needed; close captured pipes
on both success and failure. Cleanup waits for the child rather than draining
pipes that descendants may keep open. It does not terminate descendants or close
AgentGC session records; failures to reap remain visible.

The archive regression requires the existing setuptools build dependency and
explicitly skips if unavailable. CI installs setuptools and wheel in the test
interpreter before package installation, so archive checks run on every matrix
entry. The test builds a wheel and sdist without downloads, using synthetic
cache/quarantine/state files and a stale manifest in a temporary
source tree. It also reconstructs the owned sdist using validated regular-file
paths (never archive link extraction) and builds a second wheel offline. The
rebuilt wheel must have the same members and bundled skill as the direct build;
the temporary tree and archives are removed even after failure.
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

### Background watcher regressions

`tests/test_background_efficiency.py` uses fake clocks to verify maintenance
rather than interval-driven idle waits: an 11-second native watch waits for
5, 5 and 1 seconds, with only baseline/final scan transactions. Dirty events,
reconciliation and duration retain their deadlines; signal-wakeup fallback
keeps bounded short waits. Real signal wakeup descriptors and handlers are
restored, and private control watches share the directory-watch budget.
POSIX missing-file reconciliation uses selected path-index ranges, including
previously indexed nested roots, without examining unrelated workspaces. Windows
retains the previous query because BINARY index ordering does not match its
case-insensitive scope semantics. A Linux mock covers fallback candidate selection;
the separate real Windows case-rename test skips elsewhere. A fake slow baseline
also verifies a fresh heartbeat before readiness, even for short-lived watchers.

`tests/test_control_status.py` covers atomic bounded publication, malformed or
linked records, session identity, unknown legacy exclusions, stale/expired
health, bounded local backoff and one-result CLI waits. Linux subprocess tests
exercise cooperative stop and SIGTERM during idle native waits, final scanning,
interrupted status, and exclusion of private control files from the user index.
Health queries never close sessions. Existing `closed-by-user` status is terminal
but not normal completion. Isolated subprocess waits under a held WAL writer lock
verify read-only clients are not blocked by baseline writes. Missing/initializing
state is never created or migrated; a fake clock verifies opening-time accounting.
The bounded client wait backs off from 0.1 seconds to at most 1 second; it is not
itself a platform event backend.

`tests/test_inode_pressure.py` mocks filesystem counters rather than creating
large file populations. It checks current-user availability, exact thresholds
before display rounding, zero available versus zero total, unknown/unsupported
counts, per-device sampling, both output paths and absence of cleanup permission.
Run these alongside the full suite in isolated temporary directories. Do not
place cleanup fixtures beneath unverifiable Git metadata or weaken protection to
make them pass. Avoid stress-generated file trees when a small mock suffices.

Do not claim that filesystem events prove which process wrote a file.
Do not claim that quarantine frees disk space. File byte totals are logical
sizes, not unique physical blocks. Do not report CI success without reading
the actual checks for the submitted commit.

## Safety model

The SQLite store is local, private application state. It is not a security
boundary against another process with the same user permissions. Read-only
queries are safe during tracking. Cleanup requires stopped writers and a
user review. Sessions in separate state directories cannot guard each other.

Controlled watchers publish readiness only after a successful baseline and a
fresh heartbeat. Their
unique control directories live beneath the private state home and cannot be
reused. Roots, exclusions and expected expiry accompany session/PID metadata;
legacy records explicitly have unknown exclusion coverage. Recent heartbeat
health is evidence, not proof that writers are live. `watch-status --wait-ready`
and `watch-stop --wait` provide single-result waits capped at 300 seconds;
normal completion alone is success. Stop requests never signal recorded PIDs or
close session records: only the watcher ends after final reconciliation. A stop request
is not evidence that application writers have stopped. Reject linked control
paths and malformed readiness records. Readiness timeouts and early watcher
exits are tracking gaps, not permission to bypass cleanup protections.

Idle Linux native watches wait until file/control events or the nearest
heartbeat, reconciliation, dirty-scan or duration deadline. A nonblocking signal
pipe wakes SIGINT/SIGTERM and is restored/closed during finalization; failures
retain short waits with a warning. Command wrappers still bound child-exit
checks, and polling platforms keep their scan behavior. Empty native reads do
not begin write transactions. Private control notifications only wake durable
stop checks, never become user-file evidence. They count toward watch limits.

Scans walk outside write transactions and commit at most 128 file observations
per batch; missing-file candidates use bounded keyset pages with no live read
cursor spanning commits. Each candidate is re-statted under the write lock.
Partial batches remain committed if a later step fails; incomplete walks still
suppress missing-file reconciliation. This is not an atomic filesystem snapshot.
Tracking exceptions retain a gap even if final reconciliation succeeds; if the
store is still unavailable, stderr explicitly reports the unpersisted gap.
`tests/test_store_contention.py` exercises shared-store writer access during
walking, bounded batches, opening under a held WAL writer, paginated deletion,
incomplete-scan protection and exception gap reporting in isolated directories.

An unchanged scan writes scan tokens but creates no file change events.
POSIX missing-file queries use indexed path ranges rather than a shared-store-wide
pass; Windows preserves case-insensitive reconciliation with the earlier query.
Exclusions still apply, and remembered root columns need not match a later scope. A failed scan must not treat unreadable files as deleted. Native queues
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
