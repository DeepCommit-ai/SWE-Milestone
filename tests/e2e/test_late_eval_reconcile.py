"""Regression tests for late-evaluation loss (the element feature_enhancements race).

A background evaluation can finish after the runner's wait loop stops consuming
completion events; its evaluation_result.json lands on disk but summary.json is
never updated, and a later resume mislabels the milestone. Cures under test:

1. Resume-side reconciliation: a complete on-disk result is ingested through
   the normal ``_process_evaluation_result`` path instead of re-running (and a
   broken/unsafe file falls through to re-evaluation, fail-closed).
2. Exit-side harvest: the wait loop keeps consuming completions for a bounded
   grace window while evaluations are in flight instead of returning at once.
3. Atomic result writes: a torn file can never look like a finished result.
"""

import json
import threading

import pytest

from harness.e2e.evaluator import EvaluationResult
from harness.e2e.orchestrator import derive_resolution
from harness.e2e.run_e2e import E2ETrialRunner


RESULT_FIXTURE = {
    "milestone_id": "M9",
    "patch_is_None": False,
    "patch_exists": True,
    "patch_successfully_applied": True,
    "resolved": False,
    "tests_status": {
        "FAIL_TO_PASS": {"success": ["t1", "t2"], "failure": ["t3"]},
        "NONE_TO_PASS": {"success": ["n1"], "failure": []},
        "PASS_TO_PASS": {"success_count": 90, "failure": ["p1"], "missing": 9},
    },
    "test_summary": {
        "total": 104,
        "passed": 93,
        "failed": 2,
        "error": 0,
        "skipped": 0,
        "fail_to_pass_required": 3,
        "fail_to_pass_achieved": 2,
        "none_to_pass_required": 1,
        "none_to_pass_achieved": 1,
        "pass_to_pass_required": 100,
        "pass_to_pass_achieved": 90,
        "pass_to_pass_failed": 1,
        "pass_to_pass_missing": 9,
    },
    "evaluation_environment": {
        "repo_config_binding_mode": "frozen",
        "repo_config_sha256": "a" * 64,
        "runtime_policy_binding_mode": "frozen",
        "runtime_policy_sha256": "b" * 64,
        "runtime_policy_mode": "protected",
    },
}


class _Config:
    fail_to_pass_threshold = 1.0
    pass_to_pass_threshold = 0.8
    none_to_pass_threshold = 1.0


class _Orchestrator:
    def __init__(self):
        self.config = _Config()
        self.calls = []

    def _process_evaluation_result(self, mid, is_resolved, actual_passed,
                                   eval_res, error_msg, attempt=0):
        self.calls.append((mid, is_resolved, actual_passed, eval_res, attempt))
        return ("unlocked", "passed" if actual_passed else "failed", None)


def _runner(orchestrator):
    runner = object.__new__(E2ETrialRunner)
    runner.orchestrator = orchestrator
    runner.eval_event_queue = __import__("queue").Queue()
    return runner


def test_reconcile_ingests_finished_result_from_disk(tmp_path):
    (tmp_path / "evaluation_result.json").write_text(json.dumps(RESULT_FIXTURE))
    orchestrator = _Orchestrator()
    runner = _runner(orchestrator)

    assert runner._try_reconcile_finished_evaluation("M9", 0, tmp_path) is True

    [(mid, is_resolved, actual_passed, eval_res, attempt)] = orchestrator.calls
    assert (mid, attempt) == ("M9", 0)
    # 2/3 F2P misses the 1.0 threshold; P2P 90/100 meets 0.8 → not resolved
    assert is_resolved is False and actual_passed is False
    assert eval_res.pass_to_pass_success_count == 90
    assert eval_res.repo_config_sha256 == "a" * 64
    event = runner.eval_event_queue.get_nowait()
    assert event[:2] == ("eval_complete", "M9")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: None,  # file absent entirely
        lambda d: d.write_text("{truncated"),  # torn write shape
        lambda d: d.write_text(json.dumps({**RESULT_FIXTURE, "milestone_id": "OTHER"})),
        lambda d: d.write_text(json.dumps({**RESULT_FIXTURE, "infrastructure_failure": "oom"})),
        lambda d: d.write_text(json.dumps(
            {k: v for k, v in RESULT_FIXTURE.items() if k != "test_summary"}
        )),
    ],
    ids=["missing", "torn", "wrong-milestone", "infra-flagged", "incomplete"],
)
def test_reconcile_fails_closed_to_reevaluation(tmp_path, mutate):
    result_path = tmp_path / "evaluation_result.json"
    mutate(result_path)
    orchestrator = _Orchestrator()
    runner = _runner(orchestrator)

    assert runner._try_reconcile_finished_evaluation("M9", 0, tmp_path) is False
    assert orchestrator.calls == []
    assert runner.eval_event_queue.empty()


def test_resolution_thresholds_met_resolves():
    eval_res = EvaluationResult.from_result_dict(RESULT_FIXTURE)
    is_resolved, actual_passed = derive_resolution(
        eval_res,
        fail_to_pass_threshold=0.5,
        pass_to_pass_threshold=0.8,
        none_to_pass_threshold=1.0,
    )
    assert is_resolved is True
    assert actual_passed is False  # not 100% in F2P/P2P


def test_wait_loop_harvests_late_completion(monkeypatch):
    """Primary wait expires with an eval in flight → the loop keeps consuming
    and returns all_done once the late completion lands (instead of timeout)."""
    import queue as queue_module

    class _DAG:
        submitted_milestones: set = set()

        @staticmethod
        def get_next_runnable():
            return []

        @staticmethod
        def is_done():
            return True

    runner = object.__new__(E2ETrialRunner)
    runner.orchestrator = type("O", (), {"dag": _DAG(), "config": _Config()})()
    runner.orchestrator.config.evaluation_timeout = 0  # primary wait pre-expired
    runner.orchestrator.config.eval_harvest_grace_seconds = 60
    runner._state_lock = threading.Lock()
    runner.running_evaluations = {("M9", 0)}
    runner.pending_debounce = {}
    runner.eval_event_queue = queue_module.Queue()
    runner._drain_pending_events = lambda: None
    runner._process_queue_event = lambda event: None

    def _late_completion():
        with runner._state_lock:
            runner.running_evaluations.discard(("M9", 0))
        runner.eval_event_queue.put(("eval_complete", "M9", "unlocked", "failed", None))

    threading.Timer(0.3, _late_completion).start()
    assert runner._wait_for_evaluations() == "all_done"


def test_wait_loop_times_out_when_nothing_in_flight():
    import queue as queue_module

    class _DAG:
        submitted_milestones = {"M9"}

        @staticmethod
        def get_next_runnable():
            return []

        @staticmethod
        def is_done():
            return False

    runner = object.__new__(E2ETrialRunner)
    runner.orchestrator = type("O", (), {"dag": _DAG(), "config": _Config()})()
    runner.orchestrator.config.evaluation_timeout = 0
    runner.orchestrator.config.eval_harvest_grace_seconds = 60
    runner._state_lock = threading.Lock()
    runner.running_evaluations = set()
    runner.pending_debounce = {}
    runner.eval_event_queue = queue_module.Queue()
    runner._drain_pending_events = lambda: None

    assert runner._wait_for_evaluations() == "timeout"
