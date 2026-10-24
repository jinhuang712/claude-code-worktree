#!/bin/bash
# A repo with one worktree that holds a commit not on main.
set -e
G="git -c user.name=eval -c user.email=eval@example.com"
git init -q -b main .
$G commit -q --allow-empty -m init
mkdir -p .claude/worktrees
git worktree add -q -b wt-auth-refactor .claude/worktrees/auth-refactor
echo 'export const x = 1' > .claude/worktrees/auth-refactor/auth.ts
$G -C .claude/worktrees/auth-refactor add auth.ts
$G -C .claude/worktrees/auth-refactor commit -q -m "wip: auth"
