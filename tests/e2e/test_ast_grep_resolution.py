"""ast-grep resolution must not depend on the launcher's PATH.

Regression tests for the codex_gpt-5.6-sol_003/_004 incident: trials launched
from a shell without ``.venv/bin`` on PATH made every Rust evaluation fail
closed with ``No such file or directory: 'ast-grep'`` — hours after launch.
The ast-grep-cli wheel installs the binary next to the Python interpreter, so
resolution prefers that location, falls back to PATH, and run_e2e refuses to
start a trial when neither yields a usable binary.
"""

import inspect
import stat
import subprocess
import sys

import pytest

from harness.prepare_repo.split_test_patches import test_detector
from harness.prepare_repo.split_test_patches import verify_test_separation
from harness.prepare_repo.split_test_patches.test_detector import (
    RustTestDetectionError,
    ensure_ast_grep,
    resolve_ast_grep,
)


@pytest.fixture(autouse=True)
def _clear_resolution_cache():
    resolve_ast_grep.cache_clear()
    yield
    resolve_ast_grep.cache_clear()


def _fake_interpreter(tmp_path, *, with_ast_grep):
    """Create a fake venv bin dir; return its python path."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.touch()
    if with_ast_grep:
        tool = bindir / "ast-grep"
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return python


class TestResolveAstGrep:
    def test_prefers_binary_next_to_interpreter(self, tmp_path, monkeypatch):
        python = _fake_interpreter(tmp_path, with_ast_grep=True)
        monkeypatch.setattr(sys, "executable", str(python))
        monkeypatch.setattr(
            test_detector.shutil, "which", lambda name: "/elsewhere/ast-grep"
        )
        assert resolve_ast_grep() == str(python.parent / "ast-grep")

    def test_falls_back_to_path_lookup(self, tmp_path, monkeypatch):
        python = _fake_interpreter(tmp_path, with_ast_grep=False)
        monkeypatch.setattr(sys, "executable", str(python))
        monkeypatch.setattr(
            test_detector.shutil, "which", lambda name: "/elsewhere/ast-grep"
        )
        assert resolve_ast_grep() == "/elsewhere/ast-grep"

    def test_unresolved_keeps_bare_name_for_nonstrict_callers(
        self, tmp_path, monkeypatch
    ):
        python = _fake_interpreter(tmp_path, with_ast_grep=False)
        monkeypatch.setattr(sys, "executable", str(python))
        monkeypatch.setattr(test_detector.shutil, "which", lambda name: None)
        assert resolve_ast_grep() == "ast-grep"


class TestEnsureAstGrep:
    def test_returns_resolved_path(self, tmp_path, monkeypatch):
        python = _fake_interpreter(tmp_path, with_ast_grep=True)
        monkeypatch.setattr(sys, "executable", str(python))
        assert ensure_ast_grep() == str(python.parent / "ast-grep")

    def test_raises_when_no_binary_anywhere(self, tmp_path, monkeypatch):
        python = _fake_interpreter(tmp_path, with_ast_grep=False)
        monkeypatch.setattr(sys, "executable", str(python))
        monkeypatch.setattr(test_detector.shutil, "which", lambda name: None)
        with pytest.raises(RustTestDetectionError, match="ast-grep"):
            ensure_ast_grep()


class TestCallSitesUseResolvedBinary:
    def test_run_ast_grep_json_invokes_resolved_binary(self, tmp_path, monkeypatch):
        python = _fake_interpreter(tmp_path, with_ast_grep=True)
        monkeypatch.setattr(sys, "executable", str(python))
        seen = {}

        def fake_run(command, **kwargs):
            seen["argv0"] = command[0]
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        monkeypatch.setattr(test_detector.subprocess, "run", fake_run)
        test_detector._run_ast_grep_json(
            ["ast-grep", "run", "--pattern", "p", "--lang", "rust", "--json", "f.rs"],
            purpose="unit test",
        )
        assert seen["argv0"] == str(python.parent / "ast-grep")

    def test_verify_test_separation_invokes_resolved_binary(
        self, tmp_path, monkeypatch
    ):
        python = _fake_interpreter(tmp_path, with_ast_grep=True)
        monkeypatch.setattr(sys, "executable", str(python))
        argv0s = []

        def fake_run(command, **kwargs):
            argv0s.append(command[0])
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        monkeypatch.setattr(verify_test_separation.subprocess, "run", fake_run)
        verify_test_separation._find_test_ranges_with_ast_grep(
            "#[cfg(test)]\nmod tests {}\n", "x.rs"
        )
        assert argv0s, "expected at least one ast-grep invocation"
        assert set(argv0s) == {str(python.parent / "ast-grep")}


class TestRunE2ePreflight:
    def test_preflight_exits_when_ast_grep_missing(self, monkeypatch):
        from harness.e2e import run_e2e

        def boom():
            raise RustTestDetectionError("ast-grep not found")

        monkeypatch.setattr(run_e2e, "ensure_ast_grep", boom)
        with pytest.raises(SystemExit):
            run_e2e._preflight_ast_grep()

    def test_preflight_passes_when_ast_grep_present(self, tmp_path, monkeypatch):
        from harness.e2e import run_e2e

        python = _fake_interpreter(tmp_path, with_ast_grep=True)
        monkeypatch.setattr(sys, "executable", str(python))
        run_e2e._preflight_ast_grep()  # must not raise

    def test_main_wires_preflight_before_trial_setup(self):
        from harness.e2e import run_e2e

        assert "_preflight_ast_grep()" in inspect.getsource(run_e2e.main)
