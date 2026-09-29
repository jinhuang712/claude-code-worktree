---
name: land
description: Land the current worktree (a wt-* one, or any linked worktree branch) back onto the branch it was forked from - commit, rebase onto the (possibly moved) base, resolve conflicts, verify, fast-forward, clean up. Use when the user says /wt:land, "land 回去", "合回原分支", or is done with worktree work.
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

### A1. Land check

```bash
python3 "$WT" land              # adopt if needed, rebase onto the base, run check/test
```

| exit | meaning | what to do |
|---|---|---|
| 0 | `"state": "ready"`: rebased and verified | go to Phase B; pass the `finish` field to `finish --name` |
| 1 | an error; `dirty` lists uncommitted files | review `git diff`, commit (below), run `land` again. If `base_exists` is false, ask the user which branch to land on and run `land --onto <branch>`. |
| 7 | rebase conflicts | A2 |
| 8 | check/test failed | A3 |

To commit, use commands the worktree isolation guard accepts: `git add <paths>`, then `git commit -q -m "<subject>" -m "<body>"`, or write the message to a file with the Write tool and run `git commit -q -F <file>`. Write a real message describing the change (one commit per logical change is fine). A heredoc (`<<EOF`, `-F -`), `cd`, or `$(…)` is refused as "too complex to verify" and costs a turn.

**Worktrees `/wt:worktree` did not create** (a plain `git worktree add`, the built-in `EnterWorktree`, a `wt-*` branch without metadata) are adopted automatically. When the JSON has `adopted`, tell the user which base was inferred (`adopted.base`, `adopted.how`) before going on. If it fails with `candidates`, the branch also contains another branch's commits and may be stacked: ask with AskUserQuestion which candidate it should land on, run `python3 "$WT" adopt --base <branch>`, then `land` again.

`status`, `rebase` and `verify` still exist if you need one step on its own.

### A2. Conflicts (exit 7) — resolve, never abort

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

Repeat while the exit code is 7 (multi-commit rebases conflict per commit); once `continue` succeeds, run `land` again (a no-op rebase, then check/test). Never run `git rebase --abort` or `git rebase --skip` on your own; if a conflict is beyond you, stop, leave everything in place, and explain.

### A3. Red check/test (exit 8)

`land` runs `.claude/wt.json` `check` then `test`. Fix the failure in the worktree, commit, run `land` again. Do not land red.

## Phase B — main checkout

### B1. Exit

Call `ExitWorktree`. If it offers to remove the worktree, either answer is fine; `finish` handles both.

### B2. Finish

```bash
python3 "$WT" finish --name <name> [--squash -m "<message>"] [--keep] [--no-push]
```

`<name>` is the `finish` field from `land` (the branch without its `wt-` prefix; `finish` also finds `worktree-<name>` and exact branch names). The script takes a per-repo landing lock. If the base moved since your rebase (another session landed first) it rebases in place and re-runs check/test, so you do not go back into the worktree for that. It then fast-forwards the base wherever it is checked out (or moves the ref if nowhere), verifies the worktree head is an ancestor of base, pushes the base if `push` is set in `.claude/wt.json`, then removes the worktree, branch, metadata and any leftover stash. With `--squash` you must supply a commit message summarising all landed commits.

Exit codes from `finish`:

| exit | meaning | what to do |
|---|---|---|
| 7 | the base moved and the in-place rebase conflicts; nothing landed, the worktree is left mid-rebase | `EnterWorktree` with the `worktree` path from the JSON, go back to A2, `ExitWorktree`, run `finish` again |
| 8 | check/test fail on the rebased tree; nothing landed | `EnterWorktree`, fix, commit, `ExitWorktree`, run `finish` again |
| 9 | landed and cleaned up, but the push failed | report `push.error`; never force-push; usually the remote moved, so tell the user |

If it reports the fast-forward was **refused**, another session has uncommitted changes in the base checkout overlapping with this landing. Report the `checkout` path and the files; do not stash, reset or checkout on that session's behalf. The worktree is left intact for retry.

## Report

Summarise: commits landed, base branch and new head, conflicts resolved (file + one line on how), verify result, and whether the worktree was removed.
