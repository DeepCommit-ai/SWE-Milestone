"""The compile gate has no warning-text waiver (issue #21).

`_check_compilation` used to convert a nonzero build exit into success
whenever the output carried a warning line and no line matched a narrow
error list. The premise was that npm exits nonzero for warnings; it does
not — a nonzero `npm ci` / `npm run build` means a real failure. In a
chained command (navidrome: npm build then `go build`) a routine
`npm WARN` line therefore waived genuine Go compiler errors, and the run
record then affirmatively stated that compilation succeeded.

The defining property tested here is that a failed process is a failed
build regardless of what it printed. Tests target `_check_compilation`, not
the removed private helper, so they keep holding whatever the internals
become.
"""

import subprocess

import pytest

import harness.e2e.evaluator as evaluator_module
from harness.e2e.evaluator import PatchEvaluator

NPM_WARNINGS = (
    "npm WARN deprecated inflight@1.0.6: This module is not supported\n"
    "npm WARN deprecated glob@7.2.3: no longer supported\n"
    "added 1423 packages in 41s\n"
)

GO_REDECLARED = (
    "# github.com/navidrome/navidrome/plugins\n"
    "plugins/wasm_base_plugin.go:28:6: loaderFunc redeclared in this block\n"
    "\tplugins/base_capability.go:29:6: other declaration of loaderFunc\n"
)

GO_NO_SUCH_FIELD = (
    "# github.com/navidrome/navidrome/server/nativeapi\n"
    "server/nativeapi/library.go:52:9: n.libs undefined "
    "(type *Router has no field or method libs)\n"
)


def _gate(monkeypatch, *, returncode, stdout="", stderr=""):
    evaluator = object.__new__(PatchEvaluator)
    evaluator.repo_config = {
        "build_command": (
            "cd /testbed/ui && npm ci && npm run build && "
            "cd /testbed && go build -tags=netgo ."
        )
    }
    evaluator.container_name = "eval-container"
    monkeypatch.setattr(
        evaluator_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, stdout, stderr),
    )
    return evaluator


class TestNoWarningWaiver:
    def test_nonzero_exit_with_only_npm_warnings_is_a_failure(self, monkeypatch):
        """The invariant that distinguishes deletion from narrowing: nothing
        about the *text* can rescue a nonzero exit."""
        evaluator = _gate(monkeypatch, returncode=1, stdout=NPM_WARNINGS)

        success, error = evaluator._check_compilation()

        assert success is False
        assert "exit 1" in error

    def test_zero_exit_with_npm_warnings_is_still_success(self, monkeypatch):
        evaluator = _gate(monkeypatch, returncode=0, stdout=NPM_WARNINGS)

        assert evaluator._check_compilation() == (True, "")

    @pytest.mark.parametrize(
        "go_error, needle",
        [
            (GO_REDECLARED, "loaderFunc redeclared in this block"),
            (GO_NO_SUCH_FIELD, "has no field or method libs"),
        ],
    )
    def test_go_errors_behind_npm_warnings_are_not_waived(
        self, monkeypatch, go_error, needle
    ):
        """Both forms issue #21 names. Neither matched the old error list:
        `redeclared` was absent, and the list's `undefined:` carries a colon
        that `x undefined (type ...)` does not have."""
        evaluator = _gate(
            monkeypatch, returncode=1, stdout=NPM_WARNINGS + go_error
        )

        success, error = evaluator._check_compilation()

        assert success is False
        assert needle in error

    def test_go_error_on_stderr_with_warnings_on_stdout(self, monkeypatch):
        """The gate concatenates the two streams; a build whose warnings and
        errors arrive on different pipes must still fail."""
        evaluator = _gate(
            monkeypatch,
            returncode=2,
            stdout=NPM_WARNINGS.rstrip("\n"),  # no trailing newline
            stderr=GO_REDECLARED,
        )

        success, error = evaluator._check_compilation()

        assert success is False
        assert "exit 2" in error

    def test_nonzero_exit_with_empty_output_is_a_failure(self, monkeypatch):
        evaluator = _gate(monkeypatch, returncode=137, stdout="", stderr="")

        success, error = evaluator._check_compilation()

        assert success is False
        assert "exit 137" in error

    def test_unrecognized_fatal_after_warnings_is_a_failure(self, monkeypatch):
        """A kill (OOM, timeout signal) prints no recognizable compiler
        diagnostic. Treating it as success was the most dangerous case."""
        evaluator = _gate(
            monkeypatch,
            returncode=137,
            stdout="warning: unused variable\nKilled\n",
        )

        success, error = evaluator._check_compilation()

        assert success is False
        assert "Killed" in error

    def test_no_build_command_still_skips_the_gate(self, monkeypatch):
        evaluator = object.__new__(PatchEvaluator)
        evaluator.repo_config = {}
        evaluator.container_name = "eval-container"

        assert evaluator._check_compilation() == (True, "")

    def test_timeout_is_still_reported_as_a_failure(self, monkeypatch):
        evaluator = _gate(monkeypatch, returncode=0)

        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="build", timeout=600)

        monkeypatch.setattr(evaluator_module.subprocess, "run", _raise)

        success, error = evaluator._check_compilation()

        assert success is False
        assert "timed out" in error

    def test_the_waiver_helper_is_gone(self):
        """Deletion, not narrowing: no private helper survives to be revived
        by a future 'just add one more pattern' change."""
        assert not hasattr(PatchEvaluator, "_is_npm_warning_only")


class TestNoUndefinedNames:
    def test_harness_has_no_undefined_names(self):
        """`main()`'s failed-result handler referenced a bare `self`, so any
        exception taking that path raised NameError *before* the failed
        result was written — the record of a build failure was lost exactly
        when it mattered. Static check because the path is hard to reach and
        easy to break again."""
        import shutil

        ruff = shutil.which("ruff")
        if ruff is None:
            pytest.skip("ruff not installed")

        proc = subprocess.run(
            [ruff, "check", "--select", "F821", "--output-format", "concise", "harness/"],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
