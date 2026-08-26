"""rescore.py --mode filter-only (v1.0.2 filter campaigns): the stored raw result is
taken as-is, the derivative regenerated under the validated filter list with the
evaluator's ran_test_ids rule and the envelope's locks; no replay."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from harness.e2e.rescore import (
    FILTER_ONLY_TOOL_VERSION,
    FilterListError,
    filter_only_cell,
    main,
    run_campaign,
)

_spec = importlib.util.spec_from_file_location("test_rescore_helpers", Path(__file__).with_name("test_rescore.py"))
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)  # PY_A/PY_C, _payload, _make_world, BASELINE, COLLIDING

PY_A, PY_C = _h.PY_A, _h.PY_C
# A distinct F2P id (no namesake of PY_A: the stored fixture envelope is scored under
# the legacy prefix-drop key and a collision would charge PY_A as failed).
PY_F = "sklearn/tests/test_feature.py::test_feature"
UNIVERSE = {"stable_classification": {"pass_to_pass": [PY_A, PY_C], "fail_to_pass": [PY_F]}}
FL_C = {"invalid_pass_to_pass": [{"test_id": PY_C, "reason": "cross-universe conflict"}],
        "invalid_fail_to_pass": [], "invalid_none_to_pass": []}


def _world(tmp_path, payload, baseline=UNIVERSE, filter_list=FL_C, **kw):
    return _h._make_world(tmp_path, payload, baseline, filter_list=filter_list, **kw)


def _fo(cell, data_root, mirror=None, campaign="v102-test"):
    return filter_only_cell(cell, data_root=data_root, mirror_dir=mirror, campaign=campaign)


def test_missing_derivative_is_regenerated_and_mirrored_without_touching_the_raw_result(tmp_path):
    # PY_A passed, PY_F (F2P) failed, PY_C (P2P) never ran -> missing; C is waived.
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    raw_before = (cell / "evaluation_result.json").read_bytes()
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated", (rec.status, rec.reason)
    assert rec.reason == "no stored filtered file"
    assert rec.mirrored and rec.filtered_regenerated
    assert rec.delta["p2p_missing"] == {"before": 1, "after": 0}
    assert rec.delta["summary.pass_to_pass_required"] == {"before": 2, "after": 1}
    assert "resolved" not in rec.delta  # F2P still failing
    out_cell = tmp_path / "out" / "repo_x" / "trial_1" / "M1"
    assert (out_cell / "evaluation_result.json").read_bytes() == raw_before  # byte-identical copy
    assert (cell / "evaluation_result.json").read_bytes() == raw_before        # source untouched
    assert not (cell / "evaluation_result_filtered.json").exists()
    assert not (out_cell / "artifacts").exists()
    f = json.loads((out_cell / "evaluation_result_filtered.json").read_text())
    assert f["filtered"] is True and f["test_summary"]["pass_to_pass_missing"] == 0
    assert f["test_summary"]["pass_to_pass_required"] == 1 and f["test_summary"]["pass_to_pass_achieved"] == 1
    assert f["filter_stats"]["pass_to_pass_missing_filtered"] == 1
    assert f["scoring_identity"]["filtered_locks_reapplied"] is False
    m = json.loads((out_cell / "rescore_manifest.json").read_text())
    assert m["tool"] == FILTER_ONLY_TOOL_VERSION and m["mode"] == "filter-only" and m["campaign"] == "v102-test"
    assert m["action"] == "write-regenerated-derivative"
    assert m["filter_list_path"] == "test_results/M1/M1_filter_list.json" and m["filter_list_sha256"]
    assert m["filter_list_entries"] == {"invalid_fail_to_pass": 0, "invalid_none_to_pass": 0, "invalid_pass_to_pass": 1}
    assert m["ran_test_ids_count"] == 2 and "artifacts/*/eval.json" in m["ran_test_ids_rule"]
    assert m["classification_pin"]["status"] == "unavailable"  # no trial_metadata in the fixture
    notes = (out_cell / "PROMOTION_NOTES.md").read_text()
    assert "filter-only" in notes and "summary_filtered.json" in notes and "byte-identical" in notes


def test_waiving_the_only_failure_flips_resolved_and_the_flip_is_recorded(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A, PY_F]))  # only C missing
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated"
    assert rec.stored_resolved is False and rec.new_resolved is True
    assert rec.delta["resolved"] == {"before": False, "after": True}
    summary = run_campaign(data_root=data_root, cells=[cell], out_dir=tmp_path / "c", mirror=True, filter_only=True)
    assert summary["by_status"] == {"filter-regenerated": 1}
    assert summary["resolved_false_to_true"] == ["trial_1/M1"] and summary["changed_cells"] == 1
    assert summary["mirrored"] == 1 and summary["error"] == 0
    assert (tmp_path / "c" / "mirror" / "repo_x" / "trial_1" / "M1" / "evaluation_result_filtered.json").exists()


def test_rerun_on_the_promoted_derivative_is_a_verified_noop(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    _fo(cell, data_root, tmp_path / "out")
    out_cell = tmp_path / "out" / "repo_x" / "trial_1" / "M1"
    shutil.copy(out_cell / "evaluation_result_filtered.json", cell / "evaluation_result_filtered.json")
    rec = _fo(cell, data_root, tmp_path / "out2")
    assert rec.status == "filter-identity", (rec.status, rec.reason)
    assert not (tmp_path / "out2" / "repo_x").exists()
    # a derivative that differs only in non-semantic bytes (key order) is still identity
    f = json.loads((cell / "evaluation_result_filtered.json").read_text())
    (cell / "evaluation_result_filtered.json").write_text(json.dumps(f, sort_keys=True))
    assert _fo(cell, data_root, tmp_path / "out3").status == "filter-identity"


def test_stale_derivative_differing_from_the_regeneration_is_replaced(tmp_path):
    # A derivative written by an older filter (no ran_test_ids: missing not adjusted).
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    raw = json.loads((cell / "evaluation_result.json").read_text())
    old = json.loads(json.dumps(raw))
    old["filtered"] = True
    old["test_summary"]["pass_to_pass_required"] = 1  # required reduced ...
    # ... but missing left at 1 -> achieved 0 (the 17 dubbo derivatives of A_filters.md §2.3)
    (cell / "evaluation_result_filtered.json").write_text(json.dumps(old))
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated"
    assert rec.reason == "stored filtered file differs from the regeneration"
    assert rec.inputs["served"] == "filtered"
    assert rec.delta["p2p_missing"] == {"before": 1, "after": 0}


def test_no_filter_list_with_a_stale_derivative_mirrors_a_deletion(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]), filter_list=None)
    (cell / "evaluation_result_filtered.json").write_text((cell / "evaluation_result.json").read_text())
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "stale-derivative" and rec.mirrored
    out_cell = tmp_path / "out" / "repo_x" / "trial_1" / "M1"
    assert (out_cell / "evaluation_result.json").exists()
    assert not (out_cell / "evaluation_result_filtered.json").exists()
    m = json.loads((out_cell / "rescore_manifest.json").read_text())
    assert m["action"] == "delete-stale-derivative"
    # and a cell with neither list nor derivative is simply no-filter
    (cell / "evaluation_result_filtered.json").unlink()
    rec2 = _fo(cell, data_root, tmp_path / "out2")
    assert rec2.status == "no-filter" and not rec2.mirrored
    # an empty list (no entries) counts as no filter list
    data_root3, cell3 = _world(tmp_path / "w3", _h._payload(passed=[PY_A]),
                               filter_list={"invalid_pass_to_pass": [], "invalid_fail_to_pass": []})
    assert _fo(cell3, data_root3, None).status == "no-filter"


def test_ran_test_ids_is_the_union_over_every_artifact_dir(tmp_path):
    # C ran (and passed) in a second artifact directory: it is not missing, so the
    # waiver removes a passing obligation, not a missing one.
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    raw = json.loads((cell / "evaluation_result.json").read_text())
    raw["tests_status"]["PASS_TO_PASS"]["missing"] = 0
    raw["tests_status"]["PASS_TO_PASS"]["success_count"] = 2
    raw["test_summary"].update({"pass_to_pass_missing": 0, "pass_to_pass_achieved": 2})
    (cell / "evaluation_result.json").write_text(json.dumps(raw))
    second = cell / "artifacts" / "456"
    second.mkdir()
    (second / "eval.json").write_text(json.dumps({"tests": [{"nodeid": PY_C}]}))
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated"
    assert rec.manifest["ran_test_ids_count"] == 3
    f = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result_filtered.json").read_text())
    assert f["test_summary"]["pass_to_pass_missing"] == 0
    assert f["test_summary"]["pass_to_pass_required"] == 1 and f["test_summary"]["pass_to_pass_achieved"] == 1
    assert f["filter_stats"]["pass_to_pass_missing_filtered"] == 0


def test_no_eval_json_is_non_promotable(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    (cell / "artifacts" / "123" / "eval.json").unlink()
    rec = _fo(cell, data_root, tmp_path / "out")
    assert (rec.status, rec.reason) == ("non-promotable", "no-eval.json")
    shutil.rmtree(cell / "artifacts")
    assert _fo(cell, data_root, None).reason == "no-eval.json"
    assert not (tmp_path / "out" / "repo_x").exists()


def test_locks_in_the_envelope_are_reapplied_to_the_derivative(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A, PY_F]),
                             extra={"infrastructure_failure": "docker daemon died"})
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated" and rec.resolution_locked is True
    assert rec.new_resolved is False and "resolved" not in rec.delta
    f = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result_filtered.json").read_text())
    assert f["resolved"] is False and f["scoring_identity"]["filtered_locks_reapplied"] is True
    assert f["test_summary"]["pass_to_pass_missing"] == 0  # the waiver itself still applies


def test_identity_untrusted_stored_result_keeps_resolved_false(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A, PY_F]))
    raw = json.loads((cell / "evaluation_result.json").read_text())
    raw["scoring_identity"] = {"policy": "identity", "untrusted": True}
    (cell / "evaluation_result.json").write_text(json.dumps(raw))
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated" and rec.new_resolved is False


def test_cells_the_replay_tool_cannot_handle_still_get_their_derivative(tmp_path):
    # Stored result that no payload reproduces (non-replayable for rescore_cell) and a
    # pass-wins-era envelope: filter-only does not replay, so both are regenerated.
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    env = json.loads((cell / "evaluation_result.json").read_text())
    env["tests_status"]["PASS_TO_PASS"]["missing"] = 1
    env["test_summary"]["passed"] = 99  # no candidate reproduces this
    (cell / "evaluation_result.json").write_text(json.dumps(env))
    assert _h._run(cell, data_root, None).status == "non-replayable"
    rec = _fo(cell, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated"
    f = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result_filtered.json").read_text())
    assert f["test_summary"]["passed"] == 99  # raw facts carried as stored, never re-derived


def test_n2p_missing_is_adjusted_only_for_waived_ids_that_never_ran(tmp_path):
    baseline = {"stable_classification": {"pass_to_pass": [PY_A], "none_to_pass": [PY_F, PY_C]}}
    fl = {"invalid_none_to_pass": [PY_C], "invalid_pass_to_pass": [], "invalid_fail_to_pass": []}
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]), baseline=baseline, filter_list=fl)
    raw = json.loads((cell / "evaluation_result.json").read_text())
    assert raw["tests_status"]["NONE_TO_PASS"]["missing"] == 1  # C never ran
    rec = _fo(cell, data_root, tmp_path / "out")
    f = json.loads((tmp_path / "out" / "repo_x" / "trial_1" / "M1" / "evaluation_result_filtered.json").read_text())
    assert f["tests_status"]["NONE_TO_PASS"]["missing"] == 0
    assert f["test_summary"]["none_to_pass_missing"] == 0
    assert f["test_summary"]["none_to_pass_required"] == 1 and f["tests_status"]["NONE_TO_PASS"]["failure"] == [PY_F]
    assert rec.delta["n2p_missing"] == {"before": 1, "after": 0}


def test_retry_directory_uses_the_base_milestone_filter_list(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    retry = cell.parent / "M1-retry1"
    cell.rename(retry)
    rec = _fo(retry, data_root, tmp_path / "out")
    assert rec.status == "filter-regenerated" and rec.milestone == "M1-retry1"
    assert (tmp_path / "out" / "repo_x" / "trial_1" / "M1-retry1" / "evaluation_result_filtered.json").exists()


def test_classification_drift_is_non_promotable(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    # a trial pinned to a data commit whose classification differs from the current one
    import subprocess
    root = data_root.parent
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pin"], cwd=root, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    trial_dir = cell.parent.parent
    (trial_dir / "trial_metadata.json").write_text(json.dumps({"data_version": {"commit": commit}}))
    assert _fo(cell, data_root, None).status == "filter-regenerated"  # pin matches
    (data_root / "test_results" / "M1" / "M1_classification.json").write_text(
        json.dumps({"stable_classification": {"pass_to_pass": [PY_A, PY_C, "extra"], "fail_to_pass": [PY_F]}}))
    rec = _fo(cell, data_root, None)
    assert (rec.status, rec.reason) == ("non-promotable", "classification-drift")


def test_invalid_filter_list_refuses_the_whole_campaign(tmp_path):
    bad = {"invalid_pass_to_pass": [PY_C, "ghost::not_in_universe"], "invalid_fail_to_pass": [], "invalid_none_to_pass": []}
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]), filter_list=bad)
    with pytest.raises(FilterListError, match="ghost"):
        run_campaign(data_root=data_root, cells=[cell], out_dir=tmp_path / "c", mirror=True, filter_only=True)
    assert not (tmp_path / "c" / "mirror").exists()
    # the same list refuses the replay modes too (a defective waiver is a defect in every mode)
    with pytest.raises(FilterListError):
        run_campaign(data_root=data_root, cells=[cell], out_dir=tmp_path / "c2", mirror=False)
    # per-cell: the validator is also consulted when a single cell is processed directly
    rec = _fo(cell, data_root, tmp_path / "out")
    assert (rec.status, rec.reason) == ("error", "filter-list-invalid") and rec.manifest["filter_list_errors"]
    rc = main(["--data-root", str(data_root), "--cell", str(cell), "--out", str(tmp_path / "m"), "--mode", "filter-only"])
    assert rc == 2


def test_main_filter_only_exit_codes_and_outputs(tmp_path, capsys):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    rc = main(["--data-root", str(data_root), "--cell", str(cell), "--out", str(tmp_path / "m"),
               "--mode", "filter-only", "--campaign", "v102-filters"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["by_status"] == {"filter-regenerated": 1} and out["mirrored"] == 1
    records = [json.loads(l) for l in (tmp_path / "m" / "records.jsonl").read_text().splitlines()]
    assert records[0]["manifest"]["campaign"] == "v102-filters"
    assert (tmp_path / "m" / "mirror" / "repo_x" / "trial_1" / "M1" / "evaluation_result_filtered.json").exists()
    # the #24 replay modes are unchanged by the new mode
    rc = main(["--data-root", str(data_root), "--cell", str(cell), "--out", str(tmp_path / "r"), "--mode", "report"])
    assert rc == 0


def test_previous_promotion_provenance_is_kept_under_supersedes(tmp_path):
    data_root, cell = _world(tmp_path, _h._payload(passed=[PY_A], failed=[PY_F]))
    (cell / "rescore_manifest.json").write_text(json.dumps({"tool": "rescore/2", "payload_sha256": "abc"}))
    (cell / "PROMOTION_NOTES.md").write_text("# Promotion notes (issue #24 re-tally)\n- old\n")
    rec = _fo(cell, data_root, tmp_path / "out")
    out_cell = tmp_path / "out" / "repo_x" / "trial_1" / "M1"
    m = json.loads((out_cell / "rescore_manifest.json").read_text())
    assert m["supersedes"] == {"tool": "rescore/2", "payload_sha256": "abc"}
    notes = (out_cell / "PROMOTION_NOTES.md").read_text()
    assert notes.startswith("# Promotion notes (filter-only") and "issue #24 re-tally" in notes
