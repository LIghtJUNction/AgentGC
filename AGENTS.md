# AgentGC contributor instructions

Read README.md and DEVELOPMENT.md before changing behavior.
Package/project: `agentgc`. Command: `agcc`.

Keep the runtime dependency-free unless a concrete requirement justifies a
change. Keep CLI operations local. Never log full command arguments, secrets,
or file contents. Do not follow links into unselected directories.

SQLite is authoritative. File tags are best-effort hints, not permissions.
A session label is not proof of writer identity. Do not call every observed
file an agent-created file. Keep tracking gaps visible.

Cleanup is preview-first and reversible. Keep content hashes and file
identity checks. Preserve Git-tracked files, active-session files, and links.
Do not add permanent deletion or close active sessions without approval.
Never replace user files during restore. Test failures must not be solved by
weakening safety checks.

Run `python -m unittest discover -s tests -v` after installing the package.
On POSIX, `PYTHONPATH=src` can be used instead. Add a focused regression test
for a changed safety or tracking rule. Use isolated temporary directories.
Do not run cleanup commands on the user's workspace as a test.

The skill source is src/agentgc/skills/agentgc/SKILL.md. Keep it in the wheel.
Update README examples when CLI behavior changes. State which platforms
were actually tested and distinguish local results from remote CI results.
