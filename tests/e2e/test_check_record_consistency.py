"""scripts/check_record_consistency.py: the release gate over served cells."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_record_consistency.py"
_spec = importlib.util.spec_from_file_location("test_rescore_helpers", Path(__file__).with_name("test_rescore.py"))
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)  # PY_A/PY_B, _payload, _make_world, BASELINE, COLLIDING


def _world(tmp_path, payload, baseline, trial="_trial_pub", cell="M1", filter_list=None):
    data_root, cell_dir = _h._make_world(tmp_path, payload, baseline, filter_list=filter_list)
    # published layout: <log>/<repo>/e2e_trial/<trial>/evaluation/<cell>; _make_world used trial_1 -> rename
    log_root = tmp_path / "log"
    src_trial = cell_dir.parent.parent
    dst_trial = log_root / "repo_x" / "e2e_trial" / trial
    if src_trial != dst_trial:
        shutil.move(str(src_trial), str(dst_trial))
    ev = dst_trial / "evaluation"
    if cell != "M1":
        shutil.move(str(ev / "M1"), str(ev / cell))
    (ev / "summary.json").write_text(json.dumps({"results": {cell: {"attempt": 0, "eval_status": "failed"}}}))
    return data_root, log_root, ev / cell


def _run(log_root, data_root, extra=()):
    return subprocess.run([sys.executable, str(SCRIPT), "--log-root", str(log_root), "--data-root", str(data_root.parent),
                           "--out", str(log_root / ".cache" / "rc"), "--jobs", "1", *extra], capture_output=True, text=True)


def test_consistent_cell_passes_and_collision_cell_fails_until_accepted(tmp_path):
    clean = _h._payload(passed=[_h.PY_A, _h.PY_C])          # no namesake collision: legacy == identity
    data_root, log_root, cell = _world(tmp_path, clean, {"stable_classification": {"pass_to_pass": [_h.PY_A, _h.PY_C]}})
    r = _run(log_root, data_root)
    assert r.returncode == 0 and "OK:" in r.stdout, r.stdout + r.stderr
    # a second trial whose stored legacy tally differs from the identity re-tally
    data_root2, log_root2, cell2 = _world(tmp_path / "w2", _h.COLLIDING, _h.BASELINE, trial="_trial_bad")
    r = _run(log_root2, data_root2)
    assert r.returncode == 1 and "needs promotion of the re-tally" in r.stdout, r.stdout
    (log_root2 / "ACCEPTED_LEGACY.tsv").write_text("repo_x\t_trial_bad\tM1\tknown: awaiting re-tally promotion\n")
    r = _run(log_root2, data_root2)
    assert r.returncode == 0 and "accepted" in r.stdout, r.stdout


def test_stale_filtered_file_fails(tmp_path):
    clean = _h._payload(passed=[_h.PY_A, _h.PY_C])
    data_root, log_root, cell = _world(tmp_path, clean, {"stable_classification": {"pass_to_pass": [_h.PY_A, _h.PY_C]}})
    # a filtered file although the milestone has no filter list -> shadows the result
    (cell / "evaluation_result_filtered.json").write_text((cell / "evaluation_result.json").read_text())
    r = _run(log_root, data_root)
    assert r.returncode == 1 and "no filter list now" in r.stdout, r.stdout


def test_local_trials_are_skipped_unless_requested(tmp_path):
    data_root, log_root, cell = _world(tmp_path, _h.COLLIDING, _h.BASELINE, trial="local_trial")
    r = _run(log_root, data_root)
    assert r.returncode == 0, r.stdout                    # not published -> not checked
    r = _run(log_root, data_root, ["--all-trials"])
    assert r.returncode == 1, r.stdout


# --- v1.0.2: filter-list validation, derivative-missing, the shared regeneration rule ---

PY_F = "sklearn/tests/test_feature.py::test_feature"
UNIVERSE = {"stable_classification": {"pass_to_pass": [_h.PY_A, _h.PY_C], "fail_to_pass": [PY_F]}}
FL_C = {"invalid_pass_to_pass": [_h.PY_C], "invalid_fail_to_pass": [], "invalid_none_to_pass": []}


def _regenerate(cell, data_root):
    """The derivative the filter-only re-tally would promote for this cell."""
    from harness.e2e.rescore import filter_only_cell
    out = cell.parent.parent.parent.parent.parent / "mirror"
    rec = filter_only_cell(cell, data_root=data_root, mirror_dir=out)
    assert rec.status == "filter-regenerated", (rec.status, rec.reason)
    src = out / "repo_x" / cell.parent.parent.name / cell.name / "evaluation_result_filtered.json"
    shutil.copy(src, cell / "evaluation_result_filtered.json")


def test_derivative_missing_for_a_filtered_universe_fails_until_promoted(tmp_path):
    payload = _h._payload(passed=[_h.PY_A], failed=[PY_F])        # C never ran -> missing, and waived
    data_root, log_root, cell = _world(tmp_path, payload, UNIVERSE, filter_list=FL_C)
    r = _run(log_root, data_root)
    assert r.returncode == 1 and "derivative missing for a universe with a filter list" in r.stdout, r.stdout
    _regenerate(cell, data_root)
    r = _run(log_root, data_root)
    assert r.returncode == 0 and "OK:" in r.stdout, r.stdout + r.stderr


def test_derivative_regenerated_under_the_union_rule_passes_and_an_old_one_fails(tmp_path):
    payload = _h._payload(passed=[_h.PY_A], failed=[PY_F])
    data_root, log_root, cell = _world(tmp_path, payload, UNIVERSE, filter_list=FL_C)
    # C ran in a second artifact dir: the union rule says it is not missing
    second = cell / "artifacts" / "456"
    second.mkdir()
    (second / "eval.json").write_text(json.dumps({"tests": [{"nodeid": _h.PY_C}]}))
    _regenerate(cell, data_root)
    r = _run(log_root, data_root)
    assert r.returncode == 0, r.stdout
    # a derivative written under the single-payload rule disagrees on pass_to_pass_missing
    f = json.loads((cell / "evaluation_result_filtered.json").read_text())
    f["test_summary"]["pass_to_pass_missing"] = 0
    f["test_summary"]["pass_to_pass_achieved"] = 1
    f["tests_status"]["PASS_TO_PASS"]["missing"] = 0
    (cell / "evaluation_result_filtered.json").write_text(json.dumps(f))
    r = _run(log_root, data_root)
    assert r.returncode == 1 and "disagrees with the current filter list" in r.stdout, r.stdout


def test_derivative_whose_resolved_disagrees_with_the_locked_regeneration_fails(tmp_path):
    payload = _h._payload(passed=[_h.PY_A, PY_F])               # only C missing; waived -> resolvable
    data_root, log_root, cell = _world(tmp_path, payload, UNIVERSE, filter_list=FL_C)
    _regenerate(cell, data_root)
    r = _run(log_root, data_root)
    assert r.returncode == 0, r.stdout
    # the raw result later carries an infrastructure lock the derivative ignores
    raw = json.loads((cell / "evaluation_result.json").read_text())
    raw["infrastructure_failure"] = "docker daemon died"
    (cell / "evaluation_result.json").write_text(json.dumps(raw))
    r = _run(log_root, data_root)
    assert r.returncode == 1 and "resolved" in r.stdout, r.stdout


def test_invalid_filter_list_fails_every_served_cell_of_the_repo_without_crashing(tmp_path):
    payload = _h._payload(passed=[_h.PY_A], failed=[PY_F])
    bad = {"invalid_pass_to_pass": [_h.PY_C, "ghost::nowhere"], "invalid_fail_to_pass": [], "invalid_none_to_pass": []}
    data_root, log_root, cell = _world(tmp_path, payload, UNIVERSE, filter_list=bad)
    r = _run(log_root, data_root)
    assert r.returncode == 1 and "filter list invalid" in r.stdout and "ghost" in r.stdout, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
