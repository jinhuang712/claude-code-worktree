---
name: land
description: Land the current wt-* worktree back onto the branch it was forked from - commit, rebase onto the (possibly moved) base, resolve conflicts, verify, fast-forward, clean up. Use when the user says /wt:land, "land 回去", "合回原分支", or is done with worktree work.
argument-hint: "[--squash] [--keep] — squash into one commit; keep worktree after landing"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wt.py" *)
  - Bash(git status *)
  - Bash(git diff *)
  - Bash(git log *)
  - Bash(git add *)
  - Bash(git commit *)
  - Read
  - Edit
  - Write
  - ExitWorktree
  - AskUserQuestion
---

# /wt:land — land the worktree onto its base branch

`WT="${CLAUDE_PLUGIN_ROOT}/scripts/wt.py"`. Two phases. Phase A runs **inside** the worktree; phase B runs **after ExitWorktree** in the main checkout, because worktree isolation blocks git commands aimed at the main checkout.

## Phase A — inside the worktree

### A1. Status

```bash
python3 "$WT" status
```

If `dirty` is non-empty: review `git diff`, then commit with a real message describing the change (one commit per logical change is fine). If `base_exists` is false, ask the user which branch to land on and pass it to `rebase --onto <branch>`.

### A2. Rebase

```bash
python3 "$WT" rebase            # exit 0: rebased/no-op · exit 7: conflicts · exit 1: error
```

### A3. Conflicts (exit 7) — resolve, never abort

The JSON lists every conflicted file with `kind`, `base_commits`/`base_diff` (what the base branch did since the fork) and `worktree_commits`/`worktree_diff` (what we did). Roles are swapped during rebase: `<<<<<<< HEAD/ours` is the **base**, `>>>>>>> theirs` is **our** worktree commit.

For each file:
1. Read both diffs and state in one sentence why each side changed the file. Resolve to a version that keeps **both intents**; never pick a side blindly.
2. `kind: lockfile` → drop the markers by regenerating the lockfile with the project's tool (`npm install`, `uv lock`, `cargo generate-lockfile`, …) instead of hand-merging.
3. `kind: migration` → stop and ask the user; two migrations at the same ordinal need a human decision.
4. `deleted-by-*` → decide whether the deletion or the modification wins; if the file was moved on base, apply our change to the new location.
5. Remove every conflict marker. If `check` is set in the JSON, run it before continuing.

Then:

```bash
python3 "$WT" continue          # stages resolved files, runs `git rebase --continue`
```

Repeat A3 while the exit code is 7 (multi-commit rebases conflict per commit). Never run `git rebase --abort` or `git rebase --skip` on your own; if a conflict is beyond you, stop, leave everything in place, and explain.

### A4. Verify

```bash
python3 "$WT" verify            # runs .claude/wt.json "check" then "test"; exit 8 on failure
```

On exit 8: fix the failure in the worktree, commit, re-run `rebase` (no-op) and `verify`. Do not land red.

## Phase B — main checkout

### B1. Exit

Call `ExitWorktree`. If it offers to remove the worktree, either answer is fine; `finish` handles both.

### B2. Finish

```bash
python3 "$WT" finish --name <name> [--squash -m "<message>"] [--keep]
```

`<name>` is the branch without the `wt-` prefix (from `status`). The script fast-forwards the base wherever it is checked out (or moves the ref if nowhere), verifies the worktree head is an ancestor of base, then removes the worktree, branch, metadata and any leftover stash. With `--squash` you must supply a commit message summarising all landed commits.

If it reports the fast-forward was **refused**, another session has uncommitted changes in the base checkout overlapping with this landing. Report the `checkout` path and the files; do not stash, reset or checkout on that session's behalf. The worktree is left intact for retry.

## Report

Summarise: commits landed, base branch and new head, conflicts resolved (file + one line on how), verify result, and whether the worktree was removed.
