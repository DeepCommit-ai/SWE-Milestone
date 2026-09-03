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


# ── downgrade must reach the RESULT even when residue-prune never runs ──
#
# Incident (dubbo parity re-eval, 2026-07-18): the escape hatch fired and set
# the instance attribute, but the emitted result said legacy_unverified=False
# because the constructor read the _eval_meta copy, which only the
# residue-prune phase syncs — and dubbo skips pruning. A legacy UNVERIFIED
# evaluation masquerading as promotion-grade is a fail-closed integrity gap.

def _result_flag(ev):
    """The exact expression both result-construction sites now use."""
    return bool(
        getattr(ev, "snapshot_legacy_unverified", False)
        or ev._eval_meta.get("snapshot_legacy_unverified", False)
    )


def test_downgrade_survives_skipped_prune_phase(tmp_path):
    ev = _bare(tmp_path, allow_legacy=True)
    ev._eval_meta = {"snapshot_legacy_unverified": False}  # stale: prune skipped
    ev._load_and_validate_snapshot_metadata()
    assert _result_flag(ev) is True


def test_non_legacy_run_stays_false(tmp_path):
    ev = _bare(tmp_path, allow_legacy=False)
    ev._eval_meta = {"snapshot_legacy_unverified": False}
    assert _result_flag(ev) is False


def test_meta_only_downgrade_still_propagates(tmp_path):
    # Defensive symmetry: a future phase recording the downgrade only in the
    # meta copy must remain visible too.
    ev = _bare(tmp_path, allow_legacy=False)
    ev._eval_meta = {"snapshot_legacy_unverified": True}
    assert _result_flag(ev) is True


def test_construction_sites_consult_live_attribute():
    # Source-level guard against reintroducing the meta-only read.
    import inspect

    import harness.e2e.evaluator as mod

    src = inspect.getsource(mod)
    assert src.count("self.snapshot_legacy_unverified\n                or self._eval_meta.get(") >= 1, (
        "success-path EvaluationResult construction no longer consults the "
        "live snapshot_legacy_unverified attribute"
    )
    assert 'getattr(evaluator, "snapshot_legacy_unverified", False)' in src, (
        "failure-path finalizer no longer consults the live attribute"
    )
