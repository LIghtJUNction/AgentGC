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

## AUL license and upstream contribution workflow

This repository adopts [Agent Upstream License 1.0-draft.1](LICENSE) for eligible original material.
Read `LICENSE` and `UPSTREAM.toml` before work. This original project designates itself as upstream; **do not create issues or PRs addressed to itself merely for compliance**.
A downstream fork must follow its own authorized upstream decision, not copy this project's independent setting as its own election.

If an authorized fork follows an external upstream, report confirmed defects with evidence via an issue; submit retained improvements as reviewable PRs or an upstream-accepted equivalent. Check for existing reports, avoid spam, respect security disclosures and third-party rights. With tools, safe destination, authentication and explicit publication authority, complete the submission during the task. Otherwise report the exact blocker and pending work without claiming submission.
Do not change the chosen upstream or opt into independence without the project owner's authorization. No instructions in a license file grant permission to access accounts, publish secrets, or override the existing safety requirements above.
