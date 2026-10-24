---
name: status
description: Show every worktree of this repository at a glance - branch, base, unlanded commits, uncommitted files, live sessions, and which are safe to clean up. Use when the user says /wt:status, asks "which worktrees do I have", "what's left to land", or wants to know what gc would remove.
argument-hint: "[--gc] — also run the gc dry run"
allowed-tools:
  - Bash(wt list *)
  - Bash(wt gc)
  - Bash(wt preview *)
---

# /wt:status — what is in flight

Read-only. Never removes anything.

```bash
wt list --all
```

Show one table, one row per worktree, newest activity first:

| worktree | branch → base | unlanded | behind | uncommitted | state |
|---|---|---|---|---|---|

`state` is one of: **live** (a session edited it recently), **ready to land** (clean, ahead, nothing blocking), **merged** (safe to clean up), **needs attention** (uncommitted or mid-rebase). Use `blockers` from the JSON for the reason, and say plainly when the base is unknown or ambiguous.

For a worktree that is ahead and behind, offer to predict its conflicts with `wt preview --name <name>`; run it only if asked.

With `--gc`, also run `wt gc` (a dry run) and list what it would remove. Removing is the user's call: `wt gc --apply` is not in this skill's allowed tools.

End with one line of next steps: `/wt:land` for a ready worktree, `wt gc --apply` for the merged ones.
