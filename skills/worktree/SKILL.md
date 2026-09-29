---
name: worktree
description: Fork the current branch into an isolated wt-* git worktree inside this session, carrying only the changes this session made and leaving other sessions' uncommitted work in place. Use when the user says /wt:worktree, "开一个 worktree", "在 worktree 里做", or wants to isolate risky work from a shared checkout.
argument-hint: "[name] — short slug for the worktree, e.g. auth-refactor"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wt.py" *)
  - Bash(git status *)
  - Bash(git diff *)
  - EnterWorktree
  - AskUserQuestion
---

# /wt:worktree — fork this session's work into a worktree

`WT="${CLAUDE_PLUGIN_ROOT}/scripts/wt.py"`. Every subcommand prints JSON; read it, don't guess.

## 1. Plan

```bash
python3 "$WT" plan
```

Stop and tell the user if `warnings` is non-empty (detached HEAD, already on a `wt-*` branch, rebase in progress).

The dirty files come back in four groups:

| group | meaning | decision |
|---|---|---|
| `ours` | edited by this session via Edit/Write (hook-tracked) | take |
| `theirs` | edited by another live session | **never take** |
| `unknown` | nobody tracked it (Bash-generated, user-edited, build output) | you decide: take it only if you created or changed it in this conversation; otherwise leave it |
| `shared` | both this session and another one touched it | a file can't be split; use AskUserQuestion: take whole file / leave it / stop |

Do not read the file contents to decide; decide from what you did in this conversation.

## 2. Start

Pick a name: `$ARGUMENTS` if given, otherwise a 2–4 word kebab slug for the task. Then

```bash
python3 "$WT" start <name> --take <path> <path> ...
```

`start` must run from the main checkout. If this session is already inside a worktree, call `ExitWorktree` (`keep`) first.

Omit `--take` when nothing should move. The script: stashes only the listed paths, creates `.claude/worktrees/<name>` on branch `wt-<name>` from the current HEAD, re-applies the stash there, records base branch + fork sha in git config, copies `.worktreeinclude` files.

On `"ok": false`, report the `error` verbatim and stop; the script never leaves a half-state (a leftover stash tagged `wt:<name>` is resumed automatically on retry).

## 3. Enter

Call `EnterWorktree` with the `worktree` path from the JSON (it is inside `.claude/worktrees/`, so no extra prompt). Do **not** run `git worktree add` yourself and do not stay in the main checkout.

## 4. Report

One short message: branch, base, which files moved, which were left behind and why. Then continue with the user's actual task. When the work is done, the user runs `/wt:land`.

## 5. Working inside the worktree

The session is now isolated: Claude Code refuses any Bash command it cannot prove stays inside the worktree ("too complex to verify"), and every refusal wastes a turn. What gets refused most: loops, `$(…)` and backticks, heredocs (`<<EOF`, `-F -`), `VAR=…` assignments, and `cd` anywhere but this worktree. What passes: a plain command, and `python3 <file>` / `bash <file>`.

- Need a script, a multi-line edit or a commit message? Write it to a file with the Write tool (the scratchpad is fine), then run `python3 <file>` or `git commit -F <file>` as one plain command.
- Run `git add` and `git commit` as separate plain commands.
- Never `cd` out of the worktree and never aim git at another checkout.
