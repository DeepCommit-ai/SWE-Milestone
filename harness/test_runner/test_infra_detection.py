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


class TestMilestoneRequiresDockerSocket:
    def test_flag_true(self, tmp_path):
        from harness.e2e.evaluator import _milestone_requires_docker_socket

        d = tmp_path / "dockerfiles" / "M1"
        d.mkdir(parents=True)
        (d / "test_config.json").write_text(
            '[{"name": "default", "test_states": ["start", "end"],'
            ' "test_cmd": "npx jest {output_file}", "requires_docker_socket": true}]'
        )
        assert _milestone_requires_docker_socket(tmp_path, "M1") is True

    def test_absent_config_is_false(self, tmp_path):
        from harness.e2e.evaluator import _milestone_requires_docker_socket

        assert _milestone_requires_docker_socket(tmp_path, "M1") is False


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


VALID_REPORT = '{"tests": [{"nodeid": "t::a", "outcome": "passed"}], "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0}}'


class _TimeoutCapturingRunner:
    def __init__(self, output_dir, files):
        self._output_dir = output_dir
        self._files = files
        self.timeouts = []

    def run(self, script, timeout=None, extra_volumes=None):
        self.timeouts.append(timeout)
        for name, content in self._files.items():
            (self._output_dir / name).write_text(content)
        return 0, "", ""


def _two_mode_workspace(tmp_path, second_mode_extra=""):
    ws = tmp_path / "ws"
    cfg = ws / "dockerfiles" / "M1"
    cfg.mkdir(parents=True)
    (cfg / "test_config.json").write_text(
        '[{"name": "default", "test_states": ["start", "end"],'
        ' "test_cmd": "pytest {output_file}", "framework": "pytest"},'
        ' {"name": "slow", "test_states": ["start", "end"],'
        ' "test_cmd": "pytest {output_file}", "framework": "pytest"'
        + second_mode_extra
        + "}]"
    )
    out = tmp_path / "out"
    out.mkdir()
    return ws, out


class TestModeRunTimeout:
    def test_run_timeout_seconds_plumbs_through_to_runner(self, tmp_path):
        from harness.test_runner.core.milestone_attempt import run_single_state_tests

        ws, out = _two_mode_workspace(tmp_path, ', "run_timeout_seconds": 4321')
        runner = _TimeoutCapturingRunner(
            out, {"eval_default.json": VALID_REPORT, "eval_slow.json": VALID_REPORT}
        )
        run_single_state_tests(
            runner, workspace_root=ws, milestone_id="M1", output_dir=out, workers=1, timeout=60
        )
        assert runner.timeouts == [1800, 4321]


class TestDroppedReportIsLoud:
    def test_unparseable_mode_report_prints_alarm(self, tmp_path, capsys):
        from harness.test_runner.core.milestone_attempt import run_single_state_tests

        ws, out = _two_mode_workspace(tmp_path)
        runner = _TimeoutCapturingRunner(
            out, {"eval_default.json": VALID_REPORT, "eval_slow.json": '{"trunca'}
        )
        run_single_state_tests(
            runner, workspace_root=ws, milestone_id="M1", output_dir=out, workers=1, timeout=60
        )
        captured = capsys.readouterr().out
        assert "🚨" in captured
        assert "eval_slow.json" in captured
        assert "test universe shrank" in captured


class TestTransientClassification:
    def test_infrastructure_failure_error_is_transient(self):
        assert _is_transient_error(InfrastructureFailureError("runtime down")) is True

    def test_plain_value_error_is_not_transient(self):
        assert _is_transient_error(ValueError("bad input")) is False

    def test_existing_string_heuristics_preserved(self):
        assert _is_transient_error(RuntimeError("Connection reset by peer")) is True
