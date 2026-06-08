#!/usr/bin/env python3
"""In-container multi-role runner for the EvoHarness "harnessed" arm.

Runs INSIDE the EvoClaw /testbed container (launched by HarnessedFramework.build_run_command).
For each available milestone it drives a Dev -> Reviewer -> QA refinement pipeline, each role a
separate `claude` invocation conditioned by a role prompt, then submits the result by creating the
`git tag agent-impl-<mid>` that EvoClaw's watcher + grader score — exactly the same completion
signal the bare single-agent arm uses, so the A/B is fair.

Coordination bus = the container's local git + the role prompts (no Gitea, no network): the shared
state each role reads/writes is the /testbed working tree. The pipeline ALWAYS tags at the end
(submit best effort); Review/QA drive iteration to improve quality, they do not block submission.

stdlib only (the container ships /usr/bin/python3). Heavy logging to stdout, which agent_runner
captures to agent_stdout.txt.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

DIFF_CAP = 40000   # chars of diff shown to the Reviewer
SRS_CAP = 20000    # chars of SRS shown to a role
OUT_CAP = 16000    # chars of a role's prior output fed back to Dev


def log(msg):
    print(f"[harnessed {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def git(workdir, *args):
    r = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def git_commit_all(workdir, message):
    git(workdir, "add", "-A")
    r = subprocess.run(
        ["git", "-C", workdir, "-c", "user.name=harnessed", "-c",
         "user.email=harnessed@evoclaw", "commit", "-m", message],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        log(f"committed: {message}")
    elif "nothing to commit" in (r.stdout + r.stderr).lower():
        log(f"(nothing to commit for: {message})")
    else:
        log(f"commit warn ({message}): {r.stderr.strip()[:200]}")


def tag_exists(workdir, mid):
    _, out, _ = git(workdir, "tag", "-l", f"agent-impl-{mid}")
    return bool(out.strip())


def run_claude(prompt_text, args, label):
    """One claude invocation reading prompt_text on stdin; returns the result text."""
    work = "/tmp/harnessed_work"
    os.makedirs(work, exist_ok=True)
    pf = os.path.join(work, f"{label}.prompt.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    cmd = ["claude", "--model", args.model, "--output-format", "json",
           "--dangerously-skip-permissions", "--session-id", str(uuid.uuid4())]
    if args.effort:
        cmd += ["--effort", args.effort]
    log(f"-> claude [{label}] ({len(prompt_text)} chars in)")
    t0 = time.time()
    try:
        with open(pf, encoding="utf-8") as fin:
            r = subprocess.run(cmd, stdin=fin, capture_output=True, text=True,
                               cwd=args.workdir, timeout=args.call_timeout)
    except subprocess.TimeoutExpired:
        log(f"<- claude [{label}] TIMEOUT after {args.call_timeout}s")
        return ""
    dt = int(time.time() - t0)
    result = r.stdout
    try:
        data = json.loads(r.stdout)
        if isinstance(data, dict) and "result" in data:
            result = data["result"]
    except (ValueError, TypeError):
        pass
    if r.returncode != 0:
        log(f"<- claude [{label}] rc={r.returncode} in {dt}s; stderr: {r.stderr.strip()[:300]}")
    else:
        log(f"<- claude [{label}] ok in {dt}s ({len(result)} chars out)")
    return result or ""


def parse_verdict(text, positive, negative):
    """Return 'positive'/'negative' from the last 'VERDICT:' line, else None."""
    found = None
    for line in text.splitlines():
        s = line.strip().upper()
        if s.startswith("VERDICT:"):
            if positive in s:
                found = positive
            elif negative in s:
                found = negative
    return found


def extract_src_dirs(base_prompt):
    for line in base_prompt.splitlines():
        if "Source Code" in line and ":" in line:
            val = line.split(":", 1)[1].strip().strip("`* ")
            if val:
                return val
    return "the repository source directories"


def role_prompt(roles_dir, role):
    return read_file(os.path.join(roles_dir, f"{role}.md")).strip()


def process_milestone(mid, srs, args, roles, src_dirs):
    log(f"================ MILESTONE {mid} ================")
    wd = args.workdir
    _, base_commit, _ = git(wd, "rev-parse", "HEAD")
    srs = srs[:SRS_CAP]

    # --- Dev: implement ---
    dev_role = role_prompt(args.roles_dir, "dev")
    dev_task = (
        f"{dev_role}\n\n## Milestone to implement: {mid}\n"
        f"Graded source directories: {src_dirs}\n\n## Requirement (SRS)\n{srs}\n\n"
        f"Implement this now in {wd}, build/run the relevant tests, and commit your work. "
        f"Do NOT create any git tag."
    )
    run_claude(dev_task, args, f"dev-{mid}-0")
    git_commit_all(wd, f"{mid}: dev implementation")

    # --- Review loop ---
    if "reviewer" in roles:
        rev_role = role_prompt(args.roles_dir, "reviewer")
        for it in range(args.max_review_iters + 1):
            _, diff, _ = git(wd, "diff", base_commit, "HEAD")
            review_task = (
                f"{rev_role}\n\n## Milestone under review: {mid}\n## Requirement (SRS)\n{srs}\n\n"
                f"## Proposed change (git diff)\n```diff\n{diff[:DIFF_CAP]}\n```\n\nGive your verdict."
            )
            out = run_claude(review_task, args, f"review-{mid}-{it}")
            if parse_verdict(out, "APPROVE", "REQUEST_CHANGES") != "REQUEST_CHANGES":
                log(f"[review] approved/neutral @ iter {it}")
                break
            if it >= args.max_review_iters:
                log("[review] budget exhausted — proceeding to QA")
                break
            fix = (
                f"{dev_role}\n\n## Milestone: {mid}\nThe Reviewer requested changes:\n\n"
                f"{out[:OUT_CAP]}\n\nAddress them in {wd} and commit. Do NOT create any git tag."
            )
            run_claude(fix, args, f"dev-{mid}-rev{it}")
            git_commit_all(wd, f"{mid}: address review {it}")

    # --- QA loop ---
    if "qa" in roles:
        qa_role = role_prompt(args.roles_dir, "qa")
        for it in range(args.max_qa_iters + 1):
            qa_task = (
                f"{qa_role}\n\n## Milestone: {mid}\n## Requirement (SRS)\n{srs}\n\n"
                f"Build and run the project's tests in {wd} to verify the current implementation, "
                f"then give your verdict."
            )
            out = run_claude(qa_task, args, f"qa-{mid}-{it}")
            if parse_verdict(out, "PASS", "FAIL") != "FAIL":
                log(f"[qa] pass/neutral @ iter {it}")
                break
            if it >= args.max_qa_iters:
                log("[qa] budget exhausted — submitting best effort")
                break
            fix = (
                f"{dev_role}\n\n## Milestone: {mid}\nQA found failures:\n\n{out[:OUT_CAP]}\n\n"
                f"Fix them in {wd} and commit. Do NOT create any git tag."
            )
            run_claude(fix, args, f"dev-{mid}-qa{it}")
            git_commit_all(wd, f"{mid}: address QA {it}")

    # --- Submit (always) ---
    git_commit_all(wd, f"{mid}: finalize")
    rc, _, err = git(wd, "tag", f"agent-impl-{mid}")
    if rc == 0:
        log(f"[submit] TAGGED agent-impl-{mid}")
    else:
        log(f"[submit] tag failed for {mid}: {err[:200]}")


def read_queue(workspace):
    text = read_file(os.path.join(workspace, "TASK_QUEUE.md"))
    mids = [m.group(1) for m in (re.match(r"^- (\S+):", ln.strip()) for ln in text.splitlines()) if m]
    return mids, ("No tasks currently available" in text)


def srs_for(workspace, mid):
    p = os.path.join(workspace, "srs", f"{mid}_SRS.md")
    return read_file(p) if os.path.exists(p) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--session-base", default="harnessed")
    ap.add_argument("--base-prompt", default="")
    ap.add_argument("--roles-dir", required=True)
    ap.add_argument("--workspace", default="/e2e_workspace")
    ap.add_argument("--workdir", default="/testbed")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--max-review-iters", type=int, default=1)
    ap.add_argument("--max-qa-iters", type=int, default=1)
    ap.add_argument("--call-timeout", type=int, default=1800)
    ap.add_argument("--poll-secs", type=int, default=15)
    ap.add_argument("--max-idle-polls", type=int, default=16)
    ap.add_argument("--max-milestones", type=int, default=50)
    ap.add_argument("--roles", default="dev,reviewer,qa")
    args = ap.parse_args()

    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    base_prompt = read_file(args.base_prompt) if args.base_prompt else ""
    src_dirs = extract_src_dirs(base_prompt)
    log(f"runner start: model={args.model} effort={args.effort} roles={roles} "
        f"review_iters={args.max_review_iters} qa_iters={args.max_qa_iters} src_dirs={src_dirs!r}")

    processed = set()
    idle = 0
    while len(processed) < args.max_milestones:
        mids, no_more = read_queue(args.workspace)
        todo = [m for m in mids if m not in processed
                and not tag_exists(args.workdir, m) and srs_for(args.workspace, m)]
        if todo:
            idle = 0
            for mid in todo:
                process_milestone(mid, srs_for(args.workspace, mid), args, roles, src_dirs)
                processed.add(mid)
            continue
        if no_more and all(m in processed or tag_exists(args.workdir, m) for m in mids):
            log("queue drained — done")
            break
        idle += 1
        if idle >= args.max_idle_polls:
            log(f"idle limit ({args.max_idle_polls}) reached — exiting")
            break
        log(f"no new milestones; idle poll {idle}/{args.max_idle_polls}")
        time.sleep(args.poll_secs)

    log(f"runner done; processed {len(processed)} milestone(s): {sorted(processed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
