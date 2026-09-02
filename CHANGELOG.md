# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.1] - 2026-09-03

### Changed
- Marketplace renamed `huangjin-local` → `claude-code-worktree`; install is now `claude plugin marketplace add jinhuang712/claude-code-worktree && claude plugin install wt@claude-code-worktree`.
- README gained a dedicated Install section (verify / update / uninstall, per-repo config).

## [0.1.0] - 2026-09-03

### Added
- `/wt:worktree [name]` skill: fork `wt-<name>` from the current branch into `.claude/worktrees/<name>`, moving only this session's uncommitted changes, then `EnterWorktree`.
- `/wt:land [--squash] [--keep]` skill: commit, rebase onto the recorded fork point, model-driven conflict resolution with per-file base/worktree diffs, `check`/`test` verification, fast-forward back to base, cleanup.
- `scripts/wt.py` (Python 3, stdlib only) with subcommands `track plan start status rebase continue verify finish abandon list`; exit codes 0 / 1 / 7 (conflicts) / 8 (verify failed).
- `PostToolUse` hook recording per-session edited files under `.git/claude-wt/sessions/`.
- Optional per-repo config `.claude/wt.json` (`check`, `test`, `lockfiles`, `migrations`).
- End-to-end test `tests/e2e.sh`.
