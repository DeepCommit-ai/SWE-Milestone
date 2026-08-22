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
