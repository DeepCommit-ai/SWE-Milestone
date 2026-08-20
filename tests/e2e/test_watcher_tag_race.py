"""Tests for issue #20: a submission tag moving during snapshot capture must
recycle the milestone into debounce, never kill the watcher thread.

Policy (maintainer decision): the stale capture is DISCARDED and the milestone
waits for the new commit — the old commit is never evaluated. Every observed
commit stays accountable via the submission_history audit trail.

Defense layers under test:
1. call-site boundary — SubmissionTagMoved recycles without charging a
   failure; unexpected exceptions charge the existing failure counter; nothing
   escapes into the loop;
2. iteration boundary — transient docker/git errors self-heal, persistent
   ones escalate;
3. thread boundary — anything that still escapes surfaces a watcher_dead
   event, which the main loop turns into a loud abort instead of burning the
   no-progress budget on timeouts (the go-zero incident: 9 tags unscored).
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.e2e.orchestrator import E2EOrchestrator, SubmissionTagMoved
from harness.e2e.run_e2e import DebounceState, E2ETrialRunner

TAG = "agent-impl-M001"
OLD = "a" * 40
NEW = "b" * 40


def _moved(phase: str = "during-capture") -> SubmissionTagMoved:
    return SubmissionTagMoved(TAG, OLD, NEW, phase)


def _make_runner(tmp_path: Path) -> E2ETrialRunner:
    orch = MagicMock()
    orch.trial_root = tmp_path
    orch.config.debounce_seconds = 0
    orch.config.max_debounce_wait = 10_000
    orch.config.max_retries = 3
    orch._evaluated_hashes = {}
    orch._get_container_tags.return_value = {TAG}
    orch._get_tag_hash.return_value = OLD
    orch._load_summary_or_init.return_value = {"results": {}}

    dag = orch.dag
    dag.all_milestones = {"M001"}
    dag.completed_milestones = set()
    dag.failed_milestones = set()
    dag.skipped_milestones = set()
    dag.is_done.return_value = False

    runner = E2ETrialRunner(
        orchestrator=orch,
        agent_output_dir=tmp_path,
        workdir="/testbed",
        repo_src_dirs=["src"],
        agent_name="claude-code",
        model="test-model",
        timeout_ms=1000,
        prompt_version="v1",
    )
    return runner


def _prime_debounce(runner: E2ETrialRunner) -> None:
    then = time.time() - 60
    runner.pending_debounce["M001"] = DebounceState(
        tag=TAG, hash=OLD, first_seen=then, last_updated=then, milestone_id="M001"
    )


def _run_loop(runner: E2ETrialRunner) -> None:
    """Run the real watcher loop synchronously with sleeps neutralized.

    The loop keeps re-discovering the tag (Step 3) and re-submitting after
    each recycled round; the test's _handle_submission side_effect stops the
    loop when its scenario is complete.
    """
    with patch("harness.e2e.run_e2e.time.sleep"):
        runner._run_watcher_loop()


# ---------------------------------------------------------------------------
# Layer 1: call-site boundary
# ---------------------------------------------------------------------------


def test_tag_move_recycles_without_charging_failures(tmp_path):
    """4 consecutive moved-tag captures (> max_retries=3) must all recycle:
    a move is an expected race, so the failure counter never trips and the
    milestone keeps waiting for a stable tag. Before the fix, call #1 killed
    the watcher thread outright."""
    runner = _make_runner(tmp_path)
    _prime_debounce(runner)
    calls = []

    def handle(mid, tag, executor, pending_futures, attempt=0):
        calls.append(attempt)
        if len(calls) >= 5:
            runner.watcher_stop_event.set()
        raise _moved()

    runner.orchestrator._handle_submission.side_effect = handle

    _run_loop(runner)  # must return normally — no exception escapes

    # 5 attempts happened: with failure-charging this would have stopped at 3
    assert len(calls) == 5
    assert runner.orchestrator._record_submission_discard.call_count == 5
    # tracking is clean and nothing reported the watcher dead
    assert runner.running_evaluations == set()
    assert runner.eval_event_queue.empty()


def test_moved_tag_eventually_evaluates_new_commit(tmp_path):
    """Full recycle to the policy end-state: the OLD capture is discarded and
    the NEW commit — never the old one — is what eventually gets evaluated."""
    runner = _make_runner(tmp_path)
    _prime_debounce(runner)
    outcomes = []

    def handle(mid, tag, executor, pending_futures, attempt=0):
        if not outcomes:
            outcomes.append(("moved", runner.orchestrator._get_tag_hash.return_value))
            # the agent moved the tag while we were capturing
            runner.orchestrator._get_tag_hash.return_value = NEW
            raise _moved()
        outcomes.append(("evaluated", runner.orchestrator._get_tag_hash.return_value))
        runner.watcher_stop_event.set()
        return True

    runner.orchestrator._handle_submission.side_effect = handle

    _run_loop(runner)

    assert outcomes == [("moved", OLD), ("evaluated", NEW)]
    assert runner.orchestrator._record_submission_discard.call_count == 1


def test_unexpected_submission_error_still_charges_failures(tmp_path):
    """A non-tag-move exception must not kill the watcher either, but it DOES
    consume the existing submission failure budget (max_retries=3): the 4th
    round must not call _handle_submission again."""
    runner = _make_runner(tmp_path)
    _prime_debounce(runner)
    calls = []

    def handle(mid, tag, executor, pending_futures, attempt=0):
        calls.append(attempt)
        raise RuntimeError("snapshot extraction exploded")

    runner.orchestrator._handle_submission.side_effect = handle

    # stop the loop after a few idle iterations once the budget is exhausted
    idle_rounds = {"n": 0}
    real_tags = runner.orchestrator._get_container_tags.return_value

    def tags_then_stop():
        idle_rounds["n"] += 1
        if idle_rounds["n"] >= 8:
            runner.watcher_stop_event.set()
        return real_tags

    runner.orchestrator._get_container_tags.side_effect = tags_then_stop

    _run_loop(runner)

    assert len(calls) == 3  # budget respected
    runner.orchestrator._record_submission_discard.assert_not_called()
    assert runner.eval_event_queue.empty()


# ---------------------------------------------------------------------------
# Layer 2: iteration boundary (transient self-heal, persistent escalate)
# ---------------------------------------------------------------------------

def test_transient_iteration_errors_self_heal(tmp_path):
    runner = _make_runner(tmp_path)
    boom = RuntimeError("docker hiccup")

    calls = {"n": 0}

    def flaky_tags():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise boom
        if calls["n"] >= 4:
            runner.watcher_stop_event.set()
        return set()

    runner.orchestrator._get_container_tags.side_effect = flaky_tags

    with patch("harness.e2e.run_e2e.WATCHER_MAX_CONSECUTIVE_ERRORS", 3):
        _run_loop(runner)  # 2 failures < 3, then success resets the counter

    assert calls["n"] >= 4


def test_persistent_iteration_errors_escalate(tmp_path):
    runner = _make_runner(tmp_path)
    runner.orchestrator._get_container_tags.side_effect = RuntimeError("docker gone")

    with patch("harness.e2e.run_e2e.WATCHER_MAX_CONSECUTIVE_ERRORS", 3):
        with pytest.raises(RuntimeError, match="docker gone"):
            _run_loop(runner)


# ---------------------------------------------------------------------------
# Layer 3: thread boundary -> watcher_dead -> main loop abort
# ---------------------------------------------------------------------------


def test_watcher_death_surfaces_event(tmp_path):
    runner = _make_runner(tmp_path)
    with patch.object(runner, "_run_watcher_loop", side_effect=RuntimeError("boom")):
        runner.start_watcher_thread()
        runner.watcher_thread.join(timeout=10)
    assert not runner.watcher_thread.is_alive()

    event = runner.eval_event_queue.get_nowait()
    assert event[0] == "watcher_dead"
    assert "boom" in event[4]


def test_process_queue_event_translates_watcher_dead(tmp_path):
    runner = _make_runner(tmp_path)
    assert runner._process_queue_event(("watcher_dead", None, None, None, "x")) == "watcher_dead"


def test_drain_propagates_watcher_dead(tmp_path):
    runner = _make_runner(tmp_path)
    runner.eval_event_queue.put(("watcher_dead", None, None, None, "x"))
    assert runner._drain_pending_events() == "watcher_dead"


def test_wait_for_evaluations_returns_watcher_dead(tmp_path):
    runner = _make_runner(tmp_path)
    runner.orchestrator.config.evaluation_timeout = 30
    runner.eval_event_queue.put(("watcher_dead", None, None, None, "x"))
    assert runner._wait_for_evaluations() == "watcher_dead"


def test_wait_for_evaluations_receives_watcher_dead_mid_wait(tmp_path):
    runner = _make_runner(tmp_path)
    runner.orchestrator.config.evaluation_timeout = 30
    runner.orchestrator.dag.submitted_milestones = {"M001"}  # has_pending stays True
    runner.orchestrator.dag.get_next_runnable.return_value = []
    runner.orchestrator.dag.is_done.return_value = False

    def deliver():
        time.sleep(0.2)
        runner.eval_event_queue.put(("watcher_dead", None, None, None, "late"))

    t = threading.Thread(target=deliver)
    t.start()
    try:
        assert runner._wait_for_evaluations(max_wait=20) == "watcher_dead"
    finally:
        t.join()


# ---------------------------------------------------------------------------
# Orchestrator side: typed exception + no half-finished capture left behind
# ---------------------------------------------------------------------------


def _bare_orchestrator(tmp_path: Path) -> E2EOrchestrator:
    orch = object.__new__(E2EOrchestrator)
    orch.trial_root = tmp_path
    orch.dag = MagicMock()
    orch.container_name = "dummy"
    orch.repo_src_dirs = ["src"]
    orch._update_task_queue_file = MagicMock()
    orch._update_resume_state = MagicMock()
    orch._early_unlocked_milestones = set()
    return orch


def test_before_capture_move_raises_typed_and_leaves_no_state(tmp_path):
    orch = _bare_orchestrator(tmp_path)
    orch._get_tag_hash = MagicMock(return_value=NEW)  # differs from expected OLD

    with pytest.raises(SubmissionTagMoved) as exc_info:
        orch._handle_submission("M001", TAG, MagicMock(), {}, attempt=0, expected_tag_hash=OLD)

    assert exc_info.value.phase == "before-capture"
    assert exc_info.value.old_commit == OLD
    assert exc_info.value.new_commit == NEW
    # the milestone was never marked submitted: no state to reconcile
    orch.dag.mark_submitted.assert_not_called()


def test_during_capture_move_discards_snapshot_and_sidecar(tmp_path):
    """When integrity detects the move after the archive was written, the
    stale snapshot and any sidecar must be gone before the exception leaves
    _handle_submission — nothing may mistake them for a valid capture."""
    orch = _bare_orchestrator(tmp_path)
    orch._get_tag_hash = MagicMock(return_value=OLD)
    orch._get_existing_root_files_in_git = MagicMock(return_value=set())
    orch._get_build_manifest_overlay_in_git = MagicMock()
    orch._get_existing_build_manifests_in_git = MagicMock()
    orch._get_existing_src_dirs_in_git = MagicMock(return_value={"src"})
    orch._filter_tar_archive = MagicMock()

    snapshot_file = tmp_path / "evaluation" / "M001" / "source_snapshot.tar"
    sidecar = snapshot_file.parent / "source_snapshot.integrity.json"

    def fake_integrity(*args, **kwargs):
        # simulate the real sequence: sidecar might exist from a prior write
        sidecar.write_text("{}")
        raise _moved()

    orch._check_snapshot_capture_integrity = MagicMock(side_effect=fake_integrity)

    overlay = MagicMock()
    overlay.upserts = []
    overlay.deletes = []

    with patch(
        "harness.e2e.orchestrator.expand_atomic_manifest_overlay", return_value=overlay
    ), patch(
        "harness.e2e.orchestrator.get_snapshot_paths", return_value=["src"]
    ), patch(
        "harness.e2e.orchestrator.subprocess.run",
        return_value=MagicMock(returncode=0, stderr=b""),
    ):
        with pytest.raises(SubmissionTagMoved):
            orch._handle_submission("M001", TAG, MagicMock(), {}, attempt=0)

    assert not snapshot_file.exists(), "stale snapshot must be discarded"
    assert not sidecar.exists(), "stale sidecar must be discarded"
    # submitted state intentionally stays (idempotent set-add; tag exists and
    # the task must not reappear in the agent queue)
    orch.dag.mark_submitted.assert_called_once_with("M001")


def test_record_submission_discard_appends_history(tmp_path):
    orch = object.__new__(E2EOrchestrator)
    captured: dict = {"summary": {}}

    def fake_update(mutator):
        mutator(captured["summary"])

    orch._update_resume_state = fake_update

    E2EOrchestrator._record_submission_discard(orch, "M001", _moved())

    history = captured["summary"]["submission_history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["milestone_id"] == "M001"
    assert entry["old_commit"] == OLD
    assert entry["new_commit"] == NEW
    assert entry["action"] == "discarded_tag_moved"
    assert entry["phase"] == "during-capture"
    assert entry["ts"] > 0
