"""Regression tests for E2E result discovery."""

import json

import harness.e2e.collect_results as collect_results
from harness.e2e.collect_results import (
    calculate_precision_recall,
    calculate_score,
    calculate_score_reliable,
    calculate_score_v2,
    find_milestones_e2e,
    get_status,
    is_infra_invalid,
    is_resolved,
    is_zero_test_build_failure,
    load_e2e_results,
)


def test_find_milestones_e2e_accepts_repository_defined_ids(tmp_path):
    trial = "trial_001"
    evaluation = tmp_path / "e2e_trial" / trial / "evaluation"

    result_dirs = [
        "M001",
        "milestone_G01_48bca0a",
        "milestone_004",
        "maintenance_ui_ux",
        "maintenance_ui_ux-retry2",
    ]
    for milestone_id in result_dirs:
        milestone_dir = evaluation / milestone_id
        milestone_dir.mkdir(parents=True)
        (milestone_dir / "evaluation_result.json").write_text("{}", encoding="utf-8")

    # A milestone-like directory without a result is an incomplete attempt and
    # must not be discovered as an evaluated milestone.
    (evaluation / "milestone_incomplete" / "log").mkdir(parents=True)

    assert set(find_milestones_e2e(tmp_path, [trial])) == {
        "M001",
        "milestone_G01_48bca0a",
        "milestone_004",
        "maintenance_ui_ux",
    }


def test_find_milestones_e2e_combines_summary_and_result_directories(tmp_path):
    trial = "trial_001"
    evaluation = tmp_path / "e2e_trial" / trial / "evaluation"
    evaluation.mkdir(parents=True)
    (evaluation / "summary.json").write_text(
        json.dumps({"results": {"from_summary-retry1": {}}}),
        encoding="utf-8",
    )

    result_dir = evaluation / "from_result_file"
    result_dir.mkdir()
    (result_dir / "evaluation_result.json").write_text("{}", encoding="utf-8")

    assert set(find_milestones_e2e(tmp_path, [trial])) == {
        "from_summary",
        "from_result_file",
    }


def test_is_resolved_rejects_legacy_zero_test_result():
    assert not is_resolved(
        {
            "resolved": True,
            "test_summary": {"total": 0},
        }
    )


def test_zero_tests_with_required_tests_is_infra_invalid_but_scored_zero():
    result = {
        "resolved": True,
        "test_summary": {
            "total": 0,
            "fail_to_pass_required": 1,
            "none_to_pass_required": 0,
            "pass_to_pass_required": 3,
        },
    }

    assert is_infra_invalid(result)
    assert not is_resolved(result)
    assert get_status(result) == "🚫 Infra-invalid"
    assert calculate_score(result) == 0.0
    assert calculate_score_v2(result) == 0.0
    assert calculate_score_reliable(result) == 0.0
    assert calculate_precision_recall(result) == (0.0, 0.0)


def test_zero_test_compilation_failure_is_scored_zero_and_kept_in_denominator():
    result = {
        "resolved": False,
        "infra_invalid": True,  # legacy evaluator output must be corrected
        "infra_invalid_reason": "zero-tests-with-required-tests",
        "start_compile_error": "Compilation failed (exit 1): cannot find symbol",
        "test_summary": {
            "total": 0,
            "none_to_pass_required": 2,
            "pass_to_pass_required": 3,
        },
    }

    assert is_zero_test_build_failure(result)
    assert not is_infra_invalid(result)
    assert get_status(result) == "❌ Build failed"
    assert calculate_score(result) == 0.0
    assert calculate_score_v2(result) == 0.0
    assert calculate_score_reliable(result) == 0.0
    assert calculate_precision_recall(result) == (0.0, 0.0)


def test_zero_test_rust_compile_error_from_failure_path_is_scored_zero():
    result = {
        "resolved": False,
        "infra_invalid": True,
        "infra_invalid_reason": "zero-tests-with-required-tests",
        "error_message": "No valid report\nerror[E0599]: no method named `format` found",
        "test_summary": {"total": 0, "pass_to_pass_required": 1099},
    }

    assert is_zero_test_build_failure(result)
    assert not is_infra_invalid(result)
    assert calculate_score_reliable(result) == 0.0


def test_zero_test_timeout_without_compile_evidence_stays_infra_invalid():
    result = {
        "resolved": False,
        "infra_invalid": True,
        "infra_invalid_reason": "zero-tests-with-required-tests",
        "error_message": "outer test runner returned -1; timed out after 3600 seconds",
        "test_summary": {"total": 0, "pass_to_pass_required": 10},
    }

    assert not is_zero_test_build_failure(result)
    assert is_infra_invalid(result)
    assert calculate_score_reliable(result) == 0.0


def test_not_run_with_required_tests_remains_pending():
    result = {
        "eval_status": "not_run",
        "test_summary": {"total": 0, "pass_to_pass_required": 3},
    }

    assert not is_infra_invalid(result)
    assert get_status(result) == "⏳ Not run"


def test_load_e2e_results_corrects_legacy_zero_test_status(tmp_path):
    trial = "trial_001"
    evaluation = tmp_path / "e2e_trial" / trial / "evaluation"
    milestone_dir = evaluation / "M003"
    milestone_dir.mkdir(parents=True)

    raw = {
        "resolved": False,
        "test_summary": {"total": 0, "pass_to_pass_required": 0},
    }
    legacy_filtered = {
        "resolved": True,
        "test_summary": {"total": 0, "pass_to_pass_required": -52},
    }
    (milestone_dir / "evaluation_result.json").write_text(json.dumps(raw), encoding="utf-8")
    (milestone_dir / "evaluation_result_filtered.json").write_text(
        json.dumps(legacy_filtered), encoding="utf-8"
    )
    (evaluation / "summary.json").write_text(
        json.dumps(
            {
                "results": {
                    "M003": {
                        "eval_status": "passed",
                        "test_summary": legacy_filtered["test_summary"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    results, _ = load_e2e_results(tmp_path, trial)

    assert results["M003"]["eval_status"] == "failed"


def test_load_e2e_results_preserves_infra_invalid_status(tmp_path):
    trial = "trial_001"
    evaluation = tmp_path / "e2e_trial" / trial / "evaluation"
    milestone_dir = evaluation / "M004"
    milestone_dir.mkdir(parents=True)

    result = {
        "resolved": False,
        "infra_invalid": True,
        "infra_invalid_reason": "zero-tests-with-required-tests",
        "test_summary": {"total": 0, "none_to_pass_required": 2},
    }
    (milestone_dir / "evaluation_result.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    (evaluation / "summary.json").write_text(
        json.dumps({"results": {"M004": {"eval_status": "failed"}}}),
        encoding="utf-8",
    )

    results, _ = load_e2e_results(tmp_path, trial)

    assert results["M004"]["eval_status"] == "infra-invalid"
    assert results["M004"]["infra_invalid"] is True
    assert results["M004"]["infra_invalid_reason"] == "zero-tests-with-required-tests"


def test_load_e2e_results_normalizes_legacy_compile_invalid_to_scored_failure(tmp_path):
    trial = "trial_001"
    evaluation = tmp_path / "e2e_trial" / trial / "evaluation"
    milestone_dir = evaluation / "M005"
    milestone_dir.mkdir(parents=True)

    result = {
        "resolved": False,
        "infra_invalid": True,
        "infra_invalid_reason": "zero-tests-with-required-tests",
        "start_compile_error": "Compilation failed: cannot find symbol",
        "test_summary": {"total": 0, "none_to_pass_required": 2},
    }
    (milestone_dir / "evaluation_result.json").write_text(
        json.dumps(result), encoding="utf-8"
    )

    results, _ = load_e2e_results(tmp_path, trial)

    normalized = results["M005"]
    assert normalized["eval_status"] == "failed"
    assert normalized["infra_invalid"] is False
    assert normalized["infra_invalid_reason"] == ""
    assert normalized["scored_failure_reason"] == "build-failure-with-zero-tests"


def test_load_e2e_results_uses_authoritative_raw_result_over_stale_summary(tmp_path):
    trial = "trial_001"
    evaluation = tmp_path / "e2e_trial" / trial / "evaluation"
    milestone_dir = evaluation / "M006"
    milestone_dir.mkdir(parents=True)

    stale_summary_result = {
        "eval_status": "passed",
        "resolved": True,
        "tests_status": {"PASS_TO_PASS": {"success_count": 99, "missing": 0}},
        "test_summary": {
            "total": 99,
            "fail_to_pass_required": 1,
            "fail_to_pass_achieved": 1,
            "pass_to_pass_required": 99,
            "pass_to_pass_achieved": 99,
        },
    }
    raw_result = {
        "resolved": False,
        "tests_status": {"PASS_TO_PASS": {"success_count": 1, "missing": 0}},
        "test_summary": {
            "total": 2,
            "fail_to_pass_required": 1,
            "fail_to_pass_achieved": 0,
            "pass_to_pass_required": 1,
            "pass_to_pass_achieved": 1,
            "pass_to_pass_failed": 0,
            "pass_to_pass_missing": 0,
        },
    }
    (milestone_dir / "evaluation_result.json").write_text(
        json.dumps(raw_result), encoding="utf-8"
    )
    (evaluation / "summary.json").write_text(
        json.dumps({"results": {"M006": stale_summary_result}}),
        encoding="utf-8",
    )

    results, result_type_counts = load_e2e_results(
        tmp_path, trial, prefer_filtered=False
    )

    assert results["M006"]["eval_status"] == "failed"
    assert results["M006"]["test_summary"] == raw_result["test_summary"]
    assert results["M006"]["tests_status"] == raw_result["tests_status"]
    assert result_type_counts == {"filtered": 0, "unfiltered": 1}


def test_repo_summary_keeps_infra_invalid_in_all_graded_denominators(
    tmp_path, monkeypatch
):
    passing = {
        "resolved": True,
        "eval_status": "passed",
        "test_summary": {
            "total": 2,
            "fail_to_pass_required": 1,
            "fail_to_pass_achieved": 1,
            "none_to_pass_required": 0,
            "none_to_pass_achieved": 0,
            "pass_to_pass_required": 1,
            "pass_to_pass_achieved": 1,
            "pass_to_pass_failed": 0,
            "pass_to_pass_missing": 0,
        },
    }
    infra_invalid = {
        "resolved": False,
        "eval_status": "infra-invalid",
        "infra_invalid": True,
        "infra_invalid_reason": "zero-tests-with-required-tests",
        "test_summary": {
            "total": 0,
            "fail_to_pass_required": 1,
            "pass_to_pass_required": 1,
        },
    }
    compared = {
        "M001": {"result": passing},
        "M002": {"result": infra_invalid},
    }

    monkeypatch.setattr(
        collect_results, "load_selected_milestones", lambda _root: ({"M001", "M002"}, None)
    )
    monkeypatch.setattr(
        collect_results, "load_non_graded_milestones", lambda _root: set()
    )
    monkeypatch.setattr(
        collect_results,
        "compare_trials",
        lambda _root, _trials, _prefer_filtered: (compared, {}),
    )

    summary = collect_results.compute_repo_summary(
        tmp_path, ["trial_001"], trial_type="mstone"
    )

    assert summary["graded"] == 2
    assert summary["scoreable"] == 2
    assert summary["infra_invalid"] == 1
    # Infra-invalid remains visible as a diagnostic, but an emitted result is
    # still an evaluation attempt rather than an unevaluated milestone.
    assert summary["evaluated"] == 2
    assert summary["resolved"] == 1
    assert summary["resolve_pct"] == 50.0
    assert summary["score_1000"] == 50.0
    assert summary["score_full"] == 50.0
    assert summary["score_reliable"] == 50.0
    assert summary["precision"] == 50.0
    assert summary["recall"] == 50.0
