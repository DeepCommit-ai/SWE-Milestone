"""--allow-legacy-snapshot: explicit, recorded escape hatch for pre-sidecar
snapshots. Default stays fail-closed; the flag returns synthetic metadata with
an empty manifest overlay and flags the result as legacy_unverified."""

import pytest

from harness.e2e.evaluator import PatchEvaluator


def _bare(tmp_path, allow_legacy):
    ev = object.__new__(PatchEvaluator)
    ev.patch_file = tmp_path / "source_snapshot.tar"
    ev.patch_file.write_bytes(b"tar")
    ev.allow_legacy_snapshot = allow_legacy
    ev.snapshot_legacy_unverified = False
    ev._snapshot_metadata = None
    ev._manifest_overlay = None
    return ev


def test_missing_sidecar_still_fails_closed_by_default(tmp_path):
    ev = _bare(tmp_path, allow_legacy=False)
    with pytest.raises(RuntimeError, match="sidecar is missing"):
        ev._load_and_validate_snapshot_metadata()


def test_flag_returns_synthetic_metadata_and_records_downgrade(tmp_path, capsys):
    ev = _bare(tmp_path, allow_legacy=True)
    data, overlay = ev._load_and_validate_snapshot_metadata()
    assert data["legacy_unverified"] is True
    assert data["ok"] is None
    assert overlay.upserts == frozenset() and overlay.deletes == frozenset()
    assert ev.snapshot_legacy_unverified is True
    assert "LEGACY snapshot" in capsys.readouterr().out
    # cached on the instance for the other call sites
    assert ev._load_and_validate_snapshot_metadata()[0] is data
