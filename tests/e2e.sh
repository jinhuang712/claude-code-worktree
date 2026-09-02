#!/usr/bin/env bash
# End-to-end test: two sessions, /worktree takes only ours, base moves with a conflict, /land resolves & ff-merges.
set -euo pipefail
WT="$(cd "$(dirname "$0")/.." && pwd)/scripts/wt.py"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
R="$T/repo"; mkdir -p "$R"; cd "$R"
git init -q -b main . ; git config user.email t@t; git config user.name t
printf 'line1\nline2\nline3\n' > a.txt; echo b > b.txt; echo '{"lock":1}' > package-lock.json
mkdir -p .claude; echo '{"check":"test -f a.txt","test":"grep -q merged a.txt"}' > .claude/wt.json
git add -A; git commit -qm init
A=sess-A; B=sess-B
track(){ printf '{"session_id":"%s","cwd":"%s","tool_input":{"file_path":"%s"}}' "$1" "$R" "$R/$2" | python3 "$WT" track; }
fail(){ echo "FAIL: $*"; exit 1; }
j(){ python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1],{'d':d}))" "$1"; }

# --- two sessions edit different files, plus an untracked file nobody tracked
printf 'line1\nline2 A\nline3\n' > a.txt; track $A a.txt
echo bb > b.txt;                       track $B b.txt
echo c > c.txt
PLAN=$(CLAUDE_CODE_SESSION_ID=$A python3 "$WT" plan)
[ "$(echo "$PLAN" | j "[x['path'] for x in d['ours']]")" = "['a.txt']" ] || fail plan ours
[ "$(echo "$PLAN" | j "[x['path'] for x in d['theirs']]")" = "['b.txt']" ] || fail plan theirs
[ "$(echo "$PLAN" | j "[x['path'] for x in d['unknown']]")" = "['c.txt']" ] || fail plan unknown
echo "✓ plan"

# --- start: take ours + the unknown file the LLM judged as ours
OUT=$(CLAUDE_CODE_SESSION_ID=$A python3 "$WT" start feat --take a.txt c.txt)
W=$(echo "$OUT" | j "d['worktree']")
[ -d "$W" ] || fail worktree dir
[ "$(git status --porcelain)" = " M b.txt" ] || fail "main should only keep b.txt dirty: $(git status --porcelain)"
grep -q 'line2 A' "$W/a.txt" && [ -f "$W/c.txt" ] || fail changes not carried
[ "$(git -C "$W" branch --show-current)" = wt-feat ] || fail branch
[ "$(git config branch.wt-feat.wtBase)" = main ] || fail meta
echo "✓ start (b.txt stayed, a.txt+c.txt moved)"

# --- work in worktree, commit
(cd "$W"; git add -A; git commit -qm "feat: a & c")
# --- base moves with a conflicting change on the same line
printf 'line1\nline2 MAIN\nline3\n' > a.txt; git add a.txt; git commit -qm "main: a"
set +e; ST=$(cd "$W" && python3 "$WT" status); set -e
[ "$(echo "$ST" | j "d['base_moved']")" = True ] || fail base_moved

# --- rebase → conflict (exit 7) with structured report
set +e; REP=$(cd "$W" && python3 "$WT" rebase); RC=$?; set -e
[ $RC -eq 7 ] || fail "expected exit 7, got $RC: $REP"
[ "$(echo "$REP" | j "d['files'][0]['path']")" = a.txt ] || fail report path
echo "$REP" | j "d['files'][0]['base_diff']" | grep -q 'line2 MAIN' || fail report base_diff
echo "✓ rebase → exit 7 with base/worktree diffs"

# --- continue with markers still present → still 7
set +e; REP2=$(cd "$W" && python3 "$WT" continue); RC=$?; set -e
[ $RC -eq 7 ] && echo "$REP2" | grep -q unresolved_markers || fail "markers check rc=$RC $REP2"
# --- resolve (keep both intents) and continue
printf 'line1\nline2 merged(A+MAIN)\nline3\n' > "$W/a.txt"
OUT=$(cd "$W" && python3 "$WT" continue)
[ "$(echo "$OUT" | j "d['state']")" = rebased ] || fail continue
echo "✓ continue → rebased"

# --- verify runs check+test from .claude/wt.json
OUT=$(cd "$W" && python3 "$WT" verify)
[ "$(echo "$OUT" | j "d['state']")" = verified ] || fail "verify: $OUT"
echo "✓ verify"

# --- finish from main (main still has unrelated dirty b.txt)
set +e; BAD=$(cd "$W" && python3 "$WT" finish --name feat); RC=$?; set -e
[ $RC -eq 1 ] || fail "finish inside worktree should refuse"
OUT=$(python3 "$WT" finish --name feat)
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] || fail "finish: $OUT"
grep -q 'merged(A+MAIN)' a.txt && [ -f c.txt ] || fail landed content
[ "$(git status --porcelain)" = " M b.txt" ] || fail "b.txt should survive: $(git status --porcelain)"
[ ! -d "$W" ] || fail worktree not removed
git rev-parse --verify -q wt-feat >/dev/null && fail branch not deleted
git config branch.wt-feat.wtBase >/dev/null 2>&1 && fail config not removed
[ -z "$(git stash list)" ] || fail stash leftover
echo "✓ finish (ff-merge, cleanup, other session's b.txt untouched)"

# --- ff refused when main has overlapping dirty file
OUT=$(CLAUDE_CODE_SESSION_ID=$A python3 "$WT" start two)
W2=$(echo "$OUT" | j "d['worktree']")
(cd "$W2"; echo x >> a.txt; git commit -qam "two")
echo dirty >> a.txt
set +e; OUT=$(python3 "$WT" finish --name two); RC=$?; set -e
[ $RC -eq 1 ] && echo "$OUT" | grep -q refused || fail "overlap should refuse: $OUT"
[ -d "$W2" ] || fail "worktree must survive refused finish"
git checkout -q a.txt
OUT=$(python3 "$WT" finish --name two --squash -m "squashed two")
[ "$(git log -1 --format=%s)" = "squashed two" ] || fail squash
echo "✓ refuse overlapping dirty base; squash mode"

# --- abandon brings changes back
OUT=$(CLAUDE_CODE_SESSION_ID=$A python3 "$WT" start three)
W3=$(echo "$OUT" | j "d['worktree']")
(cd "$W3"; echo new > new.txt; git add new.txt; git commit -qm n; echo wip > wip.txt)
OUT=$(python3 "$WT" abandon three)
[ -f new.txt ] && [ -f wip.txt ] && [ ! -d "$W3" ] || fail "abandon: $OUT"
echo "✓ abandon"
echo ALL PASS
