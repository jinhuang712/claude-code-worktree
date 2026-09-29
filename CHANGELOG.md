# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `finish` takes a per-repo landing lock (`.git/claude-wt/land.lock`, `lockTimeoutSec`, default 300), so simultaneous landings queue.
- Opt-in push: `"push": true` (or a remote name) in `.claude/wt.json` makes `finish` push the landed base branch. Never forces; a failed push keeps the landing and exits 9. `--no-push` skips it once.
- Tests for in-place rebase, conflicts, a red test after rebase, the lock, simultaneous landings, `--keep`, a base that is not checked out, and push.

### Changed
- `finish` no longer fails with "not rebased" when another session landed after your rebase: it rebases in place and re-runs `check`/`test`. A conflict (exit 7) or a red test (exit 8) still stops it with nothing landed.
- Both skills now say which Bash command shapes the worktree isolation guard refuses (loops, `$(…)`, heredocs, `VAR=` assignments, `cd` elsewhere) and what passes instead.

### Fixed
- `finish` now finds a worktree that is detached in the middle of a rebase, through the path recorded by `start`.

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
