"""Golden replay of a real scikit-learn cell under the issue #26 collection-death
waivers (v1.0.2 W3) and the filter-only re-tally rule.

The unit tests around the filter use synthetic ids; this test drives the real
`filter_evaluation_result` / `regenerate_filtered_from_stored` with the inputs of
an actually-recorded published cell and the filter list that v1.0.2 lands, and pins
every number the campaign promotes for it.

Fixture: `fixtures/scikit_m121_collection_death_cell.json.gz`, trimmed from
  SWE-Milestone-log/scikit-learn_scikit-learn_1.5.2_1.6.0/e2e_trial/_claude-code_glm-5.2_run_002/evaluation/M12.1
  + SWE-Milestone-data test_results/M12.1/M12.1_filter_list.json (data branch v1.0.2-filters, f8f1732)
  + test_results/M12.1/M12.1_classification.json
to the raw envelope, the filter list, and the parts of `ran_test_ids` / the P2P universe
that the filter arithmetic reads (the waived ids; plus the 117 healthy
`sklearn/model_selection/tests/test_validation.py` ids, the namesake file that must
not be touched). The full-input result was asserted equal to the trimmed-input result
when the fixture was built; when the full inputs are present on this machine the
test re-checks that.

What the cell shows: 23,960 tests ran; `sklearn/utils/tests/test_validation.py`
(237 F2P) and `test_estimator_checks.py` (1 F2P + 15 P2P), `test_nca.py` (215 P2P),
`test_testing.py` (88 P2P), `test_base.py` (51 P2P) died at collection because their
imports exist only in the image's [ENV-PATCH] placeholders; all 369 waived P2P ids are
`missing` (never ran), all 238 waived F2P ids are failures.
"""
import gzip
import json
from pathlib import Path

import pytest

from harness.e2e.evaluator import (
    classification_buckets,
    collect_ran_test_ids,
    filter_evaluation_result,
    validate_filter_list,
)
from harness.e2e.rescore import regenerate_filtered_from_stored

FIXTURE = Path(__file__).parent / "fixtures" / "scikit_m121_collection_death_cell.json.gz"
CELL = Path("/data2/gangda/SWE-Milestone-log/scikit-learn_scikit-learn_1.5.2_1.6.0/e2e_trial/_claude-code_glm-5.2_run_002/evaluation/M12.1")
DATA_M121 = Path("/data2/gangda/SWE-Milestone-data/reeval/v102_data_work/scikit-learn_scikit-learn_1.5.2_1.6.0/test_results/M12.1")
UTILS_VALIDATION = "sklearn/utils/tests/test_validation.py::"
MODEL_SELECTION_VALIDATION = "sklearn/model_selection/tests/test_validation.py::"


@pytest.fixture(scope="module")
def fx():
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing: {FIXTURE}")
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def replayed(fx):
    filtered, reapplied = regenerate_filtered_from_stored(
        fx["raw"], fx["filter_list"], set(fx["ran_test_ids"]), set(fx["p2p_universe"])
    )
    return filtered, reapplied


def test_fixture_is_the_v102_list_and_the_shipped_cell(fx):
    src = fx["source"]
    assert src["ran_test_ids_full_count"] == 23960 and src["p2p_universe_full_count"] == 21446
    fl = fx["filter_list"]
    assert len(fl["invalid_fail_to_pass"]) == 238 and len(fl["invalid_pass_to_pass"]) == 369 and fl["invalid_none_to_pass"] == []
    assert all(e["reason"] and e["class"] == "issue26-collection-death" for k in ("invalid_fail_to_pass", "invalid_pass_to_pass") for e in fl[k])
    assert sum(1 for e in fl["invalid_fail_to_pass"] if e["test_id"].startswith(UTILS_VALIDATION)) == 237
    assert not any(e["test_id"].startswith(MODEL_SELECTION_VALIDATION) for k in fl for e in (fl[k] if isinstance(fl[k], list) else []))
    # every waived P2P id is in the (trimmed) universe: the list validates against the classification buckets
    assert set(e["test_id"] for e in fl["invalid_pass_to_pass"]) <= set(fx["p2p_universe"])
    raw = fx["raw"]
    assert raw["test_summary"] == {
        "total": 23960, "passed": 22130, "failed": 390, "error": 0, "skipped": 1384,
        "fail_to_pass_required": 872, "fail_to_pass_achieved": 619,
        "none_to_pass_required": 2, "none_to_pass_achieved": 2,
        "pass_to_pass_required": 21446, "pass_to_pass_achieved": 21076,
        "pass_to_pass_failed": 1, "pass_to_pass_missing": 369, "none_to_pass_missing": 0,
    }
    assert raw["resolved"] is False and raw["scoring_identity"]["policy"] == "identity-v2"


def test_waived_ids_vanish_and_the_requirements_follow(fx, replayed):
    filtered, reapplied = replayed
    exp = fx["expected"]
    assert filtered["test_summary"] == exp["test_summary"]
    assert filtered["test_summary"]["fail_to_pass_required"] == 872 - 238 == 634
    assert filtered["test_summary"]["fail_to_pass_achieved"] == 619          # no F2P success removed
    assert filtered["test_summary"]["pass_to_pass_required"] == 21446 - 369 == 21077
    assert filtered["test_summary"]["pass_to_pass_missing"] == 0             # all 369 waived P2P ids were the missing ones
    assert filtered["test_summary"]["pass_to_pass_achieved"] == 21076 and filtered["test_summary"]["pass_to_pass_failed"] == 1
    assert filtered["filter_stats"] == exp["filter_stats"] == {
        "fail_to_pass_filtered": 238, "none_to_pass_filtered": 0, "pass_to_pass_filtered": 0,
        "pass_to_pass_missing_filtered": 369, "invalid_f2p_count": 238, "invalid_n2p_count": 0, "invalid_p2p_count": 369,
    }
    removed = sorted(set(fx["raw"]["tests_status"]["FAIL_TO_PASS"]["failure"]) - set(filtered["tests_status"]["FAIL_TO_PASS"]["failure"]))
    assert removed == exp["f2p_failure_removed"] and len(removed) == 238
    assert sum(1 for t in removed if t.startswith(UTILS_VALIDATION)) == 237
    assert [t for t in removed if not t.startswith(UTILS_VALIDATION)] == [
        "sklearn/utils/tests/test_estimator_checks.py::test_yield_all_checks_legacy"]
    assert exp["f2p_success_removed"] == []
    assert filtered["tests_status"]["PASS_TO_PASS"] == {**exp["p2p_status"], "failure": exp["p2p_failure"]}
    assert filtered["tests_status"]["PASS_TO_PASS"]["missing"] == 0 and filtered["tests_status"]["PASS_TO_PASS"]["success_count"] == 21076


def test_resolved_stays_false_and_no_lock_is_invented(fx, replayed):
    filtered, reapplied = replayed
    # 15 F2P failures and 1 P2P failure remain: the waiver corrects the denominator, it does not resolve the cell
    assert filtered["resolved"] is False and fx["expected"]["resolved"] is False
    assert len(filtered["tests_status"]["FAIL_TO_PASS"]["failure"]) == 15
    assert reapplied is False and fx["expected"]["locks_reapplied"] is False
    assert filtered["scoring_identity"]["filtered_locks_reapplied"] is False
    assert filtered["filtered"] is True


def test_the_healthy_namesake_file_is_untouched(fx, replayed):
    filtered, _ = replayed
    healthy = set(fx["healthy_namesake_ids"])
    assert len(healthy) == 117 and healthy <= set(fx["ran_test_ids"])
    waived = {e["test_id"] for k in ("invalid_fail_to_pass", "invalid_pass_to_pass") for e in fx["filter_list"][k]}
    assert not (healthy & waived)
    for cat in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        for sub in ("success", "failure"):
            before = fx["raw"]["tests_status"][cat].get(sub)
            after = filtered["tests_status"][cat].get(sub)
            if isinstance(before, list):
                assert sorted(t for t in before if t.startswith(MODEL_SELECTION_VALIDATION)) == sorted(t for t in after if t.startswith(MODEL_SELECTION_VALIDATION))


def test_intersection_rule_and_legacy_count_rule_agree_for_the_valid_list(fx):
    a = filter_evaluation_result(fx["raw"], fx["filter_list"], ran_test_ids=set(fx["ran_test_ids"]))
    b = filter_evaluation_result(fx["raw"], fx["filter_list"], ran_test_ids=set(fx["ran_test_ids"]), p2p_universe=set(fx["p2p_universe"]))
    assert a == b
    assert fx["raw"]["test_summary"]["pass_to_pass_missing"] == 369  # non-mutating


@pytest.mark.skipif(not (CELL / "evaluation_result.json").exists() or not (DATA_M121 / "M12.1_filter_list.json").exists(),
                    reason="full inputs (published cell + data working copy) not present on this machine")
def test_trimmed_fixture_reproduces_the_full_input_regeneration(fx, replayed):
    raw = json.load(open(CELL / "evaluation_result.json"))
    fl = json.load(open(DATA_M121 / "M12.1_filter_list.json"))
    cls = json.load(open(DATA_M121 / "M12.1_classification.json"))
    assert validate_filter_list(fl, cls, "M12.1") == []
    ran = collect_ran_test_ids(CELL)
    assert len(ran) == fx["source"]["ran_test_ids_full_count"]
    full, _ = regenerate_filtered_from_stored(raw, fl, ran, classification_buckets(cls)["pass_to_pass"])
    assert full == replayed[0]
