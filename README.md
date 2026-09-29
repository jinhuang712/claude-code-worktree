# claude-code-worktree

**Git worktrees for parallel Claude Code sessions.** `wt` is a Claude Code plugin that moves *only your session's* edits into an isolated git worktree, then lands them back on your branch: rebased, tested, conflict-aware, and safe while other sessions land on the same repository.

```text
/wt:worktree ───► work in .claude/worktrees/<name> ───► /wt:land
fork only this        on its own branch wt-<name>         rebase · resolve conflicts · test
session's edits                                           · fast-forward · clean up
```

## Why

Several Claude Code sessions in one checkout overwrite each other's uncommitted files, and landing their work by hand means juggling stashes, rebases and merges. The built-in `EnterWorktree` creates a fresh worktree from a base ref and switches the session to it; edits you have already made stay behind, and getting the result back is up to you. `wt` covers both:

- **Only your changes move.** A hook records which files *this* session edited. When you fork, those move into the new worktree; files other sessions touched stay where they are.
- **Landing is one step.** `/wt:land` rebases onto the branch you forked from (even if it has moved), gives Claude structured per-file diffs when there are conflicts, runs your `check` and `test`, and fast-forwards the branch.
- **Safe next to other sessions.** Landings queue on a lock, a base that moved mid-landing is rebased in place and re-tested, and nothing is force-pushed, aborted or removed unless you ask.

## Quick start

```bash
claude plugin marketplace add jinhuang712/claude-code-worktree
claude plugin install wt@claude-code-worktree
```

Start a new Claude Code session, then:

1. `/wt:worktree fix-login` forks `wt-fix-login` into `.claude/worktrees/fix-login` and enters it. Whatever this session already edited moves with you.
2. Work as usual. Claude commits in the worktree.
3. `/wt:land` rebases, verifies, lands on the branch you forked from, and removes the worktree.

Requires Claude Code ≥ 2.1.185 (`EnterWorktree` / `ExitWorktree`), git ≥ 2.23 and Python 3 (standard library only). The landing lock uses `flock` (macOS, Linux); elsewhere landings are not serialized.

## How it works

### Fork only this session's changes

A `PostToolUse` hook records every file this session writes with Edit, Write, MultiEdit or NotebookEdit, in `.git/claude-wt/sessions/`. `/wt:worktree` runs `wt.py plan`, which sorts the dirty files into four groups:

| group | meaning | what happens |
|---|---|---|
| `ours` | edited by this session | moved into the worktree |
| `theirs` | edited by another session that was active recently (`sessionTtlHours`, default 12) | never touched |
| `shared` | edited by both | Claude asks: take the whole file, leave it, or stop |
| `unknown` | nobody tracked it: a shell command, you, build output | Claude takes it only if it created or changed the file in this conversation |

The move is a pathspec-limited `git stash push -u`, re-applied inside the new worktree. Entries are stored with their worktree root, so the same relative path in another worktree is a different file. An idle session's claims expire, and `start` deletes session files untouched for a week.

### Land

`/wt:land` has two phases, because worktree isolation blocks git commands aimed at the main checkout.

1. **Inside the worktree**, `wt.py land` rebases onto the recorded fork base, then runs `check` and `test` from `.claude/wt.json`. The fork point is recorded, so `rebase --onto` is correct even if the base moved or was rewritten. Conflicts stop with exit 7 and per-file base/worktree diffs; Claude resolves them keeping both intents and never aborts the rebase on its own. Lockfiles are regenerated rather than hand-merged, and migration conflicts always stop for a human.
2. **After `ExitWorktree`**, `wt.py finish` takes a per-repo lock, fast-forwards the base wherever it is checked out, and removes the worktree, branch and metadata. If the base moved again in between, it rebases in place and re-runs your checks first; a conflict or a red test lands nothing.

`--squash -m "<message>"` lands one commit; `--keep` keeps the worktree. Set `"push": true` to push the landed branch too (see [Config](#config)).

### Worktrees the plugin did not create

A plain `git worktree add`, the built-in `EnterWorktree` (`worktree-<name>` branches) or a `wt-*` branch without metadata is **adopted** the first time `land`, `status`, `finish` or `abandon` meets it. The base is the branch checked out in the main checkout, the fork point is the merge-base, and the JSON reports both in `adopted`. It refuses to guess when the branch also contains another local branch's commits (it may be stacked): the error lists `candidates`, and `wt.py adopt --base <branch>` settles it. `finish --name <n>` finds `wt-<n>`, then a branch named exactly `<n>`, then `worktree-<n>`.

### Housekeeping

`wt.py list --all` shows every linked worktree: branch, base, ahead/behind, uncommitted files, whether it is merged (a commit the base already has as a patch counts, via `git cherry`), live sessions, idle time, and the reasons `gc` would keep it.

`wt.py gc` is a dry run; `--apply` removes what it lists. A worktree is a candidate only when it is under `.claude/worktrees`, clean, not mid-rebase, fully merged into its base, has no live session, and saw no activity (HEAD, reflog, tip commit, session edits) for `gcMinAgeHours` (default 24; `--min-age-hours` overrides). A squash of several commits into one is not recognised as merged, so that worktree is kept. `--apply` takes the landing lock, re-checks each candidate, removes it without `--force`, then deletes its branch.

### Safety

- Never force-pushes. A rejected push keeps the landing, still cleans up, and exits 9 with the error.
- Never runs `git rebase --abort` or `--skip` on its own; conflicts are left in place for Claude or you to resolve.
- The fast-forward is refused by git if another session's uncommitted files overlap; the worktree stays for a retry.
- Cleanup happens only after `merge-base --is-ancestor` confirms the landed commits are on the base.
- Nothing is pushed or removed unless you ask: `push` is off by default, and `gc` is a dry run.

## Reference

### Skills

| | what it does |
|---|---|
| `/wt:worktree [name]` | From the current branch, fork `wt-<name>` into `.claude/worktrees/<name>`, moving only this session's uncommitted changes there, then enter it. |
| `/wt:land [--squash] [--keep]` | Commit, rebase onto the base, resolve conflicts, verify, exit the worktree, fast-forward the base and clean up. Works on any linked worktree. |

### `scripts/wt.py`

Every subcommand prints one JSON object. Python 3, standard library only.

| command | run from | does |
|---|---|---|
| `plan` | main checkout | sort dirty files into ours / theirs / shared / unknown |
| `start <name> [--take <path>…]` | main checkout | create the worktree and move the listed paths |
| `status` | worktree | base, drift, dirty files |
| `land [--onto <branch>]` | worktree | adopt if needed, rebase, run `check`/`test` |
| `rebase`, `continue`, `verify` | worktree | the steps of `land`, one at a time |
| `adopt [--base <branch>] [--name <n>]` | worktree or main | record base and fork point for a worktree `start` did not create |
| `finish --name <n> [--squash -m <msg>] [--keep] [--no-push]` | main checkout | land and clean up |
| `abandon <name> [--discard]` | main checkout | bring the changes back (or discard them) and remove the worktree |
| `list [--all]` | anywhere | `wt-*` branches, or every linked worktree with its state |
| `gc [--apply] [--min-age-hours <n>]` | anywhere | remove idle, merged worktrees; dry run unless `--apply` |
| `track` | hook | record a file edit (reads the hook JSON on stdin) |

Exit codes: `0` ok · `1` error or precondition (for example a dirty worktree) · `7` conflicts pending · `8` check or test failed · `9` landed but the push failed.

### Config

Optional `.claude/wt.json` in the repository you work on:

```json
{ "check": "ruff check .", "test": "pytest -q", "lockfiles": ["uv.lock"], "migrations": ["migrations/**"], "push": true }
```

| key | default | meaning |
|---|---|---|
| `check`, `test` | none | shell commands run in the worktree by `land`, and by `finish` after an in-place rebase |
| `lockfiles` | common lockfiles (`package-lock.json`, `uv.lock`, `Cargo.lock`, …) | globs regenerated instead of hand-merged on conflict |
| `migrations` | none | globs whose conflicts always stop for a human |
| `push` | off | `true` pushes the base branch to `origin` after landing; a string names another remote |
| `lockTimeoutSec` | 300 | how long `finish` waits for another landing |
| `sessionTtlHours` | 12 | how long another session counts as live after its last edit |
| `gcMinAgeHours` | 24 | how long a worktree must be idle before `gc` considers it |

A `.worktreeinclude` file in the repository root lists git-ignored files (one glob per line) to copy into each new worktree.

## Install and upgrade

```bash
claude plugin list                             # verify: wt@claude-code-worktree · enabled
claude plugin update wt@claude-code-worktree   # pull a newer version
claude plugin uninstall wt@claude-code-worktree
```

The first install command uses user scope; add `--scope project` to share the plugin through the project's `.claude/settings.json`.

An update reaches a session when it restarts or runs `/reload-plugins`; other running sessions keep the version they started with (the old version's directory stays in the plugin cache). The [CHANGELOG](CHANGELOG.md) upgrade notes say what to expect while old and new sessions share a repository.

## Development

`bash tests/e2e.sh` runs the end-to-end checks in throwaway repositories. To try a local clone, `claude plugin marketplace add /path/to/clone` instead of the GitHub slug, then `claude plugin update` after every source change (installs are copies, not symlinks).

`tools/wt-metrics.py` reads your local Claude Code transcripts and prints how worktree sessions behave: landings, `finish` failures, isolation-guard refusals, pushes per landing, `/wt:land` time. Run it with `--until` before an upgrade and `--since` after, and compare. It prints aggregates only.

Looking for a general-purpose worktree manager that is not tied to Claude Code sessions? See [Worktrunk](https://github.com/max-sixty/worktrunk).

## License

MIT. Release notes are in [CHANGELOG.md](CHANGELOG.md).
