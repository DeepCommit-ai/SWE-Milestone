#!/usr/bin/env python3
"""In-container Gitea pacer controller for the harnessed multi-role arm (spec §3, true Gitea route).

Runs INSIDE the EvoClaw /testbed container (launched by HarnessedFramework.build_run_command). It is
EvoClaw's single "agent", but internally it drives the spec's async multi-role workflow through a
**deterministic event-driven pacer** (user decision: pacer + `claude -p` per role, not 3 self-polling
/loops — identical PR-label-state-machine coordination, far less concurrency risk). One shared working
clone is sufficient because the pacer acts on one thing at a time; role separation is SEMANTIC (each
role = a `claude -p` with its own materialized harness prompt + its own session).

Flow per milestone:
  Dev (claude -p in clone) → commit → push branch → open PR (needs-review)
    → CI (build/compile → set commit status)
    → Reviewer (needs-review & CI green → approve→needs-qa / request-changes→:R)
    → QA (needs-qa → run tests → qa_passed@sha→ready-to-merge / bug→:Q)
    → merge-gate (review + qa_passed@sha + CI green → merge PR → sync Gitea main into /testbed
                  → git tag agent-impl-<mid>)   ← the ONLY seam EvoClaw's grader consumes
  Observer: stall alerts (comment only, no intervention).

Coordination bus = Gitea PR labels (vendored labels.py state machine). CI = in-container build/compile.
Gitea is reached at http://host.docker.internal:3000 (whitelisted). stdlib only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gitea_client import GiteaClient, GiteaError  # noqa: E402
from labels import next_state  # noqa: E402

SRS_CAP = 20000
OUT_CAP = 16000


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


def run_claude(prompt_text, args, label, cwd):
    """One claude invocation (fresh session) reading prompt on stdin; returns result text.
    The `session=<sid>` marker lets the harnessed log_parser map this call's transcript to its role."""
    work = "/tmp/ctl_prompts"
    os.makedirs(work, exist_ok=True)
    pf = os.path.join(work, f"{label}.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    sid = str(uuid.uuid4())
    cmd = ["claude", "--model", args.model, "--output-format", "json",
           "--dangerously-skip-permissions", "--session-id", sid]
    if args.effort:
        cmd += ["--effort", args.effort]
    log(f"-> claude [{label}] session={sid} cwd={cwd} ({len(prompt_text)} chars)")
    t0 = time.time()
    try:
        with open(pf, encoding="utf-8") as fin:
            r = subprocess.run(cmd, stdin=fin, capture_output=True, text=True, cwd=cwd,
                               timeout=args.call_timeout)
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


def role_prompt(roles_dir, role):
    return read_file(os.path.join(roles_dir, f"{role}.md")).strip()


# Toolchain bin dirs prepended to PATH for CI — the controller subprocess env doesn't reliably carry
# them (e.g. go lives at /usr/local/go/bin), so `go build` returned 127 "not found". Also pin build
# caches to /tmp (always writable) so go/cargo don't fail on a non-writable HOME/.cache. Make CI robust.
_CI_PATH = ('export PATH="/usr/local/go/bin:/go/bin:$HOME/go/bin:$HOME/.cargo/bin:/usr/local/cargo/bin:/root/.cargo/bin:$PATH"; '
            'export GOCACHE=/tmp/ci-gocache GOFLAGS=-mod=mod CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"; ')


def detect_ci_cmd(cwd):
    """Fast CI gate = build/compile (the red-light check). Full test suite is QA's job."""
    has = lambda f: os.path.exists(os.path.join(cwd, f))  # noqa: E731
    if has("go.mod"):
        cmd = "go build ./... 2>&1 | tail -40"
    elif has("Cargo.toml"):
        cmd = "cargo build --workspace 2>&1 | tail -40"
    elif has("pom.xml"):
        cmd = "mvn -q -B -DskipTests compile 2>&1 | tail -40"
    elif has("pyproject.toml") or has("setup.py"):
        cmd = "python -m compileall -q . >/dev/null 2>&1 || true; echo ok"
    elif has("package.json"):
        cmd = "npm run build --if-present 2>&1 | tail -40 || true"
    else:
        return "echo '(no build system detected — CI pass)'"
    return _CI_PATH + cmd


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
        # SEPARATE per-role retry budgets (each capped at args.max_bounces). A milestone's review
        # loop must NOT consume QA's budget (that was force-skipping QA), so reviewer/qa/ci-fix each
        # count independently.
        self.rev_bounces = {}               # mid -> times Reviewer requested changes
        self.qa_bounces = {}                # mid -> times QA found a bug
        self.ci_bounces = {}                # mid -> times Dev fixed red CI
        self.ci_done = {}                   # head_sha -> "success"/"failure"
        self.ci_out = {}                    # head_sha -> build log (for Dev to fix red CI)
        self.main_builds = None             # None=unknown; True/False once probed (baseline-aware CI)
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
        # push /testbed (EvoClaw repo @ main) → Gitea repo main
        git(self.testbed, "remote", "remove", "gitea")
        git(self.testbed, "remote", "add", "gitea", rem)
        rc, _, err = git(self.testbed, "push", "-f", "gitea", "HEAD:refs/heads/main")
        if rc != 0:
            log(f"[setup] push /testbed→gitea WARN: {err[:200]}")
        git(self.testbed, "fetch", "gitea")  # so /testbed knows gitea/main for later sync
        # working clone
        subprocess.run(["rm", "-rf", self.work])
        # FULL clone: roles diff branches against main with `git diff origin/main...HEAD` (three-dot),
        # which needs the merge-base present — a shallow clone breaks that and gives the reviewer a
        # wrong/empty diff. (The earlier shallow clone was a mis-fix for container deaths that were
        # actually an external container-reaper, not footprint.)
        r = subprocess.run(["git", "clone", rem, self.work], capture_output=True, text=True)
        if not os.path.isdir(os.path.join(self.work, ".git")):
            raise RuntimeError(f"clone failed: {r.stderr[:300]}")
        # resume-safe: skip milestones already tagged in /testbed from a prior run
        _, out, _ = git(self.testbed, "tag", "-l", "agent-impl-*")
        for t in out.split():
            self.tagged.add(t.replace("agent-impl-", ""))
        # idempotent across EvoClaw recovery restarts: rebuild the milestone→issue map from the
        # existing Gitea state, so a restart resumes instead of re-bootstrapping duplicate issues.
        for iss in self.gc.list_issues(self.repo, labels=["evoclaw-task"], state="all"):
            m = re.match(r"\[([^\]]+)\]", iss.get("title", ""))
            if m:
                self.issued[m.group(1)] = iss["number"]
        log(f"[setup] gitea repo={self.repo} pushed + cloned; tagged={sorted(self.tagged)} "
            f"existing-issues={len(self.issued)}")
        self.event(type="setup", repo=self.repo)

    # --- milestone inputs -------------------------------------------
    def available_milestones(self):
        text = read_file(os.path.join(self.args.workspace, "TASK_QUEUE.md"))
        mids = [m.group(1) for m in (re.match(r"^- (\S+):", ln.strip()) for ln in text.splitlines()) if m]
        out = []
        for mid in mids:
            srs = os.path.join(self.args.workspace, "srs", f"{mid}_SRS.md")
            if os.path.exists(srs):
                out.append(mid)
        return out, ("No tasks currently available" in text)

    def srs_of(self, mid):
        return read_file(os.path.join(self.args.workspace, "srs", f"{mid}_SRS.md"))[:SRS_CAP]

    def sync_issues(self):
        mids, _ = self.available_milestones()
        for mid in mids:
            if mid in self.issued or mid in self.tagged:
                continue
            num = self.gc.create_issue(
                self.repo, title=f"[{mid}] EvoClaw milestone",
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

    # --- role actions -----------------------------------------------
    def dev_open(self, mid, issue_num):
        branch = f"task-{re.sub(r'[^A-Za-z0-9_.-]', '-', mid)}"
        # idempotent across restarts: if a PR for this branch already exists, just mark has-pr and
        # skip — avoids re-running the expensive Dev claude call AND a 409 on create_pr.
        existing = next((p for p in self.gc.list_prs(self.repo, state="all")
                         if (p.get("head") or {}).get("ref") == branch), None)
        if existing:
            self.gc.add_labels(self.repo, issue_num, ["has-pr"])
            log(f"[dev] PR for {mid} already exists (#{existing['number']}) — skip open")
            return
        self._wc_checkout("main")
        git(self.work, "checkout", "-B", branch, "main")
        srs = self.srs_of(mid)
        task = (f"{role_prompt(self.args.roles_dir, 'dev')}\n\n## Milestone: {mid}\n## Requirement (SRS)\n{srs}\n\n"
                f"Implement this in the current repo ({self.work}), build/run relevant tests, and leave your "
                f"changes ready to commit. Do NOT git tag, branch, or open a PR — the harness handles that.")
        run_claude(task, self.args, f"dev-open-{mid}", self.work)
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
        n = self.rev_bounces.get(mid, 0) + self.qa_bounces.get(mid, 0)
        task = (f"{role_prompt(self.args.roles_dir, 'dev')}\n\n## Milestone: {mid}\n## Requirement (SRS)\n"
                f"{self.srs_of(mid)}\n\nThe team requested changes on your PR:\n\n{feedback}\n\n"
                f"Address them in the current repo ({self.work}) and leave changes ready to commit. "
                f"Do NOT git tag or open a PR.")
        run_claude(task, self.args, f"dev-fix-{mid}-{n}", self.work)
        git_commit_all(self.work, f"{mid}: address feedback")
        git(self.work, "push", "-f", "origin", branch)
        self._relabel(pr, state, next_state(state, actor="dev", verdict="fixed"))
        log(f"[dev] fixed PR #{pr['number']} ({state} -> next)")
        self.event(type="dev_fix", milestone=mid, pr=pr["number"], frm=state)

    def _build(self):
        cmd = detect_ci_cmd(self.work)
        r = subprocess.run(["/bin/sh", "-c", cmd], cwd=self.work, capture_output=True, text=True,
                           timeout=self.args.call_timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-OUT_CAP:], cmd

    def _probe_main_builds(self):
        """Baseline-aware CI: does the repo's own main build in THIS env? Some repos (e.g. navidrome's
        cgo/taglib) don't build with a generic command — we must not red-flag every PR for that. If
        main doesn't build, CI becomes advisory (a PR only fails CI if it regresses a buildable base)."""
        if self.main_builds is not None:
            return
        self._wc_checkout("main")
        ok, _out, cmd = self._build()
        self.main_builds = ok
        log(f"[ci] baseline probe: main builds={ok} ({cmd[:60]}) -> CI is {'gating' if ok else 'ADVISORY'}")
        self.event(type="ci_baseline", main_builds=ok)

    def run_ci(self, pr):
        sha = self._head_sha(pr)
        if not sha or self.ci_done.get(sha):
            return
        self._probe_main_builds()
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        ok, out, cmd = self._build()
        # Advisory when the baseline itself can't build in this env: don't gate on a broken base.
        state = "success" if (ok or not self.main_builds) else "failure"
        desc = cmd[:70] + ("" if self.main_builds else " [advisory: base unbuildable]")
        self.gc.set_commit_status(self.repo, sha, state=state, context="ci/build", description=desc[:90])
        self.ci_done[sha] = state
        self.ci_out[sha] = out
        log(f"[ci] PR #{pr['number']} head={sha[:8]} build_ok={ok} -> {state}"
            f"{'' if self.main_builds else ' (advisory)'}")
        self.event(type="ci_run", pr=pr["number"], result=state, build_ok=ok, sha=sha[:12])

    def dev_fix_ci(self, pr):
        """Spec §3.1: project CI red → Dev fixes it (without bothering Reviewer/QA)."""
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        build_log = self.ci_out.get(self._head_sha(pr), "(build failed)")
        task = (f"{role_prompt(self.args.roles_dir, 'dev')}\n\n## Milestone: {mid}\nThe project CI build is RED. "
                f"Fix the build in the current repo ({self.work}) so it compiles. Build output:\n\n"
                f"{build_log}\n\nLeave changes ready to commit. Do NOT git tag or open a PR.")
        run_claude(task, self.args, f"dev-ci-{mid}-{self.ci_bounces.get(mid,0)}", self.work)
        git_commit_all(self.work, f"{mid}: fix CI build")
        git(self.work, "push", "-f", "origin", branch)
        self.ci_bounces[mid] = self.ci_bounces.get(mid, 0) + 1
        log(f"[dev] fixed red CI on PR #{pr['number']} (ci_bounce {self.ci_bounces[mid]}/{self.args.max_bounces})")
        self.event(type="dev_fix_ci", milestone=mid, pr=pr["number"])

    def reviewer(self, pr):
        mid = self._mid_of_pr(pr)
        branch = (pr.get("head") or {}).get("ref")
        self._wc_checkout(branch)
        if self.rev_bounces.get(mid, 0) >= self.args.max_bounces:
            verdict = "approve"
            log(f"[reviewer] PR #{pr['number']} budget exhausted -> force approve")
        else:
            # The reviewer explores the REAL checked-out PR with its own tools (git diff / read code) —
            # no truncated diff string in the prompt (that caused empty/cut-off diffs and shallow review).
            task = (f"{role_prompt(self.args.roles_dir, 'reviewer')}\n\n## Milestone under review: {mid}\n"
                    f"The PR branch is checked out in your current working directory ({self.work}); its base "
                    f"is `origin/main`. Use git and read the files to review the actual change.\n\n"
                    f"## Requirement (SRS)\n{self.srs_of(mid)}\n\nReview it and give your verdict.")
            out = run_claude(task, self.args, f"review-{mid}-{self.rev_bounces.get(mid,0)}", self.work)
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
        sha = self._head_sha(pr)
        if self.qa_bounces.get(mid, 0) >= self.args.max_bounces:
            verdict = "pass"
            log(f"[qa] PR #{pr['number']} budget exhausted -> force pass")
        else:
            task = (f"{role_prompt(self.args.roles_dir, 'qa')}\n\n## Milestone under test: {mid}\n"
                    f"The PR branch is checked out in your current working directory ({self.work}); its base "
                    f"is `origin/main`.\n\n## Requirement (SRS)\n{self.srs_of(mid)}\n\n"
                    f"Build the project and run the tests to verify it, then give your verdict.")
            out = run_claude(task, self.args, f"qa-{mid}-{self.qa_bounces.get(mid,0)}", self.work)
            verdict = "pass" if parse_verdict(out, "PASS", "FAIL") != "FAIL" else "bug"
            self.gc.comment(self.repo, pr["number"], out[:OUT_CAP] or "(no output)")
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
        ci_forced = self.ci_bounces.get(mid, 0) >= self.args.max_bounces
        qa_ok = qa_forced or any(c.get("body", "").strip().startswith(f"qa_passed@{sha[:12]}")
                                 for c in (self.gc.comments(self.repo, pr["number"]) or []))
        ci_ok = ci_forced or self.ci_done.get(sha) == "success"
        return qa_ok and ci_ok

    def merge_gate(self, pr):
        mid = self._mid_of_pr(pr)
        if not self._ready(pr):
            return
        # merge in Gitea (retry: mergeability is computed async → 405 until ready)
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
        # sync merged Gitea main into /testbed and tag → EvoClaw grades the tag
        git(self.testbed, "fetch", "gitea", "main")
        git(self.testbed, "reset", "--hard", "gitea/main")
        rc, _, err = git(self.testbed, "tag", f"agent-impl-{mid}")
        self.tagged.add(mid)
        log(f"[merge-gate] merged PR #{pr['number']} -> tag agent-impl-{mid} (rc={rc})")
        self.event(type="merged_and_tagged", milestone=mid, pr=pr["number"])

    def observer(self, pr):
        state = self._pr_state(pr)
        if state in (None, "ready-to-merge"):
            return
        try:
            updated = pr.get("updated_at", "")
            age_min = 0
            if updated:
                t = time.mktime(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))
                age_min = (time.time() - t) / 60.0
        except Exception:
            age_min = 0
        if age_min > self.args.stall_min:
            self.gc.comment(self.repo, pr["number"], f"STALL ALERT: {state} for ~{int(age_min)}min (Observer, no intervention)")
            self.event(type="stall_alert", pr=pr["number"], state=state, age_min=int(age_min))

    # --- pacer loop --------------------------------------------------
    def _safe(self, label, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # one bad action must NOT crash the controller (→ EvoClaw restart loop)
            log(f"[error] {label}: {str(e)[:200]}")
            self.event(type="action_error", label=label, error=str(e)[:200])

    def run(self):
        self.setup()
        idle = 0
        last_sig = None
        for _pass in range(self.args.max_passes):
            self._safe("sync_issues", self.sync_issues)
            prs = self.gc.list_prs(self.repo, state="open")
            issues = self.gc.list_issues(self.repo, labels=["evoclaw-task"], state="open")
            for pr in prs:
                st = self._pr_state(pr)
                if st in ("needs-code-changes:R", "needs-code-changes:Q"):
                    self._safe("dev_fix", self.dev_fix, pr, st)
            for iss in issues:
                if "has-pr" in self._labels(iss):
                    continue
                mid = next((m for m, n in self.issued.items() if n == iss["number"]), None)
                if mid and mid not in self.tagged:
                    self._safe("dev_open", self.dev_open, mid, iss["number"])
            for pr in self.gc.list_prs(self.repo, state="open"):
                self._safe("ci", self.run_ci, pr)
            # Red CI on an active PR → Dev fixes it (spec §3.1: don't bother Reviewer/QA). Budget-bounded:
            # once a milestone exhausts its bounce budget, force CI green so it can proceed (avoid stall).
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
                if self._pr_state(pr) == "needs-qa":
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
            # idle = a full pass that changed no Gitea state (robust vs idempotent no-op skips).
            # Include head sha so a Dev CI-fix (new commit, same label) counts as progress, not idle.
            now = self.gc.list_prs(self.repo, state="all")
            sig = (len(self.tagged),
                   tuple(sorted((p["number"], "|".join(sorted(self._labels(p))), self._head_sha(p)[:8])
                                for p in now)))
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
    ap.add_argument("--call-timeout", type=int, default=2400)
    ap.add_argument("--max-bounces", type=int, default=10)  # per-role cap (reviewer / qa / ci each)
    ap.add_argument("--max-passes", type=int, default=200)
    ap.add_argument("--max-idle", type=int, default=16)
    ap.add_argument("--poll-secs", type=int, default=10)
    ap.add_argument("--stall-min", type=int, default=30)
    args = ap.parse_args()
    log(f"controller start: model={args.model} effort={args.effort} trial={args.trial} "
        f"gitea={os.environ.get('GITEA_URL')}")
    Controller(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
