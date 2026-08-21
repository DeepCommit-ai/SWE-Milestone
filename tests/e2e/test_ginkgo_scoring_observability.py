"""Record-only observability for lost test universes (issue #22).

An id that never appeared in the report and an id that ran and failed are
both scored as not-achieved — that is the intended semantics and this change
does not touch it. What the record could not express is *which* of the two
happened, so an analyst cannot tell a build-dead suite from a genuine failure
without re-reading raw reports.

These tests pin both halves: the new counters are populated, and the scored
numbers (achieved / resolved / the failure lists themselves) are unchanged.
"""

from harness.e2e.evaluator import (
    EvaluationResult,
    _absent_suites_from_missing_ids,
    _partition_none_to_pass,
)


def _outcomes(mapping):
    return lambda test_id: mapping.get(test_id, "unknown")


class TestPartitionNoneToPass:
    def test_absent_id_is_counted_missing_and_still_scored_as_failure(self):
        lookup = _outcomes({"pkg::ran and failed": "failed"})

        success, failure, missing = _partition_none_to_pass(
            ["pkg::ran and failed", "pkg::never ran"], lookup
        )

        assert success == []
        # Scoring semantics unchanged: both are non-achievements.
        assert failure == ["pkg::ran and failed", "pkg::never ran"]
        # New: the record now says one of them never produced a result.
        assert missing == 1

    def test_passed_ids_are_unaffected(self):
        lookup = _outcomes({"pkg::ok": "passed"})

        success, failure, missing = _partition_none_to_pass(["pkg::ok"], lookup)

        assert (success, failure, missing) == (["pkg::ok"], [], 0)

    def test_error_outcome_is_a_failure_not_missing(self):
        lookup = _outcomes({"pkg::boom": "error"})

        success, failure, missing = _partition_none_to_pass(["pkg::boom"], lookup)

        assert (success, failure, missing) == ([], ["pkg::boom"], 0)

    def test_blank_ids_are_skipped(self):
        success, failure, missing = _partition_none_to_pass(["", None], _outcomes({}))

        assert (success, failure, missing) == ([], [], 0)


class TestAbsentSuiteReconciliation:
    def test_suite_with_no_observed_tests_is_reported_absent(self):
        absent = _absent_suites_from_missing_ids(
            missing_ids=["github.com/x/proj/persistence::A > b"],
            observed_nodeids=["github.com/x/proj/conf::C > d"],
            framework="ginkgo",
        )

        assert absent == ["github.com/x/proj/persistence"]

    def test_suite_that_reported_some_tests_is_not_absent(self):
        """One missing id out of a live suite is an ordinary miss (ID drift,
        a skipped spec) — not evidence that the whole suite never ran."""
        absent = _absent_suites_from_missing_ids(
            missing_ids=["github.com/x/proj/conf::A > b"],
            observed_nodeids=["github.com/x/proj/conf::C > d"],
            framework="ginkgo",
        )

        assert absent == []

    def test_multiple_absent_suites_are_deduped_and_sorted(self):
        absent = _absent_suites_from_missing_ids(
            missing_ids=[
                "github.com/x/proj/model::A > b",
                "github.com/x/proj/model::A > c",
                "github.com/x/proj/persistence::D > e",
            ],
            observed_nodeids=["github.com/x/proj/utils::U > v"],
            framework="ginkgo",
        )

        assert absent == ["github.com/x/proj/model", "github.com/x/proj/persistence"]

    def test_scoped_to_ginkgo_ids(self):
        """Only the ``package::hierarchy`` dialect carries a suite identity in
        the id itself; do not guess one for other frameworks."""
        assert (
            _absent_suites_from_missing_ids(
                missing_ids=["test/foo.spec.ts::renders"],
                observed_nodeids=["test/bar.spec.ts::renders"],
                framework="jest",
            )
            == []
        )

    def test_ids_without_a_package_separator_are_ignored(self):
        assert (
            _absent_suites_from_missing_ids(
                missing_ids=["TestSomething"],
                observed_nodeids=[],
                framework="ginkgo",
            )
            == []
        )


def _result(**overrides):
    base = dict(
        milestone_id="M1",
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
        none_to_pass_failure=["pkg::a", "pkg::b"],
        total_tests=10,
        passed_tests=8,
        failed_tests=2,
        error_tests=0,
        skipped_tests=0,
        fail_to_pass_required=0,
        fail_to_pass_achieved=0,
        pass_to_pass_required=0,
        none_to_pass_required=2,
        none_to_pass_achieved=0,
    )
    base.update(overrides)
    return EvaluationResult(**base)


class TestResultSerialization:
    def test_missing_and_absent_suites_round_trip(self):
        result = _result(none_to_pass_missing=2, absent_suites=["github.com/x/proj/model"])

        payload = result.to_dict()
        assert payload["tests_status"]["NONE_TO_PASS"]["missing"] == 2
        assert payload["test_summary"]["none_to_pass_missing"] == 2
        assert payload["build_failure_policy"]["absent_suites"] == [
            "github.com/x/proj/model"
        ]

        restored = EvaluationResult.from_result_dict(payload)
        assert restored.none_to_pass_missing == 2
        assert restored.absent_suites == ["github.com/x/proj/model"]

    def test_legacy_payload_without_the_new_fields_still_loads(self):
        payload = _result().to_dict()
        del payload["tests_status"]["NONE_TO_PASS"]["missing"]
        del payload["test_summary"]["none_to_pass_missing"]
        del payload["build_failure_policy"]["absent_suites"]

        restored = EvaluationResult.from_result_dict(payload)
        assert restored.none_to_pass_missing == 0
        assert restored.absent_suites == []

    def test_new_fields_do_not_move_the_score(self):
        """The whole point: the counters describe the same outcome, they do
        not re-grade it."""
        plain = _result()
        annotated = _result(
            none_to_pass_missing=2, absent_suites=["github.com/x/proj/model"]
        )

        for field in (
            "resolved",
            "none_to_pass_achieved",
            "none_to_pass_required",
            "none_to_pass_failure",
            "scored_failure_reason",
            "infra_invalid_reason",
        ):
            assert getattr(plain, field) == getattr(annotated, field), field

    def test_absent_suites_alone_is_not_build_failure_evidence(self):
        """An absent suite has an ambiguous cause (build death, ID drift, a
        skipped module), so it must not by itself reclassify a zero-test cell
        from infra-invalid to a graded build failure."""
        zero = _result(
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            none_to_pass_missing=2,
            absent_suites=["github.com/x/proj/model"],
        )

        assert zero._has_build_failure_evidence() is False
        assert zero.scored_failure_reason == ""
