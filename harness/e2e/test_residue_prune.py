"""Tests for residue prune (docs/residue-prune-spec.md, phases 1a/1b).

Covers the pure decision layer:
- v2 prunability predicate (code-source conjunction + never-delete classes)
- prune-set computation (base − tar, START provenance guard, keep-list)
- V3b never-delete safety assertion
- snapshot integrity check (phase 1a)
- EvaluationResult fail-loud fields
"""

import pytest

from harness.e2e.residue_prune import (
    ResiduePruneSafetyError,
    assert_prune_set_safe,
    check_snapshot_integrity,
    compute_prune_set,
    is_prunable,
    normalize_tar_members,
)
from harness.utils.src_filter import SrcFileFilter


def make_filter(**overrides):
    """navidrome-like config — the range where every audited edge case exists."""
    cfg = dict(
        src_dirs=["core/", "db/", "plugins/", "server/", "cmd/", "ui/src/"],
        test_dirs=["**/*_test.go", "**/testdata/**", "tests/**", "ui/src/**/*.test.*"],
        exclude_patterns=["**/wire_gen.go"],
        generated_patterns=["**/*.pb.go"],
        modifiable_test_patterns=["**/agents_plugin_test.go"],
    )
    cfg.update(overrides)
    return SrcFileFilter(**cfg)


# ---------------------------------------------------------------- is_prunable


def test_plain_source_file_is_prunable():
    f = make_filter()
    assert is_prunable("core/library.go", f, keep_list=frozenset()) is True


def test_test_file_never_prunable():
    f = make_filter()
    assert is_prunable("plugins/host_scheduler_test.go", f, keep_list=frozenset()) is False


def test_modifiable_test_never_prunable():
    # Hole #1 from the false-damage audit: the exam paper itself.
    f = make_filter()
    assert is_prunable("core/agents/agents_plugin_test.go", f, keep_list=frozenset()) is False


def test_generated_file_never_prunable():
    f = make_filter()
    assert is_prunable("core/model.pb.go", f, keep_list=frozenset()) is False


def test_excluded_file_never_prunable():
    f = make_filter()
    assert is_prunable("cmd/wire_gen.go", f, keep_list=frozenset()) is False


def test_non_code_asset_never_prunable():
    # Hole #2 from the false-damage audit: go:embed .sql, stylesheets, etc.
    f = make_filter()
    assert is_prunable("db/migrations/20250611000000_init.sql", f, keep_list=frozenset()) is False
    assert is_prunable("ui/src/component.pcss", f, keep_list=frozenset()) is False
    assert is_prunable("server/token_received.html", f, keep_list=frozenset()) is False


def test_outside_src_dirs_never_prunable():
    f = make_filter()
    assert is_prunable("go.mod", f, keep_list=frozenset()) is False
    assert is_prunable(".github/workflows/ci.yml", f, keep_list=frozenset()) is False


def test_keep_list_blocks_prunable_file():
    # The one confirmed scaffolding burr: src-space test mock.
    f = make_filter()
    keep = frozenset({"core/mock_library_service.go"})
    assert is_prunable("core/mock_library_service.go", f, keep_list=frozenset()) is True
    assert is_prunable("core/mock_library_service.go", f, keep_list=keep) is False


# ---------------------------------------------------------- compute_prune_set


def test_compute_prune_set_with_start_guard():
    """Phase 1b semantics: only agent-deleted START files are pruned."""
    f = make_filter()
    base_files = {
        "core/kept.go",  # in tar -> never a candidate
        "core/deleted_by_agent.go",  # in START, not in tar -> prune
        "core/gt_added.go",  # NOT in START (GT-added) -> guard keeps it
        "core/some_test.go",  # test file -> never
        "db/migrations/001.sql",  # asset -> never
    }
    tar_files = {"core/kept.go"}
    start_files = {"core/kept.go", "core/deleted_by_agent.go", "core/some_test.go"}
    pruned = compute_prune_set(base_files, tar_files, start_files, f, keep_list=frozenset())
    assert pruned == ["core/deleted_by_agent.go"]


def test_compute_prune_set_without_start_guard():
    """Phase 2 semantics (guard lifted): GT-added files are pruned too."""
    f = make_filter()
    base_files = {"core/deleted_by_agent.go", "core/gt_added.go", "core/kept.go"}
    tar_files = {"core/kept.go"}
    pruned = compute_prune_set(base_files, tar_files, None, f, keep_list=frozenset())
    assert pruned == ["core/deleted_by_agent.go", "core/gt_added.go"]


def test_compute_prune_set_respects_keep_list():
    f = make_filter()
    base_files = {"core/mock_library_service.go", "core/deleted_by_agent.go"}
    tar_files = set()
    start_files = set(base_files)
    keep = frozenset({"core/mock_library_service.go"})
    pruned = compute_prune_set(base_files, tar_files, start_files, f, keep_list=keep)
    assert pruned == ["core/deleted_by_agent.go"]


# ------------------------------------------------------- normalize_tar_members


def test_normalize_tar_members():
    members = ["./core/a.go", "core/", "core/b.go", "", "./", "ui/src/x.jsx"]
    assert normalize_tar_members(members) == {"core/a.go", "core/b.go", "ui/src/x.jsx"}


# ------------------------------------------------------- assert_prune_set_safe


def test_assert_safe_passes_on_clean_set():
    f = make_filter()
    assert_prune_set_safe(["core/deleted_by_agent.go"], f, keep_list=frozenset())


def test_assert_safe_raises_on_test_file():
    f = make_filter()
    with pytest.raises(ResiduePruneSafetyError):
        assert_prune_set_safe(["plugins/host_scheduler_test.go"], f, keep_list=frozenset())


def test_assert_safe_raises_on_asset():
    f = make_filter()
    with pytest.raises(ResiduePruneSafetyError):
        assert_prune_set_safe(["db/migrations/001.sql"], f, keep_list=frozenset())


def test_assert_safe_raises_on_keep_list_entry():
    f = make_filter()
    keep = frozenset({"core/mock_library_service.go"})
    with pytest.raises(ResiduePruneSafetyError):
        assert_prune_set_safe(["core/mock_library_service.go"], f, keep_list=keep)


# ---------------------------------------------------- check_snapshot_integrity


def test_snapshot_integrity_ok_with_few_missing():
    """A handful of agent deletions is normal work, not a capture failure."""
    f = make_filter()
    reference = {f"core/f{i}.go" for i in range(20)} | {"core/x_test.go", "go.mod"}
    tar_files = {f"core/f{i}.go" for i in range(18)}  # 2 missing
    report = check_snapshot_integrity(reference, tar_files, f, max_missing=10)
    assert report.ok is True
    assert report.missing_count == 2
    assert report.expected_count == 20  # test file and go.mod not snapshot-includable


def test_snapshot_integrity_flags_mass_missing():
    """deepseek pathology: whole packages absent from the tar."""
    f = make_filter()
    reference = {f"core/pkg/f{i}.go" for i in range(40)}
    tar_files = {f"core/pkg/f{i}.go" for i in range(5)}  # 35 missing
    report = check_snapshot_integrity(reference, tar_files, f, max_missing=10)
    assert report.ok is False
    assert report.missing_count == 35
    assert len(report.missing_sample) <= 20
    assert all(p.startswith("core/pkg/") for p in report.missing_sample)


# ------------------------------------------------ EvaluationResult new fields


def _mk_result(**overrides):
    from harness.e2e.evaluator import EvaluationResult

    kwargs = dict(
        milestone_id="m1",
        patch_is_None=False,
        patch_exists=True,
        patch_successfully_applied=True,
        resolved=False,
        fail_to_pass_success=[],
        fail_to_pass_failure=[],
        pass_to_pass_success_count=0,
        pass_to_pass_failure=[],
        pass_to_pass_missing=0,
        none_to_pass_success=[],
        none_to_pass_failure=[],
        total_tests=0,
        passed_tests=0,
        failed_tests=0,
        error_tests=0,
        skipped_tests=0,
        fail_to_pass_required=0,
        fail_to_pass_achieved=0,
        pass_to_pass_required=0,
        none_to_pass_required=0,
        none_to_pass_achieved=0,
    )
    kwargs.update(overrides)
    return EvaluationResult(**kwargs)


def test_evaluation_result_fail_loud_defaults():
    d = _mk_result().to_dict()
    assert d["base_tag"] == ""
    assert d["fallback_triggered"] is False
    assert d["residue_prune"] == {
        "enabled": False,
        "pruned_files_count": 0,
        "pruned_files": [],
        "keep_list_hits": [],
    }
    assert d["snapshot_integrity"] == {"ok": None, "missing_count": 0}


def test_evaluation_result_fail_loud_populated():
    d = _mk_result(
        base_tag="milestone-m1-start",
        fallback_triggered=True,
        residue_prune_enabled=True,
        pruned_files_count=2,
        pruned_files=["core/a.go", "core/b.go"],
        keep_list_hits=["core/mock_library_service.go"],
        snapshot_integrity_ok=True,
        snapshot_missing_count=1,
    ).to_dict()
    assert d["base_tag"] == "milestone-m1-start"
    assert d["fallback_triggered"] is True
    assert d["residue_prune"]["pruned_files_count"] == 2
    assert d["residue_prune"]["keep_list_hits"] == ["core/mock_library_service.go"]
    assert d["snapshot_integrity"] == {"ok": True, "missing_count": 1}
