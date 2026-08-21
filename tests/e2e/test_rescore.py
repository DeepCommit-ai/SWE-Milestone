"""Re-tally tool (issue #24): replay selection, envelope patching, idempotence."""

import json
from pathlib import Path

from harness.e2e.evaluator import (
    SCORING_ID_POLICY_IDENTITY,
    SCORING_ID_POLICY_LEGACY,
    SCORING_ID_POLICY_LEGACY_PASSWINS,
    EvaluationResult,
    tally_scoring,
)
from harness.e2e.rescore import rescore_cell, run_campaign
from harness.utils.test_id_normalizer import TestIdNormalizer

PY_A = "sklearn/tests/test_pipeline.py::test_routing_passed_metadata_not_supported[decision_function]"
PY_B = (
    "sklearn/semi_supervised/tests/test_self_training.py"
    "::test_routing_passed_metadata_not_supported[decision_function]"
)


def _payload(passed=(), failed=()):
    return {
        "results": {
            "passed": list(passed),
            "failed": [{"nodeid": n} for n in failed],
            "error": [],
            "xpassed": [],
            "xfailed": [],
            "skipped": [],
        },
        "summary": {
            "total": len(passed) + len(failed),
            "passed": len(passed),
            "failed": len(failed),
            "error": 0,
            "skipped": 0,
        },
    }


def _stored_envelope(payload, baseline, policy, **extra):
    normalizer = TestIdNormalizer(framework="pytest", enable_normalization=True)
    t = tally_scoring(payload, baseline, framework="pytest", normalizer=normalizer, policy=policy)
    result = EvaluationResult(
        milestone_id="M1",
        patch_is_None=False,
        patch_exists=True,
        patch_successfully_applied=True,
        resolved=t.strict_resolved,
        fail_to_pass_success=t.fail_to_pass_success,
        fail_to_pass_failure=t.fail_to_pass_failure,
        pass_to_pass_success_count=t.pass_to_pass_success_count,
        pass_to_pass_failure=t.pass_to_pass_failure,
        pass_to_pass_missing=t.pass_to_pass_missing,
        none_to_pass_success=t.none_to_pass_success,
        none_to_pass_failure=t.none_to_pass_failure,
        none_to_pass_missing=t.none_to_pass_missing,
        total_tests=t.total_tests,
        passed_tests=t.passed_tests,
        failed_tests=t.failed_tests,
        error_tests=t.error_tests,
        skipped_tests=t.skipped_tests,
        fail_to_pass_required=len(t.fail_to_pass_ids),
        fail_to_pass_achieved=len(t.fail_to_pass_success),
        pass_to_pass_required=len(t.pass_to_pass_ids),
        none_to_pass_required=len(t.none_to_pass_ids),
        none_to_pass_achieved=len(t.none_to_pass_success),
        **extra,
    )
    env = result.to_dict()
    env.pop("scoring_identity", None)  # stored results predate the block
    env["extra_diagnostic"] = "keep me"
    return env


def _make_world(tmp_path, payload, baseline, stored_policy=SCORING_ID_POLICY_LEGACY, pid="123", extra=None):
    data_root = tmp_path / "data" / "repo_x"
    (data_root / "test_results" / "M1").mkdir(parents=True)
    (data_root / "test_results" / "M1" / "M1_classification.json").write_text(json.dumps(baseline))
    (data_root / "dockerfiles" / "M1").mkdir(parents=True)
    (data_root / "dockerfiles" / "M1" / "test_config.json").write_text(
        json.dumps([{"name": "default", "test_cmd": "python -m pytest", "framework": "pytest"}])
    )
    cell = tmp_path / "log" / "repo_x" / "e2e_trial" / "trial_1" / "evaluation" / "M1"
    art = cell / "artifacts" / pid
    art.mkdir(parents=True)
    (art / "eval_summary.json").write_text(json.dumps(payload))
    (art / "eval.json").write_text(
        json.dumps({"tests": [{"nodeid": n} for n in payload["results"]["passed"]]})
    )
    env = _stored_envelope(payload, baseline, stored_policy, **(extra or {}))
    (cell / "evaluation_result.json").write_text(json.dumps(env, indent=2))
    return data_root, cell


def _run(cell, data_root, mirror):
    return rescore_cell(
        cell,
        data_root=data_root,
        repo_config={},
        repo_config_sha256="",
        mirror_dir=mirror,
        scratch=mirror.parent / "scratch" if mirror else cell.parent.parent / "scratch",
    )


BASELINE = {"stable_classification": {"pass_to_pass": [PY_A], "fail_to_fail": [PY_B]}}


def test_replayable_cell_flips_false_positive_regression(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    stored = json.loads((cell / "evaluation_result.json").read_text())
    assert stored["resolved"] is False  # legacy fail-close charged PY_A

    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable"
    assert rec.era == "fail-close"
    assert rec.selected_payload == "artifacts/123/eval_summary.json"
    assert rec.delta["p2p_failure"] == {"before": 1, "after": 0}
    assert rec.delta["resolved"] == {"before": False, "after": True}
    assert rec.changed_ids["p2p_failure"]["lost"] == [PY_A]
    assert rec.invariant_failures == []

    mirrored = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_text())
    assert mirrored["resolved"] is True
    assert mirrored["tests_status"]["PASS_TO_PASS"]["failure"] == []
    assert mirrored["extra_diagnostic"] == "keep me"
    assert mirrored["scoring_identity"]["policy"] == SCORING_ID_POLICY_IDENTITY
    assert mirrored["scoring_identity"]["payload_path"] == "artifacts/123/eval_summary.json"
    assert mirrored["scoring_identity"]["retally"]["replay_era"] == "fail-close"
    for key in stored:
        if key in ("resolved", "tests_status", "test_summary"):
            continue
        assert mirrored[key] == stored[key], key
    manifest = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "rescore_manifest.json").read_text())
    assert manifest["payload_sha256"] == rec.selected_sha256
    assert manifest["classification_sha256"]


def test_retally_is_idempotent(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    _run(cell, data_root, tmp_path / "out1")
    _run(cell, data_root, tmp_path / "out2")
    a = (tmp_path / "out1" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_bytes()
    b = (tmp_path / "out2" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_bytes()
    assert a == b


def test_pass_wins_era_is_detected(tmp_path):
    # Stored under the pre-2026-07-15 last-write aggregation: the namesake pass
    # overwrote the failure, so the stored cell looks resolved.
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE, stored_policy=SCORING_ID_POLICY_LEGACY_PASSWINS)
    rec = _run(cell, data_root, None)
    assert rec.status == "replayable"
    assert rec.era == "pass-wins"
    assert rec.delta == {}  # identity agrees with the lucky old value


def test_no_payload_is_non_replayable(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    for p in (cell / "artifacts").rglob("*"):
        if p.is_file():
            p.unlink()
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "no-payload"


def test_stored_result_that_no_candidate_reproduces_is_non_replayable(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    env = json.loads((cell / "evaluation_result.json").read_text())
    env["tests_status"]["PASS_TO_PASS"]["missing"] = 99
    (cell / "evaluation_result.json").write_text(json.dumps(env))
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "no-candidate-reproduces"
    assert rec.manifest["nearest"]


def test_two_reproducing_candidates_are_ambiguous(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    second = cell / "artifacts" / "456"
    second.mkdir()
    (second / "eval_summary.json").write_text(json.dumps(payload))
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "ambiguous-candidates"
    assert rec.candidates == 2


def test_resolution_locks_in_envelope_are_preserved(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(
        tmp_path, payload, BASELINE, extra={"infrastructure_failure": "docker daemon died"}
    )
    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable"
    assert rec.resolution_locked is True
    assert rec.new_resolved is False
    mirrored = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_text())
    assert mirrored["resolved"] is False
    assert mirrored["infrastructure_failure"] == "docker daemon died"


def test_retry_directory_uses_base_milestone_classification(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    retry = cell.parent / "M1-retry1"
    cell.rename(retry)
    rec = _run(retry, data_root, None)
    assert rec.status == "replayable"
    assert rec.milestone == "M1-retry1"


def test_campaign_writes_records_and_summary(tmp_path):
    payload = _payload(passed=[PY_A], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, BASELINE)
    summary = run_campaign(data_root=data_root, cells=[cell], out_dir=tmp_path / "campaign", mirror=False)
    assert summary["cells"] == 1 and summary["replayable"] == 1
    assert summary["resolved_false_to_true"] == ["trial_1/M1"]
    assert summary["delta_totals"]["p2p_failure"] == -1
    assert (tmp_path / "campaign" / "records.jsonl").exists()
    assert not (tmp_path / "campaign" / "mirror").exists()
