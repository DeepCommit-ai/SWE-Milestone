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


# --- filter-list validation and the intersection rule (v1.0.2, R1) -----------

import json

import pytest

from harness.e2e.evaluator import (
    FILTER_LIST_SCHEMA_VERSION,
    classification_buckets,
    collect_ran_test_ids,
    generate_filtered_evaluation,
    validate_filter_list,
    validate_workspace_filter_lists,
)

A, B, C, D = "t/a.py::a", "t/b.py::b", "t/c.py::c", "t/d.py::d"
CLS = {"stable_classification": {"pass_to_pass": [A, B], "fail_to_pass": [C], "none_to_pass": [{"test_id": D}]},
       "classification": {"pass_to_pass": [A, B, "flaky"], "fail_to_pass": [C], "none_to_pass": [D]}}


def test_classification_buckets_use_the_stable_classification_and_accept_dict_entries():
    b = classification_buckets(CLS)
    assert b == {"pass_to_pass": {A, B}, "fail_to_pass": {C}, "none_to_pass": {D}}


def test_validate_filter_list_accepts_a_valid_list_in_both_entry_formats():
    fl = {"version": FILTER_LIST_SCHEMA_VERSION,
          "invalid_pass_to_pass": [A, {"test_id": B, "reason": "conflict"}],
          "invalid_fail_to_pass": [{"test_id": C, "reason": "x"}],
          "invalid_none_to_pass": [D]}
    assert validate_filter_list(fl, CLS, "M1") == []
    assert validate_filter_list({"invalid_pass_to_pass": []}, CLS) == []


@pytest.mark.parametrize("fl, needle", [
    ({"invalid_pass_to_pass": ["nope"]}, "not in the classification's pass_to_pass bucket"),
    ({"invalid_pass_to_pass": [C]}, "not in the classification's pass_to_pass bucket"),       # wrong bucket
    ({"invalid_fail_to_pass": [A]}, "fail_to_pass/none_to_pass bucket"),                    # wrong bucket
    ({"invalid_none_to_pass": [C]}, ""),                                                       # f2p/n2p merged: ok
    ({"invalid_pass_to_pass": [A, {"test_id": A}]}, "duplicate test id"),                    # within a bucket
    ({"invalid_pass_to_pass": [A], "invalid_fail_to_pass": [A]}, "duplicate test id"),       # across buckets
    ({"version": 2, "invalid_pass_to_pass": [A]}, "unsupported filter list version"),
    ({"invalid_pass_to_pass": [{"test_id": A, "condition": {"owner_before": "M0"}}]}, "conditional entries"),
    ({"invalid_pass_to_pass": [{"reason": "no id"}]}, "no string test_id"),
    ({"invalid_pass_to_pass": "A"}, "is not a list"),
])
def test_validate_filter_list_rejects_every_silent_misscoring_shape(fl, needle):
    errors = validate_filter_list(fl, CLS, "M1")
    if needle:
        assert errors and any(needle in e for e in errors), errors
        assert all(e.startswith("M1: ") for e in errors)
    else:
        assert errors == []


def _raw_two_p2p_one_failed():
    return {
        "resolved": False,
        "tests_status": {"FAIL_TO_PASS": {"success": [C], "failure": []},
                         "NONE_TO_PASS": {"success": [], "failure": []},
                         "PASS_TO_PASS": {"failure": [A], "success_count": 1, "missing": 0}},
        "test_summary": {"total": 3, "fail_to_pass_required": 1, "fail_to_pass_achieved": 1,
                         "none_to_pass_required": 0, "none_to_pass_achieved": 0,
                         "pass_to_pass_required": 2, "pass_to_pass_achieved": 1,
                         "pass_to_pass_failed": 1, "pass_to_pass_missing": 0},
    }


def test_p2p_universe_none_keeps_the_legacy_count_rule_and_a_universe_makes_it_an_intersection():
    fl = {"invalid_pass_to_pass": [A, "ghost-not-in-universe"]}
    legacy = filter_evaluation_result(_raw_two_p2p_one_failed(), fl, ran_test_ids={A, B, C})
    # legacy: both listed ids subtract from required although only A exists
    assert legacy["test_summary"]["pass_to_pass_required"] == 0
    assert legacy["filter_stats"]["invalid_p2p_count"] == 2
    strict = filter_evaluation_result(_raw_two_p2p_one_failed(), fl, ran_test_ids={A, B, C}, p2p_universe={A, B})
    assert strict["test_summary"]["pass_to_pass_required"] == 1
    assert strict["test_summary"]["pass_to_pass_achieved"] == 1
    assert strict["test_summary"]["pass_to_pass_failed"] == 0
    assert strict["filter_stats"]["invalid_p2p_count"] == 1
    assert strict["resolved"] is True
    # a valid list (every id in the universe) is byte-identical under both rules
    fl_ok = {"invalid_pass_to_pass": [A]}
    assert filter_evaluation_result(_raw_two_p2p_one_failed(), fl_ok, ran_test_ids={A, B, C}) == \
        filter_evaluation_result(_raw_two_p2p_one_failed(), fl_ok, ran_test_ids={A, B, C}, p2p_universe={A, B})


def test_p2p_universe_also_bounds_the_missing_adjustment():
    raw = _raw_two_p2p_one_failed()
    raw["tests_status"]["PASS_TO_PASS"] = {"failure": [], "success_count": 1, "missing": 1}
    raw["test_summary"].update({"pass_to_pass_failed": 0, "pass_to_pass_missing": 1, "pass_to_pass_achieved": 1})
    fl = {"invalid_pass_to_pass": ["ghost"]}  # never ran, not in the universe
    out = filter_evaluation_result(raw, fl, ran_test_ids={A, C}, p2p_universe={A, B})
    assert out["test_summary"]["pass_to_pass_missing"] == 1        # ghost must not "explain" B's absence
    assert out["test_summary"]["pass_to_pass_required"] == 2
    out_legacy = filter_evaluation_result(raw, fl, ran_test_ids={A, C})
    assert out_legacy["test_summary"]["pass_to_pass_missing"] == 0  # the legacy defect this guards against


def _workspace(tmp_path, filter_list, classification=CLS, mid="M1"):
    tr = tmp_path / "data" / "repo" / "test_results" / mid
    tr.mkdir(parents=True)
    (tr / f"{mid}_classification.json").write_text(json.dumps(classification))
    if filter_list is not None:
        (tr / f"{mid}_filter_list.json").write_text(json.dumps(filter_list) if not isinstance(filter_list, str) else filter_list)
    return tmp_path / "data" / "repo"


def test_validate_workspace_filter_lists_reports_per_milestone_and_unreadable_files(tmp_path):
    ws = _workspace(tmp_path, {"invalid_pass_to_pass": [A]})
    assert validate_workspace_filter_lists(ws) == {}
    ws2 = _workspace(tmp_path / "w2", {"invalid_pass_to_pass": ["nope"]})
    errs = validate_workspace_filter_lists(ws2)
    assert list(errs) == ["M1"] and "nope" in errs["M1"][0]
    ws3 = _workspace(tmp_path / "w3", "{not json")
    assert "unreadable filter list" in validate_workspace_filter_lists(ws3)["M1"][0]
    ws4 = _workspace(tmp_path / "w4", {"invalid_pass_to_pass": [A]})
    (ws4 / "test_results" / "M1" / "M1_classification.json").unlink()
    assert "cannot read" in validate_workspace_filter_lists(ws4)["M1"][0]
    assert validate_workspace_filter_lists(tmp_path / "nowhere") == {}


def _cell(tmp_path, raw, eval_jsons):
    cell = tmp_path / "cell"
    for pid, ids in eval_jsons.items():
        d = cell / "artifacts" / pid
        d.mkdir(parents=True)
        (d / "eval.json").write_text(json.dumps({"tests": [{"nodeid": i} for i in ids]}))
    (cell / "evaluation_result.json").write_text(json.dumps(raw))
    return cell


def test_collect_ran_test_ids_union_and_none_without_artifacts(tmp_path):
    cell = _cell(tmp_path, _raw_two_p2p_one_failed(), {"1": [A], "2": [B, C]})
    assert collect_ran_test_ids(cell) == {A, B, C}
    assert collect_ran_test_ids(tmp_path / "nope") is None
    (cell / "artifacts" / "2" / "eval.json").write_text("{broken")
    assert collect_ran_test_ids(cell) == {A}                       # unreadable file skipped, not fatal
    empty = tmp_path / "empty"
    (empty / "artifacts").mkdir(parents=True)
    assert collect_ran_test_ids(empty) == set()


def test_generate_filtered_evaluation_fails_closed_on_an_invalid_list(tmp_path):
    ws = _workspace(tmp_path, {"invalid_pass_to_pass": [A, "ghost"]})
    cell = _cell(tmp_path, _raw_two_p2p_one_failed(), {"1": [A, B, C]})
    stale = cell / "evaluation_result_filtered.json"
    stale.write_text("{}")
    assert generate_filtered_evaluation(cell / "evaluation_result.json", ws, "M1") is None
    assert not stale.exists() and (cell / "evaluation_result_filtered.json.stale").exists()
    raw = json.loads((cell / "evaluation_result.json").read_text())
    assert raw["scoring_blocked"] is True and any("ghost" in e for e in raw["filter_list_error"])
    # the stamp is not a score change: the scored fields are untouched
    assert raw["test_summary"] == _raw_two_p2p_one_failed()["test_summary"]


def test_generate_filtered_evaluation_writes_the_intersection_result_for_a_valid_list(tmp_path):
    ws = _workspace(tmp_path, {"invalid_pass_to_pass": [A]})
    cell = _cell(tmp_path, _raw_two_p2p_one_failed(), {"1": [A, B, C]})
    out = generate_filtered_evaluation(cell / "evaluation_result.json", ws, "M1")
    assert out == cell / "evaluation_result_filtered.json"
    f = json.loads(out.read_text())
    assert f["resolved"] is True and f["test_summary"]["pass_to_pass_required"] == 1
    raw = json.loads((cell / "evaluation_result.json").read_text())
    assert "scoring_blocked" not in raw


def test_run_e2e_preflight_refuses_an_invalid_workspace(tmp_path):
    from harness.e2e.run_e2e import _preflight_filter_lists

    _preflight_filter_lists(_workspace(tmp_path, {"invalid_pass_to_pass": [A]}))  # valid: returns
    with pytest.raises(SystemExit):
        _preflight_filter_lists(_workspace(tmp_path / "bad", {"invalid_pass_to_pass": ["ghost"]}))
