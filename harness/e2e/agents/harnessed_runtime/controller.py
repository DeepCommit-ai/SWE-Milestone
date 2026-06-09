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
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gitea_client import GiteaClient, GiteaError  # noqa: E402
from labels import next_state  # noqa: E402

SRS_CAP = 20000
OUT_CAP = 16000
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
    found = None
    for line in text.splitlines():
        s = line.strip().upper()
        if s.startswith("VERDICT:"):
            if positive in s:
                found = positive
            elif negative in s:
                found = negative
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
            r = subprocess.run(cmd, stdin=fin, capture_output=True, text=True, cwd=cwd, timeout=args.call_timeout)
    except subprocess.TimeoutExpired:
        log(f"<- claude [{label}] TIMEOUT after {args.call_timeout}s")
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
        rc, _, err = git(self.testbed, "push", "-f", "gitea", "HEAD:refs/heads/main")
        if rc != 0:
            log(f"[setup] push /testbed→gitea WARN: {err[:200]}")
        git(self.testbed, "fetch", "gitea")
        subprocess.run(["rm", "-rf", self.work])
        r = subprocess.run(["git", "clone", rem, self.work], capture_output=True, text=True)
        if not os.path.isdir(os.path.join(self.work, ".git")):
            raise RuntimeError(f"clone failed: {r.stderr[:300]}")
        # resume-safe: skip milestones already tagged in /testbed from a prior run
        _, out, _ = git(self.testbed, "tag", "-l", "agent-impl-*")
        for t in out.split():
            self.tagged.add(t.replace("agent-impl-", ""))
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
    def _default_ci_body(self):
        has = lambda f: os.path.exists(os.path.join(self.work, f))  # noqa: E731
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
                    "set -e\n" + _CI_ENV_LINES + "\n" + self._default_ci_body() + "\n")
        os.chmod(ci_sh, 0o755)
        wf = os.path.join(self.work, ".gitea", "workflows")
        os.makedirs(wf, exist_ok=True)
        with open(os.path.join(wf, "ci.yaml"), "w", encoding="utf-8") as f:
            f.write(_CI_WORKFLOW_YAML)
        git_commit_all(self.work, "ci: seed project CI pipeline (Dev maintains)")
        git(self.work, "push", "origin", "main")
        log("[setup] seeded ci.sh + .gitea/workflows/ci.yaml into main")

    def _run_project_ci(self):
        """Run the Dev-maintained project CI (ci.sh) in the current working tree. Returns (ok, log)."""
        ci = os.path.join(self.work, "ci.sh")
        body = "bash ci.sh" if os.path.exists(ci) else self._default_ci_body().replace("\n", " && ")
        r = subprocess.run(["/bin/sh", "-c", _CI_PATH + body], cwd=self.work,
                           capture_output=True, text=True, timeout=self.args.call_timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-OUT_CAP:]

    def _probe_main_ci(self):
        """Does untouched main pass the project CI in THIS env? If not (e.g. missing system lib), CI is
        advisory so we don't red-flag every PR for a pre-existing env gap — but we log it loudly."""
        if self.main_ci_ok is not None:
            return
        self._wc_checkout("main")
        ok, _out = self._run_project_ci()
        self.main_ci_ok = ok
        log(f"[ci] baseline probe: main CI {'PASS -> gating' if ok else 'FAIL -> ADVISORY (env)'}")
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

    def _head_sha(self, pr):
        return ((pr.get("head") or {}).get("sha")) or ""

    def _relabel(self, pr, old, new):
        self.gc.remove_labels(self.repo, pr["number"], [old])
        self.gc.add_labels(self.repo, pr["number"], [new])

    def _wc_checkout(self, ref):
        git(self.work, "fetch", "origin")
        git(self.work, "checkout", "-B", ref, f"origin/{ref}")

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
                f"tests locally. Leave changes ready to commit. Do NOT git tag, branch, or open a PR.")
        self._call("dev", mid, task, f"open-{mid}")
        git_commit_all(self.work, f"{mid}: implement")
        git(self.work, "push", "-f", "origin", branch)
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
        comments = self.gc.comments(self.repo, pr["number"]) or []
        feedback = comments[-1]["body"][:OUT_CAP] if comments else "(see review)"
        fresh = (f"## Your PR for milestone {mid} was sent back. Requirement (SRS): `{self.srs_path(mid)}`.\n"
                 f"The team requested these changes:\n\n{feedback}\n\nAddress them in the current repo "
                 f"({self.work}); keep the project CI green. Leave changes ready to commit. No git tag / PR.")
        resume = (f"## Your PR for milestone {mid} was sent back with requested changes:\n\n{feedback}\n\n"
                  f"Address them in the current repo ({self.work}); keep CI green. No git tag / PR. "
                  f"(SRS unchanged at `{self.srs_path(mid)}` if you need it.)")
        self._call("dev", mid, fresh, f"fix-{mid}-{self.rev_bounces.get(mid,0)+self.qa_bounces.get(mid,0)}",
                   resume_task=resume)
        git_commit_all(self.work, f"{mid}: address feedback")
        git(self.work, "push", "-f", "origin", branch)
        self._relabel(pr, state, next_state(state, actor="dev", verdict="fixed"))
        log(f"[dev] fixed PR #{pr['number']} ({state} -> next)")
        self.event(type="dev_fix", milestone=mid, pr=pr["number"], frm=state)

    def dev_fix_ci(self, pr):
        """Spec §3.1: project CI red → Dev fixes it (without bothering Reviewer/QA)."""
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        build_log = self.ci_out.get(self._head_sha(pr), "(ci failed)")
        fresh = (f"## The project CI on your PR for milestone {mid} is RED. As CI owner, fix the REAL cause "
                 f"in the current repo ({self.work}) so build + tests pass — do NOT weaken CI to hide it. "
                 f"CI output:\n\n{build_log}\n\nLeave changes ready to commit. No git tag / PR.")
        resume = (f"## Your PR's CI for milestone {mid} is now RED. Fix the real cause (do not weaken CI). "
                  f"CI output:\n\n{build_log}\n\nLeave changes ready to commit. No git tag / PR.")
        self._call("dev", mid, fresh, f"ci-{mid}-{self.ci_bounces.get(mid,0)}", resume_task=resume)
        git_commit_all(self.work, f"{mid}: fix CI")
        git(self.work, "push", "-f", "origin", branch)
        self.ci_bounces[mid] = self.ci_bounces.get(mid, 0) + 1
        log(f"[dev] fixed red CI on PR #{pr['number']} (ci_bounce {self.ci_bounces[mid]}/{self.args.max_bounces})")
        self.event(type="dev_fix_ci", milestone=mid, pr=pr["number"])

    def run_ci(self, pr):
        sha = self._head_sha(pr)
        if not sha or self.ci_done.get(sha):
            return
        self._probe_main_ci()
        self._wc_checkout((pr.get("head") or {}).get("ref"))
        ok, out = self._run_project_ci()
        state = "success" if (ok or not self.main_ci_ok) else "failure"
        desc = "ci.sh (build+test)" + ("" if self.main_ci_ok else " [advisory: base CI fails in env]")
        self.gc.set_commit_status(self.repo, sha, state=state, context="ci/build", description=desc[:90])
        self.ci_done[sha] = state
        self.ci_out[sha] = out
        log(f"[ci] PR #{pr['number']} head={sha[:8]} ci_ok={ok} -> {state}{'' if self.main_ci_ok else ' (advisory)'}")
        self.event(type="ci_run", pr=pr["number"], result=state, ci_ok=ok, sha=sha[:12])

    def reviewer(self, pr):
        mid = self._mid_of_pr(pr)
        self._wc_checkout((pr.get("head") or {}).get("ref"))
        if self.rev_bounces.get(mid, 0) >= self.args.max_bounces:
            verdict = "approve"
            log(f"[reviewer] PR #{pr['number']} budget exhausted -> force approve")
        else:
            fresh = (f"## PR under review: milestone {mid}\nThe PR branch is checked out in your working "
                     f"directory ({self.work}); base is `origin/main`. Read the requirement (SRS) at "
                     f"`{self.srs_path(mid)}`, then review the actual change (`git diff origin/main...HEAD`).\n"
                     f"ALSO audit the Dev's CI maintenance using the ci-maintenance-check skill (read it at "
                     f"`{CI_SKILL}`). Then give your verdict.")
            resume = (f"## Re-review: milestone {mid}\nThe Dev pushed a new commit addressing the changes you "
                      f"requested on this PR. Re-examine the current diff (`git diff origin/main...HEAD`) — focus "
                      f"on whether your concerns are resolved, and re-check CI maintenance. (SRS unchanged at "
                      f"`{self.srs_path(mid)}`.) Give your verdict.")
            out = self._call("reviewer", mid, fresh, f"{mid}-{self.rev_bounces.get(mid,0)}", resume_task=resume)
            self.gc.comment(self.repo, pr["number"], out[:OUT_CAP] or "(no output)")
            verdict = "approve" if parse_verdict(out, "APPROVE", "REQUEST_CHANGES") != "REQUEST_CHANGES" else "request-changes"
        if verdict == "request-changes":
            self.rev_bounces[mid] = self.rev_bounces.get(mid, 0) + 1
        self._relabel(pr, "needs-review", next_state("needs-review", actor="reviewer", verdict=verdict))
        log(f"[reviewer] PR #{pr['number']} -> {verdict} (rev_bounce {self.rev_bounces.get(mid,0)}/{self.args.max_bounces})")
        self.event(type="review_verdict", milestone=mid, pr=pr["number"], verdict=verdict)

    def qa(self, pr):
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        if self.qa_bounces.get(mid, 0) >= self.args.max_bounces:
            verdict, out = "pass", ""
            log(f"[qa] PR #{pr['number']} budget exhausted -> force pass")
        else:
            fresh = (f"## PR under test: milestone {mid}\nThe PR branch is checked out in your working "
                     f"directory ({self.work}); base is `origin/main`. Requirement (SRS): `{self.srs_path(mid)}`.\n"
                     f"As Test-Suite owner: build the project and run its tests, AND write/strengthen tests in "
                     f"the suite that deeply exercise this milestone's required behavior (mirror the codebase's "
                     f"conventions / verify cross-type interface symmetry). Tests you add are committed. Give "
                     f"your verdict from real execution.")
            resume = (f"## Re-test: milestone {mid}\nThe Dev pushed a fix for the bug you found on this PR. "
                      f"Re-verify by building + running the tests (including the ones you added). (SRS unchanged "
                      f"at `{self.srs_path(mid)}`.) Give your verdict from real execution.")
            out = self._call("qa", mid, fresh, f"{mid}-{self.qa_bounces.get(mid,0)}", resume_task=resume)
            verdict = "pass" if parse_verdict(out, "PASS", "FAIL") != "FAIL" else "bug"
            self.gc.comment(self.repo, pr["number"], out[:OUT_CAP] or "(no output)")
        # QA maintains the Test Suite: commit + push any tests it wrote/updated (head sha will change;
        # CI re-runs on the new head next pass before merge).
        if git_commit_all(self.work, f"{mid}: QA tests") and verdict != "pass":
            pass
        git(self.work, "push", "-f", "origin", branch)
        pr = self.gc.get_pr(self.repo, pr["number"])  # refresh head sha after QA's commit
        sha = self._head_sha(pr)
        if verdict == "pass":
            self.gc.comment(self.repo, pr["number"], f"qa_passed@{sha[:12]}")
            self._relabel(pr, "needs-qa", "ready-to-merge")
        else:
            self.qa_bounces[mid] = self.qa_bounces.get(mid, 0) + 1
            self._relabel(pr, "needs-qa", "needs-code-changes:Q")
        log(f"[qa] PR #{pr['number']} -> {verdict} (qa_bounce {self.qa_bounces.get(mid,0)}/{self.args.max_bounces})")
        self.event(type="qa_verdict", milestone=mid, pr=pr["number"], verdict=verdict)

    def _ready(self, pr):
        sha = self._head_sha(pr)
        mid = self._mid_of_pr(pr)
        qa_forced = self.qa_bounces.get(mid, 0) >= self.args.max_bounces
        qa_ok = qa_forced or any(c.get("body", "").strip().startswith(f"qa_passed@{sha[:12]}")
                                 for c in (self.gc.comments(self.repo, pr["number"]) or []))
        ci_ok = (self.ci_bounces.get(mid, 0) >= self.args.max_bounces) or self.ci_done.get(sha) == "success"
        return qa_ok and ci_ok

    def merge_gate(self, pr):
        mid = self._mid_of_pr(pr)
        if not self._ready(pr):
            return
        for _ in range(40):
            try:
                self.gc.merge_pr(self.repo, pr["number"], method="merge")
                break
            except GiteaError as e:
                if "405" in str(e) or "Please try again" in str(e):
                    time.sleep(0.5)
                    continue
                log(f"[merge] PR #{pr['number']} merge error: {str(e)[:160]}")
                return
        git(self.testbed, "fetch", "gitea", "main")
        git(self.testbed, "reset", "--hard", "gitea/main")
        rc, _, _ = git(self.testbed, "tag", f"agent-impl-{mid}")
        self.tagged.add(mid)
        log(f"[merge-gate] merged PR #{pr['number']} -> tag agent-impl-{mid} (rc={rc})")
        self.event(type="merged_and_tagged", milestone=mid, pr=pr["number"])

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
            self._safe("sync_issues", self.sync_issues)
            for pr in self.gc.list_prs(self.repo, state="open"):
                st = self._pr_state(pr)
                if st in ("needs-code-changes:R", "needs-code-changes:Q"):
                    self._safe("dev_fix", self.dev_fix, pr, st)
            for iss in self.gc.list_issues(self.repo, labels=["evoclaw-task"], state="open"):
                if "has-pr" in self._labels(iss):
                    continue
                mid = next((m for m, n in self.issued.items() if n == iss["number"]), None)
                if mid and mid not in self.tagged:
                    self._safe("dev_open", self.dev_open, mid, iss["number"])
            for pr in self.gc.list_prs(self.repo, state="open"):
                self._safe("ci", self.run_ci, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                sha = self._head_sha(pr)
                if self._pr_state(pr) in ("needs-review", "needs-qa") and self.ci_done.get(sha) == "failure":
                    if self.ci_bounces.get(self._mid_of_pr(pr), 0) >= self.args.max_bounces:
                        self.ci_done[sha] = "success"
                        self.gc.set_commit_status(self.repo, sha, state="success", context="ci/build",
                                                  description="forced (budget exhausted)")
                        log(f"[ci] PR #{pr['number']} budget exhausted -> force CI green")
                    else:
                        self._safe("dev_fix_ci", self.dev_fix_ci, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                if self._pr_state(pr) == "needs-review" and self.ci_done.get(self._head_sha(pr)) == "success":
                    self._safe("reviewer", self.reviewer, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                if self._pr_state(pr) == "needs-qa" and self.ci_done.get(self._head_sha(pr)) == "success":
                    self._safe("qa", self.qa, pr)
            for pr in self.gc.list_prs(self.repo, state="open"):
                if self._pr_state(pr) == "ready-to-merge":
                    self._safe("merge", self.merge_gate, pr)
                else:
                    self._safe("observer", self.observer, pr)

            mids, no_more = self.available_milestones()
            if mids and all(m in self.tagged for m in mids) and no_more:
                log("[pacer] all milestones tagged + queue drained — done")
                break
            now = self.gc.list_prs(self.repo, state="all")
            sig = (len(self.tagged), tuple(sorted(
                (p["number"], "|".join(sorted(self._labels(p))), self._head_sha(p)[:8]) for p in now)))
            if sig == last_sig:
                idle += 1
                if idle >= self.args.max_idle:
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
    ap.add_argument("--max-passes", type=int, default=200)
    ap.add_argument("--max-idle", type=int, default=16)
    ap.add_argument("--poll-secs", type=int, default=10)
    ap.add_argument("--stall-min", type=int, default=30)
    args = ap.parse_args()
    log(f"controller start: model={args.model} effort={args.effort} trial={args.trial} "
        f"sessions='{args.session_config}' gitea={os.environ.get('GITEA_URL')}")
    Controller(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
