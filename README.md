# wt — session-native git worktrees for Claude Code

Two skills and one script:

| | what it does |
|---|---|
| `/wt:worktree [name]` | From the current branch, fork `wt-<name>` into `.claude/worktrees/<name>`, moving **only this session's** uncommitted changes there. Other sessions' dirty files stay put. Then `EnterWorktree`. |
| `/wt:land [--squash] [--keep]` | Commit, rebase onto the base branch (which may have moved), let Claude resolve conflicts with structured context, run `check`/`test`, `ExitWorktree`, fast-forward the base, clean up. Works on any linked worktree, not only ones `/wt:worktree` made. |

Why not the native `EnterWorktree` alone? It forks from `origin/HEAD` by default, cannot name the branch, does not remember which branch you forked from, and has no land step.

### Worktrees the plugin did not create

A plain `git worktree add`, the built-in `EnterWorktree` (`worktree-<name>` branches) or a `wt-*` branch without metadata is **adopted** the first time `land`, `status`, `finish` or `abandon` meets it. The base is the branch checked out in the main checkout, the fork point is the merge-base, and the JSON says so in `adopted`. It refuses to guess when the branch also contains another local branch's commits (it may be stacked): the error lists `candidates`, and `wt.py adopt --base <branch>` settles it. `finish --name <n>` finds `wt-<n>`, then a branch named exactly `<n>`, then `worktree-<n>`.

## How "only our changes" works

A `PostToolUse` hook records every file this session edits through Edit/Write into `.git/claude-wt/sessions/<session>.txt`. `wt.py plan` splits the dirty tree into `ours` / `theirs` / `shared` / `unknown`; the skill takes `ours`, judges `unknown` from its own conversation, asks about `shared`, never touches `theirs`. Moving is a pathspec-limited `git stash push -u` re-applied in the new worktree.

Each entry is `<worktree root><TAB><path>`, so the same relative path edited in another worktree is a different file. Another session only counts as live for `sessionTtlHours` (default 12) after its last edit; a file it touched before that is `unknown`, not `theirs`. A repeat edit refreshes the clock, and `start` deletes session files untouched for a week.

## Housekeeping

`wt.py list --all` shows every linked worktree: branch, base, ahead/behind, uncommitted files, whether it is merged (a commit the base already has as a patch counts, via `git cherry`), live sessions, idle time, and the reasons `gc` would keep it. `wt.py gc` is a dry run; `--apply` removes what it lists.

A worktree is a candidate only when it is under `.claude/worktrees`, clean, not mid-rebase, fully merged into its base, has no live session, and saw no activity (HEAD, reflog, tip commit, session edits) for `gcMinAgeHours` (default 24; `--min-age-hours` overrides). Nothing outside `.claude/worktrees` is touched, and a squash of several commits into one is not recognised as merged, so that worktree is kept. `--apply` takes the landing lock, re-checks each candidate, removes it without `--force`, then deletes its branch.

## Landing safety

- Fork point is recorded (`branch.wt-<name>.wtBaseSha`), so `rebase --onto` is correct even if the base was force-pushed.
- Conflicts never auto-abort; exit code 7 returns per-file base/worktree diffs for the model.
- If another session lands between your rebase and `finish`, `finish` rebases in place and re-runs `check`/`test` instead of failing. A conflict (exit 7) or a red test (exit 8) still stops it with nothing landed.
- `finish` runs under a per-repo lock (`.git/claude-wt/land.lock`), so simultaneous landings queue instead of racing.
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
{ "check": "ruff check .", "test": "pytest -q", "lockfiles": ["uv.lock"], "migrations": ["migrations/**"], "push": true, "lockTimeoutSec": 300, "sessionTtlHours": 12, "gcMinAgeHours": 24 }
```

`check`/`test` run in `/wt:land` before landing; `lockfiles` are regenerated instead of hand-merged on conflict; `migrations` conflicts always stop for a human.

`push` is off unless set. `true` pushes the base branch to `origin` after landing, a string names another remote, and `wt.py finish --no-push` skips it once. It never forces: a rejected push keeps the landing, still cleans up, and exits 9 with the error in the `push` field. `lockTimeoutSec` (default 300) is how long `finish` waits for another landing. `sessionTtlHours` and `gcMinAgeHours` are described under Housekeeping and "How only our changes works".

## License

MIT. See [CHANGELOG.md](CHANGELOG.md) for release notes.

## Script

`scripts/wt.py` (Python 3, stdlib only). Subcommands: `track plan start status adopt land rebase continue verify finish abandon list gc`. `land` is `adopt` + `rebase` + `verify` in one call. Exit codes: 0 ok · 1 error · 7 conflicts pending · 8 verify failed · 9 landed but push failed. Tests: `tests/e2e.sh`.
