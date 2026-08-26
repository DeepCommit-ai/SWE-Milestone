"""Regression tests for the compatibility-mode Go package narrowing path.

The `go list` inventory command and its row parser must agree on the column
separator.  A previous regression embedded a literal backslash-t in the
template's plain text (Go templates do not escape-process literal text), so
every row failed the tab partition and the narrowing fallback could never
succeed (glm-5.2-1m_001 go-zero M014/M022/M028).
"""

import subprocess

import harness.e2e.evaluator as evaluator_module
from harness.e2e.evaluator import PatchEvaluator


def _filter_evaluator():
    evaluator = object.__new__(PatchEvaluator)
    evaluator.container_name = "eval-container"
    evaluator._eval_meta = {}
    evaluator._go_exec_env = {}
    evaluator._go_test_import_owners = {
        "example.com/mod/core/a": {"./core/a"},
    }
    return evaluator


def _run_filter(evaluator):
    return evaluator._configure_partial_go_test_package_filter(
        workdir="/testbed",
        base_env={},
        unsafe_test_imports={"example.com/mod/core/a"},
    )


def test_go_list_command_and_parser_agree_on_tab(monkeypatch):
    """The template renders the tab via Go printf; the parser splits on it."""
    commands = []

    def go_exec(command, **_kwargs):
        commands.append(command)
        rows = [
            "example.com/mod/core/a\t/testbed/core/a",
            "example.com/mod/core/b\t/testbed/core/b",
            "example.com/mod\t/testbed",
        ]
        return subprocess.CompletedProcess([], 0, "\n".join(rows), "")

    evaluator = _filter_evaluator()
    monkeypatch.setattr(evaluator, "_go_exec", go_exec, raising=False)
    monkeypatch.setattr(
        evaluator_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    ok, error = _run_filter(evaluator)

    assert ok, error
    # The separator must be rendered by Go's own template engine — a raw
    # backslash-t in template literal text is emitted verbatim, never a tab.
    assert '{{printf "%s\\t%s" .ImportPath .Dir}}' in commands[0]
    assert evaluator._eval_meta["go_partial_package_filter_applied"] is True
    assert evaluator._eval_meta["go_partial_package_filter_excluded"] == ["./core/a"]
    assert evaluator._eval_meta["go_partial_package_filter_included"] == 2
    assert (
        evaluator._go_exec_env["EVOCLAW_GO_TEST_PACKAGE_FILE"]
        == "/tmp/evoclaw-safe-test-packages"
    )


def test_go_list_literal_backslash_t_rows_fail_closed(monkeypatch):
    """Rows using a literal backslash-t (the old bug) must fail, not misparse."""

    def go_exec(_command, **_kwargs):
        rows = ["example.com/mod/core/a\\t/testbed/core/a"]
        return subprocess.CompletedProcess([], 0, "\n".join(rows), "")

    evaluator = _filter_evaluator()
    monkeypatch.setattr(evaluator, "_go_exec", go_exec, raising=False)

    ok, error = _run_filter(evaluator)

    assert not ok
    assert "cannot parse submitted package inventory" in error
    assert "malformed go list row" in error
