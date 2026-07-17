import subprocess
from pathlib import Path

import harness.e2e.evaluator as evaluator_module
from harness.e2e.config import E2EConfig
from harness.e2e.evaluator import PatchEvaluator


def _evaluator(monkeypatch, compile_results):
    evaluator = object.__new__(PatchEvaluator)
    evaluator.milestone_id = "M001"
    evaluator.build_failure_fail_closed = True
    evaluator._eval_meta = {
        "base_tag": "",
        "fallback_triggered": False,
        "end_compile_error": "",
        "start_compile_error": "",
        "build_failure_diagnostics": [],
    }
    monkeypatch.setattr(evaluator, "_checkout_to_tag", lambda suffix: (True, ""))
    evaluator.apply_calls = []
    monkeypatch.setattr(
        evaluator,
        "_apply_tar_to_container",
        lambda base_suffix, gt_test_suffix: evaluator.apply_calls.append(
            (base_suffix, gt_test_suffix)
        ) or (True, ""),
    )
    results = iter(compile_results)
    monkeypatch.setattr(evaluator, "_check_compilation", lambda: next(results))
    return evaluator


def test_compile_failure_on_both_bases_fails_closed(monkeypatch):
    evaluator = _evaluator(
        monkeypatch,
        [(False, "END compile error"), (False, "START compile error")],
    )

    success, error = evaluator._apply_tar_with_fallback()

    assert success is False
    assert "START compile error" in error
    assert "END compile error" in error
    assert evaluator._eval_meta["base_tag"] == "milestone-M001-start"
    assert evaluator._eval_meta["fallback_triggered"] is True
    assert evaluator._eval_meta["end_compile_error"] == "END compile error"
    assert evaluator.apply_calls == [("end", "end"), ("start", "end")]


def test_start_fallback_can_succeed_after_end_compile_failure(monkeypatch):
    evaluator = _evaluator(
        monkeypatch,
        [(False, "END compile error"), (True, "")],
    )

    success, error = evaluator._apply_tar_with_fallback()

    assert success is True
    assert error == ""
    assert evaluator._eval_meta["base_tag"] == "milestone-M001-start"
    assert evaluator._eval_meta["end_compile_error"] == "END compile error"
    assert evaluator.apply_calls == [("end", "end"), ("start", "end")]


def test_compile_failure_compatibility_policy_continues_to_test_runner(monkeypatch):
    evaluator = _evaluator(
        monkeypatch,
        [(False, "END compile error"), (False, "START compile error")],
    )
    evaluator.build_failure_fail_closed = False

    success, error = evaluator._apply_tar_with_fallback()

    assert success is True
    assert error == ""
    assert evaluator._eval_meta["start_compile_error"] == "START compile error"
    assert "START compile error" in evaluator._eval_meta["build_failure_diagnostics"][0]


def test_build_failure_policy_config_defaults_compatible_and_accepts_strict(tmp_path: Path):
    assert E2EConfig().build_failure_fail_closed is False

    config_path = tmp_path / "e2e_config.yaml"
    config_path.write_text("evaluation:\n  build_failure_fail_closed: true\n")
    assert E2EConfig(config_path).build_failure_fail_closed is True


def test_compilation_error_anchors_at_real_go_fatal(monkeypatch):
    evaluator = object.__new__(PatchEvaluator)
    evaluator.repo_config = {"build_command": "go build -v ./..."}
    evaluator.container_name = "eval-container"

    output = "\n".join(
        [
            "errors",
            "oserror",
            "github.com/example/project/core",
            "core/service.go:42:7: undefined: missingSymbol",
            "core/service.go:43:2: too many errors",
        ]
    )
    monkeypatch.setattr(
        evaluator_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, output, ""),
    )

    success, error = evaluator._check_compilation()

    assert success is False
    assert "core/service.go:42:7: undefined: missingSymbol" in error
    assert "Compilation failed (exit 1)" in error
    assert "\nerrors\n" not in error
