"""Regression tests for filtered evaluation invariants."""

from copy import deepcopy

from harness.e2e.evaluator import filter_evaluation_result


def _empty_failed_result():
    return {
        "resolved": False,
        "patch_status": {"compilation_success": False},
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": []},
            "NONE_TO_PASS": {"success": [], "failure": []},
            "PASS_TO_PASS": {"failure": [], "success_count": 0},
        },
        "test_summary": {
            "total": 0,
            "fail_to_pass_required": 0,
            "fail_to_pass_achieved": 0,
            "none_to_pass_required": 0,
            "none_to_pass_achieved": 0,
            "pass_to_pass_required": 0,
            "pass_to_pass_achieved": 0,
            "pass_to_pass_failed": 0,
            "pass_to_pass_missing": 0,
        },
    }


def test_zero_test_result_cannot_become_resolved_or_negative():
    raw = _empty_failed_result()
    original = deepcopy(raw)
    filter_list = {
        "invalid_fail_to_pass": [],
        "invalid_none_to_pass": [],
        "invalid_pass_to_pass": [f"test_{i}" for i in range(52)],
    }

    filtered = filter_evaluation_result(raw, filter_list)

    assert raw == original  # filtering remains non-mutating
    assert filtered["resolved"] is False
    assert filtered["test_summary"]["pass_to_pass_required"] == 0
    assert filtered["test_summary"]["pass_to_pass_achieved"] == 0
    assert filtered["filter_stats"]["pass_to_pass_filtered"] == 0
    assert filtered["filter_stats"]["invalid_p2p_count"] == 52


def test_invalid_p2p_count_cannot_drive_required_below_zero():
    raw = {
        "resolved": False,
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": []},
            "NONE_TO_PASS": {"success": [], "failure": []},
            "PASS_TO_PASS": {"failure": ["invalid_1"], "success_count": 0},
        },
        "test_summary": {
            "total": 1,
            "fail_to_pass_required": 0,
            "fail_to_pass_achieved": 0,
            "none_to_pass_required": 0,
            "none_to_pass_achieved": 0,
            "pass_to_pass_required": 1,
            "pass_to_pass_achieved": 0,
            "pass_to_pass_failed": 1,
            "pass_to_pass_missing": 0,
        },
    }
    filter_list = {
        "invalid_fail_to_pass": [],
        "invalid_none_to_pass": [],
        "invalid_pass_to_pass": ["invalid_1", "not_in_this_result"],
    }

    filtered = filter_evaluation_result(raw, filter_list)

    assert filtered["test_summary"]["pass_to_pass_required"] == 0
    assert filtered["test_summary"]["pass_to_pass_achieved"] == 0
    assert filtered["test_summary"]["pass_to_pass_failed"] == 0


def test_filtered_p2p_missing_is_synchronized_in_both_serialized_views():
    raw = {
        "resolved": False,
        "tests_status": {
            "FAIL_TO_PASS": {"success": [], "failure": []},
            "NONE_TO_PASS": {"success": [], "failure": []},
            "PASS_TO_PASS": {
                "failure": [],
                "success_count": 1,
                "missing": 2,
            },
        },
        "test_summary": {
            "total": 3,
            "fail_to_pass_required": 0,
            "fail_to_pass_achieved": 0,
            "none_to_pass_required": 0,
            "none_to_pass_achieved": 0,
            "pass_to_pass_required": 3,
            "pass_to_pass_achieved": 1,
            "pass_to_pass_failed": 0,
            "pass_to_pass_missing": 2,
        },
    }
    filter_list = {
        "invalid_fail_to_pass": [],
        "invalid_none_to_pass": [],
        "invalid_pass_to_pass": ["invalid_missing_test"],
    }

    filtered = filter_evaluation_result(
        raw, filter_list, ran_test_ids={"valid_passing_test"}
    )

    assert filtered["test_summary"]["pass_to_pass_required"] == 2
    assert filtered["test_summary"]["pass_to_pass_missing"] == 1
    assert filtered["test_summary"]["pass_to_pass_achieved"] == 1
    assert filtered["tests_status"]["PASS_TO_PASS"]["missing"] == 1
    assert filtered["tests_status"]["PASS_TO_PASS"]["success_count"] == 1
