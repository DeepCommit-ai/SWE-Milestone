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
    tar_files = {f"core/f{i}.go" for i in range(18)} | {"go.mod"}  # 2 missing
    report = check_snapshot_integrity(
        reference, tar_files, f, max_missing=10, extra_build_manifests={"go.mod"}
    )
    assert report.ok is True
    assert report.missing_count == 2
    assert report.expected_count == 21  # test file excluded; root go.mod retained


def test_snapshot_integrity_covers_root_build_manifest():
    f = make_filter()
    report = check_snapshot_integrity(
        {"core/f.go", "go.mod"},
        {"core/f.go"},
        f,
        extra_build_manifests={"go.mod"},
    )
    assert report.expected_count == 2
    assert report.missing_count == 1
    assert report.missing_sample == ["go.mod"]


def test_snapshot_integrity_covers_only_explicit_external_build_manifests():
    f = make_filter()
    reference = {
        "core/f.go",
        "dubbo-test/changed/pom.xml",
        "dubbo-test/untouched/pom.xml",
    }
    report = check_snapshot_integrity(
        reference,
        {"core/f.go"},
        f,
        extra_build_manifests={"dubbo-test/changed/pom.xml"},
    )
    assert report.expected_count == 2
    assert report.missing_count == 1
    assert report.missing_sample == ["dubbo-test/changed/pom.xml"]


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
    assert d["end_compile_error"] == ""
    assert d["residue_prune"] == {
        "enabled": False,
        "extensions": [],
        "keep_list": [],
        "policy_source": "",
        "policy_sha256": "",
        "enablement_source": "",
        "pruned_files_count": 0,
        "pruned_files": [],
        "keep_list_hits": [],
        "skipped_reason": "",
    }
    assert d["snapshot_integrity"] == {"ok": None, "missing_count": 0, "legacy_unverified": False}


def test_pinned_repo_config_is_java_only_and_ignores_live_metadata_drift():
    from harness.e2e.evaluator import resolve_residue_prune_config

    digest = "a" * 64
    repo_config = {
        "repo_src_dirs": ["dubbo-rpc"],
        "test_dirs": ["**/src/test/**", "**/*Test.java"],
        "exclude": ["**/target/**"],
        "residue_prune": True,
        "prune_extensions": [".java"],
        "prune_keep_list": [],
    }
    # Deliberately conflicting live metadata: the frozen repo config must win.
    metadata = {
        "repo_src_dirs": ["wrong-live-root"],
        "test_dirs": ["wrong-live-tests/**"],
        "residue_prune": False,
        "prune_extensions": [".groovy"],
        "prune_keep_list": ["dubbo-rpc/Keep.java"],
    }

    resolved = resolve_residue_prune_config(
        repo_config,
        metadata,
        repo_config_binding_mode="trial-pinned",
        repo_config_sha256=digest,
    )

    assert resolved.requested is True
    assert resolved.policy_source == "repo-config-pinned"
    assert resolved.policy_sha256 == digest
    assert resolved.extensions == frozenset({".java"})
    assert resolved.keep_list == frozenset()
    assert resolved.src_filter is not None
    assert is_prunable(
        "dubbo-rpc/src/main/java/Service.java",
        resolved.src_filter,
        resolved.keep_list,
        resolved.extensions,
    )
    assert not is_prunable(
        "dubbo-rpc/src/main/groovy/Service.groovy",
        resolved.src_filter,
        resolved.keep_list,
        resolved.extensions,
    )


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
    infra_invalid = _mk_result(
        total_tests=0,
        none_to_pass_required=1,
    )
    assert infra_invalid.scoring_untrusted is True
    assert infra_invalid.resolved is False
    assert infra_invalid.infra_invalid_reason == "zero-tests-with-required-tests"
    assert infra_invalid.to_dict()["eval_status"] == "infra-invalid"


def test_required_test_counts_prefer_stable_classification():
    from harness.e2e.evaluator import baseline_required_test_counts

    baseline = {
        "classification": {
            "fail_to_pass": ["unstable-f2p"],
            "none_to_pass": ["unstable-n2p"],
            "pass_to_pass": ["unstable-p2p"],
        },
        "stable_classification": {
            "fail_to_pass": ["stable-f2p"],
            "none_to_pass": [],
            "pass_to_pass": ["stable-p2p-1", "stable-p2p-2"],
        },
    }

    assert baseline_required_test_counts(baseline) == {
        "fail_to_pass": 1,
        "none_to_pass": 0,
        "pass_to_pass": 2,
    }
    # the integrity gate is GONE: mass-missing is no longer a fail-closed reason
    # (a near-empty tar prunes and scores honestly, it is not "protected").
    assert "snapshot-integrity-failed" not in _fail_closed_reasons()


def test_orchestrator_preserves_locked_false_verdicts():
    """F1: outer threshold recompute must AND every locked-false verdict."""
    from harness.e2e import orchestrator as orch_mod
    import inspect

    src = inspect.getsource(orch_mod._run_evaluation_once)
    assert "resolution_locked_false" in src, (
        "orchestrator does not preserve evaluator locked-false verdicts"
    )


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
        end_compile_error="core/service.go:42:7: undefined: missingSymbol",
        residue_prune_enabled=True,
        residue_prune_extensions=[".java"],
        residue_prune_keep_list=[],
        residue_prune_policy_source="repo-config-pinned",
        residue_prune_policy_sha256="a" * 64,
        residue_prune_enablement_source="repo-config-pinned",
        pruned_files_count=2,
        pruned_files=["core/a.go", "core/b.go"],
        keep_list_hits=["core/mock_library_service.go"],
        snapshot_integrity_ok=True,
        snapshot_missing_count=1,
        residue_prune_skipped_reason="ls-tree-failed",
    ).to_dict()
    assert d["base_tag"] == "milestone-m1-start"
    assert d["fallback_triggered"] is True
    assert d["end_compile_error"] == "core/service.go:42:7: undefined: missingSymbol"
    assert d["residue_prune"]["extensions"] == [".java"]
    assert d["residue_prune"]["policy_source"] == "repo-config-pinned"
    assert d["residue_prune"]["policy_sha256"] == "a" * 64
    assert d["residue_prune"]["pruned_files_count"] == 2
    assert d["residue_prune"]["keep_list_hits"] == ["core/mock_library_service.go"]
    assert d["residue_prune"]["skipped_reason"] == "ls-tree-failed"
    assert d["snapshot_integrity"] == {"ok": True, "missing_count": 1, "legacy_unverified": False}

    from harness.e2e.evaluator import EvaluationResult

    restored = EvaluationResult.from_result_dict(d)
    assert restored.residue_prune_enabled is True
    assert restored.residue_prune_extensions == [".java"]
    assert restored.residue_prune_policy_source == "repo-config-pinned"
    assert restored.residue_prune_policy_sha256 == "a" * 64


# ---------------------------------------------------------------------------
# Capture-side loss detection (spec §11.4-H rewrite, 2026-07-11)
# ---------------------------------------------------------------------------

from harness.e2e.residue_prune import (  # noqa: E402
    capture_scope_covered,
    classify_capture_loss,
    parse_status_porcelain_z,
)

SNAPSHOT_PATHS = ["core/", "server", "go.mod", "go.sum"]


def _z(*entries):
    return "\0".join(entries) + "\0"


def test_porcelain_z_basic_entries():
    out = _z(" M core/a.go", "?? core/new.go", "A  server/b.go")
    assert parse_status_porcelain_z(out) == ["core/a.go", "core/new.go", "server/b.go"]


def test_porcelain_z_skips_deletions():
    # A deleted-but-uncommitted file is not lost work: the tar keeps the
    # tagged version. Both index (D ) and worktree ( D) deletions drop.
    out = _z("D  core/gone.go", " D core/gone2.go", " M core/kept.go")
    assert parse_status_porcelain_z(out) == ["core/kept.go"]


def test_porcelain_z_rename_consumes_original_path():
    # R entries carry one extra NUL field (the original path) — it must be
    # consumed, NOT parsed as a standalone entry.
    out = _z("R  core/new_name.go", "core/old_name.go", "?? core/x.go")
    assert parse_status_porcelain_z(out) == ["core/new_name.go", "core/x.go"]


def test_porcelain_z_empty_and_garbage():
    assert parse_status_porcelain_z("") == []
    assert parse_status_porcelain_z("\0") == []
    assert parse_status_porcelain_z("xy") == []  # too short / no separator


def test_capture_scope_covered_dir_prefix_and_exact_file():
    assert capture_scope_covered("core/a/b.go", SNAPSHOT_PATHS)
    assert capture_scope_covered("server/main.go", SNAPSHOT_PATHS)
    assert capture_scope_covered("go.mod", SNAPSHOT_PATHS)
    assert not capture_scope_covered("docs/README.md", SNAPSHOT_PATHS)
    # Prefix must be component-wise: "server2/x.go" is NOT under "server".
    assert not capture_scope_covered("server2/x.go", SNAPSHOT_PATHS)
    assert not capture_scope_covered("go.mod.bak", SNAPSHOT_PATHS)


def test_classify_capture_loss_buckets():
    f = make_filter()
    paths = [
        "core/uncommitted.go",       # in scope -> lost_in
        "scripts/helper.py",         # outside scope -> lost_out
        "core/foo_test.go",          # test file -> dropped (by-design filtering)
        "core/wire_gen.go",          # excluded -> dropped
    ]
    lost_in, lost_out = classify_capture_loss(paths, f, ["core/", "server/"])
    assert lost_in == ["core/uncommitted.go"]
    assert lost_out == ["scripts/helper.py"]


def test_classify_capture_loss_empty():
    f = make_filter()
    assert classify_capture_loss([], f, SNAPSHOT_PATHS) == ([], [])


# ---------------------------------------------------------------------------
# Default-OFF compatibility policy + all-language predicate edges
# ---------------------------------------------------------------------------

from harness.e2e.residue_prune import resolve_prune_enablement  # noqa: E402


def test_enablement_default_off_with_partition():
    assert resolve_prune_enablement(None, True) == (False, "default-off")


def test_enablement_legacy_without_partition_stays_off():
    # Old datasets without src/test split: additive overlay, NOT untrusted.
    assert resolve_prune_enablement(None, False) == (False, "legacy-no-partition")


def test_enablement_explicit_flag_honored():
    assert resolve_prune_enablement(False, True) == (False, "explicit")
    assert resolve_prune_enablement(True, True) == (True, "explicit")
    # Explicit True without partition: still "requested" — the evaluator's
    # config-invalid fail-closed path (F3) takes it from here.
    assert resolve_prune_enablement(True, False) == (True, "explicit")


# Real per-range filter configs (copied from SWE-Milestone-data metadata) —
# each range's audited never-delete edge must hold under the multi-language
# default extension set.

def _filter_from(cfg):
    return SrcFileFilter(
        src_dirs=cfg["src"], test_dirs=cfg["test"],
        exclude_patterns=cfg.get("excl", []), generated_patterns=cfg.get("gen", []),
        modifiable_test_patterns=cfg.get("mod", []),
    )


NUSHELL = {"src": ["src/", "crates/"],
           "test": ["tests/**", "benches/**", "crates/*/tests/**", "crates/*/benches/**",
                    "crates/nu-cmd-lang/src/example_test.rs"],
           "excl": ["crates/nu_plugin_python/**"]}
RIPGREP = {"src": ["crates/"],
           "test": ["tests/**", "crates/*/tests/**", "crates/*/benches/**", "benchsuite/**", "fuzz/**"],
           "excl": ["crates/*/examples/**", "crates/core/flags/doc/*.help", "crates/core/flags/doc/*.1"]}
ELEMENT = {"src": ["src/", "packages/shared-components/src/", "res/css/"],
           "test": ["test/**", "playwright/**", "__mocks__/**", "**/*.test.*", "**/*.spec.*",
                    "**/__snapshots__/**", "**/__tests__/**"],
           "excl": ["**/*.stories.*", "**/i18n/strings/**"]}
DUBBO = {"src": ["dubbo-common/", "dubbo-plugin/", "dubbo-config/"],
         "test": ["**/src/test/**", "**/*Test.java", "**/*Tests.java", "dubbo-test/**", "dubbo-demo/**"],
         "excl": ["**/target/**"]}
SCIKIT = {"src": ["sklearn/"],
          "test": ["**/test_*.py", "**/conftest.py", "**/tests/**"],
          "excl": ["sklearn/datasets/data/**", "sklearn/datasets/descr/**"]}


def test_all_language_rust_src_prunable_tests_protected():
    f = _filter_from(NUSHELL)
    assert is_prunable("crates/nu-cli/src/repl.rs", f, frozenset())
    # Inline-test FILES named in test_dirs are tests, never pruned (V1-R).
    assert not is_prunable("crates/nu-cmd-lang/src/example_test.rs", f, frozenset())
    assert not is_prunable("crates/nu-command/tests/commands/ls.rs", f, frozenset())
    # Excluded plugin examples: never pruned.
    assert not is_prunable("crates/nu_plugin_python/plugin.py", f, frozenset())
    # Non-code assets in src dirs: never pruned.
    assert not is_prunable("crates/nu-command/assets/228_themes.zip", f, frozenset())


def test_all_language_ripgrep_docs_and_completions_protected():
    f = _filter_from(RIPGREP)
    assert is_prunable("crates/core/main.rs", f, frozenset())
    # Generated-doc/completion assets (audited §8.3-style edges): no code ext.
    assert not is_prunable("crates/core/flags/doc/rg.1", f, frozenset())
    assert not is_prunable("crates/core/flags/doc/args.help", f, frozenset())


def test_all_language_element_pcss_protected_tsx_prunable():
    f = _filter_from(ELEMENT)
    assert is_prunable("src/components/views/rooms/RoomListPanel/EmptyRoomList.tsx", f, frozenset())
    # .pcss (audited §8.3: 9 GT-added in this range): non-code ext, never pruned.
    assert not is_prunable("res/css/views/rooms/_EmptyRoomList.pcss", f, frozenset())
    assert not is_prunable("src/components/structures/RoomView.test.tsx", f, frozenset())
    assert not is_prunable("src/i18n/strings/en_EN.json", f, frozenset())


def test_all_language_dubbo_spi_and_mustache_protected():
    f = _filter_from(DUBBO)
    assert is_prunable("dubbo-plugin/dubbo-mutiny/src/main/java/org/apache/dubbo/mutiny/calls/MutinyClientCalls.java", f, frozenset())
    # SPI declaration files and mustache templates (audited §8.3: real
    # flip-score risk if deleted): no code extension, never pruned.
    assert not is_prunable("dubbo-plugin/dubbo-mutiny/src/main/resources/META-INF/dubbo/org.apache.dubbo.rpc.protocol.tri.stub.StubSuppliers", f, frozenset())
    assert not is_prunable("dubbo-config/dubbo-config-spring/src/main/resources/template/service.mustache", f, frozenset())
    assert not is_prunable("dubbo-common/src/test/java/org/apache/dubbo/FooTest.java", f, frozenset())


def test_all_language_scikit_pyx_prunable_data_protected():
    f = _filter_from(SCIKIT)
    assert is_prunable("sklearn/utils/_unique.py", f, frozenset())
    assert is_prunable("sklearn/tree/_tree.pyx", f, frozenset())
    assert is_prunable("sklearn/tree/_tree.pxd", f, frozenset())
    assert not is_prunable("sklearn/utils/tests/test_unique.py", f, frozenset())
    assert not is_prunable("sklearn/datasets/data/iris.csv", f, frozenset())
    assert not is_prunable("sklearn/datasets/descr/iris.rst", f, frozenset())
