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
    DEFAULT_PRUNE_EXTENSIONS,
    ResiduePruneSafetyError,
    assert_prune_set_safe,
    check_snapshot_integrity,
    compute_prune_set,
    is_prunable,
    normalize_keep_list,
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


GO_ONLY = frozenset({".go"})


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


# ---- F3: per-range extension whitelist (language scoping) ----


def test_extensions_param_scopes_language():
    # navidrome phase 1: prune only Go, never its ui/src TS front-end.
    f = make_filter()
    assert is_prunable("ui/src/actions/library.ts", f, keep_list=frozenset()) is True  # default whitelist
    assert is_prunable("ui/src/actions/library.ts", f, keep_list=frozenset(), extensions=GO_ONLY) is False
    assert is_prunable("core/library.go", f, keep_list=frozenset(), extensions=GO_ONLY) is True


def test_default_extensions_is_multilang():
    assert ".go" in DEFAULT_PRUNE_EXTENSIONS and ".ts" in DEFAULT_PRUNE_EXTENSIONS


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


def test_compute_prune_set_extension_scoped():
    # F3: with extensions={.go}, a deleted .ts survives even inside src.
    f = make_filter()
    base_files = {"core/gone.go", "ui/src/gone.ts"}
    start_files = set(base_files)
    pruned = compute_prune_set(base_files, set(), start_files, f, keep_list=frozenset(), extensions=GO_ONLY)
    assert pruned == ["core/gone.go"]


def test_compute_prune_set_honors_capture_excluded():
    """F2 (drift): a path the capture filter dropped from the tar (test/exclude
    at capture time) must not be pruned even if the current eval filter calls
    it source. Its tar-absence is by-design filtering, not agent deletion.
    """
    f = make_filter()
    base_files = {"core/foo_check.go", "core/deleted_by_agent.go"}
    start_files = set(base_files)
    # eval-side filter (drifted): foo_check.go now looks like source.
    assert is_prunable("core/foo_check.go", f, keep_list=frozenset()) is True
    pruned = compute_prune_set(
        base_files,
        tar_files=set(),
        start_files=start_files,
        src_filter=f,
        keep_list=frozenset(),
        capture_excluded=frozenset({"core/foo_check.go"}),
    )
    assert pruned == ["core/deleted_by_agent.go"]  # foo_check.go protected by witness


def test_capture_witness_from_config_covers_agent_deleted_test():
    """F4 (codex): the witness is rebuilt from the capture filter config against
    the START tree, so it protects a GT test the agent DELETED (which the old
    agent-tree-derived file list would have missed)."""
    from harness.e2e.residue_prune import capture_excluded_from_config, capture_filter_config

    f = make_filter()
    cfg = capture_filter_config(f)
    # START tree has a test file the agent later deleted (absent from its tar).
    start = {"core/foo_check.go", "core/impl.go", "plugins/host_scheduler_test.go"}
    witness = capture_excluded_from_config(cfg, start)
    # the *_test.go is a capture-time test -> in witness -> protected
    assert "plugins/host_scheduler_test.go" in witness
    # plain source -> not in witness
    assert "core/impl.go" not in witness


def test_assert_safe_raises_on_capture_excluded():
    # F2: V3b aborts if a capture-excluded path reaches the prune set.
    f = make_filter()
    with pytest.raises(ResiduePruneSafetyError):
        assert_prune_set_safe(
            ["core/foo_check.go"],
            f,
            keep_list=frozenset(),
            capture_excluded=frozenset({"core/foo_check.go"}),
        )


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


def test_snapshot_integrity_relative_threshold():
    """F1: threshold is relative — a tiny tree missing a few isn't flagged,
    but a large tree missing a large fraction is (absolute floor still applies)."""
    f = make_filter()
    # small tree, 3 missing of 12: absolute floor (10) keeps it ok
    small_ref = {f"core/f{i}.go" for i in range(12)}
    small_tar = {f"core/f{i}.go" for i in range(9)}
    assert check_snapshot_integrity(small_ref, small_tar, f).ok is True
    # large tree, 20% missing: relative rule flags it even though we could set
    # an absolute floor high
    big_ref = {f"core/f{i}.go" for i in range(200)}
    big_tar = {f"core/f{i}.go" for i in range(160)}  # 40 missing = 20%
    assert check_snapshot_integrity(big_ref, big_tar, f).ok is False


# ---- L4: keep-list normalization ----


def test_normalize_keep_list():
    raw = ["./core/mock.go", "core/x.go/", "  db/y.go  ", "core/x.go"]
    assert normalize_keep_list(raw) == frozenset({"core/mock.go", "core/x.go", "db/y.go"})


def test_keep_list_protects_after_normalization():
    f = make_filter()
    keep = normalize_keep_list(["./core/mock_library_service.go"])
    assert is_prunable("core/mock_library_service.go", f, keep_list=keep) is False


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
        "skipped_reason": "",
    }
    assert d["snapshot_integrity"] == {"ok": None, "missing_count": 0}


def test_scoring_untrusted_property():
    """F1 (codex critical): a mechanism-failure skip reason makes the result
    scoring-untrusted, so the orchestrator's threshold recompute cannot flip
    resolved back to True."""
    assert _mk_result().scoring_untrusted is False
    assert _mk_result(residue_prune_skipped_reason="ls-tree-failed").scoring_untrusted is True
    assert _mk_result(residue_prune_skipped_reason="tar-unreadable").scoring_untrusted is True
    assert _mk_result(residue_prune_skipped_reason="config-invalid").scoring_untrusted is True
    # a completed prune (or nothing to prune) is trusted
    assert _mk_result(residue_prune_skipped_reason="").scoring_untrusted is False
    # the integrity gate is GONE: mass-missing is no longer a fail-closed reason
    # (a near-empty tar prunes and scores honestly, it is not "protected").
    assert "snapshot-integrity-failed" not in _fail_closed_reasons()


def test_orchestrator_ands_scoring_untrusted():
    """F1: the orchestrator resolution recompute must AND in the safety flag."""
    from harness.e2e import orchestrator as orch_mod
    import inspect

    src = inspect.getsource(orch_mod._run_evaluation_once)
    assert "scoring_untrusted" in src, "orchestrator does not consult scoring_untrusted"


def test_extension_normalization():
    """F6: extensions normalize (lowercase, require leading dot); absent != empty."""
    from harness.e2e.residue_prune import normalize_extensions

    assert normalize_extensions(["GO", ".Rs", "py"]) == frozenset({".go", ".rs", ".py"})
    assert normalize_extensions(None) is None  # absent -> caller uses default
    assert normalize_extensions([]) == frozenset()  # empty -> prune nothing (not default)


def _fail_closed_reasons():
    from harness.e2e.residue_prune import FAIL_CLOSED_SKIP_REASONS

    return FAIL_CLOSED_SKIP_REASONS


def test_fail_closed_reasons_are_mechanism_failures_only():
    """The fail-closed set covers only mechanism failures — NOT a heuristic
    integrity gate (which was removed). Pin it so nobody re-adds a
    'snapshot suspicious -> skip+protect' path (the fail-open hole)."""
    assert _fail_closed_reasons() == frozenset({"ls-tree-failed", "tar-unreadable", "config-invalid"})


def test_compare_results_consults_fail_closed_set():
    from harness.e2e import evaluator as ev_mod
    import inspect

    src = inspect.getsource(ev_mod.PatchEvaluator.compare_results)
    assert "FAIL_CLOSED_SKIP_REASONS" in src


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
        residue_prune_skipped_reason="ls-tree-failed",
    ).to_dict()
    assert d["base_tag"] == "milestone-m1-start"
    assert d["fallback_triggered"] is True
    assert d["residue_prune"]["pruned_files_count"] == 2
    assert d["residue_prune"]["keep_list_hits"] == ["core/mock_library_service.go"]
    assert d["residue_prune"]["skipped_reason"] == "ls-tree-failed"
    assert d["snapshot_integrity"] == {"ok": True, "missing_count": 1}
