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

# ─── finish when the base moved after the worktree's own rebase (another session landed first) ───
newrepo(){  # fresh repo named $1 with main + one commit; cd into it
  mkdir -p "$T/$1"; cd "$T/$1"
  git init -q -b main . ; git config user.email t@t; git config user.name t
  echo base > base.txt; git add -A; git commit -qm init
}
wtstart(){ CLAUDE_CODE_SESSION_ID=$A python3 "$WT" start "$1" | j "d['worktree']"; }
wtcommit(){ (cd "$1"; echo "$2" > "$2.txt"; git add "$2.txt"; git commit -qm "$2"); }

# non-conflicting: finish rebases in place, lands linearly, cleans up
newrepo race
W=$(wtstart feat-a); wtcommit "$W" a
(cd "$W"; python3 "$WT" rebase >/dev/null)
echo M > m.txt; git add m.txt; git commit -qm "main m"
OUT=$(python3 "$WT" finish --name feat-a)
[ "$(echo "$OUT" | j "d['rebased_during_finish']")" = True ] || fail "in-place rebase not reported: $OUT"
[ "$(echo "$OUT" | j "d['base_moved_by']")" = 1 ] || fail "base_moved_by: $OUT"
[ -f a.txt ] && [ -f m.txt ] || fail "both changes must be on main"
[ "$(git log -2 --format=%s | tr '\n' ,)" = "a,main m," ] || fail "history must be linear: $(git log --oneline)"
[ ! -d "$W" ] || fail "worktree not removed"
echo "✓ finish rebases in place when base moved"

# conflicting: exit 7, nothing landed, worktree left mid-rebase; resolve, continue, finish
newrepo conflict
echo x > shared.txt; git add -A; git commit -qm shared
W=$(wtstart feat-b)
(cd "$W"; echo wt > shared.txt; git add -A; git commit -qm "wt shared"; python3 "$WT" rebase >/dev/null)
echo main > shared.txt; git add -A; git commit -qm "main shared"
set +e; OUT=$(python3 "$WT" finish --name feat-b); RC=$?; set -e
[ $RC -eq 7 ] || fail "expected exit 7, got $RC: $OUT"
[ "$(echo "$OUT" | j "d['landed']")" = False ] || fail "must not land on conflict"
[ "$(echo "$OUT" | j "d['worktree'].split('/')[-1]")" = feat-b ] || fail "worktree path missing: $OUT"
[ "$(git -C "$W" status --short | grep -c '^UU shared.txt')" = 1 ] || fail "worktree should be mid-rebase"
[ "$(git log -1 --format=%s)" = "main shared" ] || fail "main must not move on conflict"
set +e; OUT=$(python3 "$WT" finish --name feat-b); RC=$?; set -e
[ $RC -eq 7 ] || fail "finish during a rebase in progress must stay exit 7, got $RC: $OUT"
printf 'merged\n' > "$W/shared.txt"
(cd "$W"; python3 "$WT" continue >/dev/null)
OUT=$(python3 "$WT" finish --name feat-b)
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] && grep -q merged shared.txt || fail "landing after resolving: $OUT"
[ "$(echo "$OUT" | j "'rebased_during_finish' in d")" = False ] || fail "no second in-place rebase expected"
echo "✓ finish conflict → exit 7, resolve, continue, finish"

# a failing test on the rebased tree: exit 8, nothing landed, worktree intact
newrepo verifyfail
mkdir -p .claude; echo '{"test":"test ! -f poison.txt"}' > .claude/wt.json; git add -A; git commit -qm cfg
W=$(wtstart feat-c); wtcommit "$W" c
(cd "$W"; python3 "$WT" rebase >/dev/null)
echo p > poison.txt; git add poison.txt; git commit -qm poison
set +e; OUT=$(python3 "$WT" finish --name feat-c); RC=$?; set -e
[ $RC -eq 8 ] || fail "expected exit 8, got $RC: $OUT"
[ "$(echo "$OUT" | j "d['landed']")" = False ] || fail "must not land red"
[ "$(git log -1 --format=%s)" = poison ] && [ -d "$W" ] || fail "main moved or worktree lost"
echo "✓ finish verifies after an in-place rebase (exit 8, nothing landed)"

# lock: a landing waits for the lock, gives up after lockTimeoutSec
newrepo lock
mkdir -p .claude
W=$(wtstart feat-d); wtcommit "$W" d
LOCK="$(git rev-parse --git-common-dir)/claude-wt/land.lock"; mkdir -p "$(dirname "$LOCK")"
python3 -c "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(4)" "$LOCK" > "$T/held.out" &
HOLDER=$!
for _ in $(seq 50); do grep -q held "$T/held.out" && break; sleep 0.1; done
echo '{"lockTimeoutSec":1}' > .claude/wt.json
set +e; OUT=$(python3 "$WT" finish --name feat-d); RC=$?; set -e
[ $RC -eq 1 ] && echo "$OUT" | grep -q "another landing" || fail "lock timeout: rc=$RC $OUT"
[ -d "$W" ] || fail "worktree must survive a lock timeout"
echo '{"lockTimeoutSec":30}' > .claude/wt.json
T0=$(date +%s)
OUT=$(python3 "$WT" finish --name feat-d)
[ $(( $(date +%s) - T0 )) -ge 1 ] || fail "finish did not wait for the lock"
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] || fail "landing after the lock was released: $OUT"
wait $HOLDER
echo "✓ finish serializes on the landing lock (timeout, then waits and lands)"

# two landings at once: both land, linearly, no merge commits
newrepo parallel
W1=$(wtstart p1); W2=$(wtstart p2); wtcommit "$W1" p1; wtcommit "$W2" p2
python3 "$WT" finish --name p1 > "$T/f1.out" & P1=$!
python3 "$WT" finish --name p2 > "$T/f2.out" & P2=$!
wait $P1; wait $P2
[ -f p1.txt ] && [ -f p2.txt ] || fail "both landings must be on main"
[ "$(git rev-list --count main)" = 3 ] && [ -z "$(git log --merges --oneline)" ] || fail "history: $(git log --oneline)"
[ ! -d "$W1" ] && [ ! -d "$W2" ] || fail "worktrees not removed"
echo "✓ two simultaneous finishes both land"

# --keep, and a base that is not checked out anywhere (ref is moved)
newrepo keep
W=$(wtstart feat-k); wtcommit "$W" k
git switch -q -c elsewhere
OUT=$(python3 "$WT" finish --name feat-k --keep)
echo "$OUT" | grep -q "not checked out anywhere" || fail "base ref move: $OUT"
[ "$(git rev-parse main)" = "$(git -C "$W" rev-parse HEAD)" ] || fail "main should point at the worktree head"
[ -d "$W" ] && git rev-parse -q --verify wt-feat-k >/dev/null || fail "--keep must keep worktree and branch"
echo "✓ finish --keep; base not checked out"

# ─── opt-in push: "push": true in .claude/wt.json ───
remote_sha(){ git --git-dir="$T/$1.git" rev-parse main; }
newrepo pushy
git init -q --bare -b main "$T/origin.git"; git remote add origin "$T/origin.git"
mkdir -p .claude; echo '{"push":true}' > .claude/wt.json; git add -A; git commit -qm cfg; git push -q origin main
W=$(wtstart feat-p); wtcommit "$W" p
OUT=$(python3 "$WT" finish --name feat-p)
[ "$(echo "$OUT" | j "d['push']['ok']")" = True ] || fail "push not reported ok: $OUT"
[ "$(remote_sha origin)" = "$(git rev-parse main)" ] || fail "origin/main should equal main after landing"
echo "✓ push: true pushes the landed base"

# origin moved elsewhere: the push is rejected (never forced), the landing still stands
git clone -q "$T/origin.git" "$T/other"
(cd "$T/other"; git config user.email t@t; git config user.name t; echo o > o.txt; git add o.txt; git commit -qm other; git push -q origin main)
OTHER=$(remote_sha origin)
W=$(wtstart feat-q); wtcommit "$W" q
set +e; OUT=$(python3 "$WT" finish --name feat-q); RC=$?; set -e
[ $RC -eq 9 ] || fail "expected exit 9, got $RC: $OUT"
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] && [ "$(echo "$OUT" | j "d['push']['ok']")" = False ] || fail "push failure report: $OUT"
[ -f q.txt ] && [ ! -d "$W" ] || fail "landing and cleanup must stand after a rejected push"
[ "$(remote_sha origin)" = "$OTHER" ] || fail "a rejected push must not change origin"
echo "✓ rejected push → exit 9, landing kept, origin untouched"

# --no-push skips it
W=$(wtstart feat-r); wtcommit "$W" r
OUT=$(python3 "$WT" finish --name feat-r --no-push)
[ "$(echo "$OUT" | j "'push' in d")" = False ] || fail "--no-push must not push: $OUT"
echo "✓ --no-push"

# a remote without the config: nothing is pushed
newrepo nopush
git init -q --bare -b main "$T/nopush-origin.git"; git remote add origin "$T/nopush-origin.git"; git push -q origin main
BEFORE=$(git --git-dir="$T/nopush-origin.git" rev-parse main)
W=$(wtstart feat-s); wtcommit "$W" s
OUT=$(python3 "$WT" finish --name feat-s)
[ "$(echo "$OUT" | j "'push' in d")" = False ] && [ "$(git --git-dir="$T/nopush-origin.git" rev-parse main)" = "$BEFORE" ] || fail "no config, no push: $OUT"
echo "✓ no push without the config"

# ─── foreign worktrees: adopt them instead of failing ───
# a plain `git worktree add`, outside .claude/worktrees, base moved: status adopts, finish rebases and lands
newrepo foreign
git worktree add -q -b foo "$T/foo-wt"
(cd "$T/foo-wt"; echo f > f.txt; git add f.txt; git commit -qm f)
echo M > m.txt; git add m.txt; git commit -qm "main m"
ST=$(cd "$T/foo-wt" && python3 "$WT" status)
[ "$(echo "$ST" | j "d['base']")" = main ] || fail "adopted base: $ST"
echo "$ST" | j "d['adopted']['how']" | grep -q "main checkout" || fail "adoption reason: $ST"
[ "$(echo "$ST" | j "[d['ahead'], d['behind']]")" = "[1, 1]" ] || fail "ahead/behind: $ST"
ST=$(cd "$T/foo-wt" && python3 "$WT" status)
[ "$(echo "$ST" | j "'adopted' in d")" = False ] || fail "adoption should be recorded once: $ST"
OUT=$(python3 "$WT" finish --name foo)
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] && [ -f f.txt ] && [ -f m.txt ] || fail "foreign landing: $OUT"
[ ! -d "$T/foo-wt" ] && [ -z "$(git branch --list foo)" ] || fail "foreign worktree and branch must be cleaned up"
echo "✓ foreign worktree: adopted by status, lands via finish"

# outside a linked worktree there is nothing to adopt
set +e; OUT=$(python3 "$WT" status); RC=$?; set -e
[ $RC -eq 1 ] && echo "$OUT" | grep -q "not inside a linked worktree" || fail "status in the main checkout: rc=$RC $OUT"

# a built-in style worktree-<name> branch: finish --name finds it and adopts it from the main checkout
newrepo native
git worktree add -q -b worktree-bar "$T/native-bar"
(cd "$T/native-bar"; echo b > b.txt; git add b.txt; git commit -qm b)
OUT=$(python3 "$WT" finish --name bar)
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] && [ "$(echo "$OUT" | j "d['adopted']['base']")" = main ] || fail "worktree-<name>: $OUT"
[ ! -d "$T/native-bar" ] || fail "worktree-<name> worktree not removed"
echo "✓ worktree-<name> branch resolved and adopted by finish"

# a wt-* branch without metadata, through `land`
newrepo nometa
git worktree add -q -b wt-baz "$T/nometa-baz"
(cd "$T/nometa-baz"; echo z > z.txt; git add z.txt; git commit -qm z)
OUT=$(cd "$T/nometa-baz" && python3 "$WT" land)
[ "$(echo "$OUT" | j "[d['state'], d['adopted']['base'], d['finish']]")" = "['ready', 'main', 'baz']" ] || fail "wt-* without metadata: $OUT"
OUT=$(python3 "$WT" finish --name baz)
[ "$(echo "$OUT" | j "d['landed']")" = 1 ] || fail "landing the adopted wt-* branch: $OUT"
echo "✓ wt-* without metadata adopted by land"

# stacked on another branch: refuse to guess, offer candidates, then adopt --base
newrepo stacked
git switch -q -c feat-x; echo x > x.txt; git add x.txt; git commit -qm x1; git switch -q main
git worktree add -q -b child "$T/child-wt" feat-x
(cd "$T/child-wt"; echo c > c.txt; git add c.txt; git commit -qm c1)
set +e; OUT=$(cd "$T/child-wt" && python3 "$WT" status); RC=$?; set -e
[ $RC -eq 1 ] && echo "$OUT" | grep -q stacked || fail "stacked branch must be refused: rc=$RC $OUT"
[ "$(echo "$OUT" | j "sorted(d['candidates'])")" = "['feat-x', 'main']" ] || fail "candidates: $OUT"
[ -z "$(git config --get branch.child.wtBase || true)" ] || fail "nothing may be recorded when adoption is refused"
OUT=$(cd "$T/child-wt" && python3 "$WT" adopt --base feat-x)
[ "$(echo "$OUT" | j "[d['base'], d['adopted']['ahead']]")" = "['feat-x', 1]" ] || fail "adopt --base: $OUT"
echo "✓ stacked branch refused, adopt --base resolves it"

# abandon works on a foreign worktree too
newrepo abandonforeign
git worktree add -q -b old "$T/old-wt"
(cd "$T/old-wt"; echo n > n.txt; git add n.txt; git commit -qm n; echo w > w.txt)
OUT=$(python3 "$WT" abandon old)
[ -f n.txt ] && [ -f w.txt ] && [ ! -d "$T/old-wt" ] || fail "abandon foreign: $OUT"
echo "✓ abandon on a foreign worktree"

# ─── land: rebase + verify in one call ───
newrepo landing
mkdir -p .claude; echo '{"test":"test ! -f poison.txt"}' > .claude/wt.json; git add -A; git commit -qm cfg
W=$(wtstart feat-l); wtcommit "$W" l
echo M > m.txt; git add m.txt; git commit -qm "main m"
OUT=$(cd "$W" && python3 "$WT" land)
[ "$(echo "$OUT" | j "[d['state'], d['rebased'], d['verify']['configured'], d['finish']]")" = "['ready', True, True, 'feat-l']" ] || fail "land ready: $OUT"
echo x > "$W/dirty.txt"
set +e; OUT=$(cd "$W" && python3 "$WT" land); RC=$?; set -e
[ $RC -eq 1 ] && echo "$OUT" | grep -q dirty.txt || fail "land with a dirty worktree: rc=$RC $OUT"
rm "$W/dirty.txt"
echo p > poison.txt; git add poison.txt; git commit -qm poison
set +e; OUT=$(cd "$W" && python3 "$WT" land); RC=$?; set -e
[ $RC -eq 8 ] || fail "land with a red test: rc=$RC $OUT"
echo "✓ land: ready, dirty → exit 1, red test → exit 8"

newrepo landconflict
echo x > shared.txt; git add -A; git commit -qm shared
W=$(wtstart feat-m)
(cd "$W"; echo wt > shared.txt; git add -A; git commit -qm "wt shared")
echo main > shared.txt; git add -A; git commit -qm "main shared"
set +e; OUT=$(cd "$W" && python3 "$WT" land); RC=$?; set -e
[ $RC -eq 7 ] || fail "land with a conflict: rc=$RC $OUT"
printf 'merged\n' > "$W/shared.txt"
(cd "$W"; python3 "$WT" continue >/dev/null)
OUT=$(cd "$W" && python3 "$WT" land)
[ "$(echo "$OUT" | j "[d['state'], d['rebased']]")" = "['ready', False]" ] || fail "land after resolving: $OUT"
echo "✓ land: conflict → exit 7, then a no-op rebase after continue"

# ─── session tracking: a dead session or another worktree must not misclassify files ───
age(){ python3 -c "import os,sys,time; t=time.time()-float(sys.argv[2])*3600; os.utime(sys.argv[1],(t,t))" "$1" "$2"; }
sfile(){ f="$(git rev-parse --git-common-dir)/claude-wt/sessions/$1.txt"; mkdir -p "$(dirname "$f")"; echo "$f"; }
trk(){ printf '{"session_id":"%s","cwd":"%s","tool_input":{"file_path":"%s"}}' "$1" "$2" "$3" | python3 "$WT" track; }
groups(){ CLAUDE_CODE_SESSION_ID=$1 python3 "$WT" plan | j "{k: [e['path'] for e in d[k]] for k in ('ours','theirs','shared','unknown')}"; }
THEIRS="{'ours': [], 'theirs': ['src/a.txt'], 'shared': [], 'unknown': []}"
UNKNOWN="{'ours': [], 'theirs': [], 'shared': [], 'unknown': ['src/a.txt']}"

newrepo tracking
mkdir -p src; echo a > src/a.txt; git add -A; git commit -qm files; echo change >> src/a.txt
trk OLD "$PWD" "$PWD/src/a.txt"
[ "$(groups NEW)" = "$THEIRS" ] || fail "a recently active session's file is theirs: $(groups NEW)"
age "$(sfile OLD)" 48
[ "$(groups NEW)" = "$UNKNOWN" ] || fail "a session idle for 2 days must not claim the file: $(groups NEW)"
trk OLD "$PWD" "$PWD/src/a.txt"
[ "$(groups NEW)" = "$THEIRS" ] || fail "editing the same file again must refresh liveness: $(groups NEW)"
echo "✓ stale sessions expire; a repeat edit keeps a session live"

# the same relative path edited in another worktree is not the same file
newrepo tracking2
mkdir -p src; echo a > src/a.txt; git add -A; git commit -qm files
W=$(wtstart other)
echo x >> src/a.txt; trk MAIN "$PWD" "$PWD/src/a.txt"
(cd "$W"; echo y >> src/a.txt; trk OTHER "$W" "$W/src/a.txt")
[ "$(groups MAIN)" = "{'ours': ['src/a.txt'], 'theirs': [], 'shared': [], 'unknown': []}" ] || fail "another worktree's edit must not make it shared: $(groups MAIN)"
echo "✓ edits in another worktree do not collide"

# lines written before roots were kept still count while recent, and expire like the rest
newrepo legacy
echo l > l.txt; git add -A; git commit -qm l; echo m >> l.txt
printf 'l.txt\n' > "$(sfile LEG)"
[ "$(groups NEW)" = "{'ours': [], 'theirs': ['l.txt'], 'shared': [], 'unknown': []}" ] || fail "recent legacy line: $(groups NEW)"
age "$(sfile LEG)" 48
[ "$(groups NEW)" = "{'ours': [], 'theirs': [], 'shared': [], 'unknown': ['l.txt']}" ] || fail "stale legacy line: $(groups NEW)"
echo "✓ legacy session lines"

# start prunes week-old session files and keeps the moved files ours inside the worktree
newrepo prune
: > "$(sfile ANCIENT)"; age "$(sfile ANCIENT)" 240; : > "$(sfile RECENT)"
echo c > c.txt; git add -A; git commit -qm c; echo more >> c.txt
trk "$A" "$PWD" "$PWD/c.txt"
W=$(CLAUDE_CODE_SESSION_ID=$A python3 "$WT" start carry --take c.txt | j "d['worktree']")
[ ! -f "$(sfile ANCIENT)" ] && [ -f "$(sfile RECENT)" ] || fail "start must prune only files older than a week"
[ "$(cd "$W" && CLAUDE_CODE_SESSION_ID=$A python3 "$WT" plan | j "[e['path'] for e in d['ours']]")" = "['c.txt']" ] || fail "moved files must stay ours in the worktree"
echo "✓ start prunes old session files; taken files stay ours"

# ─── list --all and gc ───
blockers(){ echo "$OUT" | j "' | '.join(b for x in d['worktrees'] if x['branch']=='$1' for b in x['blockers'])"; }
newrepo gc
OLD="$(python3 -c 'import time; print(int(time.time()) - 3 * 86400, "+0000")')"                     # a landed worktree idle for 3 days
W1=$(wtstart merged); (cd "$W1"; echo m1 > m1.txt; git add m1.txt; GIT_AUTHOR_DATE="$OLD" GIT_COMMITTER_DATE="$OLD" git commit -qm m1)
python3 "$WT" finish --name merged --keep >/dev/null
age "$(git rev-parse --git-common-dir)/worktrees/merged/HEAD" 72; age "$(git rev-parse --git-common-dir)/worktrees/merged/logs/HEAD" 72
W2=$(wtstart dirty); echo junk > "$W2/junk.txt"                                                     # clean history, uncommitted file
W3=$(wtstart unmerged); wtcommit "$W3" u1                                                           # commit not on main
W4=$(wtstart squashed); wtcommit "$W4" s1; git cherry-pick "$(git -C "$W4" rev-parse HEAD)" >/dev/null   # same patch, other sha
W5=$(wtstart live); trk sess-live "$W5" "$W5/x.txt"                                                 # a session just edited here
git worktree add -q -b ext "$T/gc-ext"                                                              # outside .claude/worktrees
git worktree add -q -b worktree-native .claude/worktrees/native                                     # built-in style, no metadata
OUT=$(python3 "$WT" list --all --min-age-hours 0)
[ "$(echo "$OUT" | j "len(d['worktrees'])")" = 7 ] || fail "list --all should show 7 worktrees: $OUT"
blockers wt-dirty | grep -q "uncommitted" || fail "dirty: $(blockers wt-dirty)"
blockers wt-unmerged | grep -q "not on main" || fail "unmerged: $(blockers wt-unmerged)"
blockers wt-live | grep -q "session" || fail "live: $(blockers wt-live)"
blockers ext | grep -q "outside .claude/worktrees" || fail "external: $(blockers ext)"
[ -z "$(blockers wt-merged)$(blockers wt-squashed)$(blockers worktree-native)" ] || fail "these three have no blockers: merged=[$(blockers wt-merged)] squashed=[$(blockers wt-squashed)] native=[$(blockers worktree-native)]"
echo "✓ list --all explains what blocks each worktree"

OUT=$(python3 "$WT" gc)
[ "$(echo "$OUT" | j "[c['branch'] for c in d['candidates']]")" = "['wt-merged']" ] || fail "with the 24h default only the idle merged worktree qualifies: $OUT"
echo "$OUT" | grep -q "min age" || fail "recent worktrees must be kept for the minimum age: $OUT"
OUT=$(python3 "$WT" gc --min-age-hours 0)
[ "$(echo "$OUT" | j "sorted(c['branch'] for c in d['candidates'])")" = "['worktree-native', 'wt-merged', 'wt-squashed']" ] || fail "gc candidates: $OUT"
[ "$(echo "$OUT" | j "d['dry_run']")" = True ] && [ -d "$W1" ] && [ -d "$W4" ] || fail "a dry run must remove nothing"
echo "✓ gc dry run: merged, patch-equivalent and built-in worktrees are candidates; minimum age respected"

OUT=$(python3 "$WT" gc --min-age-hours 0 --apply)
[ "$(echo "$OUT" | j "len(d['removed'])")" = 3 ] || fail "gc --apply: $OUT"
[ ! -d "$W1" ] && [ ! -d "$W4" ] && [ ! -d .claude/worktrees/native ] || fail "candidates must be removed"
[ -z "$(git branch --list wt-merged wt-squashed worktree-native)" ] || fail "their branches must be deleted"
[ -d "$W2" ] && [ -d "$W3" ] && [ -d "$W5" ] && [ -d "$T/gc-ext" ] || fail "dirty, unmerged, live and external worktrees must survive"
[ -f s1.txt ] && [ -f m1.txt ] || fail "landed work must still be on main"
echo "✓ gc --apply removes only the candidates"

git worktree add -q -b worktree-cur .claude/worktrees/cur
OUT=$(cd .claude/worktrees/cur && python3 "$WT" gc --min-age-hours 0)
echo "$OUT" | j "[k['why'] for k in d['kept'] if k['branch']=='worktree-cur']" | grep -q "current worktree" || fail "gc must not offer the worktree it runs in: $OUT"
echo "✓ gc leaves the current worktree alone"
echo ALL PASS
