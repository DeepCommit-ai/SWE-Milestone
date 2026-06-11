#!/usr/bin/env python3
"""In-container Gitea pacer controller for the harnessed multi-role arm (spec §3, true Gitea route).

Runs INSIDE the EvoClaw /testbed container (launched by HarnessedFramework.build_run_command). It is
EvoClaw's single "agent", but internally drives the spec's multi-role workflow through a deterministic
event-driven pacer (pacer + `claude -p` per role; identical PR-label-state-machine coordination, far
less concurrency risk than 3 self-polling /loops).

Spec-faithful module ownership (§3.1):
  Dev      = Source Code + project CI Pipeline (owns + MAINTAINS ci.sh / .gitea/workflows/ci.yaml).
  Reviewer = admission-audit authority; elevated review on sensitive paths; audits CI maintenance via
             the ci-maintenance-check skill.
  QA       = public Test Suite — builds, runs, and MAINTAINS tests (writes/updates them), commits them.

Hard gate: CI (Dev's ci.sh = lint/build/tests) must be GREEN before Reviewer acts and before merge;
review + qa_passed@sha + CI green → merge → sync Gitea main into /testbed → `git tag agent-impl-<mid>`
(the only seam EvoClaw's grader consumes).

Session strategy is per-role configurable (A=fresh每call / B=持久per-role / C=per-milestone resume),
e.g. dev:B,reviewer:C,qa:C. Coordination bus = Gitea PR labels (vendored labels.py). stdlib only.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gitea_client import GiteaClient, GiteaError  # noqa: E402
from labels import next_state  # noqa: E402

SRS_CAP = 20000
OUT_CAP = 16000
# Give up merging a PR after this many merge_gate attempts across passes. Empty PRs are pre-filtered by
# the ahead==0 check, so by the time this trips the PR is up-to-date and non-empty — the cap only guards
# against a merge API that keeps failing transiently (e.g. Gitea's async mergeability re-check 405s).
MERGE_GIVEUP = 3
# At most one controller may drive the repo (see takeover_singleton).
PIDFILE = "/tmp/harnessed_controller.pid"
RUNTIME = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.expanduser("~/.claude/skills")
CI_SKILL = os.path.join(SKILL_DIR, "ci-maintenance-check", "SKILL.md")

# Toolchain on PATH + writable build caches (the controller subprocess env doesn't reliably carry them
# — go lives at /usr/local/go/bin, and HOME/.cache may be unwritable). Prepended to any CI invocation.
_CI_PATH = ('export PATH="/usr/local/go/bin:/go/bin:$HOME/go/bin:$HOME/.cargo/bin:/usr/local/cargo/bin:/root/.cargo/bin:$PATH"; '
            'export GOCACHE=/tmp/ci-gocache GOFLAGS=-mod=mod CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"; ')
_CI_ENV_LINES = ('export PATH="/usr/local/go/bin:/go/bin:$HOME/go/bin:$HOME/.cargo/bin:/usr/local/cargo/bin:/root/.cargo/bin:$PATH"\n'
                 'export GOCACHE=/tmp/ci-gocache GOFLAGS=-mod=mod CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"\n')
_CI_WORKFLOW_YAML = """\
# Project CI — a Gitea Action that runs the Dev-maintained ci.sh on every PR push.
# Dev owns and MAINTAINS this pipeline (lint + build + tests). Reviewer audits it.
name: ci
on: [push, pull_request]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run project CI
        run: bash ci.sh
"""


def log(msg):
    print(f"[ctl {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def run_proc(cmd, *, timeout, stdin=None, cwd=None):
    """subprocess.run-alike that puts the child in its OWN process group and kills the WHOLE group on
    timeout. Plain subprocess.run(timeout=) SIGKILLs only the direct child — its children (claude's bash
    tools, ci.sh's build/test trees) survive as orphans and keep mutating the shared work clone while the
    controller moves on. Raises subprocess.TimeoutExpired like run()."""
    p = subprocess.Popen(cmd, stdin=(subprocess.DEVNULL if stdin is None else stdin),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=cwd, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # start_new_session=True -> child's pid == its pgid
        except (ProcessLookupError, PermissionError, OSError):
            p.kill()
        p.wait()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out or "", err or "")


def takeover_singleton():
    """Enforce AT MOST ONE live controller per container. The outer harness's timeout kills only its
    host-side `docker exec` client — the in-container controller survives — and its recovery loop then
    execs a SECOND controller into the same container: two pacers racing the same Gitea label machine,
    shared clone, and /testbed tag would corrupt grading. Takeover semantics: the NEW controller is
    authoritative (the harness only relaunches when it considers the old one dead); kill the previous
    instance and its stray role calls, then resume from persisted Gitea state (the design is restart-safe)."""
    try:
        old = int((read_file(PIDFILE).strip() or "0"))
        if old > 0 and old != os.getpid() and os.path.isdir(f"/proc/{old}"):
            cmdline = read_file(f"/proc/{old}/cmdline").replace("\x00", " ")
            if "controller.py" in cmdline:
                log(f"[takeover] killing previous controller pid={old} (outer harness relaunched us)")
                try:
                    os.killpg(old, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(old, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                # Reap the old instance's in-flight role calls / CI runs (each leads its own process
                # group via run_proc, so killing the controller alone would orphan them).
                subprocess.run(["pkill", "-9", "-f", "claude --model"], capture_output=True)
                time.sleep(1)
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:  # never let the guard itself kill the run
        log(f"[takeover] non-fatal: {e}")


def git(cwd, *args, check=False):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def git_commit_all(cwd, message):
    git(cwd, "add", "-A")
    r = subprocess.run(["git", "-C", cwd, "-c", "user.name=harnessed", "-c",
                        "user.email=harnessed@evoclaw", "commit", "-m", message],
                       capture_output=True, text=True)
    return r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr).lower()


def role_prompt(roles_dir, role):
    return read_file(os.path.join(roles_dir, f"{role}.md")).strip()


def parse_verdict(text, positive, negative):
    """Scan for an explicit 'VERDICT:' line. Returns positive / negative / None. The NEGATIVE token is
    checked FIRST so 'VERDICT: not approved' / 'do not pass' read as negative — a quality gate must fail
    closed, and callers treat None (no / garbled verdict, e.g. a timed-out role) as the negative outcome."""
    found = None
    for line in text.splitlines():
        s = line.strip().upper()
        if s.startswith("VERDICT:"):
            if negative in s:
                found = negative
            elif positive in s:
                found = positive
    return found


def run_claude(prompt_text, args, label, cwd, session_id, resume):
    """One claude invocation. resume=True → `--resume <sid>` (continue the role's session); else a fresh
    `--session-id <sid>`. The `session=<sid>` marker lets the log_parser map the transcript to its role."""
    work = "/tmp/ctl_prompts"
    os.makedirs(work, exist_ok=True)
    pf = os.path.join(work, f"{label}.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    cmd = ["claude", "--model", args.model, "--output-format", "json", "--dangerously-skip-permissions"]
    cmd += (["--resume", session_id] if resume else ["--session-id", session_id])
    if args.effort:
        cmd += ["--effort", args.effort]
    log(f"-> claude [{label}] {'resume' if resume else 'session'}={session_id} cwd={cwd} ({len(prompt_text)} chars)")
    t0 = time.time()
    try:
        with open(pf, encoding="utf-8") as fin:
            r = run_proc(cmd, stdin=fin, cwd=cwd, timeout=args.call_timeout)
    except subprocess.TimeoutExpired:
        log(f"<- claude [{label}] TIMEOUT after {args.call_timeout}s (process group killed)")
        return ""
    dt = int(time.time() - t0)
    out = r.stdout
    try:
        data = json.loads(r.stdout)
        if isinstance(data, dict) and "result" in data:
            out = data["result"]
    except (ValueError, TypeError):
        pass
    tag = "ok" if r.returncode == 0 else f"rc={r.returncode}"
    log(f"<- claude [{label}] {tag} in {dt}s ({len(out)} chars); {r.stderr.strip()[:150]}")
    return out or ""


class Sessions:
    """Per-role Claude session strategy (how much memory a role carries between its calls):
      fresh      = a fresh session EVERY call — no memory carried (the agent re-reads/re-explores each time).
      milestone  = one session per (role, milestone) — memory WITHIN a milestone (open→fix→re-review etc.),
                   reset between milestones.
      persistent = one session per role for the WHOLE trial — memory across ALL milestones (the spec /loop
                   ideal: a role that builds up knowledge of the repo; context grows over a long run).
    Configured per role, e.g. {"dev":"persistent","reviewer":"milestone","qa":"milestone"}. The legacy
    single letters A/B/C are accepted as aliases (fresh/persistent/milestone)."""

    _ALIAS = {"a": "fresh", "b": "persistent", "c": "milestone",
              "fresh": "fresh", "persistent": "persistent", "milestone": "milestone"}

    def __init__(self, strategy_map):
        self.strat = {k: self._norm(v) for k, v in strategy_map.items()}
        self.ids = {}        # key -> session_id
        self.started = set()  # session_ids already started (so next call --resumes)

    @staticmethod
    def _norm(v):
        return Sessions._ALIAS.get((v or "milestone").strip().lower(), "milestone")

    def strategy(self, role):
        return self.strat.get(role, "milestone")

    def acquire(self, role, mid):
        """Return (session_id, is_resume) for this role+milestone under its configured strategy."""
        s = self.strategy(role)
        if s == "fresh":
            return str(uuid.uuid4()), False
        key = role if s == "persistent" else f"{role}:{mid}"
        sid = self.ids.get(key)
        if sid is None:
            sid = str(uuid.uuid4())
            self.ids[key] = sid
        resume = sid in self.started
        self.started.add(sid)
        return sid, resume


def parse_session_config(spec):
    """'dev:persistent,reviewer:milestone,qa:milestone' -> {'dev':'persistent',...}. Values are
    normalized (incl. legacy A/B/C aliases) by Sessions._norm at use."""
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if ":" in part:
            role, strat = part.split(":", 1)
            out[role.strip()] = strat.strip()
    return out


class Controller:
    def __init__(self, args):
        self.args = args
        self.gc = GiteaClient(os.environ["GITEA_URL"], os.environ["GITEA_TOKEN"],
                              owner=os.environ.get("GITEA_ORG", "evoclaw"))
        self.repo = self._safe_repo(args.trial)
        self.work = args.work               # shared working clone of the Gitea repo
        self.testbed = args.testbed         # EvoClaw repo — receives merged main + the grading tag
        self.events = os.path.join(args.event_log)
        self.issued = {}                    # mid -> issue number
        self.tagged = set()
        self.abandoned = set()              # mids given up on (empty PR / CI gate never green / unresolved conflict);
                                            # an abandon still TAGS current main (honest miss) so the DAG unblocks
        self.merge_attempts = {}            # mid -> merge_gate attempt count for the CURRENT head (give up after
                                            # MERGE_GIVEUP; reset whenever integration replaces the head)
        self.conflict_bounces = {}          # mid -> resolution attempts for the CURRENT conflict episode
                                            # (give up after max_bounces; reset when an episode resolves)
        self.dev_noop = {}                  # mid -> consecutive dev_fix calls that produced NO new commit
        self.reval_rev = {}                 # mid -> truthful context for the reviewer's next resume prompt
        self.reval_qa = {}                  # mid -> truthful context for QA's next resume prompt
        self._integrated_this_pass = False  # merge queue: at most ONE main-integration per pass
        self.last_tag_ts = 0.0              # when the newest agent-impl tag was created (clean-exit settling)
        # SEPARATE per-role retry budgets (each capped at args.max_bounces) so the review loop doesn't
        # consume QA's budget (which was force-skipping QA).
        self.rev_bounces = {}
        self.qa_bounces = {}
        self.ci_bounces = {}
        self.ci_done = {}                   # head_sha -> "success"/"failure"
        self.ci_out = {}                    # head_sha -> CI log (for Dev to fix red CI)
        self.main_ci_ok = None              # None=unprobed; does untouched main pass the project CI?
        self.sessions = Sessions(parse_session_config(args.session_config))
        self._seen = set()                  # (role, mid) already called once — distinguishes a new-milestone
                                            # resume (strategy B) from a same-milestone iteration in _call.
        os.makedirs(os.path.dirname(self.events), exist_ok=True)

    @staticmethod
    def _safe_repo(trial):
        return re.sub(r"[^A-Za-z0-9_.-]", "-", f"evoh-{trial}")

    def event(self, **kw):
        kw["ts"] = time.time()
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw) + "\n")

    def remote(self):
        return self.gc.git_remote(self.repo)

    # --- setup -------------------------------------------------------
    def setup(self):
        self.gc.ensure_repo(self.repo)
        rem = self.remote()
        git(self.testbed, "remote", "remove", "gitea")
        git(self.testbed, "remote", "add", "gitea", rem)
        # Seed Gitea main from /testbed ONLY on a fresh repo (no PRs ever opened). On a restart the Gitea
        # repo already holds merged milestone work that /testbed may not — force-pushing /testbed over it
        # would DESTROY merged milestones. In that case adopt gitea/main as the source of truth instead.
        existing_prs = bool(self.gc.list_prs(self.repo, state="all"))
        git(self.testbed, "fetch", "gitea")
        if not existing_prs:
            rc, _, err = git(self.testbed, "push", "-f", "gitea", "HEAD:refs/heads/main")
            if rc != 0:
                log(f"[setup] seed /testbed->gitea main WARN: {err[:200]}")
        else:
            log("[setup] existing PRs on Gitea — adopting gitea/main (NOT force-pushing; preserves merged work)")
            git(self.testbed, "reset", "--hard", "gitea/main")
        subprocess.run(["rm", "-rf", self.work])
        r = subprocess.run(["git", "clone", rem, self.work], capture_output=True, text=True)
        if not os.path.isdir(os.path.join(self.work, ".git")):
            raise RuntimeError(f"clone failed: {r.stderr[:300]}")
        # resume-safe: skip milestones already tagged in /testbed from a prior run
        _, out, _ = git(self.testbed, "tag", "-l", "agent-impl-*")
        for t in out.split():
            self.tagged.add(t.replace("agent-impl-", ""))
        # Resume-safe: rebuild abandoned from the persisted 'harness-abandoned' label so a milestone we gave
        # up merging isn't re-opened / re-processed (and idle-spun) after a restart.
        for pr in self.gc.list_prs(self.repo, state="all"):
            if "harness-abandoned" in self._labels(pr):
                fm = self._mid_of_pr(pr)
                if fm:
                    self.abandoned.add(fm)
        for iss in self.gc.list_issues(self.repo, labels=["evoclaw-task"], state="all"):
            m = re.match(r"\[([^\]]+)\]", iss.get("title", ""))
            if m:
                self.issued[m.group(1)] = iss["number"]
        self._install_skills()
        self._seed_ci()
        log(f"[setup] repo={self.repo} cloned; tagged={sorted(self.tagged)} issues={len(self.issued)} "
            f"sessions={self.sessions.strat}")
        self.event(type="setup", repo=self.repo, sessions=self.sessions.strat)

    def _install_skills(self):
        """Install role skills (e.g. ci-maintenance-check) into the agent's ~/.claude/skills so the
        Reviewer can load them. Copied, not committed (lives in HOME, not the repo)."""
        src = os.path.join(RUNTIME, "skills")
        if not os.path.isdir(src):
            return
        os.makedirs(SKILL_DIR, exist_ok=True)
        for name in os.listdir(src):
            d = os.path.join(SKILL_DIR, name)
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(os.path.join(src, name), d)
        log(f"[setup] installed skills: {os.listdir(src)}")

    # --- CI (Dev-owned project pipeline) ----------------------------
    def _default_ci_body(self, tree):
        has = lambda f: os.path.exists(os.path.join(tree, f))  # noqa: E731
        if has("Cargo.toml"):
            return 'echo "[ci] build"; cargo build --workspace\necho "[ci] test"; cargo test --workspace'
        if has("go.mod"):
            return 'echo "[ci] build"; go build ./...\necho "[ci] vet"; go vet ./...\necho "[ci] test"; go test ./...'
        if has("pom.xml"):
            return 'echo "[ci] test"; mvn -q -B test'
        if has("pyproject.toml") or has("setup.py"):
            return 'echo "[ci] compile"; python -m compileall -q .\necho "[ci] test"; python -m pytest -q'
        if has("package.json"):
            return 'npm ci || npm install\nnpm run build --if-present\nnpm test --if-present'
        return 'echo "(no build system detected)"'

    def _seed_ci(self):
        """Seed a baseline project CI (ci.sh + .gitea/workflows/ci.yaml) into main if absent, so Dev has
        a real pipeline to MAINTAIN from milestone 1. Not in a graded source dir → no grading impact."""
        self._wc_checkout("main")
        ci_sh = os.path.join(self.work, "ci.sh")
        if os.path.exists(ci_sh):
            return
        with open(ci_sh, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n# Project CI — owned & MAINTAINED by Dev (lint + build + tests).\n"
                    "set -e\n" + _CI_ENV_LINES + "\n" + self._default_ci_body(self.work) + "\n")
        os.chmod(ci_sh, 0o755)
        wf = os.path.join(self.work, ".gitea", "workflows")
        os.makedirs(wf, exist_ok=True)
        with open(os.path.join(wf, "ci.yaml"), "w", encoding="utf-8") as f:
            f.write(_CI_WORKFLOW_YAML)
        git_commit_all(self.work, "ci: seed project CI pipeline (Dev maintains)")
        git(self.work, "push", "origin", "main")
        log("[setup] seeded ci.sh + .gitea/workflows/ci.yaml into main")

    def _run_project_ci(self, commit_ish):
        """Run the Dev-maintained project CI (ci.sh) on `commit_ish` in an EPHEMERAL git worktree —
        created fresh per run, removed after. Returns (ok, log).

        WHY ISOLATED (navidrome three-arm post-mortem): the shared clone accumulates GITIGNORED build
        artifacts that mtime-based Makefile rules never rebuild — navidrome's plugins/testdata
        plugin.wasm, born green at baseline, masked a wasip1-only interface break through EVERY later
        in-tree CI run (trial green, eval-container red), in both harness arms. Devs may test in their
        own (dirty) tree for speed, but the GATE must build from a state where no artifact predates the
        change under test — standard real-world CI semantics. A fresh worktree guarantees that
        structurally, for any repo, with no per-repo knowledge of what to clean. The shared GOCACHE
        (content-addressed: a hit requires identical inputs, so it can only speed up a true green,
        never fake one) keeps the cold-build cost low.

        A TIMEOUT is a FAILING result, not an exception: letting TimeoutExpired escape to _safe would
        leave ci_done[sha] unset, so the same full-length hang re-runs every pass (and after every
        restart) with no red signal ever reaching dev_fix_ci / the CI budget."""
        git(self.work, "fetch", "origin")
        holder = tempfile.mkdtemp(prefix="ci-gate-")
        wt = os.path.join(holder, "tree")
        try:
            rc, _, err = git(self.work, "worktree", "add", "--detach", wt, commit_ish)
            if rc != 0:
                raise RuntimeError(f"ci worktree add {commit_ish} failed: {err[:200]}")
            ci = os.path.join(wt, "ci.sh")
            body = "bash ci.sh" if os.path.exists(ci) else self._default_ci_body(wt).replace("\n", " && ")
            try:
                r = run_proc(["/bin/sh", "-c", _CI_PATH + body], cwd=wt, timeout=self.args.call_timeout)
            except subprocess.TimeoutExpired:
                return False, (f"(CI TIMED OUT after {self.args.call_timeout}s — a hanging step is a FAILING "
                               f"gate; find and fix the blocking command, e.g. a foregrounded server or a "
                               f"test waiting on stdin/network)")
            return r.returncode == 0, (r.stdout + r.stderr)[-OUT_CAP:]
        finally:
            # Best-effort teardown, robust to dirty/broken worktrees: `remove --force` handles the
            # normal case; rmtree catches what it refuses (e.g. submodule leftovers); prune clears the
            # admin record if the directory was nuked out from under git.
            git(self.work, "worktree", "remove", "--force", wt)
            shutil.rmtree(holder, ignore_errors=True)
            git(self.work, "worktree", "prune")

    def _probe_main_ci(self):
        """DIAGNOSTIC: does untouched main pass the project CI in THIS env? Recorded so Dev knows whether
        it must also fix a broken base build (e.g. a missing system lib) to make CI a valid passable gate.
        This NO LONGER weakens the gate — CI is always enforced on the real ci.sh result."""
        if self.main_ci_ok is not None:
            return
        ok, _out = self._run_project_ci("origin/main")
        self.main_ci_ok = ok
        log(f"[ci] baseline probe: untouched main CI {'PASS' if ok else 'FAIL (Dev must make the gate valid)'}")
        self.event(type="ci_baseline", main_ci_ok=ok)

    # --- milestone inputs -------------------------------------------
    def available_milestones(self):
        text = read_file(os.path.join(self.args.workspace, "TASK_QUEUE.md"))
        mids = [m.group(1) for m in (re.match(r"^- (\S+):", ln.strip()) for ln in text.splitlines()) if m]
        return [m for m in mids if os.path.exists(os.path.join(self.args.workspace, "srs", f"{m}_SRS.md"))], \
               ("No tasks currently available" in text)

    def srs_of(self, mid):
        return read_file(os.path.join(self.args.workspace, "srs", f"{mid}_SRS.md"))[:SRS_CAP]

    def srs_path(self, mid):
        """Container path where the SRS lives — accessible to every role agent. We reference it instead
        of pasting the SRS into prompts (especially on resume, the session has already read it)."""
        return f"{self.args.workspace}/srs/{mid}_SRS.md"

    def sync_issues(self):
        mids, _ = self.available_milestones()
        for mid in mids:
            if mid in self.issued or mid in self.tagged:
                continue
            num = self.gc.create_issue(self.repo, title=f"[{mid}] EvoClaw milestone",
                                       body=f"milestone_id: {mid}\n\n## SRS\n{self.srs_of(mid)}",
                                       labels=["evoclaw-task"])
            self.issued[mid] = num
            log(f"[bootstrap] issue #{num} for {mid}")
            self.event(type="issue_created", milestone=mid, issue=num)

    # --- gitea state helpers ----------------------------------------
    def _labels(self, item):
        return [l["name"] for l in (item.get("labels") or [])]

    def _mid_of_pr(self, pr):
        for ln in (pr.get("body") or "").splitlines():
            if ln.strip().startswith("milestone_id:"):
                return ln.split(":", 1)[1].strip()
        return None

    def _pr_state(self, pr):
        for s in ("needs-code-changes:R", "needs-code-changes:Q", "needs-review", "needs-qa", "ready-to-merge"):
            if s in self._labels(pr):
                return s
        return None

    def _resolved_mid(self, pr):
        """True iff this PR's milestone is already tagged/abandoned — role handlers must not spend real
        calls on it (possible after a partial abandon: tag persisted but the state-label removal lost)."""
        mid = self._mid_of_pr(pr)
        return mid in self.tagged or mid in self.abandoned

    def _head_sha(self, pr):
        return ((pr.get("head") or {}).get("sha")) or ""

    def _relabel(self, pr, old, new):
        # Add the NEW state before removing the old: if the process dies / Gitea errors between the two
        # calls, the PR keeps a state label (still actionable) instead of being orphaned with none (which
        # _pr_state reads as None -> only the no-op observer touches it -> it never advances or merges).
        self.gc.add_labels(self.repo, pr["number"], [new])
        self.gc.remove_labels(self.repo, pr["number"], [old])

    def _wc_checkout(self, ref):
        git(self.work, "fetch", "origin")
        # Hard-clean the SHARED working clone before switching branches. A prior role in the same pass
        # (especially Reviewer, which never commits) can leave tracked edits / untracked files that make
        # `checkout -B` ABORT — silently stranding the tree on the wrong branch so the next role acts on
        # the wrong milestone's code. Reset + clean guarantees a pristine checkout; a hard failure raises
        # (caught by _safe) so the action retries instead of running on the wrong tree.
        git(self.work, "reset", "--hard")
        git(self.work, "clean", "-fd")
        rc, _, err = git(self.work, "checkout", "-B", ref, f"origin/{ref}")
        if rc != 0:
            raise RuntimeError(f"checkout {ref} failed in shared clone: {err[:200]}")

    def _push(self, branch):
        """Force-push the shared clone's branch to Gitea, CHECKED. A silently-failed push would leave the
        remote head stale, so Reviewer/QA/merge act on the OLD code while the label says 'fixed'. Raising
        (caught by _safe) retries the action instead."""
        rc, _, err = git(self.work, "push", "-f", "origin", branch)
        if rc != 0:
            raise RuntimeError(f"push {branch} -> origin failed: {err[:160]}")

    def _call(self, role, mid, fresh_task, label_suffix, resume_task=None):
        """Run the role's claude with the right prompt for one of THREE situations:
          1. fresh session              → full role.md system prompt + fresh_task;
          2. resumed session, but the FIRST task for THIS milestone (strategy B carried the session over
             from a prior milestone) → "(continuing — new milestone)" + fresh_task (new assignment, no
             role.md re-sent);
          3. resumed session, an ITERATION on the same milestone (re-review / re-fix / re-test) → a lean
             "(continuing)" + resume_task delta (which PR/milestone, what happened; SRS by path, not pasted).
        Distinguishing (2) from (3) is essential: without it, B's first review of milestone N+1 would wrongly
        get the 're-review, you requested changes' delta for a PR the role never saw."""
        sid, resume = self.sessions.acquire(role, mid)
        first_for_mid = (role, mid) not in self._seen
        self._seen.add((role, mid))
        if not resume:
            prompt = f"{role_prompt(self.args.roles_dir, role)}\n\n{fresh_task}"
        elif first_for_mid:
            prompt = (f"(Continuing as {role} — now on a NEW milestone; your working tree has been reset to it.)"
                      f"\n\n{fresh_task}")
        else:
            prompt = f"(Continuing your work as {role}.)\n\n{resume_task or fresh_task}"
        return run_claude(prompt, self.args, f"{role}-{label_suffix}", self.work, sid, resume)

    def _wip_note(self, mid):
        """WIP-policy text injected into the Dev task when --wip-limit is set, so the model KNOWS the
        team's WIP cap and where to stop. Injected via the task (not roles/dev.md) on purpose: it rides
        into ALL THREE _call prompt variants — fresh, resumed-new-milestone (the persistent-Dev resume
        path), and resumed-iteration (fallback) — and stays out of WIP-unlimited trials entirely."""
        if not self.args.wip_limit:
            return ""
        return (f"\n\nWIP POLICY: this team works on at most {self.args.wip_limit} milestone(s) at a time — "
                f"right now that is THIS one. Implement ONLY milestone {mid}: do NOT implement or "
                f"pre-implement any other milestone's requirement, even if you remember one from earlier "
                f"work — every future milestone gets its own PR later, cut from the then-current merged "
                f"main. When this milestone's implementation is complete and ready to commit, STOP and "
                f"end your turn.")

    # --- role actions -----------------------------------------------
    def dev_open(self, mid, issue_num):
        branch = f"task-{re.sub(r'[^A-Za-z0-9_.-]', '-', mid)}"
        existing = next((p for p in self.gc.list_prs(self.repo, state="all")
                         if (p.get("head") or {}).get("ref") == branch), None)
        if existing:
            self.gc.add_labels(self.repo, issue_num, ["has-pr"])
            log(f"[dev] PR for {mid} already exists (#{existing['number']}) — skip open")
            return
        self._wc_checkout("main")
        git(self.work, "checkout", "-B", branch, "main")
        task = (f"## Milestone to implement: {mid}\nRead the full requirement (SRS) at `{self.srs_path(mid)}`.\n\n"
                f"Implement it in the current repo ({self.work}). As CI owner, keep the project CI building + "
                f"passing after your change, extending it if new coverage is needed. Build/run the relevant "
                f"tests locally. Leave changes ready to commit. Do NOT git tag, branch, or open a PR."
                f"{self._wip_note(mid)}")
        self._call("dev", mid, task, f"open-{mid}")
        git_commit_all(self.work, f"{mid}: implement")
        _, ahead, _ = git(self.work, "rev-list", "--count", "origin/main..HEAD")
        if ahead.strip() in ("", "0"):
            # Dev produced NO diff vs main (empty implementation — seen with persistent Dev sessions on the
            # DAG tail). The PR will be unmergeable; flag it loudly. The merge gate later abandons it
            # honestly (tagging current MAIN as an explicit miss) instead of pretending it merged.
            log(f"[dev] WARNING: {mid} branch has NO changes vs origin/main — Dev produced an EMPTY implementation")
            self.event(type="empty_impl", milestone=mid)
        self._push(branch)
        try:
            pr = self.gc.create_pr(self.repo, head=branch, base="main", title=f"Implement {mid}",
                                   body=f"milestone_id: {mid}", labels=["needs-review"])
        except GiteaError as e:
            if "409" in str(e):
                self.gc.add_labels(self.repo, issue_num, ["has-pr"])
                log(f"[dev] PR for {mid} already existed (409) — marked has-pr")
                return
            raise
        self.gc.add_labels(self.repo, issue_num, ["has-pr"])
        log(f"[dev] opened PR #{pr} for {mid} (needs-review)")
        self.event(type="pr_opened", milestone=mid, pr=pr)

    def dev_fix(self, pr, state):
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref") or f"task-{mid}"
        self._wc_checkout(branch)
        old_sha = self._head_sha(pr)
        comments = self.gc.comments(self.repo, pr["number"]) or []
        feedback = comments[-1]["body"][:OUT_CAP] if comments else "(see review)"
        noop = self.dev_noop.get(mid, 0)
        escalate = ("" if not noop else
                    f"\n\nNOTE: your previous {noop} attempt(s) at this produced NO new commit, so the PR has "
                    f"not moved. You MUST leave a concrete change ready to commit — code/tests addressing the "
                    f"feedback, or (if you are convinced no change is needed) a code comment at the disputed "
                    f"spot explaining why. An empty turn is not acceptable.")
        fresh = (f"## Your PR for milestone {mid} was sent back. Requirement (SRS): `{self.srs_path(mid)}`.\n"
                 f"The team requested these changes:\n\n{feedback}\n\nAddress them in the current repo "
                 f"({self.work}); keep the project CI green. Leave changes ready to commit. No git tag / PR."
                 f"{escalate}")
        resume = (f"## Your PR for milestone {mid} was sent back with requested changes:\n\n{feedback}\n\n"
                  f"Address them in the current repo ({self.work}); keep CI green. No git tag / PR. "
                  f"(SRS unchanged at `{self.srs_path(mid)}` if you need it.){escalate}")
        self._call("dev", mid, fresh, f"fix-{mid}-{self.rev_bounces.get(mid,0)+self.qa_bounces.get(mid,0)}",
                   resume_task=resume)
        git_commit_all(self.work, f"{mid}: address feedback")
        self._push(branch)
        # ZERO-PROGRESS GUARD: a dev_fix that produced no new commit must not bounce the label forward —
        # the reviewer would re-judge an IDENTICAL diff under a "Dev pushed a new commit" premise, burning
        # a review bounce per no-op (observed live: a 10x no-op ping-pong storm ending in force-approve).
        _, new_sha, _ = git(self.work, "rev-parse", "HEAD")
        if new_sha == old_sha:
            self.dev_noop[mid] = noop + 1
            if self.dev_noop[mid] < 3:
                log(f"[dev] fix produced NO new commit (noop {self.dev_noop[mid]}/3) — keeping {state}, re-prompting next pass")
                self.event(type="dev_fix_noop", milestone=mid, pr=pr["number"], frm=state, count=self.dev_noop[mid])
                return
            # 3 no-ops: stop spinning Dev; advance honestly so the team judges the UNCHANGED head as such.
            self.gc.comment(self.repo, pr["number"],
                            f"harness: Dev made NO further changes after {self.dev_noop[mid]} fix attempts — "
                            f"advancing for an honest re-judgement of the unchanged head {old_sha[:12]}")
        self.dev_noop.pop(mid, None)
        self._relabel(pr, state, next_state(state, actor="dev", verdict="fixed"))
        log(f"[dev] fixed PR #{pr['number']} ({state} -> next)")
        self.event(type="dev_fix", milestone=mid, pr=pr["number"], frm=state)

    def dev_fix_ci(self, pr):
        """Spec §3.1: project CI red → Dev fixes it (without bothering Reviewer/QA)."""
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        build_log = self.ci_out.get(self._head_sha(pr), "(ci failed)")
        base_note = ("" if self.main_ci_ok else
                     "\n\nIMPORTANT: even the UNTOUCHED base does not build / pass CI in this environment "
                     "(e.g. a missing system or build dependency). As CI owner you must still make CI a REAL, "
                     "PASSABLE hard gate: install the missing build dependency if you can, or evolve ci.sh to "
                     "build/test what it can and gate on regressions RELATIVE to that baseline — your call, but "
                     "the gate must genuinely validate this change and never be faked green.")
        sync_hint = ("\n\nTip: if `origin/main` has advanced since this branch was cut (e.g. a CI/build fix "
                     "already landed there), you may `git merge origin/main` into this branch first and build "
                     "on it instead of re-deriving the fix.")
        fresh = (f"## The project CI on your PR for milestone {mid} is RED. As CI owner, fix the REAL cause "
                 f"in the current repo ({self.work}) so build + tests pass — do NOT weaken CI to hide it. "
                 f"CI output:\n\n{build_log}{base_note}{sync_hint}\n\nLeave changes ready to commit. No git tag / PR.")
        resume = (f"## Your PR's CI for milestone {mid} is now RED. Fix the real cause (do not weaken CI). "
                  f"CI output:\n\n{build_log}{base_note}{sync_hint}\n\nLeave changes ready to commit. No git tag / PR.")
        self._call("dev", mid, fresh, f"ci-{mid}-{self.ci_bounces.get(mid,0)}", resume_task=resume)
        git_commit_all(self.work, f"{mid}: fix CI")
        self._push(branch)
        self.ci_bounces[mid] = self.ci_bounces.get(mid, 0) + 1
        log(f"[dev] fixed red CI on PR #{pr['number']} (ci_bounce {self.ci_bounces[mid]}/{self.args.max_bounces})")
        self.event(type="dev_fix_ci", milestone=mid, pr=pr["number"])

    def run_ci(self, pr):
        sha = self._head_sha(pr)
        if not sha or self.ci_done.get(sha):
            return
        self._probe_main_ci()  # DIAGNOSTIC only (does the untouched base build in this env?) — never a gate weaken
        # CI runs on the EXACT head sha in its own ephemeral worktree (never the shared clone): the
        # status we set certifies this sha, so checking out `origin/<branch>` (which may have advanced)
        # would certify the wrong code; and the shared tree's leftover build artifacts must not vouch.
        ok, out = self._run_project_ci(sha)
        # CI is a REAL hard gate: the status is the ACTUAL ci.sh result. The framework NEVER force-greens a
        # failing PR (not even when the untouched base fails to build) — making CI a valid, passable gate is
        # the Dev's job (spec §3.1: Dev owns + maintains the pipeline; install the build deps, or write a
        # baseline-adaptive ci.sh, whatever the scenario needs). The framework only ENFORCES green-before-merge.
        state = "success" if ok else "failure"
        self.gc.set_commit_status(self.repo, sha, state=state, context="ci/build", description="ci.sh (build+test)")
        self.ci_done[sha] = state
        self.ci_out[sha] = out
        log(f"[ci] PR #{pr['number']} head={sha[:8]} ci_ok={ok} -> {state}"
            f"{'' if self.main_ci_ok else ' (NB: untouched base ALSO fails to build here — Dev must make the gate valid)'}")
        self.event(type="ci_run", pr=pr["number"], result=state, ci_ok=ok, sha=sha[:12], base_builds=self.main_ci_ok)

    def reviewer(self, pr):
        mid = self._mid_of_pr(pr)
        self._wc_checkout((pr.get("head") or {}).get("ref"))
        forced = self.rev_bounces.get(mid, 0) >= self.args.max_bounces
        if forced:
            verdict = "approve"
            # Terminal safety valve, made VISIBLE in the artifact: this is pacing, not a real approval.
            self.gc.comment(self.repo, pr["number"],
                            "harness: review budget exhausted — FORCE-APPROVED without a review call "
                            "(terminal safety valve, not a real approval)")
            log(f"[reviewer] PR #{pr['number']} budget exhausted -> force approve")
        else:
            fresh = (f"## PR under review: milestone {mid}\nThe PR branch is checked out in your working "
                     f"directory ({self.work}); base is `origin/main`. Read the requirement (SRS) at "
                     f"`{self.srs_path(mid)}`, then review the actual change (`git diff origin/main...HEAD`).\n"
                     f"ALSO audit the Dev's CI maintenance using the ci-maintenance-check skill (read it at "
                     f"`{CI_SKILL}`). Then give your verdict.")
            # Resume prompts must state TRUE premises. The old text asserted "addressing the changes you
            # requested" — false after a merge-conflict resolution (the reviewer had APPROVED), steering
            # the re-review the merge queue depends on into a rubber-stamp.
            note = self.reval_rev.pop(mid, None)
            if note:
                resume = (f"## Re-review: milestone {mid}\nSince your last verdict, {note}. This is NOT a "
                          f"response to review feedback — re-review the FULL current diff "
                          f"(`git diff origin/main...HEAD`) and judge whether the merge preserved BOTH the "
                          f"previously-approved change AND the incoming main-side changes (nothing dropped on "
                          f"either side). Re-check CI maintenance. (SRS at `{self.srs_path(mid)}`.) Give your verdict.")
            else:
                resume = (f"## Re-review: milestone {mid}\nThe PR has NEW commits since your last review of it. "
                          f"If you previously requested changes, check whether they are addressed; in any case "
                          f"re-examine the full current diff (`git diff origin/main...HEAD`) and re-check CI "
                          f"maintenance. (SRS unchanged at `{self.srs_path(mid)}`.) Give your verdict.")
            out = self._call("reviewer", mid, fresh, f"{mid}-{self.rev_bounces.get(mid,0)}", resume_task=resume)
            self.gc.comment(self.repo, pr["number"], out[:OUT_CAP] or "(no output)")
            verdict = "approve" if parse_verdict(out, "APPROVE", "REQUEST_CHANGES") == "APPROVE" else "request-changes"
        if verdict == "request-changes":
            self.rev_bounces[mid] = self.rev_bounces.get(mid, 0) + 1
        self._relabel(pr, "needs-review", next_state("needs-review", actor="reviewer", verdict=verdict))
        log(f"[reviewer] PR #{pr['number']} -> {verdict} (rev_bounce {self.rev_bounces.get(mid,0)}/{self.args.max_bounces})")
        # forced=True marks the budget-exhaustion safety valve (no real review call) — without it the
        # event stream can't distinguish a real approval from a force (only the PR comment / sub-second
        # timing gave it away during analysis).
        self.event(type="review_verdict", milestone=mid, pr=pr["number"], verdict=verdict, forced=forced)

    def qa(self, pr):
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        forced = self.qa_bounces.get(mid, 0) >= self.args.max_bounces
        if forced:
            verdict, out = "pass", ""
            self.gc.comment(self.repo, pr["number"],
                            "harness: QA budget exhausted — FORCE-PASSED without a verification call "
                            "(terminal safety valve, not a real pass)")
            log(f"[qa] PR #{pr['number']} budget exhausted -> force pass")
        else:
            fresh = (f"## PR under test: milestone {mid}\nThe PR branch is checked out in your working "
                     f"directory ({self.work}); base is `origin/main`. Requirement (SRS): `{self.srs_path(mid)}`.\n"
                     f"As Test-Suite owner: build the project and run its tests, AND write/strengthen tests in "
                     f"the suite that deeply exercise this milestone's required behavior (mirror the codebase's "
                     f"conventions / verify cross-type interface symmetry). Tests you add are committed. Give "
                     f"your verdict from real execution.")
            # Truthful resume premise (the old text asserted "a fix for the bug you found" — false after a
            # merge-conflict resolution, where QA had PASSED).
            qnote = self.reval_qa.pop(mid, None)
            if qnote:
                resume = (f"## Re-test: milestone {mid}\nSince your PASS, {qnote}. Re-verify the milestone ON "
                          f"THE MERGED RESULT: build and run the full test suite (including tests you added), "
                          f"and confirm the milestone's required behavior still holds with main's incoming "
                          f"changes merged in. (SRS at `{self.srs_path(mid)}`.) Give your verdict from real execution.")
            else:
                resume = (f"## Re-test: milestone {mid}\nThe PR has NEW commits since your last QA round. "
                          f"Re-verify by building + running the tests (including the ones you added). (SRS "
                          f"unchanged at `{self.srs_path(mid)}`.) Give your verdict from real execution.")
            out = self._call("qa", mid, fresh, f"{mid}-{self.qa_bounces.get(mid,0)}", resume_task=resume)
            verdict = "pass" if parse_verdict(out, "PASS", "FAIL") == "PASS" else "bug"
            self.gc.comment(self.repo, pr["number"], out[:OUT_CAP] or "(no output)")
        # QA maintains the Test Suite: commit + push any tests it wrote/updated (head sha will change;
        # CI re-runs on the new head next pass before merge).
        git_commit_all(self.work, f"{mid}: QA tests")
        self._push(branch)
        pr = self.gc.get_pr(self.repo, pr["number"])  # refresh head sha after QA's commit
        sha = self._head_sha(pr)
        if verdict == "pass":
            self.gc.comment(self.repo, pr["number"], f"qa_passed@{sha[:12]}")
            self._relabel(pr, "needs-qa", "ready-to-merge")
        else:
            self.qa_bounces[mid] = self.qa_bounces.get(mid, 0) + 1
            self._relabel(pr, "needs-qa", "needs-code-changes:Q")
        log(f"[qa] PR #{pr['number']} -> {verdict} (qa_bounce {self.qa_bounces.get(mid,0)}/{self.args.max_bounces})")
        self.event(type="qa_verdict", milestone=mid, pr=pr["number"], verdict=verdict, forced=forced)

    def _qa_certified(self, pr, sha):
        """True iff a qa_passed comment certifies exactly this head sha."""
        return any(c.get("body", "").strip().startswith(f"qa_passed@{sha[:12]}")
                   for c in (self.gc.comments(self.repo, pr["number"]) or []))

    def _ready(self, pr):
        sha = self._head_sha(pr)
        mid = self._mid_of_pr(pr)
        qa_ok = (self.qa_bounces.get(mid, 0) >= self.args.max_bounces) or self._qa_certified(pr, sha)
        # CI must be green on the ACTUAL current head sha — never a milestone-level budget bypass, which
        # would let a NEW (e.g. QA-committed) head merge without CI ever running on it.
        ci_ok = self.ci_done.get(sha) == "success"
        return qa_ok and ci_ok

    def _rev_count(self, range_expr):
        """Number of commits in `range_expr` (e.g. 'origin/main..origin/<branch>') in the work clone.
        RAISES on any git failure (caught by _safe -> the pass retries): an error must NEVER read as 0,
        because ahead==0 triggers irreversible abandonment and behind==0 lets a stale branch skip
        integration — 0 has to be a POSITIVE determination."""
        rc, out, err = git(self.work, "rev-list", "--count", range_expr)
        if rc != 0 or not (out or "").strip().isdigit():
            raise RuntimeError(f"rev-list {range_expr} failed (rc={rc}): {err[:160]}")
        return int(out.strip())

    def _tag_milestone(self, mid, commit=None):
        """Create the grading tag agent-impl-<mid> in /testbed at `commit` (a verified sha, e.g. the PR's
        merge commit) or at fresh gitea/main. The fetch/reset are CHECKED: a silently-failed fetch would
        tag a STALE main — graded against a tree missing the merged work. Idempotent."""
        if mid in self.tagged:
            return
        self.last_tag_ts = time.time()  # clean-exit settling: a fresh tag may still unlock dependents
        git(self.testbed, "fetch", "gitea", "main", check=True)
        target = commit or "gitea/main"
        if commit and git(self.testbed, "rev-parse", "-q", "--verify", f"{commit}^{{commit}}")[0] != 0:
            log(f"[tag] {mid}: requested commit {str(commit)[:12]} not found after fetch — tagging gitea/main instead")
            target = "gitea/main"
        git(self.testbed, "reset", "--hard", target, check=True)
        git(self.testbed, "tag", f"agent-impl-{mid}")
        # rc alone is untrustworthy (128 = exists, fine); verify the tag truly resolves, force-create if not
        # (the grader consumes ONLY this tag — a missing tag is a lost milestone).
        if git(self.testbed, "rev-parse", "-q", "--verify", f"refs/tags/agent-impl-{mid}")[0] != 0:
            git(self.testbed, "tag", "-f", f"agent-impl-{mid}", check=True)
        self.tagged.add(mid)

    def _abandon(self, pr, mid, reason, event_type, **fields):
        """Give up on a milestone — the SINGLE seam for every abandon path (empty PR / CI gate never green /
        unresolved conflict / unmergeable). Crucially it still SUBMITS current main for honest grading:
        the bare arm's failure mode is symmetric (it tags whatever it built, right or wrong), the tag is
        what unblocks dependent milestones in the orchestrator's DAG, and it lets the task queue drain so
        the run can end cleanly. The grader sees a tree WITHOUT this milestone's implementation — an honest
        miss, never a faked pass. Tag-first ordering makes a partial failure self-heal: if the label calls
        die after the tag exists, the milestone is already in `tagged` (skipped everywhere) and setup()
        rebuilds that from the tag on restart."""
        self._tag_milestone(mid)
        self.abandoned.add(mid)
        st = self._pr_state(pr)
        self.gc.add_labels(self.repo, pr["number"], ["harness-abandoned"])
        if st:
            self.gc.remove_labels(self.repo, pr["number"], [st])
        try:
            self.gc.comment(self.repo, pr["number"],
                            f"harness: ABANDONED ({reason}) — current main tagged for honest grading "
                            f"(this milestone's implementation is NOT in it)")
        except GiteaError:
            pass  # transparency comment is best-effort
        log(f"[abandon] PR #{pr['number']} ({mid}) {reason} -> abandoned; current main tagged (honest miss)")
        self.event(type=event_type, milestone=mid, pr=pr["number"], reason=reason, **fields)

    def reconcile_merged_untagged(self):
        """A merged PR whose grading tag is missing = a crash/error between merge_pr and the tag. Closed
        PRs are invisible to every open-PR handler, so without this sweep the milestone would be lost
        forever (merged code, no tag, no abandon, frozen dependents). Runs once per pass; idempotent."""
        for pr in self.gc.list_prs(self.repo, state="closed"):
            if not pr.get("merged"):
                continue
            mid = self._mid_of_pr(pr)
            if not mid or mid in self.tagged:
                continue
            log(f"[reconcile] PR #{pr['number']} ({mid}) merged but UNTAGGED — creating the grading tag now")
            self._tag_milestone(mid, pr.get("merge_commit_sha"))
            self.event(type="merged_and_tagged", milestone=mid, pr=pr["number"], reconciled=True)

    def _conflict_resolved(self, conflicted):
        """True iff every originally-conflicted file is now MARKER-free. Stages first (`git add -A`):
        editing alone never clears an unmerged index entry, and Dev isn't required to stage — judging by
        the index (the old `diff --diff-filter=U` check) misread a perfect unstaged resolution as a
        failure and let a staged-with-markers file pass. So we check CONTENT, not the index. Only the
        <<<<<<< / >>>>>>> anchors are tested ('=======' alone is a legitimate line, e.g. RST headings)."""
        git(self.work, "add", "-A")
        for path in conflicted.splitlines():
            path = path.strip()
            if not path:
                continue
            for ln in read_file(os.path.join(self.work, path)).splitlines():
                if ln.startswith("<<<<<<<") or ln.startswith(">>>>>>>"):
                    return False
        return True

    def integrate_main(self, pr):
        """MERGE QUEUE: bring a stale PR branch up to date with main, then re-test the MERGED RESULT before
        it may merge (Graydon Hoare's not-rocket-science rule). The FRAMEWORK does the mechanical merge:
          - clean merge  -> certify + push the merged head and force re-CI on it; QA carries over (a
                            mechanical update needs only re-CI, which re-runs QA's committed tests). If
                            re-CI is red (a SEMANTIC conflict), the red-CI router sends it back to Dev.
          - conflict     -> DEV (the source-code owner) resolves it in place; then full re-review + re-QA +
                            re-CI in a FRESH validation epoch (budgets reset), because a conflict
                            resolution is a substantive change that can drop one side's intent.
        Conflict resolution is Dev's job (authoring), never QA's (verification) — that keeps QA independent."""
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        if not branch:
            raise RuntimeError(f"PR #{pr['number']} has no head ref")
        self._wc_checkout(branch)  # pristine checkout of the PR branch (fetches origin)
        rc, _, _ = git(self.work, "-c", "user.name=harnessed", "-c", "user.email=harnessed@evoclaw",
                       "merge", "--no-edit", "origin/main")
        if rc == 0:
            # Clean integration. Certify the LOCAL merge commit BEFORE pushing: the qa_passed comment is
            # the recovery anchor — pushed-but-uncertified is the one state nothing can route (a permanent
            # ready-to-merge zombie), while certified-but-unpushed just re-integrates next pass (the stray
            # comment matches no head and is harmless).
            _, sha, _ = git(self.work, "rev-parse", "HEAD")
            self.ci_done.pop(sha, None)            # re-CI the merged head: test the MERGE RESULT
            self.merge_attempts.pop(mid, None)     # new head -> fresh merge-retry budget
            self.gc.comment(self.repo, pr["number"], f"qa_passed@{sha[:12]} (carried over clean main-integration)")
            self._push(branch)
            log(f"[merge-queue] PR #{pr['number']} ({mid}) cleanly integrated main -> re-CI merged head {sha[:8]}")
            self.event(type="integrated_main", milestone=mid, pr=pr["number"], conflict=False)
            return
        conflicted = (git(self.work, "diff", "--name-only", "--diff-filter=U")[1] or "").strip()
        if not conflicted:
            # merge failed WITHOUT leaving a conflicted index (lock contention, bad ref, ...) — an
            # environment error, not a conflict. Never hand Dev a non-mid-merge tree; retry next pass.
            git(self.work, "merge", "--abort")
            raise RuntimeError("merge origin/main failed with no conflicted paths (transient git error?)")
        # Textual conflict: hand it to Dev to resolve in the mid-merge work tree (markers present).
        n = self.conflict_bounces.get(mid, 0) + 1
        self.conflict_bounces[mid] = n
        task = (f"## Merge conflict — milestone {mid}\n`main` advanced while your PR was open and now conflicts "
                f"with your branch. The repo at {self.work} is mid-merge; these files contain conflict markers "
                f"(<<<<<<< ======= >>>>>>>):\n\n{conflicted}\n\nEdit the files to resolve EVERY conflict so BOTH "
                f"your change and the incoming changes from main are correctly preserved — do not drop either "
                f"side. Then make sure the project CI still builds and passes. You may `git add` the resolved "
                f"files (the harness stages and completes the merge either way); do NOT run git "
                f"commit/merge/abort/reset/tag.")
        self._call("dev", mid, task, f"resolve-{mid}-{n}")
        merge_head = git(self.work, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0
        markers_ok = self._conflict_resolved(conflicted)  # stages everything, then checks marker content
        if merge_head and markers_ok:
            # normal case: complete the merge commit (MERGE_HEAD makes it a true 2-parent merge)
            git_commit_all(self.work, f"{mid}: resolve merge conflict with main")
        elif markers_ok and git(self.work, "merge-base", "--is-ancestor", "origin/main", "HEAD")[0] == 0:
            # Dev completed the merge itself (dev.md's standing 'commit your work' instinct) — accept it.
            log(f"[merge-queue] PR #{pr['number']} ({mid}) Dev committed the merge itself — accepting")
        else:
            # Failed attempt: either markers remain, or Dev DESTROYED the merge state (abort/reset) so
            # there is nothing integrated to accept — without this check that destruction read as
            # 'resolved' and looped the full re-validation cycle forever with no budget. Roll back;
            # retry next pass or abandon once the budget is spent.
            if merge_head:
                git(self.work, "merge", "--abort")
            else:
                git(self.work, "reset", "--hard")
                git(self.work, "clean", "-fd")
            if n >= self.args.max_bounces:
                self._abandon(pr, mid, "merge conflict unresolved within budget", "conflict_unresolved", attempts=n)
            else:
                why = "markers remain" if not markers_ok else "merge state destroyed"
                log(f"[merge-queue] PR #{pr['number']} ({mid}) conflict NOT resolved ({why}; try {n}/{self.args.max_bounces}) — retry")
            return
        # Resolution complete -> fresh validation epoch + FULL re-validation of the merged result.
        _, sha, _ = git(self.work, "rev-parse", "HEAD")
        self.ci_done.pop(sha, None)
        self.conflict_bounces.pop(mid, None)   # episode over: a future, unrelated conflict gets a fresh budget
        self.merge_attempts.pop(mid, None)     # new head -> fresh merge-retry budget
        # NEW VALIDATION EPOCH: budgets exhausted in the ORIGINAL cycle must not force-approve/force-pass
        # the resolution sight-unseen — the re-review/re-QA the merge queue depends on must be REAL.
        self.rev_bounces.pop(mid, None)
        self.qa_bounces.pop(mid, None)
        note = ("`main` was merged into this PR after your earlier verdict and Dev resolved the resulting "
                f"merge conflicts (commit '{mid}: resolve merge conflict with main')")
        self.reval_rev[mid] = note
        self.reval_qa[mid] = note
        try:
            self.gc.comment(self.repo, pr["number"],
                            f"harness: merged origin/main with conflicts; Dev resolved them @{sha[:12]} — "
                            f"entering FULL re-validation (review + QA + CI on the merged result)")
        except GiteaError:
            pass  # transparency comment is best-effort
        # Push BEFORE relabel: if the relabel is lost to a crash, the PR sits ready-to-merge with an
        # uncertified head and the merge_gate recovery handler routes it back through QA next pass.
        self._push(branch)
        self._relabel(pr, "ready-to-merge", "needs-review")
        log(f"[merge-queue] PR #{pr['number']} ({mid}) conflict resolved -> re-review+QA+CI on {sha[:8]}")
        self.event(type="conflict_resolved", milestone=mid, pr=pr["number"], attempts=n)

    def merge_gate(self, pr):
        mid = self._mid_of_pr(pr)
        if mid in self.tagged or mid in self.abandoned:
            return  # idempotent: never re-merge / re-tag a milestone already resolved
        sha = self._head_sha(pr)
        if (self.ci_done.get(sha) == "success" and self.qa_bounces.get(mid, 0) < self.args.max_bounces
                and not self._qa_certified(pr, sha)):
            # RECOVERY: ready-to-merge + green CI but NO qa certificate for the CURRENT head. No normal
            # transition produces this — it appears only when a crash/Gitea error split a head update from
            # its qa_passed comment. Nothing else routes it (qa() needs the needs-qa label; the observer
            # skips ready-to-merge), so without this it is a permanent silent zombie. Re-run QA on it.
            self._relabel(pr, "ready-to-merge", "needs-qa")
            self.gc.comment(self.repo, pr["number"],
                            f"harness: head {sha[:12]} has green CI but no QA certificate (interrupted "
                            f"update recovered) — re-running QA")
            log(f"[merge] PR #{pr['number']} ({mid}) green CI but uncertified head -> back to needs-qa (recovery)")
            self.event(type="qa_recertify", milestone=mid, pr=pr["number"], sha=sha[:12])
            return
        if not self._ready(pr):
            return
        # MERGE QUEUE staleness check (before any actual merge): main may have advanced under this PR.
        # fetch + rev-list are CHECKED/raising — an error here must surface (retry next pass), never read
        # as ahead==0 (irreversible abandon) or behind==0 (stale branch slips past integration).
        branch = (pr.get("head") or {}).get("ref")
        if not branch:
            raise RuntimeError(f"PR #{pr['number']} has no head ref")
        git(self.work, "fetch", "origin", check=True)
        ahead = self._rev_count(f"origin/main..origin/{branch}")    # commits the branch adds on top of main
        behind = self._rev_count(f"origin/{branch}..origin/main")   # commits on main the branch still lacks
        if ahead == 0:
            # EMPTY PR (Dev produced no diff vs main) — nothing to merge; abandon honestly.
            self._abandon(pr, mid, "empty PR (no diff vs main)", "merge_failed")
            return
        if behind > 0:
            # STALE: integrate main + re-test the merged result before this PR may merge. Never merge a
            # branch that was reviewed/tested only against an older main. At most ONE integration per pass
            # (a true merge queue): integrating every stale PR each pass is quadratic in wasted full-CI
            # runs — each merge invalidates every other freshly re-CI'd head.
            if self._integrated_this_pass:
                log(f"[merge-queue] PR #{pr['number']} ({mid}) stale; queue busy this pass — waits its turn")
                return
            self._integrated_this_pass = True
            self._safe("integrate_main", self.integrate_main, pr)
            return
        merged = False
        for _ in range(40):
            try:
                self.gc.merge_pr(self.repo, pr["number"], method="merge")
                merged = True
                break
            except GiteaError as e:
                es = str(e)
                # "405 / Please try again later" is Gitea's reply to its async mergeability re-check (the
                # head moved moments ago, e.g. QA's test commit earlier in this pass). Retry the transient
                # case; the give-up logic below bounds anything persistent.
                if "405" in es or "Please try again" in es:
                    time.sleep(0.5)
                    continue
                log(f"[merge] PR #{pr['number']} merge error: {es[:160]}")
                break
        if not merged:
            # Do NOT tag a milestone whose PR did not actually merge at its own commit. Retry across a few
            # passes for transients, then abandon (which still tags current MAIN as an honest miss).
            n = self.merge_attempts.get(mid, 0) + 1
            self.merge_attempts[mid] = n
            if n >= MERGE_GIVEUP:
                self._abandon(pr, mid, "up-to-date but merge keeps failing", "merge_failed", attempts=n)
            else:
                log(f"[merge] PR #{pr['number']} ({mid}) not merged (attempt {n}/{MERGE_GIVEUP}) — will retry")
            return
        # Tag at the PR's TRUE merge commit (anchored, not 'whatever main is now'); if anything below
        # fails, the per-pass reconcile_merged_untagged sweep re-creates the tag from the closed PR.
        mcs = None
        try:
            mcs = (self.gc.get_pr(self.repo, pr["number"]) or {}).get("merge_commit_sha")
        except GiteaError:
            pass
        self._tag_milestone(mid, mcs)
        log(f"[merge-gate] merged PR #{pr['number']} -> tag agent-impl-{mid} @ {(mcs or 'gitea/main')[:12]}")
        self.event(type="merged_and_tagged", milestone=mid, pr=pr["number"])

    def _open_work(self):
        """Open new milestone work (issue without a PR -> dev_open). Under --wip-limit N this caps the
        WORK IN PROGRESS: at most N PRs in flight at once, and run() calls this at the END of the pass
        (stop starting, start finishing). WIP=1 is the strict one-PR-at-a-time flow: each PR is cut from
        the freshest main, so the merge queue's stale/integrate path never fires, and persistent-Dev
        pre-implementation (the Trial-1 empty-PR root cause) is structurally prevented. wip_limit=0
        (default) keeps the original open-everything behavior and the original pass position."""
        limit = self.args.wip_limit
        active = 0
        if limit:
            active = sum(1 for p in self.gc.list_prs(self.repo, state="open")
                         if self._pr_state(p) and not self._resolved_mid(p))
        for iss in self.gc.list_issues(self.repo, labels=["evoclaw-task"], state="open"):
            if limit and active >= limit:
                break  # pipeline at WIP capacity — finish in-flight PRs before starting new work
            if "has-pr" in self._labels(iss):
                continue
            mid = next((m for m, n in self.issued.items() if n == iss["number"]), None)
            if mid and mid not in self.tagged and mid not in self.abandoned:
                self._safe("dev_open", self.dev_open, mid, iss["number"])
                active += 1

    def _ci_red_route(self, pr, st):
        """Route a PR whose CURRENT head has red CI. Extracted from run() so every Gitea call here runs
        under _safe (the inline version could crash the whole controller on one HTTP error)."""
        mid = self._mid_of_pr(pr)
        sha = self._head_sha(pr)
        if self.ci_bounces.get(mid, 0) >= self.args.max_bounces:
            # Dev could not make the CI gate pass within budget. A hard gate is NEVER force-greened —
            # abandon (no merge; current MAIN is tagged as an honest miss) so a broken build can't ship.
            self._abandon(pr, mid, "CI gate never green within budget", "ci_gate_failed")
        elif st == "ready-to-merge":
            # CI went red on an already-QA'd head (QA's new tests fail, or a main-integration broke the
            # build semantically). Route back through Dev+QA — and SAY WHY: dev_fix feeds Dev the LAST
            # comment as feedback, which right after a clean integration is the qa_passed carry-over
            # marker (zero signal). Post the red CI tail so the fix isn't blind.
            tail = (self.ci_out.get(sha) or "(no CI output recorded)")[-3000:]
            self.gc.comment(self.repo, pr["number"],
                            f"harness: CI RED on current head {sha[:12]} (after QA pass / main-integration — "
                            f"possibly a semantic conflict with newly-merged main). CI output tail:\n\n{tail}")
            self._relabel(pr, "ready-to-merge", "needs-code-changes:Q")
            log(f"[ci] PR #{pr['number']} ready-to-merge but CI RED -> back to needs-code-changes:Q")
        else:
            self._safe("dev_fix_ci", self.dev_fix_ci, pr)

    def observer(self, pr):
        state = self._pr_state(pr)
        if state in (None, "ready-to-merge"):
            return
        try:
            updated = pr.get("updated_at", "")
            age_min = (time.time() - time.mktime(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))) / 60.0 if updated else 0
        except Exception:
            age_min = 0
        if age_min > self.args.stall_min:
            self.gc.comment(self.repo, pr["number"], f"STALL ALERT: {state} for ~{int(age_min)}min (Observer, no intervention)")
            self.event(type="stall_alert", pr=pr["number"], state=state, age_min=int(age_min))

    # --- pacer loop --------------------------------------------------
    def _safe(self, label, fn, *a):
        try:
            fn(*a)
        except Exception as e:  # one bad action must NOT crash the controller (→ EvoClaw restart loop)
            log(f"[error] {label}: {str(e)[:200]}")
            self.event(type="action_error", label=label, error=str(e)[:200])

    def run(self):
        self.setup()
        idle, last_sig = 0, None
        for _ in range(self.args.max_passes):
            self._integrated_this_pass = False
            self._safe("sync_issues", self.sync_issues)
            # Heal merged-but-untagged milestones FIRST (crash between merge_pr and tag — the closed PR is
            # invisible to every other handler).
            self._safe("reconcile", self.reconcile_merged_untagged)
            for pr in self.gc.list_prs(self.repo, state="open"):
                st = self._pr_state(pr)
                if st in ("needs-code-changes:R", "needs-code-changes:Q") and not self._resolved_mid(pr):
                    self._safe("dev_fix", self.dev_fix, pr, st)
            if not self.args.wip_limit:
                self._open_work()  # original behavior: open everything up front
            for pr in self.gc.list_prs(self.repo, state="open"):
                if not self._resolved_mid(pr):  # a resolved PR's CI result is never consumed — skip the build
                    self._safe("ci", self.run_ci, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                st = self._pr_state(pr)
                if st in ("needs-review", "needs-qa", "ready-to-merge") and not self._resolved_mid(pr) \
                        and self.ci_done.get(self._head_sha(pr)) == "failure":
                    self._safe("ci_red", self._ci_red_route, pr, st)
            for pr in self.gc.list_prs(self.repo, state="open"):
                if self._pr_state(pr) == "needs-review" and not self._resolved_mid(pr) \
                        and self.ci_done.get(self._head_sha(pr)) == "success":
                    self._safe("reviewer", self.reviewer, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                if self._pr_state(pr) == "needs-qa" and not self._resolved_mid(pr) \
                        and self.ci_done.get(self._head_sha(pr)) == "success":
                    self._safe("qa", self.qa, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                if self._pr_state(pr) == "ready-to-merge":
                    self._safe("merge", self.merge_gate, pr)
                else:
                    self._safe("observer", self.observer, pr)
            if self.args.wip_limit:
                # WIP mode: open new work only AFTER this pass tried to finish in-flight work (merges
                # above just freed WIP slots) — stop starting, start finishing.
                self._open_work()

            # CLEAN EXIT: queue drained AND every milestone we ever issued is resolved AND no PR is still
            # in flight. (The old predicate required the queue to list tasks AND say 'No tasks currently
            # available' simultaneously — mutually exclusive, so it never fired and every run ended via
            # the idle counter. With tag-on-abandon, resolved milestones leave the queue and unblock their
            # dependents, so this drains naturally.)
            mids, no_more = self.available_milestones()
            open_prs = self.gc.list_prs(self.repo, state="open")
            active = [p for p in open_prs if self._pr_state(p)]
            done = self.tagged | self.abandoned
            # SETTLING PERIOD: a fresh tag takes the watcher's debounce (~120s) + grading before its
            # dependents appear in the queue — exiting inside that window misreads "all done" and costs
            # an outer-harness controller relaunch per milestone generation (observed live). Only exit
            # once the newest tag has had time to unlock whatever it unlocks.
            settled = (time.time() - self.last_tag_ts) > 180
            if no_more and self.issued and all(m in done for m in self.issued) and not active and settled:
                log("[pacer] queue drained, all issued milestones resolved (tagged/abandoned), no active PRs — done")
                break
            now = self.gc.list_prs(self.repo, state="all")
            sig = (len(self.tagged), tuple(sorted(
                (p["number"], "|".join(sorted(self._labels(p))), self._head_sha(p)[:8]) for p in now)))
            if sig == last_sig:
                idle += 1
                if idle >= self.args.max_idle and settled:
                    # (same settling rule as clean-exit: a just-created tag may be about to unlock
                    # dependents — idling out inside that window costs an outer relaunch per generation)
                    log(f"[pacer] idle limit ({self.args.max_idle}) — exiting")
                    break
                log(f"[pacer] idle {idle}/{self.args.max_idle}; tagged={sorted(self.tagged)}")
                time.sleep(self.args.poll_secs)
            else:
                idle = 0
            last_sig = sig
        log(f"[pacer] done; tagged {len(self.tagged)}: {sorted(self.tagged)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--trial", required=True)
    ap.add_argument("--workspace", default="/e2e_workspace")
    ap.add_argument("--testbed", default="/testbed")
    ap.add_argument("--roles-dir", required=True)
    ap.add_argument("--work", default="/tmp/ctl_work")
    ap.add_argument("--event-log", default="/e2e_workspace/harnessed_events.jsonl")
    ap.add_argument("--session-config", default="dev:persistent,reviewer:milestone,qa:milestone")
    ap.add_argument("--call-timeout", type=int, default=2400)
    ap.add_argument("--max-bounces", type=int, default=10)
    ap.add_argument("--wip-limit", type=int, default=0,
                    help="max PRs in flight at once (0 = unlimited/original behavior); under a limit, "
                         "new work opens at the END of the pass and the Dev prompt states the WIP policy")
    ap.add_argument("--max-passes", type=int, default=200)
    ap.add_argument("--max-idle", type=int, default=16)
    ap.add_argument("--poll-secs", type=int, default=10)
    ap.add_argument("--stall-min", type=int, default=30)
    args = ap.parse_args()
    takeover_singleton()  # at most ONE controller per container (outer recovery may relaunch over a live one)
    log(f"controller start: model={args.model} effort={args.effort} trial={args.trial} "
        f"sessions='{args.session_config}' gitea={os.environ.get('GITEA_URL')}")
    Controller(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
