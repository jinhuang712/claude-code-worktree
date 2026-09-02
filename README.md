# wt — session-native git worktrees for Claude Code

Two skills and one script:

| | what it does |
|---|---|
| `/wt:worktree [name]` | From the current branch, fork `wt-<name>` into `.claude/worktrees/<name>`, moving **only this session's** uncommitted changes there. Other sessions' dirty files stay put. Then `EnterWorktree`. |
| `/wt:land [--squash] [--keep]` | Commit, rebase onto the base branch (which may have moved), let Claude resolve conflicts with structured context, run `check`/`test`, `ExitWorktree`, fast-forward the base, clean up. |

Why not the native `EnterWorktree` alone? It forks from `origin/HEAD` by default, cannot name the branch, does not remember which branch you forked from, and has no land step.

## How "only our changes" works

A `PostToolUse` hook records every file this session edits through Edit/Write into `.git/claude-wt/sessions/<session>.txt`. `wt.py plan` splits the dirty tree into `ours` / `theirs` / `shared` / `unknown`; the skill takes `ours`, judges `unknown` from its own conversation, asks about `shared`, never touches `theirs`. Moving is a pathspec-limited `git stash push -u` re-applied in the new worktree.

## Landing safety

- Fork point is recorded (`branch.wt-<name>.wtBaseSha`), so `rebase --onto` is correct even if the base was force-pushed.
- Conflicts never auto-abort; exit code 7 returns per-file base/worktree diffs for the model.
- Fast-forward is done in whatever checkout has the base; git itself refuses if another session's uncommitted files overlap.
- Cleanup happens only after `merge-base --is-ancestor` confirms the commits are on base.

## Install

Requires Claude Code ≥ 2.1.185 (for `EnterWorktree` / `ExitWorktree`), git ≥ 2.23 and Python 3.

```bash
# 1. register this repo as a plugin marketplace (one-time)
claude plugin marketplace add jinhuang712/claude-code-worktree

# 2. install the plugin (user scope by default; add --scope project to share via .claude/settings.json)
claude plugin install wt@claude-code-worktree
```

Start a new Claude Code session; `/wt:worktree` and `/wt:land` are now available and the file-tracking hook is active.

```bash
claude plugin list                             # verify: wt@claude-code-worktree · enabled
claude plugin update wt@claude-code-worktree   # pull a newer version
claude plugin uninstall wt@claude-code-worktree
```

Developing locally: `claude plugin marketplace add /path/to/clone` instead of the GitHub slug, then `claude plugin update` after every source change (installs are copies, not symlinks).

### Optional per-repo config

`.claude/wt.json` in the repository you work on:

```json
{ "check": "ruff check .", "test": "pytest -q", "lockfiles": ["uv.lock"], "migrations": ["migrations/**"] }
```

`check`/`test` run in `/wt:land` before landing; `lockfiles` are regenerated instead of hand-merged on conflict; `migrations` conflicts always stop for a human.

## License

MIT. See [CHANGELOG.md](CHANGELOG.md) for release notes.

## Script

`scripts/wt.py` (Python 3, stdlib only). Subcommands: `track plan start status rebase continue verify finish abandon list`. Exit codes: 0 ok · 1 error · 7 conflicts pending · 8 verify failed. Tests: `tests/e2e.sh`.
