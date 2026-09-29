#!/usr/bin/env python3
"""claude-code-worktree: deterministic git state machine behind /worktree and /land.

Stdlib only. Every subcommand prints one JSON object on stdout.
Exit codes: 0 ok · 1 error/precondition · 7 merge conflicts pending · 8 verify failed · 9 landed, push failed
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # no flock (Windows): landings are then not serialized
    fcntl = None

EXIT_OK, EXIT_ERR, EXIT_CONFLICT, EXIT_VERIFY, EXIT_PUSH = 0, 1, 7, 8, 9
BRANCH_PREFIX = "wt-"
WT_DIR = ".claude/worktrees"
STASH_TAG = "wt:"
CONFIG_FILE = ".claude/wt.json"
DIFF_LIMIT = 6000  # chars of diff context per conflicted file
LOCK_TIMEOUT = 300  # seconds `finish` waits for another landing; override with "lockTimeoutSec"
MAX_STACK_CHECK = 200  # local branches examined when deciding whether a foreign worktree is stacked
PUSH_TIMEOUT = 120  # seconds `git push` may take after landing; below LOCK_TIMEOUT so a hung remote frees the lock


class WtError(Exception):
    def __init__(self, msg: str, code: int = EXIT_ERR, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# ─── git helpers ────────────────────────────────────────────────────────────

def git(*args: str, cwd: str | Path | None = None, check: bool = True, env: dict | None = None) -> str:
    e = dict(os.environ, GIT_EDITOR="true", GIT_TERMINAL_PROMPT="0")
    if env:
        e.update(env)
    p = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, env=e)
    if check and p.returncode != 0:
        raise WtError(f"git {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def git_rc(*args: str, cwd=None) -> tuple[int, str, str]:
    p = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                       env=dict(os.environ, GIT_EDITOR="true", GIT_TERMINAL_PROMPT="0"))
    return p.returncode, p.stdout, p.stderr


class Repo:
    def __init__(self, cwd: str | None = None):
        self.cwd = Path(cwd or os.getcwd()).resolve()
        rc, out, _ = git_rc("rev-parse", "--show-toplevel", cwd=self.cwd)
        if rc:
            raise WtError(f"not inside a git repository: {self.cwd}")
        self.root = Path(out.strip())
        self.common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.root).strip())
        self.gitdir = Path(git("rev-parse", "--path-format=absolute", "--git-dir", cwd=self.root).strip())
        self.main_root = self._main_root()

    def _main_root(self) -> Path:
        for wt in self.worktrees():
            return Path(wt["path"])  # first entry is always the main worktree
        return self.root

    def worktrees(self) -> list[dict]:
        out = git("worktree", "list", "--porcelain", cwd=self.root)
        res, cur = [], {}
        for line in out.splitlines():
            if not line.strip():
                if cur:
                    res.append(cur)
                cur = {}
                continue
            k, _, v = line.partition(" ")
            if k == "worktree":
                cur["path"] = v
            elif k == "branch":
                cur["branch"] = v.removeprefix("refs/heads/")
            elif k in ("detached", "bare", "locked", "prunable"):
                cur[k] = True
        if cur:
            res.append(cur)
        return res

    def branch(self) -> str | None:
        rc, out, _ = git_rc("symbolic-ref", "--quiet", "--short", "HEAD", cwd=self.root)
        return out.strip() if rc == 0 else None

    def sha(self, ref: str) -> str:
        return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=self.root).strip()

    def ref_exists(self, ref: str) -> bool:
        return git_rc("rev-parse", "--verify", "--quiet", ref, cwd=self.root)[0] == 0

    def cfg(self, key: str) -> str | None:
        rc, out, _ = git_rc("config", "--get", key, cwd=self.root)
        return out.strip() if rc == 0 else None

    def set_cfg(self, key: str, val: str):
        git("config", key, val, cwd=self.root)

    def status(self, cwd: Path | None = None) -> list[dict]:
        """Porcelain v1 -z, untracked-files=all. Paths relative to worktree root."""
        out = git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=cwd or self.root)
        items, parts, i = [], out.split("\0"), 0
        while i < len(parts):
            rec = parts[i]
            if not rec:
                i += 1
                continue
            xy, path = rec[:2], rec[3:]
            entry = {"xy": xy, "path": path}
            if xy[0] in "RC" or xy[1] in "RC":
                i += 1
                entry["orig"] = parts[i]
            items.append(entry)
            i += 1
        return items

    def rebase_in_progress(self) -> bool:
        return (self.gitdir / "rebase-merge").exists() or (self.gitdir / "rebase-apply").exists()

    def find_stash(self, tag: str) -> str | None:
        out = git("stash", "list", "--format=%gd%x00%gs", cwd=self.root)
        for line in out.splitlines():
            ref, _, msg = line.partition("\0")
            if msg.endswith(f": {tag}") or msg == tag or f" {tag}" in msg:
                return ref
        return None

    # sessions ------------------------------------------------------------
    @property
    def sessions_dir(self) -> Path:
        d = self.common / "claude-wt" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def session_files(self, sid: str) -> set[str]:
        f = self.sessions_dir / f"{sid}.txt"
        return set(p for p in f.read_text().splitlines() if p) if f.exists() else set()

    def other_session_files(self, sid: str) -> dict[str, list[str]]:
        """path -> [session ids] for every session other than sid."""
        res: dict[str, list[str]] = {}
        for f in self.sessions_dir.glob("*.txt"):
            other = f.stem
            if other == sid:
                continue
            for p in f.read_text().splitlines():
                if p:
                    res.setdefault(p, []).append(other)
        return res

    # config --------------------------------------------------------------
    def config(self) -> dict:
        for base in (self.main_root, self.root):
            f = base / CONFIG_FILE
            if f.exists():
                try:
                    return json.loads(f.read_text())
                except json.JSONDecodeError as e:
                    raise WtError(f"invalid {f}: {e}")
        return {}


def session_id(explicit: str | None) -> str:
    sid = explicit or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        raise WtError("no session id: pass --session or run inside Claude Code (CLAUDE_CODE_SESSION_ID)")
    return sid


def sanitize(name: str) -> str:
    name = name.strip().removeprefix(BRANCH_PREFIX)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not name:
        raise WtError("worktree name is empty after sanitizing")
    return name


def emit(obj: dict, code: int = EXIT_OK):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def kind_of(path: str, cfg: dict) -> str | None:
    for pat in cfg.get("lockfiles", ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
                                     "poetry.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"]):
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(os.path.basename(path), pat):
            return "lockfile"
    for pat in cfg.get("migrations", []):
        if fnmatch.fnmatch(path, pat):
            return "migration"
    return None


# ─── track (hook) ───────────────────────────────────────────────────────────

def cmd_track(_args):
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    sid = data.get("session_id")
    if not path or not sid:
        return
    p = Path(path)
    if not p.is_absolute():
        p = Path(data.get("cwd") or os.getcwd()) / p
    try:
        repo = Repo(str(p.parent if p.parent.exists() else data.get("cwd") or os.getcwd()))
        rel = os.path.relpath(p.resolve(strict=False), repo.root)
    except Exception:
        return
    if rel.startswith(".."):
        return
    f = repo.sessions_dir / f"{sid}.txt"
    existing = repo.session_files(sid)
    if rel not in existing:
        with f.open("a") as fh:
            fh.write(rel + "\n")


# ─── plan ───────────────────────────────────────────────────────────────────

def classify(repo: Repo, sid: str) -> dict:
    ours, theirs = repo.session_files(sid), repo.other_session_files(sid)
    groups = {"ours": [], "theirs": [], "shared": [], "unknown": []}
    for it in repo.status():
        p = it["path"]
        entry = {"path": p, "xy": it["xy"]}
        if "orig" in it:
            entry["orig"] = it["orig"]
        mine, other = p in ours, p in theirs
        if mine and other:
            entry["sessions"] = theirs[p]
            groups["shared"].append(entry)
        elif mine:
            groups["ours"].append(entry)
        elif other:
            entry["sessions"] = theirs[p]
            groups["theirs"].append(entry)
        else:
            groups["unknown"].append(entry)
    return groups


def cmd_plan(args):
    repo = Repo()
    sid = session_id(args.session)
    br = repo.branch()
    groups = classify(repo, sid)
    warnings = []
    if br is None:
        warnings.append("HEAD is detached; /worktree needs a branch to land back to")
    if br and br.startswith(BRANCH_PREFIX):
        warnings.append(f"already on {br}; stacking worktrees is not supported")
    if repo.rebase_in_progress():
        warnings.append("a rebase is in progress in this checkout")
    emit({
        "branch": br, "head": repo.sha("HEAD") if repo.ref_exists("HEAD") else None,
        "root": str(repo.root), "session": sid,
        "suggested_take": [e["path"] for e in groups["ours"]],
        **groups, "warnings": warnings,
        "existing": [w for w in repo.worktrees() if w.get("branch", "").startswith(BRANCH_PREFIX)],
    })


# ─── start ──────────────────────────────────────────────────────────────────

def ensure_excluded(repo: Repo):
    if git_rc("check-ignore", "-q", WT_DIR, cwd=repo.main_root)[0] == 0:
        return
    exclude = repo.common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = f"/{WT_DIR}/"
    if not exclude.exists() or line not in exclude.read_text().splitlines():
        with exclude.open("a") as fh:
            fh.write(f"\n# added by claude-code-worktree\n{line}\n")


def copy_worktreeinclude(repo: Repo, dest: Path) -> list[str]:
    inc = repo.main_root / ".worktreeinclude"
    if not inc.exists():
        return []
    patterns = [l.strip() for l in inc.read_text().splitlines() if l.strip() and not l.startswith("#")]
    ignored = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=repo.main_root).split("\0")
    copied = []
    for rel in ignored:
        if not rel or rel.startswith(WT_DIR):
            continue
        if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/") + "/*") for p in patterns):
            src, dst = repo.main_root / rel, dest / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(rel)
    return copied


def cmd_start(args):
    repo = Repo()
    sid = session_id(args.session)
    name = sanitize(args.name)
    branch = BRANCH_PREFIX + name
    base = repo.branch()
    if base is None:
        raise WtError("HEAD is detached; check out a branch first")
    if base.startswith(BRANCH_PREFIX):
        raise WtError(f"already on {base}; stacking worktrees is not supported")
    if repo.rebase_in_progress():
        raise WtError("a rebase is in progress; finish or abort it first")
    if repo.root != repo.main_root and not args.allow_nested:
        raise WtError(f"run /worktree from the main checkout ({repo.main_root}), not from another worktree")

    wt_path = repo.main_root / WT_DIR / name
    tag = STASH_TAG + name
    resumed_stash = repo.find_stash(tag)

    if repo.ref_exists(f"refs/heads/{branch}") and not resumed_stash:
        raise WtError(f"branch {branch} already exists (use a different name or `wt.py abandon {name}`)")
    if wt_path.exists() and any(wt_path.iterdir()):
        raise WtError(f"{wt_path} already exists")

    dirty = {it["path"]: it for it in repo.status()}
    take = []
    for p in args.take or []:
        p = os.path.relpath((repo.root / p).resolve(strict=False), repo.root)
        if p not in dirty:
            raise WtError(f"--take {p}: not a modified/untracked path")
        take.append(p)
        if "orig" in dirty[p]:
            take.append(dirty[p]["orig"])

    base_sha = repo.sha("HEAD")
    steps = []

    if take and not resumed_stash:
        git("stash", "push", "--include-untracked", "-m", tag, "--", *take, cwd=repo.root)
        resumed_stash = repo.find_stash(tag)
        steps.append(f"stashed {len(take)} path(s) as {tag}")
    elif resumed_stash:
        steps.append(f"resuming from leftover stash {resumed_stash} ({tag})")

    ensure_excluded(repo)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if repo.ref_exists(f"refs/heads/{branch}"):
        git("worktree", "add", str(wt_path), branch, cwd=repo.main_root)
    else:
        git("worktree", "add", str(wt_path), "-b", branch, base_sha, cwd=repo.main_root)
    steps.append(f"created worktree {wt_path} on {branch}")

    repo.set_cfg(f"branch.{branch}.wtBase", base)
    repo.set_cfg(f"branch.{branch}.wtBaseSha", base_sha)
    repo.set_cfg(f"branch.{branch}.wtSession", sid)
    repo.set_cfg(f"branch.{branch}.wtPath", str(wt_path))

    applied = []
    if resumed_stash:
        rc, out, err = git_rc("stash", "apply", resumed_stash, cwd=wt_path)
        if rc:
            raise WtError(f"stash apply failed in worktree; stash {resumed_stash} kept: {err.strip()}",
                          worktree=str(wt_path), stash=resumed_stash)
        applied = [it["path"] for it in repo.status(cwd=wt_path)]
        git("stash", "drop", resumed_stash, cwd=repo.root)
        steps.append(f"applied {len(applied)} path(s) into worktree and dropped stash")

    copied = copy_worktreeinclude(repo, wt_path)
    if copied:
        steps.append(f"copied {len(copied)} .worktreeinclude file(s)")

    # carry over the session's touched-file list so `plan` inside the worktree still works
    emit({
        "ok": True, "name": name, "branch": branch, "base": base, "base_sha": base_sha,
        "worktree": str(wt_path), "taken": applied, "copied": copied, "steps": steps,
        "next": f"EnterWorktree(path=\"{wt_path}\")",
    })


# ─── status / meta ──────────────────────────────────────────────────────────

def wt_meta(repo: Repo, branch: str) -> dict:
    base = repo.cfg(f"branch.{branch}.wtBase")
    if not base:
        raise WtError(f"{branch} has no wtBase metadata; was it created by /worktree?")
    return {
        "branch": branch, "base": base,
        "base_sha": repo.cfg(f"branch.{branch}.wtBaseSha"),
        "session": repo.cfg(f"branch.{branch}.wtSession"),
        "path": repo.cfg(f"branch.{branch}.wtPath"),
    }


def default_base(repo: Repo, branch: str) -> tuple[str | None, str]:
    """Best guess at the branch a foreign worktree should land on, and why."""
    rc, out, _ = git_rc("symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo.main_root)
    cur = out.strip() if rc == 0 else None
    if cur and cur != branch:
        return cur, "the branch checked out in the main checkout"
    rc, out, _ = git_rc("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=repo.root)
    head = out.strip().removeprefix("origin/") if rc == 0 else None
    if head and head != branch and repo.ref_exists(f"refs/heads/{head}"):
        return head, "origin's default branch"
    for b in ("main", "master"):
        if b != branch and repo.ref_exists(f"refs/heads/{b}"):
            return b, f"the branch named {b}"
    return None, ""


def stacked_on(repo: Repo, branch: str, base: str, fork: str) -> list[str]:
    """Other local branches whose commits `branch` also contains beyond the fork point. Landing on `base`
    would drag those commits along, so the real base is probably one of them."""
    alts = []
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=repo.root).split()
    for other in refs[:MAX_STACK_CHECK]:
        if other in (branch, base):
            continue
        rc, mb, _ = git_rc("merge-base", other, branch, cwd=repo.root)
        mb = mb.strip()
        if rc or not mb or mb == fork:
            continue
        if git_rc("merge-base", "--is-ancestor", branch, other, cwd=repo.root)[0] == 0:
            continue  # `other` was built on top of `branch`, so it cannot be its base
        if git_rc("merge-base", "--is-ancestor", fork, mb, cwd=repo.root)[0] == 0:
            alts.append(other)
    return alts


def adopt_branch(repo: Repo, branch: str, wt_path: Path | None = None, base: str | None = None) -> dict:
    """Record base and fork-point metadata for a worktree branch `start` did not create, so status, rebase,
    verify and finish work on it. Refuses when the base is ambiguous instead of guessing."""
    how = "given with --base"
    if base is None:
        base, how = default_base(repo, branch)
        if not base:
            raise WtError(f"cannot infer which branch {branch} should land on; run `wt.py adopt --base <branch>`")
    if base == branch or not repo.ref_exists(f"refs/heads/{base}"):
        raise WtError(f"base branch {base} is not usable for {branch}; run `wt.py adopt --base <branch>`")
    rc, out, _ = git_rc("merge-base", "--all", base, branch, cwd=repo.root)
    forks = out.split()
    if rc or not forks:
        raise WtError(f"{branch} and {base} share no history")
    if len(forks) > 1:
        raise WtError(f"{branch} and {base} have more than one merge base (criss-cross history), which "
                      "/wt:land cannot rebase safely; land it by hand")
    fork = forks[0]
    if how != "given with --base":
        alts = stacked_on(repo, branch, base, fork)
        if alts:
            raise WtError(f"{branch} also contains commits of {', '.join(alts)}, so it may be stacked on one of "
                          f"them rather than on {base}; choose with `wt.py adopt --base <branch>`",
                          candidates=[base, *alts])
    if wt_path is None:
        wt_path = next((Path(w["path"]) for w in repo.worktrees() if w.get("branch") == branch), None)
    repo.set_cfg(f"branch.{branch}.wtBase", base)
    repo.set_cfg(f"branch.{branch}.wtBaseSha", fork)
    repo.set_cfg(f"branch.{branch}.wtSession",
                 os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "")
    repo.set_cfg(f"branch.{branch}.wtPath", str(wt_path or ""))
    m = wt_meta(repo, branch)
    m["adopted"] = {"base": base, "how": how, "fork": fork,
                    "ahead": int(git("rev-list", "--count", f"{fork}..{branch}", cwd=repo.root).strip())}
    return m


def meta_or_adopt(repo: Repo, branch: str) -> dict:
    if repo.cfg(f"branch.{branch}.wtBase"):
        return wt_meta(repo, branch)
    return adopt_branch(repo, branch)


def resolve_branch(repo: Repo, name: str) -> str:
    """`wt-<name>` first (what `start` makes), then the branch named exactly `name`, then `worktree-<name>`
    (what the built-in EnterWorktree makes)."""
    raw, tried = name.strip(), []
    for cand in (BRANCH_PREFIX + sanitize(raw), raw, "worktree-" + raw.removeprefix("worktree-")):
        if cand not in tried:
            tried.append(cand)
            if repo.ref_exists(f"refs/heads/{cand}"):
                return cand
    raise WtError(f"no branch found for {name!r} (tried {', '.join(tried)})")


def current_wt(repo: Repo) -> dict:
    br = repo.branch()
    if br is None:  # detached during an in-progress rebase: recover the branch being rebased
        for d in ("rebase-merge", "rebase-apply"):
            hn = repo.gitdir / d / "head-name"
            if hn.exists():
                br = hn.read_text().strip().removeprefix("refs/heads/")
    if not br:
        raise WtError("HEAD is detached; run this inside a worktree that is on a branch")
    if repo.cfg(f"branch.{br}.wtBase"):
        return wt_meta(repo, br)
    if repo.root == repo.main_root:
        raise WtError(f"not inside a linked worktree (on {br}); run this inside the worktree")
    return adopt_branch(repo, br, repo.root)


def cmd_adopt(args):
    repo = Repo()
    if args.name:
        branch = resolve_branch(repo, args.name)
        wt = next((Path(w["path"]) for w in repo.worktrees() if w.get("branch") == branch), None)
    else:
        branch, wt = repo.branch(), repo.root
        if not branch or repo.root == repo.main_root:
            raise WtError("run `adopt` inside a linked worktree that is on a branch, or pass --name")
    emit({"ok": True, **adopt_branch(repo, branch, wt, args.base)})


def base_checkout(repo: Repo, base: str) -> str | None:
    for w in repo.worktrees():
        if w.get("branch") == base:
            return w["path"]
    return None


def cmd_status(args):
    repo = Repo()
    m = current_wt(repo)
    base, branch = m["base"], m["branch"]
    if not repo.ref_exists(f"refs/heads/{base}"):
        emit({**m, "base_exists": False, "error": f"base branch {base} no longer exists"}, EXIT_ERR)
    base_now = repo.sha(base)
    dirty = [it["path"] for it in repo.status()]
    ahead = git("rev-list", "--count", f"{base}..HEAD", cwd=repo.root).strip()
    behind = git("rev-list", "--count", f"HEAD..{base}", cwd=repo.root).strip()
    rebased = git_rc("merge-base", "--is-ancestor", base, "HEAD", cwd=repo.root)[0] == 0
    emit({
        **m, "base_exists": True, "base_now": base_now, "base_moved": base_now != m["base_sha"],
        "ahead": int(ahead), "behind": int(behind), "rebased_onto_base": rebased,
        "dirty": dirty, "rebase_in_progress": repo.rebase_in_progress(),
        "base_checked_out_at": base_checkout(repo, base),
        "commits": git("log", "--oneline", f"{base}..HEAD", cwd=repo.root).splitlines(),
        "worktree": str(repo.root),
    })


# ─── rebase / continue ──────────────────────────────────────────────────────

def conflict_report(repo: Repo, m: dict) -> dict:
    cfg = repo.config()
    base, branch, fork = m["base"], m["branch"], m["base_sha"]
    files = []
    for it in repo.status():
        xy = it["xy"]
        if "U" not in xy and xy not in ("AA", "DD"):
            continue
        p = it["path"]
        kind = kind_of(p, cfg) or {
            "UU": "content", "AA": "both-added", "DD": "both-deleted",
            "DU": "deleted-by-us(base)", "UD": "deleted-by-them(worktree)",
            "AU": "added-by-us(base)", "UA": "added-by-them(worktree)",
        }.get(xy, "content")
        base_log = git("log", "--oneline", f"{fork}..{base}", "--", p, cwd=repo.root).splitlines()
        base_diff = git("diff", fork, base, "--", p, cwd=repo.root)
        ours_log = git("log", "--oneline", f"{fork}..{branch}", "--", p, cwd=repo.root).splitlines()
        ours_diff = git("diff", fork, branch, "--", p, cwd=repo.root)
        files.append({
            "path": p, "xy": xy, "kind": kind,
            "base_commits": base_log, "base_diff": base_diff[:DIFF_LIMIT],
            "worktree_commits": ours_log, "worktree_diff": ours_diff[:DIFF_LIMIT],
        })
    prog = {}
    rm = repo.gitdir / "rebase-merge"
    if rm.exists():
        for k in ("msgnum", "end"):
            f = rm / k
            if f.exists():
                prog[k] = int(f.read_text().strip() or 0)
    return {
        "state": "conflict", **m, "files": files, "progress": prog,
        "note": ("During rebase the roles are swapped: in conflict markers, `ours`/HEAD is the BASE "
                 "branch and `theirs` is the WORKTREE commit being replayed. Keep the intent of both."),
        "check": cfg.get("check"),
        "next": "resolve every file, run the check, then `wt.py continue`; never `git rebase --abort` on your own",
    }


def finish_rebase_ok(repo: Repo, m: dict) -> dict:
    base_now = repo.sha(m["base"])
    repo.set_cfg(f"branch.{m['branch']}.wtBaseSha", base_now)
    return {"state": "rebased", **m, "base_sha": base_now,
            "commits": git("log", "--oneline", f"{m['base']}..HEAD", cwd=repo.root).splitlines(),
            "next": "`wt.py verify` (if check/test configured), then ExitWorktree and `wt.py finish --name <name>`"}


def do_rebase(repo: Repo, m: dict, onto: str | None = None) -> tuple[dict, int]:
    """Rebase the worktree's branch onto its base. Returns (report, exit code); raises WtError."""
    if repo.rebase_in_progress():
        return conflict_report(repo, m), EXIT_CONFLICT
    if repo.status():
        raise WtError("worktree is dirty; commit (or stash) before rebasing",
                      dirty=[it["path"] for it in repo.status()])
    if not repo.ref_exists(f"refs/heads/{m['base']}"):
        raise WtError(f"base branch {m['base']} no longer exists; use `wt.py rebase --onto <branch>`")
    if onto:
        m["base"] = onto
        repo.set_cfg(f"branch.{m['branch']}.wtBase", onto)
    base_now = repo.sha(m["base"])
    if git_rc("merge-base", "--is-ancestor", base_now, "HEAD", cwd=repo.root)[0] == 0:
        return {**finish_rebase_ok(repo, m), "noop": True}, EXIT_OK
    rc, out, err = git_rc("rebase", "--onto", m["base"], m["base_sha"], m["branch"], cwd=repo.root)
    if rc == 0:
        return finish_rebase_ok(repo, m), EXIT_OK
    if repo.rebase_in_progress():
        return conflict_report(repo, m), EXIT_CONFLICT
    raise WtError(f"rebase failed: {err.strip()}")


def cmd_rebase(args):
    repo = Repo()
    emit(*do_rebase(repo, current_wt(repo), args.onto))


def cmd_continue(args):
    repo = Repo()
    m = current_wt(repo)
    if not repo.rebase_in_progress():
        raise WtError("no rebase in progress; run `wt.py rebase`")
    unresolved = []
    for it in repo.status():
        p = repo.root / it["path"]
        if "U" in it["xy"] or it["xy"] in ("AA", "DD"):
            if p.is_file():
                try:
                    txt = p.read_text(errors="replace")
                except OSError:
                    txt = ""
                if re.search(r"^(<{7}|={7}|>{7})( |$)", txt, re.M):
                    unresolved.append(it["path"])
    if unresolved:
        emit({**conflict_report(repo, m), "unresolved_markers": unresolved,
              "next": "conflict markers still present in the files above"}, EXIT_CONFLICT)
    git("add", "-A", "--", ".", cwd=repo.root)
    rc, out, err = git_rc("rebase", "--continue", cwd=repo.root)
    if rc == 0 and not repo.rebase_in_progress():
        emit(finish_rebase_ok(repo, m))
    if repo.rebase_in_progress():
        emit(conflict_report(repo, m), EXIT_CONFLICT)
    raise WtError(f"rebase --continue failed: {err.strip()}")


# ─── verify ─────────────────────────────────────────────────────────────────

def run_shell(cmd: str, cwd: Path) -> dict:
    p = subprocess.run(cmd, shell=True, cwd=cwd, text=True, capture_output=True)
    tail = (p.stdout + p.stderr)[-4000:]
    return {"cmd": cmd, "rc": p.returncode, "output_tail": tail}


def do_verify(repo: Repo, m: dict) -> tuple[dict, int]:
    """Run .claude/wt.json check then test in the worktree. Returns (report, exit code)."""
    cfg = repo.config()
    results, failed = [], False
    for key in ("check", "test"):
        if cfg.get(key):
            r = run_shell(cfg[key], repo.root)
            r["step"] = key
            results.append(r)
            if r["rc"]:
                failed = True
                break
    return ({"state": "verify-failed" if failed else "verified", **m, "results": results,
             "configured": bool(results)}, EXIT_VERIFY if failed else EXIT_OK)


def cmd_verify(args):
    repo = Repo()
    emit(*do_verify(repo, current_wt(repo)))


def cmd_land(args):
    """Everything in phase A after the commit, in one call: adopt if needed, rebase onto the base, verify.
    A dirty worktree exits 1, conflicts exit 7, a red check/test exits 8; nothing is landed here."""
    repo = Repo()
    m = current_wt(repo)
    report, code = do_rebase(repo, m, args.onto)
    if code:
        emit(report, code)
    verdict, vcode = do_verify(repo, dict(m))
    if vcode:
        emit({**verdict, "rebased": not report.get("noop", False)}, vcode)
    emit({"state": "ready", **m, "base_sha": report["base_sha"], "rebased": not report.get("noop", False),
          "commits": report["commits"], "worktree": str(repo.root),
          "verify": {"configured": verdict["configured"], "results": verdict["results"]},
          "finish": m["branch"].removeprefix(BRANCH_PREFIX),
          "next": "ExitWorktree, then `wt.py finish --name <finish>`"})


# ─── finish ─────────────────────────────────────────────────────────────────

def cleanup(repo: Repo, m: dict, force_branch: bool) -> list[str]:
    steps = []
    name = m["branch"].removeprefix(BRANCH_PREFIX)
    path = m.get("path")
    for w in repo.worktrees():
        if w.get("branch") == m["branch"]:
            path = w["path"]
    if path and Path(path).exists():
        git("worktree", "remove", "--force", path, cwd=repo.main_root)
        steps.append(f"removed worktree {path}")
    git("worktree", "prune", cwd=repo.main_root)
    if repo.ref_exists(f"refs/heads/{m['branch']}"):
        git("branch", "-D" if force_branch else "-d", m["branch"], cwd=repo.main_root)
        steps.append(f"deleted branch {m['branch']}")
    git_rc("config", "--remove-section", f"branch.{m['branch']}", cwd=repo.main_root)
    st = repo.find_stash(STASH_TAG + name)
    if st:
        git("stash", "drop", st, cwd=repo.main_root)
        steps.append(f"dropped leftover stash {st}")
    return steps


@contextlib.contextmanager
def landing_lock(repo: Repo, timeout: float):
    """Serialize `finish` across every session sharing this repository (flock on .git/claude-wt/land.lock)."""
    if fcntl is None:
        yield
        return
    path = repo.common / "claude-wt" / "land.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise WtError(f"another landing has held {path} for {timeout:g}s; retry in a moment")
                time.sleep(0.2)
        yield
    finally:
        fh.close()  # closing the descriptor releases the lock


def worktree_of(repo: Repo, m: dict) -> Path | None:
    """The worktree holding m['branch']. Mid-rebase its HEAD is detached and git lists no branch for it,
    so fall back to the path recorded by `start`."""
    wts = repo.worktrees()
    for w in wts:
        if w.get("branch") == m["branch"]:
            return Path(w["path"])
    rec = m.get("path")
    if rec:
        for w in wts:
            if os.path.realpath(w["path"]) == os.path.realpath(rec):
                return Path(w["path"])
    return None


def conflict_stop(wt_path: Path, report: dict, why: str):
    """Exit 7 from `finish`: the worktree is mid-rebase and needs a session inside it."""
    emit({**report, "landed": False, "worktree": str(wt_path),
          "next": f"{why}; EnterWorktree(path=\"{wt_path}\"), resolve the files above, `wt.py continue`, "
                  "ExitWorktree, then `wt.py finish` again"}, EXIT_CONFLICT)


def rebase_before_landing(m: dict, wt_path: Path) -> dict:
    """The base moved after the worktree's own rebase (another session landed first). Redo the rebase in
    place under the landing lock instead of bouncing the session back into the worktree. Conflicts stop
    with exit 7, a failing check/test with exit 8; nothing is landed in either case."""
    wrepo = Repo(str(wt_path))
    behind = int(git("rev-list", "--count", f"{m['branch']}..{m['base']}", cwd=wrepo.root).strip())
    report, code = do_rebase(wrepo, dict(m))
    if code == EXIT_CONFLICT:
        conflict_stop(wt_path, report, f"{m['base']} moved while landing and the rebase conflicts")
    verdict, vcode = do_verify(wrepo, dict(m))
    if vcode:
        emit({**verdict, "landed": False, "rebased_during_finish": True, "worktree": str(wt_path),
              "next": f"{m['base']} moved while landing and check/test fail on the rebased tree; fix it in the "
                      "worktree, commit, then `wt.py finish` again"}, vcode)
    return {"rebased_during_finish": True, "base_moved_by": behind, "verified": verdict["configured"]}


def push_remote(repo: Repo) -> str | None:
    """`"push": true` in .claude/wt.json means origin; a string names the remote; anything else, no push."""
    v = repo.config().get("push")
    if v is True:
        return "origin"
    return v if isinstance(v, str) and v else None


def push_base(repo: Repo, base: str, remote: str, where: str | None) -> dict:
    """Push the landed base branch. Never forces: a rejected push is reported, and the landing stands."""
    cwd = where or str(repo.root)
    if git_rc("remote", "get-url", remote, cwd=cwd)[0]:
        return {"ok": False, "remote": remote, "error": f"no remote named {remote}"}
    ref = f"refs/heads/{base}"
    try:
        p = subprocess.run(["git", "push", remote, f"{ref}:{ref}"], cwd=cwd, text=True, capture_output=True,
                           timeout=PUSH_TIMEOUT, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except subprocess.TimeoutExpired:
        return {"ok": False, "remote": remote, "error": f"git push did not finish within {PUSH_TIMEOUT}s"}
    if p.returncode:
        return {"ok": False, "remote": remote, "error": (p.stderr or p.stdout).strip()[-1500:]}
    return {"ok": True, "remote": remote, "ref": base, "sha": repo.sha(base)}


def cmd_finish(args):
    repo = Repo()
    branch = resolve_branch(repo, args.name)
    if repo.branch() == branch:
        raise WtError("run `finish` from the main checkout after ExitWorktree, not inside the worktree")
    m = meta_or_adopt(repo, branch)
    base = m["base"]
    if not repo.ref_exists(f"refs/heads/{base}"):
        raise WtError(f"base branch {base} no longer exists; pass --onto <branch> to `wt.py rebase` inside the worktree first")
    with landing_lock(repo, float(repo.config().get("lockTimeoutSec", LOCK_TIMEOUT))):
        finish_locked(repo, args, m, branch)


def finish_locked(repo: Repo, args, m: dict, branch: str):
    base = m["base"]
    wt_path = worktree_of(repo, m)
    if wt_path:
        wrepo = Repo(str(wt_path))
        if wrepo.rebase_in_progress():
            conflict_stop(wt_path, conflict_report(wrepo, m), "a rebase is still in progress in the worktree")
        if wrepo.status():
            raise WtError("worktree has uncommitted changes; commit them first",
                          dirty=[it["path"] for it in wrepo.status()])
    rebased = None
    if git_rc("merge-base", "--is-ancestor", base, branch, cwd=repo.root)[0] != 0:
        if not wt_path:
            raise WtError(f"{branch} is not rebased onto {base} and has no worktree to rebase in; "
                          "check it out in a worktree and run `wt.py rebase` there first")
        rebased = rebase_before_landing(m, wt_path)

    commits = git("log", "--oneline", f"{base}..{branch}", cwd=repo.root).splitlines()
    if not commits:
        steps = ["nothing to land: worktree has no commits beyond base"]
        if not args.keep:
            steps += cleanup(repo, m, force_branch=True)
        emit({"ok": True, "landed": 0, "base": base, "steps": steps})

    where = base_checkout(repo, base)
    steps = []
    if rebased:
        steps.append(f"rebased {branch} onto {base} in place ({rebased['base_moved_by']} new commit(s) since "
                     "its own rebase)")
    target_sha = repo.sha(branch)

    if args.squash:
        if not where:
            raise WtError(f"--squash needs {base} checked out somewhere (it isn't); check it out or land without --squash")
        msg = args.message or ("Land " + branch + "\n\n" + "\n".join(commits))
        rc, out, err = git_rc("merge", "--squash", branch, cwd=where)
        if rc:
            raise WtError(f"squash merge refused: {err.strip()}")
        git("commit", "-m", msg, cwd=where)
        if git("diff", "--stat", base, branch, cwd=where).strip():
            raise WtError("squash result differs from worktree tree; not cleaning up")
        steps.append(f"squash-merged {len(commits)} commit(s) into {base} at {where}")
        force_branch = True
    else:
        if where:
            rc, out, err = git_rc("merge", "--ff-only", branch, cwd=where)
            if rc:
                raise WtError(f"fast-forward of {base} at {where} refused (usually uncommitted changes there "
                              f"overlapping with this landing): {err.strip()}", checkout=where)
            steps.append(f"fast-forwarded {base} at {where}")
        else:
            git("branch", "-f", base, branch, cwd=repo.root)
            steps.append(f"moved ref {base} (not checked out anywhere)")
        force_branch = False

    if git_rc("merge-base", "--is-ancestor", target_sha, base, cwd=repo.root)[0] != 0 and not args.squash:
        raise WtError("verification failed: worktree head is not an ancestor of base after landing; nothing cleaned up")

    remote = None if args.no_push else push_remote(repo)
    push = push_base(repo, base, remote, where) if remote else None
    if not args.keep:
        steps += cleanup(repo, m, force_branch=force_branch)
    out = {"ok": True, "landed": len(commits), "base": base, "base_now": repo.sha(base),
           "commits": commits, "steps": steps, "kept": bool(args.keep), **(rebased or {}),
           **({"adopted": m["adopted"]} if "adopted" in m else {})}
    if push:
        out["push"] = push
    emit(out, EXIT_OK if not push or push["ok"] else EXIT_PUSH)


# ─── abandon / list ─────────────────────────────────────────────────────────

def cmd_abandon(args):
    repo = Repo()
    branch = resolve_branch(repo, args.name)
    if repo.branch() == branch:
        raise WtError("run `abandon` from the main checkout, not inside the worktree")
    m = meta_or_adopt(repo, branch)
    wt_path = worktree_of(repo, m)
    steps, patch_file = [], None
    if not args.discard:
        fork = m["base_sha"]
        if wt_path and wt_path.exists():
            git("add", "-A", cwd=wt_path)
            patch = git("diff", "--binary", fork, "--cached", cwd=wt_path)
        else:
            patch = git("diff", "--binary", fork, branch, cwd=repo.root)
        if patch.strip():
            patch_file = repo.common / "claude-wt" / f"abandon-{sanitize(branch)}.patch"
            patch_file.parent.mkdir(parents=True, exist_ok=True)
            patch_file.write_text(patch)
            rc, out, err = git_rc("apply", "--3way", str(patch_file), cwd=repo.root)
            if rc:
                emit({"ok": False, "error": "could not apply changes back cleanly; worktree left intact",
                      "patch": str(patch_file), "stderr": err.strip()[-2000:]}, EXIT_ERR)
            git("reset", "-q", cwd=repo.root)  # leave changes unstaged, like a normal working tree
            steps.append(f"applied worktree changes back into {repo.root} (patch kept at {patch_file})")
    steps += cleanup(repo, m, force_branch=True)
    emit({"ok": True, "steps": steps, "patch": str(patch_file) if patch_file else None})


def cmd_list(_args):
    repo = Repo()
    out = git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}*", cwd=repo.root)
    wts = {w.get("branch"): w["path"] for w in repo.worktrees()}
    items = []
    for br in out.splitlines():
        try:
            m = wt_meta(repo, br)
        except WtError:
            m = {"branch": br, "base": None}
        m["worktree"] = wts.get(br)
        if m.get("base") and repo.ref_exists(f"refs/heads/{m['base']}"):
            m["ahead"] = int(git("rev-list", "--count", f"{m['base']}..{br}", cwd=repo.root).strip())
            m["behind"] = int(git("rev-list", "--count", f"{br}..{m['base']}", cwd=repo.root).strip())
        items.append(m)
    emit({"worktrees": items, "main": str(repo.main_root)})


# ─── main ───────────────────────────────────────────────────────────────────

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="wt.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("track", help="PostToolUse hook: record files edited by this session (stdin JSON)")
    p = sub.add_parser("plan", help="classify dirty files: ours / theirs / shared / unknown")
    p.add_argument("--session")
    p = sub.add_parser("start", help="create wt-<name> worktree from current branch, taking listed paths")
    p.add_argument("name"); p.add_argument("--take", nargs="*", default=[]); p.add_argument("--session")
    p.add_argument("--allow-nested", action="store_true")
    sub.add_parser("status", help="inside worktree: base, drift, dirty files")
    p = sub.add_parser("rebase", help="inside worktree: rebase onto base (exit 7 on conflict)")
    p.add_argument("--onto", help="override base branch (e.g. when the original was deleted)")
    sub.add_parser("continue", help="inside worktree: after resolving conflicts")
    sub.add_parser("verify", help="inside worktree: run .claude/wt.json check/test")
    p = sub.add_parser("adopt", help="record base/fork metadata for a worktree branch start did not create")
    p.add_argument("--base", help="branch it should land on (default: inferred; refuses when ambiguous)")
    p.add_argument("--name", help="branch name, to adopt from the main checkout instead of inside the worktree")
    p = sub.add_parser("land", help="inside worktree: adopt if needed, rebase onto base, verify (before finish)")
    p.add_argument("--onto", help="override base branch (e.g. when the original was deleted)")
    p = sub.add_parser("finish", help="from main checkout: ff-merge a worktree branch into its base and clean up")
    p.add_argument("--name", required=True); p.add_argument("--squash", action="store_true")
    p.add_argument("--message", "-m"); p.add_argument("--keep", action="store_true")
    p.add_argument("--no-push", action="store_true", help="skip the push configured in .claude/wt.json")
    p = sub.add_parser("abandon", help="from main checkout: bring changes back (or discard) and remove worktree")
    p.add_argument("name"); p.add_argument("--discard", action="store_true")
    sub.add_parser("list", help="list wt-* branches and their metadata")
    args = ap.parse_args(argv)
    try:
        globals()[f"cmd_{args.cmd}"](args)
    except WtError as e:
        emit({"ok": False, "error": str(e), **e.extra}, e.code)


if __name__ == "__main__":
    main()
