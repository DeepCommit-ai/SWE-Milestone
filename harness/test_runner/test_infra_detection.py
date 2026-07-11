"""Tests for F-2a: infrastructure-failure detection, fail-closed scoring,
and transient-retry classification (see docs/post_verify/infra-failure-audit.md — this is
the deterministic layer; the skill sweep feeds new signatures into it).
"""


from harness.e2e.evaluator import EvaluationResult, InfrastructureFailureError
from harness.e2e.orchestrator import _is_transient_error
from harness.test_runner.core.test_executor import detect_infrastructure_failure

TESTCONTAINERS_OUTPUT = """\
FAIL test/unit-tests/matrixrtc.spec.ts
  ● Test suite failed to run
    Could not find a working container runtime strategy
        at getContainerRuntimeClient (node_modules/testcontainers/src/index.ts:12:11)
"""

DOCKER_DAEMON_OUTPUT = """\
docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?
"""

CLEAN_OUTPUT = """\
Tests: 118 passed, 118 total
error handling suite passed (asserts on error strings, not a real failure)
"""


class TestDetectInfrastructureFailure:
    def test_testcontainers_runtime_unavailable(self):
        sig = detect_infrastructure_failure(TESTCONTAINERS_OUTPUT)
        assert sig is not None
        assert "container runtime strategy" in sig

    def test_docker_daemon_unreachable(self):
        sig = detect_infrastructure_failure(DOCKER_DAEMON_OUTPUT)
        assert sig is not None
        assert "Docker daemon" in sig

    def test_clean_output_returns_none(self):
        assert detect_infrastructure_failure(CLEAN_OUTPUT) is None

    def test_signature_inside_giant_single_line_json(self):
        # Real reports are often one giant JSON line; the returned snippet
        # must contain the matched signature, not the start of the line.
        blob = '{"tests":[' + '{"nodeid":"x","outcome":"passed"},' * 50000 + \
               '{"nodeid":"y","longrepr":"Could not find a working container runtime strategy"}]}'
        sig = detect_infrastructure_failure(blob)
        assert sig is not None
        assert "container runtime strategy" in sig


class TestScanFileForInfrastructureFailure:
    def test_signature_beyond_first_megabyte(self, tmp_path):
        from harness.e2e.evaluator import _scan_file_for_infrastructure_failure

        p = tmp_path / "eval.json"
        p.write_text("x" * 2_500_000 + "\nCannot connect to the Docker daemon at unix:///var/run/docker.sock\n")
        sig = _scan_file_for_infrastructure_failure(p)
        assert sig is not None
        assert "Docker daemon" in sig

    def test_clean_large_file(self, tmp_path):
        from harness.e2e.evaluator import _scan_file_for_infrastructure_failure

        p = tmp_path / "eval.json"
        p.write_text("ok " * 1_000_000)
        assert _scan_file_for_infrastructure_failure(p) is None


def _mk_result(**overrides) -> EvaluationResult:
    kwargs = dict(
        milestone_id="M001",
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


class TestScoringUntrusted:
    def test_default_result_is_trusted(self):
        assert _mk_result().scoring_untrusted is False

    def test_infrastructure_failure_locks_scoring_untrusted(self):
        res = _mk_result(infrastructure_failure="Could not find a working container runtime strategy")
        assert res.scoring_untrusted is True

    def test_residue_prune_fail_closed_still_works(self):
        res = _mk_result(residue_prune_skipped_reason="tar-unreadable")
        assert res.scoring_untrusted is True

    def test_to_dict_carries_infrastructure_failure(self):
        res = _mk_result(infrastructure_failure="sig-text")
        assert res.to_dict()["infrastructure_failure"] == "sig-text"


class TestTransientClassification:
    def test_infrastructure_failure_error_is_transient(self):
        assert _is_transient_error(InfrastructureFailureError("runtime down")) is True

    def test_plain_value_error_is_not_transient(self):
        assert _is_transient_error(ValueError("bad input")) is False

    def test_existing_string_heuristics_preserved(self):
        assert _is_transient_error(RuntimeError("Connection reset by peer")) is True
