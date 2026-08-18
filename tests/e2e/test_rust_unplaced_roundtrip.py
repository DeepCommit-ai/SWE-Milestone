"""Serialization round-trip for the test-injection-scope-mismatch diagnostics.

Nested GT test regions without an agent anchor scope are reported (not
injected) as ``rust_gt_unplaced_*``; a later reconciliation pass must see the
same diagnostics it wrote, and every result must carry the harness revision
that produced it.
"""


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
        pass_to_pass_missing=6,
        none_to_pass_success=[],
        none_to_pass_failure=[],
        total_tests=100,
        passed_tests=94,
        failed_tests=0,
        error_tests=0,
        skipped_tests=0,
        fail_to_pass_required=0,
        fail_to_pass_achieved=0,
        pass_to_pass_required=100,
        none_to_pass_required=0,
        none_to_pass_achieved=0,
    )
    kwargs.update(overrides)
    return EvaluationResult(**kwargs)


UNPLACED = [
    "crates/nu-protocol/src/pipeline/byte_stream.rs :: mod:split_read[0] "
    ":: fn:simple :: fns=simple,with_empty_fields,complex_delimiter"
]


def test_unplaced_fields_survive_to_dict_and_from_result_dict():
    from harness.e2e.evaluator import EvaluationResult

    d = _mk_result(
        rust_gt_unplaced_count=1,
        rust_gt_unplaced_tests=list(UNPLACED),
        harness_revision="abc123def456+dirty",
    ).to_dict()

    env = d["evaluation_environment"]
    assert env["rust_gt_unplaced_count"] == 1
    assert env["rust_gt_unplaced_tests"] == UNPLACED
    assert env["harness_revision"] == "abc123def456+dirty"

    restored = EvaluationResult.from_result_dict(d)
    assert restored.rust_gt_unplaced_count == 1
    assert restored.rust_gt_unplaced_tests == UNPLACED
    assert restored.harness_revision == "abc123def456+dirty"


def test_unplaced_defaults_are_empty_and_do_not_invalidate():
    d = _mk_result().to_dict()
    env = d["evaluation_environment"]
    assert env["rust_gt_unplaced_count"] == 0
    assert env["rust_gt_unplaced_tests"] == []
    # Diagnostics alone never mark a cell infra-invalid: a cell with test
    # results and missing P2P stays a scored result.
    assert d["infra_invalid"] is False
