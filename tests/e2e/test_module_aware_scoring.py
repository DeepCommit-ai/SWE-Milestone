"""Regression tests for module-aware Maven/Gradle scoring IDs."""

from harness.e2e.evaluator import (
    _build_scoring_test_outcomes,
    _lookup_scoring_outcome,
)
from harness.utils.test_id_normalizer import TestIdNormalizer as _TestIdNormalizer


def _indexes(payload):
    normalizer = _TestIdNormalizer(framework="maven", enable_normalization=True)
    exact, normalized, moduleless = _build_scoring_test_outcomes(
        payload,
        framework="maven",
        normalizer=normalizer,
    )
    return normalizer, exact, normalized, moduleless


def _lookup(test_id, indexes):
    normalizer, exact, normalized, moduleless = indexes
    return _lookup_scoring_outcome(
        test_id,
        framework="maven",
        outcomes=exact,
        normalized_groups=normalized,
        java_moduleless_groups=moduleless,
        normalizer=normalizer,
    )


def test_same_class_and_method_in_two_modules_remain_distinct():
    indexes = _indexes(
        {
            "results": {
                "passed": ["module-a::org.example.SharedTest::sameName"],
                "failed": [{"nodeid": "module-b::org.example.SharedTest::sameName"}],
            }
        }
    )

    assert _lookup("module-a::org.example.SharedTest::sameName", indexes) == "passed"
    assert _lookup("module-b::org.example.SharedTest::sameName", indexes) == "failed"


def test_moduleless_fallback_refuses_ambiguous_cross_module_match():
    indexes = _indexes(
        {
            "results": {
                "passed": [
                    "module-a::org.example.SharedTest::sameName",
                    "module-b::org.example.SharedTest::sameName",
                ]
            }
        }
    )

    assert _lookup("org.example.SharedTest::sameName", indexes) == "unknown"


def test_moduleless_fallback_accepts_one_unique_owner():
    indexes = _indexes(
        {"results": {"passed": ["module-a::org.example.SharedTest::sameName"]}}
    )

    assert _lookup("org.example.SharedTest::sameName", indexes) == "passed"
    assert _lookup("legacy-module::org.example.SharedTest::sameName", indexes) == "unknown"


def test_moduleful_baseline_can_match_one_moduleless_runtime_id():
    indexes = _indexes(
        {"results": {"passed": ["org.example.SharedTest::sameName"]}}
    )

    assert _lookup("module-a::org.example.SharedTest::sameName", indexes) == "passed"


def test_java_hashcode_normalization_does_not_strip_module():
    indexes = _indexes(
        {
            "results": {
                "passed": [
                    "module-a::org.example.ParamTest::body [Book@5faeeb56]"
                ]
            }
        }
    )

    assert (
        _lookup("module-a::org.example.ParamTest::body [Book@62f11ebb]", indexes)
        == "passed"
    )
