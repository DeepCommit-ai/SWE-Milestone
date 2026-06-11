"""Regression test for the hermetic CI gate (navidrome plugin.wasm mtime-mask incident).

Run with pytest, or standalone:  python harness/e2e/agents/harnessed_runtime/test_hermetic_ci_gate.py

The incident (navidrome three-arm post-mortem, 2026-06): the shared work clone accumulated a
GITIGNORED build artifact (plugins/testdata/*/plugin.wasm) whose Makefile rule depends only on
plugin.go's mtime. Born green at baseline (controller's _probe_main_ci + Dev onboarding ci.sh runs),
it was never rebuilt again — so an interface break in a DIFFERENT file (the wasip1-only 4-line
TimeNow wrapper miss) sailed through every in-tree CI run green and only went red in the eval
container's fresh checkout, after merge, when it was too late. Both harness arms hit it; the bare
arm escaped only because it ran its first real build AFTER its edits (cold tree -> red -> 26s fix).

This test rebuilds the trap in miniature and asserts the gate semantics:
  - OLD (in-tree) gate on the broken sha  -> FALSE GREEN (mask reproduced)
  - NEW _run_project_ci (ephemeral worktree) on the same sha -> RED
  - teardown leaves no worktree registered and no ci-gate-* dir behind
"""
import os
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(__file__))
import controller  # noqa: E402

FILES = {
    "lib.sh": "time_now() { date; }\n",                        # the "api" (navidrome: plugins/host/scheduler)
    "gen.sh": "echo generated\n",                              # the "plugin source" (navidrome: plugin.go)
    "Makefile": ("out.bin: gen.sh\n"                           # mtime dep on gen.sh ONLY — the trap
                 "\tbash -n lib.sh\n"
                 "\tgrep -q time_now lib.sh   # 'compile against the api'\n"
                 "\techo ok > out.bin\n"),
    ".gitignore": "out.bin\n",                                 # survives `git clean -fd`, like plugin.wasm
    "ci.sh": "#!/bin/sh\nset -e\nmake\n",
}


def _sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def test_hermetic_gate_catches_mtime_masked_break(tmp_path=None):
    base = str(tmp_path) if tmp_path else tempfile.mkdtemp(prefix="hermetic-ci-test-")
    origin, work = os.path.join(base, "origin.git"), os.path.join(base, "work")
    _sh(f"git init -q --bare {origin}")
    _sh(f"git clone -q {origin} {work}")
    _sh("git config user.email t@t && git config user.name t", cwd=work)
    for name, body in FILES.items():
        with open(os.path.join(work, name), "w") as f:
            f.write(body)
    _sh("chmod +x ci.sh && git add -A && git commit -qm baseline && git push -q origin HEAD:main", cwd=work)

    # t0: baseline in-tree CI (= _probe_main_ci / Dev onboarding) births out.bin GREEN
    rc, _ = _sh("sh ci.sh", cwd=work)
    assert rc == 0 and os.path.exists(os.path.join(work, "out.bin"))

    # t1: break the "api" in a file the Makefile rule does NOT depend on (the agent's miss)
    with open(os.path.join(work, "lib.sh"), "w") as f:
        f.write("some_other_fn() { :; }\n")
    _sh("git commit -qam 'break api' && git push -q origin HEAD:main", cwd=work)
    sha = _sh("git rev-parse HEAD", cwd=work)[1]

    # OLD gate semantics (_wc_checkout + in-tree ci.sh): the gitignored artifact vouches -> FALSE GREEN
    _sh("git fetch -q origin && git reset -q --hard && git clean -fdq && git checkout -qB main origin/main", cwd=work)
    rc_old, out_old = _sh("sh ci.sh", cwd=work)
    assert rc_old == 0, f"expected the in-tree gate to reproduce the FALSE GREEN, got rc={rc_old}: {out_old}"
    assert "up to date" in out_old

    # NEW gate: the real Controller._run_project_ci in an ephemeral worktree -> RED
    fake = types.SimpleNamespace(work=work, args=types.SimpleNamespace(call_timeout=60))
    ok_new, log_new = controller.Controller._run_project_ci(fake, sha)
    assert ok_new is False, f"hermetic gate must go RED on the broken sha, got green:\n{log_new}"

    # teardown: nothing left registered, nothing left on disk
    wt_lines = _sh("git worktree list", cwd=work)[1].splitlines()
    assert len(wt_lines) == 1, f"worktree leaked: {wt_lines}"
    leftovers = [d for d in os.listdir(tempfile.gettempdir()) if d.startswith("ci-gate-")]
    assert not leftovers, f"ci-gate dirs leaked: {leftovers}"


def _seed_repo(base):
    """origin + work clone with the FILES baseline committed; returns (origin, work, baseline_sha)."""
    origin, work = os.path.join(base, "origin.git"), os.path.join(base, "work")
    _sh(f"git init -q --bare {origin}")
    _sh(f"git clone -q {origin} {work}")
    _sh("git config user.email t@t && git config user.name t", cwd=work)
    for name, body in FILES.items():
        with open(os.path.join(work, name), "w") as f:
            f.write(body)
    _sh("chmod +x ci.sh && git add -A && git commit -qm baseline && git push -q origin HEAD:main", cwd=work)
    return origin, work, _sh("git rev-parse HEAD", cwd=work)[1]


def test_qa_worktree_is_fresh_each_call_and_pushes_tests():
    base = tempfile.mkdtemp(prefix="hermetic-qa-test-")
    _, work, sha = _seed_repo(base)
    fake = types.SimpleNamespace(work=work, args=types.SimpleNamespace(call_timeout=60))
    try:
        # shared clone polluted with the gitignored artifact (the trap)
        rc, _ = _sh("sh ci.sh", cwd=work)
        assert rc == 0 and os.path.exists(os.path.join(work, "out.bin"))

        # 1) QA's worktree is created at the exact sha and does NOT inherit the artifact
        controller.Controller._qa_checkout(fake, sha)
        assert _sh("git rev-parse HEAD", cwd=controller.QA_WORK)[1] == sha
        assert not os.path.exists(os.path.join(controller.QA_WORK, "out.bin")), \
            "QA worktree must not inherit gitignored artifacts from the shared clone"

        # 2) leftovers from a QA round are wiped by the next recreation
        with open(os.path.join(controller.QA_WORK, "out.bin"), "w") as f:
            f.write("stale artifact from a previous QA round")
        controller.Controller._qa_checkout(fake, sha)
        assert not os.path.exists(os.path.join(controller.QA_WORK, "out.bin")), \
            "recreation must wipe artifacts left by the previous QA round"

        # 3) tests QA commits in the worktree fast-forward onto the branch
        with open(os.path.join(controller.QA_WORK, "qa_added_test.sh"), "w") as f:
            f.write("echo qa test\n")
        _sh("git add -A && git -c user.name=qa -c user.email=qa@t commit -qm 'qa tests'",
            cwd=controller.QA_WORK)
        controller.Controller._qa_push(fake, "main")
        _sh("git fetch -q origin", cwd=work)
        rc, _ = _sh("git cat-file -e origin/main:qa_added_test.sh", cwd=work)
        assert rc == 0, "QA's committed test must reach the branch on origin"
    finally:
        _sh(f"git worktree remove --force {controller.QA_WORK}", cwd=work)
        _sh(f"rm -rf {controller.QA_WORK}")


if __name__ == "__main__":
    test_hermetic_gate_catches_mtime_masked_break()
    test_qa_worktree_is_fresh_each_call_and_pushes_tests()
    print("PASS: old gate = false green (mask reproduced); new hermetic gate = red; "
          "QA worktree fresh per call + tests reach the branch; clean teardown.")
