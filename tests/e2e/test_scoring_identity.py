"""Scoring identity (issue #24).

The scoring key is a test's identity. Two distinct tests that share a name
must never share one outcome slot: a passing test would inherit a namesake's
failure (fail-close), and a test that never ran would be credited with a
namesake's pass (missing masking). These tests pin the identity-preserving key
for every framework, keep the legacy policies replayable, and cover the
evaluator-side consequences (collision records, untrusted locking, provenance).
"""

import json
from pathlib import Path

import pytest

from harness.e2e.evaluator import (
    SCORING_ID_POLICY_IDENTITY,
    SCORING_ID_POLICY_LEGACY,
    SCORING_ID_POLICY_LEGACY_PASSWINS,
    EvaluationResult,
    _build_scoring_indexes,
    _build_scoring_test_outcomes,
    _infer_framework_from_test_config,
    _lookup_scoring_outcome,
    normalize_scoring_nodeid,
    select_classification,
    tally_scoring,
)
from harness.utils.test_id_normalizer import TestIdNormalizer

JEST_A = (
    "test/unit-tests/components/views/rooms/NotificationDecoration/Notifications-test.tsx"
    "::<Notifications /> > clear all notifications > clears all notifications"
)
JEST_B = (
    "test/unit-tests/components/views/rooms/notifications/Notifications2-test.tsx"
    "::<Notifications /> > clear all notifications > clears all notifications"
)
PY_A = "sklearn/tests/test_pipeline.py::test_routing_passed_metadata_not_supported[decision_function]"
PY_B = (
    "sklearn/semi_supervised/tests/test_self_training.py"
    "::test_routing_passed_metadata_not_supported[decision_function]"
)
CARGO_A = "glob::tests::any1"
CARGO_B = "pathutil::tests::any1"
GO_A = "github.com/zeromicro/go-zero/core/cmdline/TestEnterToContinue"
GINKGO_BASELINE = "github.com/navidrome/navidrome/persistence::PlaylistRepository > Exists > returns true"
GINKGO_RUNTIME = "persistence::PlaylistRepository > Exists > returns true"
GINKGO_OTHER_PKG = "utils::PlaylistRepository > Exists > returns true"

IDENTITY_FRAMEWORKS = [
    "pytest",
    "jest",
    "cargo",
    "go_test",
    "vitest",
    "playwright",
    "mocha",
    "unittest",
    "django_runtests",
    "nushell_script",
    None,
    "some-future-framework",
]


def _payload(passed=(), failed=(), error=(), skipped=(), xpassed=(), xfailed=(), total=None):
    results = {
        "passed": list(passed),
        "failed": [{"nodeid": n} for n in failed],
        "error": [{"nodeid": n} for n in error],
        "xpassed": [{"nodeid": n} for n in xpassed],
        "xfailed": [{"nodeid": n} for n in xfailed],
        "skipped": [{"tests": list(skipped)}] if skipped else [],
    }
    n = (
        total
        if total is not None
        else len(passed) + len(failed) + len(error) + len(skipped) + len(xpassed) + len(xfailed)
    )
    return {
        "results": results,
        "summary": {
            "total": n,
            "passed": len(passed),
            "failed": len(failed),
            "error": len(error),
            "skipped": len(skipped),
        },
    }


def _tally(payload, classification, framework, policy=SCORING_ID_POLICY_IDENTITY):
    baseline = {"stable_classification": classification}
    normalizer = TestIdNormalizer(framework=framework, enable_normalization=True)
    return tally_scoring(
        payload, baseline, framework=framework, normalizer=normalizer, policy=policy
    )


# --- the key itself -------------------------------------------------------


@pytest.mark.parametrize("framework", IDENTITY_FRAMEWORKS)
@pytest.mark.parametrize("nodeid", [JEST_A, JEST_B, PY_A, CARGO_A, GO_A, "weird @abc123 title > x"])
def test_identity_policy_returns_raw_id(framework, nodeid):
    assert normalize_scoring_nodeid(nodeid, framework) == nodeid


@pytest.mark.parametrize("framework", IDENTITY_FRAMEWORKS)
def test_identity_policy_keeps_namesakes_distinct(framework):
    assert normalize_scoring_nodeid(JEST_A, framework) != normalize_scoring_nodeid(JEST_B, framework)
    assert normalize_scoring_nodeid(PY_A, framework) != normalize_scoring_nodeid(PY_B, framework)
    assert normalize_scoring_nodeid(CARGO_A, framework) != normalize_scoring_nodeid(CARGO_B, framework)


def test_legacy_policy_still_drops_prefix_for_replay():
    legacy = SCORING_ID_POLICY_LEGACY
    assert normalize_scoring_nodeid(JEST_A, "jest", policy=legacy) == normalize_scoring_nodeid(
        JEST_B, "jest", policy=legacy
    )
    assert normalize_scoring_nodeid(CARGO_A, "cargo", policy=legacy) == "tests::any1"


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        normalize_scoring_nodeid(PY_A, "pytest", policy="not-a-policy")


@pytest.mark.parametrize("policy", [SCORING_ID_POLICY_IDENTITY, SCORING_ID_POLICY_LEGACY])
def test_maven_keeps_module_and_folds_hashcode(policy):
    a = "module-a::org.example.ParamTest::body [Book@5faeeb56]"
    b = "module-a::org.example.ParamTest::body [Book@62f11ebb]"
    other_module = "module-b::org.example.ParamTest::body [Book@5faeeb56]"
    assert normalize_scoring_nodeid(a, "maven", policy=policy) == normalize_scoring_nodeid(
        b, "maven", policy=policy
    )
    assert normalize_scoring_nodeid(a, "maven", policy=policy) != normalize_scoring_nodeid(
        other_module, "maven", policy=policy
    )
    assert normalize_scoring_nodeid(a, "maven", policy=policy).startswith("module-a::")


def test_ginkgo_bridge_is_unchanged_under_identity():
    # Module-qualified baseline must still meet the module-relative runtime id.
    assert normalize_scoring_nodeid(GINKGO_BASELINE, "ginkgo") == normalize_scoring_nodeid(
        GINKGO_RUNTIME, "ginkgo"
    )


def test_identity_does_not_rewrite_hash_like_text_outside_java():
    title = "test/unit-tests/x-test.ts::renders token @deadbe"
    assert normalize_scoring_nodeid(title, "jest") == title


# --- the #24 defect, both directions ---------------------------------------


def test_jest_pair_no_longer_inherits_namesake_failure():
    payload = _payload(passed=[JEST_A], failed=[JEST_B])
    classification = {"pass_to_pass": [JEST_A], "fail_to_fail": [JEST_B]}
    new = _tally(payload, classification, "jest")
    old = _tally(payload, classification, "jest", policy=SCORING_ID_POLICY_LEGACY)
    assert new.pass_to_pass_failure == []
    assert new.pass_to_pass_success_count == 1
    assert old.pass_to_pass_failure == [JEST_A]


def test_pytest_p2p_resolves_when_only_namesake_f2f_fails():
    # scikit-learn M11 shape: the P2P test passes; a same-named F2F test in
    # another file fails (as the baseline expects it to).
    payload = _payload(passed=[PY_A], failed=[PY_B])
    classification = {"pass_to_pass": [PY_A], "fail_to_fail": [PY_B]}
    new = _tally(payload, classification, "pytest")
    old = _tally(payload, classification, "pytest", policy=SCORING_ID_POLICY_LEGACY)
    assert new.strict_resolved is True
    assert old.strict_resolved is False


def test_cargo_pair_is_distinct():
    payload = _payload(passed=[CARGO_A], failed=[CARGO_B])
    classification = {"pass_to_pass": [CARGO_A, CARGO_B]}
    new = _tally(payload, classification, "cargo")
    assert new.pass_to_pass_failure == [CARGO_B]
    assert new.pass_to_pass_success_count == 1


def test_go_test_ids_have_no_prefix_to_drop():
    assert normalize_scoring_nodeid(GO_A, "go_test", policy=SCORING_ID_POLICY_LEGACY) == GO_A
    assert normalize_scoring_nodeid(GO_A, "go_test") == GO_A


def test_missing_p2p_is_not_masked_by_namesake_pass():
    payload = _payload(passed=[PY_B])
    classification = {"pass_to_pass": [PY_A, PY_B]}
    new = _tally(payload, classification, "pytest")
    old = _tally(payload, classification, "pytest", policy=SCORING_ID_POLICY_LEGACY)
    assert (new.pass_to_pass_success_count, new.pass_to_pass_missing) == (1, 1)
    assert new.missing_p2p_ids == [PY_A]
    assert (old.pass_to_pass_success_count, old.pass_to_pass_missing) == (2, 0)


def test_missing_f2p_and_n2p_are_not_credited_via_namesake():
    payload = _payload(passed=[PY_B])
    classification = {"fail_to_pass": [PY_A], "none_to_pass": [PY_A]}
    new = _tally(payload, classification, "pytest")
    old = _tally(payload, classification, "pytest", policy=SCORING_ID_POLICY_LEGACY)
    assert new.fail_to_pass_failure == [PY_A] and new.fail_to_pass_success == []
    assert new.none_to_pass_failure == [PY_A] and new.none_to_pass_missing == 1
    assert old.fail_to_pass_success == [PY_A]
    assert old.none_to_pass_success == [PY_A]


@pytest.mark.parametrize(
    "observed, legacy_bucket",
    [
        ("passed", "success"),
        ("failed", "failure"),
        ("error", "failure"),
        ("skipped", "missing"),
        ("xfailed", "success"),
        ("xpassed", "failure"),
    ],
)
def test_namesake_outcome_matrix(observed, legacy_bucket):
    kwargs = {observed: [PY_B]}
    payload = _payload(**kwargs)
    classification = {"pass_to_pass": [PY_A]}
    new = _tally(payload, classification, "pytest")
    old = _tally(payload, classification, "pytest", policy=SCORING_ID_POLICY_LEGACY)
    # Identity: PY_A was never observed, whatever PY_B did.
    assert (new.pass_to_pass_success_count, len(new.pass_to_pass_failure), new.pass_to_pass_missing) == (0, 0, 1)
    legacy = {
        "success": (1, 0, 0),
        "failure": (0, 1, 0),
        "missing": (0, 0, 1),
    }[legacy_bucket]
    assert (old.pass_to_pass_success_count, len(old.pass_to_pass_failure), old.pass_to_pass_missing) == legacy


# --- repeated observations and collisions ----------------------------------


@pytest.mark.parametrize("first, second", [("passed", "failed"), ("failed", "passed")])
def test_repeated_observation_aggregates_conservatively_regardless_of_order(first, second):
    payload = {"results": {first: [PY_A] if first == "passed" else [{"nodeid": PY_A}], second: [PY_A] if second == "passed" else [{"nodeid": PY_A}]}, "summary": {"total": 2}}
    exact, _, _ = _build_scoring_test_outcomes(payload, framework="pytest")
    assert exact[PY_A] == "failed"


def test_passwins_policy_lets_last_observation_win():
    payload = {"results": {"failed": [{"nodeid": PY_A}], "passed": [PY_A]}, "summary": {"total": 2}}
    idx = _build_scoring_indexes(payload, framework="pytest", policy=SCORING_ID_POLICY_LEGACY_PASSWINS)
    legacy_key = normalize_scoring_nodeid(PY_A, "pytest", policy=SCORING_ID_POLICY_LEGACY_PASSWINS)
    assert idx.exact[legacy_key] == "passed"
    idx_new = _build_scoring_indexes(payload, framework="pytest")
    assert idx_new.exact[PY_A] == "failed"


def test_same_raw_id_repeated_is_not_a_collision():
    payload = {"results": {"failed": [{"nodeid": PY_A}], "passed": [PY_A]}, "summary": {"total": 2}}
    idx = _build_scoring_indexes(payload, framework="pytest")
    assert idx.collisions == [] and idx.untrusted is False


def test_maven_hashcode_instances_are_an_approved_collision():
    a = "module-a::org.example.ParamTest::body [Book@5faeeb56]"
    b = "module-a::org.example.ParamTest::body [Book@62f11ebb]"
    idx = _build_scoring_indexes(_payload(passed=[a], failed=[b]), framework="maven")
    assert len(idx.collisions) == 1
    assert idx.collisions[0]["kind"] == "java-hashcode-instances"
    assert idx.collisions[0]["approved"] is True
    assert idx.untrusted is False


def test_ginkgo_residual_collision_is_recorded_not_untrusted():
    idx = _build_scoring_indexes(
        _payload(passed=[GINKGO_RUNTIME], failed=[GINKGO_OTHER_PKG]), framework="ginkgo"
    )
    assert [c["kind"] for c in idx.collisions] == ["ginkgo-prefix-drop-residual"]
    assert idx.untrusted is False


def test_legacy_replay_collisions_never_mark_untrusted():
    idx = _build_scoring_indexes(
        _payload(passed=[PY_A], failed=[PY_B]), framework="pytest", policy=SCORING_ID_POLICY_LEGACY
    )
    assert [c["kind"] for c in idx.collisions] == ["legacy-prefix-drop"]
    assert idx.untrusted is False


def test_unexpected_lossy_key_marks_scoring_untrusted(monkeypatch):
    import harness.e2e.evaluator as ev

    def lossy(nodeid, framework, go_module=None, policy=SCORING_ID_POLICY_IDENTITY):
        return nodeid.rsplit("::", 1)[-1]

    monkeypatch.setattr(ev, "normalize_scoring_nodeid", lossy)
    idx = ev._build_scoring_indexes(_payload(passed=[PY_A], failed=[PY_B]), framework="pytest")
    assert idx.untrusted is True
    assert idx.collisions[0]["kind"] == "unexpected-lossy-key"
    tally = ev.tally_scoring(
        _payload(passed=[PY_A], failed=[PY_B]),
        {"stable_classification": {"pass_to_pass": [PY_A]}},
        framework="pytest",
        normalizer=None,
    )
    assert tally.identity_untrusted is True


def test_collision_records_are_sorted_and_order_independent():
    p1 = _payload(passed=[PY_A, CARGO_A], failed=[PY_B, CARGO_B])
    p2 = _payload(passed=[CARGO_A, PY_A], failed=[CARGO_B, PY_B])
    i1 = _build_scoring_indexes(p1, framework="pytest", policy=SCORING_ID_POLICY_LEGACY)
    i2 = _build_scoring_indexes(p2, framework="pytest", policy=SCORING_ID_POLICY_LEGACY)
    assert i1.collisions == i2.collisions
    assert i1.exact == i2.exact


# --- lookup fallbacks keep working ------------------------------------------


def test_lookup_exact_and_fuzzy_fallback_under_identity():
    normalizer = TestIdNormalizer(framework="go_test", enable_normalization=True)
    payload = _payload(passed=["github.com/x/y/TestMap/JBzrWpYM", "github.com/x/y/TestMap/bYuXm9Hl"])
    exact, normalized, java = _build_scoring_test_outcomes(payload, framework="go_test", normalizer=normalizer)
    assert (
        _lookup_scoring_outcome(
            "github.com/x/y/TestMap/zzzzzzzz",
            framework="go_test",
            outcomes=exact,
            normalized_groups=normalized,
            java_moduleless_groups=java,
            normalizer=normalizer,
        )
        == "passed"
    )


# --- tally semantics preserved -----------------------------------------------


def test_select_classification_prefers_stable():
    assert select_classification({"stable_classification": {"a": 1}, "classification": {"b": 2}})[1] == "stable_classification"
    assert select_classification({"classification": {"b": 2}})[1] == "classification"
    assert select_classification({"pass_to_pass": []})[1] == "root"


def test_tally_zero_tests_with_required_is_never_resolved():
    payload = _payload(total=0)
    tally = _tally(payload, {"pass_to_pass": [PY_A]}, "pytest")
    assert tally.strict_resolved is False
    assert tally.pass_to_pass_missing == 1


def test_tally_category_conservation():
    payload = _payload(passed=[PY_A, JEST_A], failed=[CARGO_A])
    classification = {
        "fail_to_pass": [PY_A, PY_B],
        "pass_to_pass": [JEST_A, JEST_B, CARGO_A],
        "none_to_pass": [GO_A],
    }
    t = _tally(payload, classification, "pytest")
    assert len(t.fail_to_pass_ids) == len(t.fail_to_pass_success) + len(t.fail_to_pass_failure)
    assert len(t.pass_to_pass_ids) == (
        t.pass_to_pass_success_count + len(t.pass_to_pass_failure) + t.pass_to_pass_missing
    )
    assert len(t.none_to_pass_ids) == len(t.none_to_pass_success) + len(t.none_to_pass_failure)
    assert t.none_to_pass_missing <= len(t.none_to_pass_failure)


def test_tally_falls_back_to_new_tests_for_n2p():
    payload = _payload(passed=[PY_A])
    baseline = {
        "stable_classification": {"pass_to_pass": []},
        "new_tests": [{"test_id": PY_A, "end_outcome": "passed"}, {"test_id": PY_B, "end_outcome": "failed"}],
    }
    t = tally_scoring(payload, baseline, framework="pytest", normalizer=None)
    assert t.none_to_pass_success == [PY_A]


# --- framework inference (PR-0) --------------------------------------------


def test_infer_framework_accepts_legacy_object_form(tmp_path):
    cfg_dir = tmp_path / "dockerfiles" / "M020"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "test_config.json").write_text(
        json.dumps(
            {
                "test_framework": "maven",
                "test_command_template": "mvn test -Dtest={test_class} -pl {module}",
                "test_classes": [{"class_name": "org.apache.X", "module": "m", "test_command": "mvn test"}],
            }
        )
    )
    assert _infer_framework_from_test_config(tmp_path, "M020") == "maven"


def test_infer_framework_object_form_without_explicit_key_uses_commands(tmp_path):
    cfg_dir = tmp_path / "dockerfiles" / "M021"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "test_config.json").write_text(
        json.dumps({"test_command_template": "python -m pytest {test_class}"})
    )
    assert _infer_framework_from_test_config(tmp_path, "M021") == "pytest"


def test_infer_framework_list_form_unchanged(tmp_path):
    cfg_dir = tmp_path / "dockerfiles" / "M001"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "test_config.json").write_text(json.dumps([{"name": "default", "test_cmd": "cargo test"}]))
    assert _infer_framework_from_test_config(tmp_path, "M001") == "cargo"
    (cfg_dir / "test_config.json").write_text(json.dumps("garbage"))
    assert _infer_framework_from_test_config(tmp_path, "M001") is None


# --- result envelope ----------------------------------------------------------


def _result(**overrides):
    base = dict(
        milestone_id="M1",
        patch_is_None=False,
        patch_exists=True,
        patch_successfully_applied=True,
        resolved=True,
        fail_to_pass_success=[],
        fail_to_pass_failure=[],
        pass_to_pass_success_count=1,
        pass_to_pass_failure=[],
        pass_to_pass_missing=0,
        none_to_pass_success=[],
        none_to_pass_failure=[],
        total_tests=1,
        passed_tests=1,
        failed_tests=0,
        error_tests=0,
        skipped_tests=0,
        fail_to_pass_required=0,
        fail_to_pass_achieved=0,
        pass_to_pass_required=1,
        none_to_pass_required=0,
        none_to_pass_achieved=0,
    )
    base.update(overrides)
    return EvaluationResult(**base)


def test_result_carries_scoring_identity_and_roundtrips():
    r = _result(
        scoring_id_policy=SCORING_ID_POLICY_IDENTITY,
        scoring_identity={"policy": SCORING_ID_POLICY_IDENTITY, "payload_sha256": "abc", "untrusted": False},
    )
    d = r.to_dict()
    assert d["scoring_identity"]["policy"] == SCORING_ID_POLICY_IDENTITY
    assert d["scoring_identity"]["payload_sha256"] == "abc"
    back = EvaluationResult.from_result_dict(d)
    assert back.scoring_id_policy == SCORING_ID_POLICY_IDENTITY
    assert back.identity_collision_untrusted is False


def test_identity_untrusted_locks_resolution():
    r = _result(identity_collision_untrusted=True)
    assert r.scoring_untrusted is True
    assert r.resolution_locked_false is True
    d = r.to_dict()
    assert d["scoring_identity"]["untrusted"] is True
    assert EvaluationResult.from_result_dict(d).identity_collision_untrusted is True


def test_legacy_result_without_scoring_identity_still_loads():
    d = _result().to_dict()
    d.pop("scoring_identity")
    back = EvaluationResult.from_result_dict(d)
    assert back.scoring_id_policy == ""
    assert back.identity_collision_untrusted is False


# --- real-fixture property (skipped when the data root is absent) ------------

DATA_ROOT = Path("/data2/gangda/SWE-Milestone-data")
REPO_FRAMEWORKS = {
    "BurntSushi_ripgrep_14.1.1_15.0.0": "cargo",
    "scikit-learn_scikit-learn_1.5.2_1.6.0": "pytest",
    "element-hq_element-web_v1.11.95_v1.11.97": "jest",
    "zeromicro_go-zero_v1.6.0_v1.9.3": "go_test",
    "nushell_nushell_0.106.0_0.108.0": "cargo",
}


@pytest.mark.skipif(not DATA_ROOT.exists(), reason="benchmark data root not available")
@pytest.mark.parametrize("repo, framework", sorted(REPO_FRAMEWORKS.items()))
def test_identity_key_is_injective_on_real_universes(repo, framework):
    universes = sorted((DATA_ROOT / repo / "test_results").glob("*/*_classification.json"))[:4]
    assert universes, f"no classification files for {repo}"
    for path in universes:
        doc = json.loads(path.read_text())
        classification, _ = select_classification(doc)
        ids = set()
        for bucket in ("fail_to_pass", "none_to_pass", "pass_to_pass", "fail_to_fail", "pass_to_fail"):
            for item in classification.get(bucket) or []:
                ids.add(item if isinstance(item, str) else item.get("test_id", ""))
        ids.discard("")
        keys = {normalize_scoring_nodeid(i, framework) for i in ids}
        assert len(keys) == len(ids), f"{path.name}: identity key merged {len(ids) - len(keys)} ids"
