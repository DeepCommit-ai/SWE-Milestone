"""Re-tally tool (issue #24): replay selection, eras, envelope patching, locks,
input pinning, idempotence."""

import json
import shutil
import subprocess

import pytest

from harness.e2e.evaluator import (
    SCORING_ID_POLICY_IDENTITY,
    SCORING_ID_POLICY_LEGACY,
    SCORING_ID_POLICY_LEGACY_PASSWINS,
    EvaluationResult,
    tally_scoring,
)
from harness.e2e.rescore import ERA_AGNOSTIC, rescore_cell, run_campaign
from harness.utils.test_id_normalizer import TestIdNormalizer

PY_A = "sklearn/tests/test_pipeline.py::test_routing_passed_metadata_not_supported[decision_function]"
PY_B = (
    "sklearn/semi_supervised/tests/test_self_training.py"
    "::test_routing_passed_metadata_not_supported[decision_function]"
)
PY_C = "sklearn/tests/test_other.py::test_unrelated"


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


def _make_world(tmp_path, payload, baseline, stored_policy=SCORING_ID_POLICY_LEGACY, pid="123",
                extra=None, filter_list=None):
    data_root = tmp_path / "data" / "repo_x"
    (data_root / "test_results" / "M1").mkdir(parents=True)
    (data_root / "test_results" / "M1" / "M1_classification.json").write_text(json.dumps(baseline))
    if filter_list is not None:
        (data_root / "test_results" / "M1" / "M1_filter_list.json").write_text(json.dumps(filter_list))
    (data_root / "dockerfiles" / "M1").mkdir(parents=True)
    (data_root / "dockerfiles" / "M1" / "test_config.json").write_text(
        json.dumps([{"name": "default", "test_cmd": "python -m pytest", "framework": "pytest"}])
    )
    cell = tmp_path / "log" / "repo_x" / "e2e_trial" / "trial_1" / "evaluation" / "M1"
    art = cell / "artifacts" / pid
    art.mkdir(parents=True)
    (art / "eval_summary.json").write_text(json.dumps(payload))
    (art / "eval.json").write_text(
        json.dumps({"tests": [{"nodeid": n} for n in payload["results"]["passed"]]
                    + [{"nodeid": n["nodeid"]} for n in payload["results"]["failed"]]})
    )
    env = _stored_envelope(payload, baseline, stored_policy, **(extra or {}))
    (cell / "evaluation_result.json").write_text(json.dumps(env, indent=2))
    return data_root, cell


def _run(cell, data_root, mirror, **kw):
    return rescore_cell(
        cell,
        data_root=data_root,
        repo_config={},
        repo_config_sha256="",
        mirror_dir=mirror,
        scratch=(mirror.parent if mirror else cell.parent.parent) / "scratch",
        **kw,
    )


BASELINE = {"stable_classification": {"pass_to_pass": [PY_A], "fail_to_fail": [PY_B]}}
# A universe with a collision whose outcomes disagree, so the two legacy
# scorers give different stored results (era is decidable).
COLLIDING = _payload(passed=[PY_A], failed=[PY_B])


def test_replayable_cell_flips_false_positive_regression(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    stored = json.loads((cell / "evaluation_result.json").read_text())
    assert stored["resolved"] is False  # legacy fail-close charged PY_A

    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable"
    assert rec.era == "fail-close"
    assert rec.frozen is False
    assert rec.selected_payload == "artifacts/123/eval_summary.json"
    assert rec.delta["p2p_failure"] == {"before": 1, "after": 0}
    assert rec.delta["resolved"] == {"before": False, "after": True}
    assert rec.changed_ids["p2p_failure"]["lost"] == [PY_A]
    assert rec.invariant_failures == []
    assert rec.mirrored is True
    assert rec.inputs["test_config_sha256"]

    out_cell = tmp_path / "out" / "repo_x" / "trial_1" / "M1"
    mirrored = json.loads((out_cell / "evaluation_result.json").read_text())
    assert mirrored["resolved"] is True
    assert mirrored["tests_status"]["PASS_TO_PASS"]["failure"] == []
    assert mirrored["tests_status"]["NONE_TO_PASS"]["missing"] == 0
    assert mirrored["extra_diagnostic"] == "keep me"
    assert mirrored["scoring_identity"]["policy"] == SCORING_ID_POLICY_IDENTITY
    assert mirrored["scoring_identity"]["payload_path"] == "artifacts/123/eval_summary.json"
    assert mirrored["scoring_identity"]["retally"]["replay_era"] == "fail-close"
    assert mirrored["scoring_identity"]["match_trace"]["pass_to_pass"] == {"exact": 1}
    for key in stored:
        if key in ("resolved", "tests_status", "test_summary"):
            continue
        assert mirrored[key] == stored[key], key
    assert (out_cell / "artifacts" / "123" / "eval_summary.json").exists()
    manifest = json.loads((out_cell / "rescore_manifest.json").read_text())
    assert manifest["payload_sha256"] == rec.selected_sha256
    assert manifest["classification_sha256"] and manifest["test_config_sha256"]
    assert manifest["replay_policies_reproducing"] == [SCORING_ID_POLICY_LEGACY]
    notes = (out_cell / "PROMOTION_NOTES.md").read_text()
    assert "summary.json" in notes and "feedback_report.md" in notes and "artifacts.tar.gz" in notes


def test_retally_is_repeatable_and_then_a_verified_noop(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    _run(cell, data_root, tmp_path / "out1")
    _run(cell, data_root, tmp_path / "out2")
    a = (tmp_path / "out1" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_bytes()
    b = (tmp_path / "out2" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_bytes()
    assert a == b

    # Re-score the corrected output itself (as a promoted cell would look).
    promoted = tmp_path / "log" / "repo_x" / "e2e_trial" / "trial_1" / "evaluation" / "M1"
    shutil.copy(tmp_path / "out1" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json",
                promoted / "evaluation_result.json")
    rec = _run(promoted, data_root, tmp_path / "out3")
    assert rec.status == "already-identity"
    assert rec.delta == {}
    assert not (tmp_path / "out3" / "repo_x").exists()


def test_era_agnostic_cell_is_not_frozen(tmp_path):
    # No disagreeing collision: both legacy scorers reproduce the stored result.
    payload = _payload(passed=[PY_C], failed=[PY_B])
    data_root, cell = _make_world(tmp_path, payload, {"stable_classification": {"pass_to_pass": [PY_A, PY_C]}})
    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable"
    assert rec.era == ERA_AGNOSTIC
    assert rec.frozen is False
    # PY_A never ran; legacy credited it via namesake PY_B's failure... as a
    # regression, identity counts it missing.
    assert rec.delta["p2p_failure"] == {"before": 1, "after": 0}
    assert rec.delta["p2p_missing"] == {"before": 0, "after": 1}


def test_pass_wins_era_is_detected_and_frozen_by_default(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE, stored_policy=SCORING_ID_POLICY_LEGACY_PASSWINS)
    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable"
    assert rec.era == "pass-wins"
    assert rec.frozen is True
    assert rec.mirrored is False
    assert rec.delta == {}  # identity agrees with the lucky old value here
    assert not (tmp_path / "out" / "repo_x").exists()
    rec2 = _run(cell, data_root, tmp_path / "out2", include_pass_wins=True)
    assert rec2.frozen is False and rec2.mirrored is True


def test_no_payload_is_non_replayable(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    for p in (cell / "artifacts").rglob("*"):
        if p.is_file():
            p.unlink()
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "no-payload"


def test_unreadable_candidate_is_non_replayable(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    broken = cell / "artifacts" / "999"
    broken.mkdir()
    (broken / "eval_summary.json").write_text("{not json")
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "unreadable-candidate"
    assert rec.candidates_unreadable == 1


def test_stored_result_that_no_candidate_reproduces_is_non_replayable(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    env = json.loads((cell / "evaluation_result.json").read_text())
    env["tests_status"]["PASS_TO_PASS"]["missing"] = 99
    (cell / "evaluation_result.json").write_text(json.dumps(env))
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "no-candidate-reproduces"
    assert rec.manifest["nearest"]


def test_two_reproducing_candidates_that_agree_under_identity_are_consistent(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    second = cell / "artifacts" / "456"
    second.mkdir()
    # Same evidence written twice (different bytes: indentation), e.g. a
    # re-run that produced an identical report. Immaterial ambiguity.
    (second / "eval_summary.json").write_text(json.dumps(COLLIDING, indent=2))
    rec = _run(cell, data_root, None)
    assert rec.status == "replayable"
    assert rec.candidates == 2
    assert rec.ambiguous_consistent is True
    assert len(rec.reproducing_shas) == 2
    assert rec.selected_payload == "artifacts/123/eval_summary.json"  # first by path
    assert rec.manifest["ambiguous_consistent"] is True


def test_two_reproducing_candidates_that_disagree_under_identity_stay_ambiguous(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    second = cell / "artifacts" / "456"
    second.mkdir()
    # Under the legacy prefix-drop key PY_A/PY_B merge and fail-close to
    # failed either way, so both payloads reproduce the stored result; under
    # the identity key they disagree on PY_A. Which one was scored is unknown.
    swapped = _payload(passed=[PY_B], failed=[PY_A])
    (second / "eval_summary.json").write_text(json.dumps(swapped))
    rec = _run(cell, data_root, None)
    assert rec.status == "non-replayable"
    assert rec.reason == "ambiguous-candidates"
    assert rec.candidates == 2
    assert rec.ambiguous_consistent is False


def test_same_path_different_bytes_in_tarball_is_a_distinct_candidate(tmp_path):
    import tarfile

    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    # Tarball carries a *different* payload under the same relative path.
    other = _payload(passed=[PY_A, PY_C], failed=[PY_B])
    stage = tmp_path / "stage" / "artifacts" / "123"
    stage.mkdir(parents=True)
    (stage / "eval_summary.json").write_text(json.dumps(other))
    with tarfile.open(cell / "artifacts.tar.gz", "w:gz") as tf:
        tf.add(stage, arcname="artifacts/123")
    rec = _run(cell, data_root, None)
    assert rec.candidates == 2
    # Only the on-disk copy reproduces the stored result, so the cell is still
    # uniquely replayable — but the divergent tar copy was seen, not shadowed.
    assert rec.status == "replayable"
    assert rec.selected_payload == "artifacts/123/eval_summary.json"


def test_resolution_locks_in_envelope_are_preserved(tmp_path):
    data_root, cell = _make_world(
        tmp_path, COLLIDING, BASELINE, extra={"infrastructure_failure": "docker daemon died"}
    )
    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable"
    assert rec.resolution_locked is True
    assert rec.new_resolved is False
    mirrored = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result.json").read_text())
    assert mirrored["resolved"] is False
    assert mirrored["infrastructure_failure"] == "docker daemon died"


def test_filtered_result_keeps_locks_and_n2p_missing(tmp_path):
    baseline = {"stable_classification": {"pass_to_pass": [PY_A, PY_C], "none_to_pass": [PY_B]}}
    payload = _payload(passed=[PY_A])  # PY_C never ran; PY_B never ran
    filter_list = {"invalid_pass_to_pass": [PY_C], "invalid_fail_to_pass": [], "invalid_none_to_pass": []}
    data_root, cell = _make_world(
        tmp_path, payload, baseline, extra={"infrastructure_failure": "docker daemon died"},
        filter_list=filter_list,
    )
    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable" and rec.filtered_regenerated is True
    out_cell = tmp_path / "out" / "repo_x" / "trial_1" / "M1"
    filtered = json.loads((out_cell / "evaluation_result_filtered.json").read_text())
    # Filtering removed the only P2P problem, but the infrastructure lock must
    # still hold, and the N2P test that never ran stays counted as missing.
    assert filtered["resolved"] is False
    assert filtered["scoring_identity"]["filtered_locks_reapplied"] is True
    assert filtered["tests_status"]["NONE_TO_PASS"]["missing"] == 1
    assert filtered["test_summary"]["none_to_pass_missing"] == 1


def test_filtered_result_needs_the_selected_eval_json(tmp_path):
    baseline = {"stable_classification": {"pass_to_pass": [PY_A, PY_C]}}
    filter_list = {"invalid_pass_to_pass": [PY_C], "invalid_fail_to_pass": [], "invalid_none_to_pass": []}
    data_root, cell = _make_world(tmp_path, _payload(passed=[PY_A]), baseline, filter_list=filter_list)
    (cell / "artifacts" / "123" / "eval.json").unlink()
    rec = _run(cell, data_root, tmp_path / "out")
    assert rec.status == "replayable" and rec.mirrored is True
    assert rec.filtered_regenerated is False
    assert rec.filtered_reason == "selected-eval.json-unavailable"
    assert any("filtered" in s for s in rec.manifest["stale_after_retally"])


def test_repo_config_drift_is_non_replayable(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    env = json.loads((cell / "evaluation_result.json").read_text())
    env["evaluation_environment"]["repo_config_sha256"] = "deadbeef"
    env["evaluation_environment"]["repo_config_binding_mode"] = "bound"
    (cell / "evaluation_result.json").write_text(json.dumps(env))
    rec = rescore_cell(cell, data_root=data_root, repo_config={}, repo_config_sha256="cafebabe",
                       mirror_dir=None, scratch=tmp_path / "scratch")
    assert rec.status == "non-replayable"
    assert rec.reason == "repo-config-drift"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_classification_drift_against_trial_data_commit_is_non_replayable(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    subprocess.run(["git", "init", "-q"], cwd=data_root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=data_root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pin"], cwd=data_root, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=data_root, capture_output=True, text=True, check=True).stdout.strip()
    trial_dir = cell.parent.parent
    (trial_dir / "trial_metadata.json").write_text(json.dumps({"data_version": {"commit": commit}}))
    rec = _run(cell, data_root, None)
    assert rec.status == "replayable"
    assert rec.inputs["classification_pin"]["status"] == "match"
    # Now the classification on disk changes after the trial's pinned commit.
    cls = data_root / "test_results" / "M1" / "M1_classification.json"
    cls.write_text(json.dumps({"stable_classification": {"pass_to_pass": [PY_A, PY_C], "fail_to_fail": [PY_B]}}))
    rec2 = _run(cell, data_root, None)
    assert rec2.status == "non-replayable"
    assert rec2.reason == "classification-drift"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_classification_pin_resolves_when_git_root_is_above_data_root(tmp_path):
    """Real layout: the data repository root is the parent of <data_root>
    (``.../SWE-Milestone-data/<repo>``); ``git show`` must use a cwd-relative
    path or the pin silently never resolves."""
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    git_root = data_root.parent
    g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=git_root, check=True)
    subprocess.run(g + ["add", "-A"], cwd=git_root, check=True)
    subprocess.run(g + ["commit", "-qm", "pin"], cwd=git_root, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_root, capture_output=True, text=True, check=True).stdout.strip()
    trial_dir = cell.parent.parent
    (trial_dir / "trial_metadata.json").write_text(json.dumps({"data_version": {"commit": commit}}))
    rec = _run(cell, data_root, None)
    assert rec.status == "replayable"
    assert rec.inputs["classification_pin"]["status"] == "match"
    # drift after the pinned commit
    cls = data_root / "test_results" / "M1" / "M1_classification.json"
    cls.write_text(json.dumps({"stable_classification": {"pass_to_pass": [PY_A, PY_C], "fail_to_fail": [PY_B]}}))
    rec2 = _run(cell, data_root, None)
    assert (rec2.status, rec2.reason) == ("non-replayable", "classification-drift")
    # a recorded commit that cannot be read is a broken pin, not "no pin"
    (trial_dir / "trial_metadata.json").write_text(json.dumps({"data_version": {"commit": "0" * 40}}))
    rec3 = _run(cell, data_root, None)
    assert (rec3.status, rec3.reason) == ("non-replayable", "classification-pin-unresolvable")
    assert rec3.inputs["classification_pin"]["status"] == "unresolvable"


def test_mirror_extracts_artifacts_when_the_payload_only_survives_in_the_tarball(tmp_path):
    import tarfile

    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    stage = tmp_path / "stage" / "artifacts" / "123"
    stage.mkdir(parents=True)
    for name in ("eval_summary.json", "eval.json"):
        shutil.copy(cell / "artifacts" / "123" / name, stage / name)
    with tarfile.open(cell / "artifacts.tar.gz", "w:gz") as tf:
        tf.add(stage, arcname="artifacts/123")
    shutil.rmtree(cell / "artifacts")  # only the tarball remains
    mirror = tmp_path / "mirror"
    rec = _run(cell, data_root, mirror)
    assert rec.status == "replayable" and rec.mirrored
    out = mirror / "repo_x" / "trial_1" / "M1"
    assert (out / "artifacts" / "123" / "eval_summary.json").exists()
    assert (out / "artifacts" / "123" / "eval.json").exists()
    assert "extracted from artifacts.tar.gz" in (out / "PROMOTION_NOTES.md").read_text()
    assert rec.manifest["payload_source"] == "tarball"


def test_retry_directory_uses_base_milestone_classification(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    retry = cell.parent / "M1-retry1"
    cell.rename(retry)
    rec = _run(retry, data_root, None)
    assert rec.status == "replayable"
    assert rec.milestone == "M1-retry1"


def test_campaign_writes_records_and_summary(tmp_path):
    data_root, cell = _make_world(tmp_path, COLLIDING, BASELINE)
    summary = run_campaign(data_root=data_root, cells=[cell], out_dir=tmp_path / "campaign", mirror=False)
    assert summary["cells"] == 1 and summary["replayable"] == 1
    assert summary["era"]["fail-close"] == 1
    assert summary["resolved_false_to_true"] == ["trial_1/M1"]
    assert summary["delta_totals"]["p2p_failure"] == -1
    assert (tmp_path / "campaign" / "records.jsonl").exists()
    assert not (tmp_path / "campaign" / "mirror").exists()
