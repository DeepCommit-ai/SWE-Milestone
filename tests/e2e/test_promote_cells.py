"""scripts/promote_cells.py: atomic, append-only, idempotent promotion into the primary record."""
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "promote_cells.py"
REPO = "repo_x"


def _result(resolved, total=10):
    return {"milestone_id": "M1", "resolved": resolved, "patch_successfully_applied": True,
            "test_summary": {"total": total, "passed": total - 1, "failed": 1, "error": 0}}


def _world(tmp_path):
    log = tmp_path / "log"
    ev = log / REPO / "e2e_trial" / "trial_1" / "evaluation"
    for cell, resolved in (("M1", False), ("M1-retry1", False), ("M2", False)):
        d = ev / cell
        (d / "artifacts" / "111").mkdir(parents=True)
        (d / "evaluation_result.json").write_text(json.dumps(_result(resolved)))
        (d / "artifacts" / "111" / "eval_summary.json").write_text('{"old": true}')
        (d / "source_snapshot.tar").write_bytes(b"frozen")
        with tarfile.open(d / "artifacts.tar.gz", "w:gz") as tf:
            tf.add(d / "artifacts", arcname="artifacts")
    (ev / "M1-retry1" / "evaluation_result_filtered.json").write_text(json.dumps(_result(False, 9)))  # stale
    summary = {"results": {
        "M1": {"attempt": 0, "eval_status": "failed", "dag_status": "unlocked", "test_summary": {"total": 10}},
        "M1-retry1": {"attempt": 1, "eval_status": "failed", "dag_status": "unlocked", "test_summary": {"total": 10}},
        "M2": {"attempt": 0, "eval_status": "failed", "dag_status": "unlocked", "test_summary": {"total": 10}},
    }, "milestone_status": {"available": ["M3"]}}
    (ev / "summary.json").write_text(json.dumps(summary))
    (ev / "summary_filtered.json").write_text(json.dumps(dict(summary, filtered=True)))
    # mirror source: new result for M1-retry1 (resolved), new artifacts, no filtered; M2 unchanged copy
    mir = tmp_path / "mirror"
    m = mir / REPO / "trial_1" / "M1-retry1"
    (m / "artifacts" / "222").mkdir(parents=True)
    (m / "evaluation_result.json").write_text(json.dumps(_result(True, 12)))
    (m / "artifacts" / "222" / "eval_summary.json").write_text('{"new": true}')
    (m / "rescore_manifest.json").write_text('{"tool": "x"}')
    m2 = mir / REPO / "trial_1" / "M2"
    m2.mkdir(parents=True)
    (m2 / "evaluation_result.json").write_text(json.dumps(_result(False)))
    return log, mir, ev


def _run(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_dry_run_writes_nothing_and_execute_promotes_atomically(tmp_path):
    log, mir, ev = _world(tmp_path)
    before = (ev / "M1-retry1" / "evaluation_result.json").read_text()
    common = ["--log-root", str(log), "--repo", REPO, "--source-root", str(mir), "--layout", "mirror",
              "--all-in-source", "--backup-root", str(tmp_path / "bk"), "--campaign", "c1"]
    r = _run(common)
    assert r.returncode == 0 and "DRY RUN" in r.stdout and "to promote: 1" in r.stdout, r.stdout + r.stderr
    assert "M2: already promoted" in r.stdout.replace("trial_1/", "")
    assert (ev / "M1-retry1" / "evaluation_result.json").read_text() == before
    r = _run(common + ["--execute"])
    assert r.returncode == 0, r.stdout + r.stderr
    cell = ev / "M1-retry1"
    assert json.loads((cell / "evaluation_result.json").read_text())["resolved"] is True
    assert not (cell / "evaluation_result_filtered.json").exists()            # stale filtered deleted
    assert (cell / "artifacts" / "222" / "eval_summary.json").exists() and not (cell / "artifacts" / "111").exists()
    with tarfile.open(cell / "artifacts.tar.gz") as tf:
        assert any(m.name.endswith("222/eval_summary.json") for m in tf.getmembers())
    assert (cell / "rescore_manifest.json").exists()
    assert (cell / "source_snapshot.tar").read_bytes() == b"frozen"
    bk = tmp_path / "bk" / "c1" / REPO / "trial_1" / "M1-retry1"
    assert json.loads((bk / "evaluation_result.json").read_text())["resolved"] is False
    assert (bk / "evaluation_result_filtered.json").exists() and (bk / "artifacts" / "111").exists() and (bk / "artifacts.tar.gz").exists()
    summary = json.loads((ev / "summary.json").read_text())
    assert summary["results"]["M1-retry1"]["eval_status"] == "passed" and summary["results"]["M1-retry1"]["attempt"] == 1
    assert summary["results"]["M1-retry1"]["test_summary"]["total"] == 12
    assert summary["results"]["M1"]["eval_status"] == "failed"                   # other keys untouched
    assert summary["completed"] == ["M1-retry1"] and set(summary["failed"]) == {"M1", "M2"}
    assert summary["milestone_status"]["available"] == ["M3"]
    filt = json.loads((ev / "summary_filtered.json").read_text())
    assert filt["results"]["M1-retry1"]["eval_status"] == "passed" and filt["filtered"] is True
    assert (tmp_path / "bk" / "c1" / REPO / "trial_1" / "summary.json.before").exists()
    # idempotent: a second run has nothing to do, and the append-only backup refuses reuse
    r = _run(common + ["--execute"])
    assert r.returncode == 0 and "to promote: 0" in r.stdout
    r = _run(["--log-root", str(log), "--repo", REPO, "--source-root", str(mir), "--layout", "mirror",
              "--cell", "trial_1/M1", "--backup-root", str(tmp_path / "bk"), "--campaign", "c1", "--execute"])
    assert r.returncode in (0, 3)  # M1 not in source -> skipped (0) ... and a reused campaign with work refuses (3)


def test_summary_keys_are_never_created_for_unknown_attempts(tmp_path):
    log, mir, ev = _world(tmp_path)
    # a retry2 directory exists in the record and in the source, but the summary does not know it
    d = ev / "M1-retry2"; d.mkdir()
    (d / "evaluation_result.json").write_text(json.dumps(_result(False)))
    m = mir / REPO / "trial_1" / "M1-retry2"; m.mkdir()
    (m / "evaluation_result.json").write_text(json.dumps(_result(True)))
    r = _run(["--log-root", str(log), "--repo", REPO, "--source-root", str(mir), "--layout", "mirror",
              "--cell", "trial_1/M1-retry2", "--backup-root", str(tmp_path / "bk"), "--campaign", "c2", "--execute"])
    assert r.returncode == 0, r.stdout + r.stderr
    summary = json.loads((ev / "summary.json").read_text())
    assert "M1-retry2" not in summary["results"]
    manifest = json.loads((tmp_path / "bk" / "c2" / f"PROMOTION_MANIFEST_{REPO}.json").read_text())
    assert any("not created" in (m.get("note") or "") for m in manifest)


def test_campaign_layout_and_missing_source_is_skipped(tmp_path):
    log, mir, ev = _world(tmp_path)
    camp = tmp_path / "camp"
    c = camp / REPO / "e2e_trial" / "trial_1" / "evaluation" / "M2"
    c.mkdir(parents=True)
    (c / "evaluation_result.json").write_text(json.dumps(_result(True)))
    r = _run(["--log-root", str(log), "--repo", REPO, "--source-root", str(camp), "--layout", "campaign",
              "--cell", "trial_1/M2", "--cell", "trial_1/M9", "--backup-root", str(tmp_path / "bk"), "--campaign", "c3"])
    assert r.returncode == 0 and "to promote: 1" in r.stdout and "M9: source has no evaluation_result.json" in r.stdout
