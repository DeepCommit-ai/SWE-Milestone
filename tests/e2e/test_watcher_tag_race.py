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
    seen_expected = []

    def handle(mid, tag, executor, pending_futures, attempt=0, expected_tag_hash=None):
        calls.append(attempt)
        seen_expected.append(expected_tag_hash)
        if len(calls) >= 5:
            runner.watcher_stop_event.set()
        raise _moved()

    runner.orchestrator._handle_submission.side_effect = handle

    _run_loop(runner)  # must return normally — no exception escapes

    # 5 attempts happened: with failure-charging this would have stopped at 3
    assert len(calls) == 5
    # the debounce-observed hash arms the before-capture freshness check
    assert seen_expected == [OLD] * 5
    # tracking is clean and nothing reported the watcher dead
    assert runner.running_evaluations == set()
    assert runner.eval_event_queue.empty()


def test_moved_tag_eventually_evaluates_new_commit(tmp_path):
    """Full recycle to the policy end-state: the OLD capture is discarded and
    the NEW commit — never the old one — is what eventually gets evaluated."""
    runner = _make_runner(tmp_path)
    _prime_debounce(runner)
    outcomes = []

    def handle(mid, tag, executor, pending_futures, attempt=0, expected_tag_hash=None):
        if not outcomes:
            outcomes.append(("moved", expected_tag_hash))
            # the agent moved the tag while we were capturing
            runner.orchestrator._get_tag_hash.return_value = NEW
            raise _moved()
        outcomes.append(("evaluated", expected_tag_hash))
        runner.watcher_stop_event.set()
        return True

    runner.orchestrator._handle_submission.side_effect = handle

    _run_loop(runner)

    assert outcomes == [("moved", OLD), ("evaluated", NEW)]


def test_unexpected_submission_error_still_charges_failures(tmp_path):
    """A non-tag-move exception must not kill the watcher either, but it DOES
    consume the existing submission failure budget (max_retries=3): the 4th
    round must not call _handle_submission again."""
    runner = _make_runner(tmp_path)
    _prime_debounce(runner)
    calls = []

    def handle(mid, tag, executor, pending_futures, attempt=0, expected_tag_hash=None):
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


# ---------------------------------------------------------------------------
# Regressions from the adversarial review of this fix
# ---------------------------------------------------------------------------


def test_retry_unexpected_errors_respect_budget(tmp_path):
    """Review finding: a persistently failing retry submission was charged to
    neither counter and relaunched every ~2s forever. It must consume the
    submission budget and give up on that hash after max_retries."""
    runner = _make_runner(tmp_path)
    runner.orchestrator._evaluated_hashes = {"M001": OLD}  # already evaluated once
    runner.orchestrator._get_tag_hash.return_value = NEW  # tag moved -> retry path
    calls = []

    def handle(mid, tag, executor, pending_futures, attempt=0, expected_tag_hash=None):
        calls.append((attempt, expected_tag_hash))
        raise RuntimeError("retry archive exploded")

    runner.orchestrator._handle_submission.side_effect = handle

    rounds = {"n": 0}

    def tags_then_stop():
        rounds["n"] += 1
        if rounds["n"] >= 8:
            runner.watcher_stop_event.set()
        return {TAG}

    runner.orchestrator._get_container_tags.side_effect = tags_then_stop

    _run_loop(runner)

    assert len(calls) == 3, f"budget not respected: {calls}"
    assert all(exp == NEW for _, exp in calls)


def test_retry_tag_move_still_uncharged(tmp_path):
    """Moves on the retry path keep recycling without consuming the budget."""
    runner = _make_runner(tmp_path)
    runner.orchestrator._evaluated_hashes = {"M001": OLD}
    runner.orchestrator._get_tag_hash.return_value = NEW
    calls = []

    def handle(mid, tag, executor, pending_futures, attempt=0, expected_tag_hash=None):
        calls.append(attempt)
        if len(calls) >= 5:
            runner.watcher_stop_event.set()
        raise _moved()

    runner.orchestrator._handle_submission.side_effect = handle

    _run_loop(runner)

    assert len(calls) == 5  # would stop at 3 if moves were charged


def test_resume_priming_restarts_debounce_clocks(tmp_path):
    """Review finding: preserving a dead process's first_seen let
    max_debounce_wait force-capture a commit stable for only seconds."""
    runner = _make_runner(tmp_path)
    ancient = time.time() - 99_999
    runner._resume_pending_debounce = {
        "M001": {"tag": TAG, "tag_hash": OLD, "first_seen_ts": ancient, "last_updated_ts": ancient}
    }
    runner.orchestrator.config.debounce_seconds = 3600  # nothing may submit in this test
    runner.orchestrator.config.max_debounce_wait = 7200

    rounds = {"n": 0}

    def tags_then_stop():
        rounds["n"] += 1
        if rounds["n"] >= 2:
            runner.watcher_stop_event.set()
        return {TAG}

    runner.orchestrator._get_container_tags.side_effect = tags_then_stop

    _run_loop(runner)

    state = runner.pending_debounce["M001"]
    assert state.first_seen > ancient + 90_000, "first_seen must restart at priming time"
    runner.orchestrator._handle_submission.assert_not_called()


def test_wait_detects_dead_watcher_without_event(tmp_path):
    """Review finding: watcher_dead queued after the initial drain could be
    missed by the state-check returns. The liveness backstop catches a dead
    thread regardless of event delivery."""
    runner = _make_runner(tmp_path)
    runner.orchestrator.config.evaluation_timeout = 30
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    runner.watcher_thread = t
    runner._watcher_exited_clean = False  # died, did not finish

    assert runner._wait_for_evaluations(max_wait=10) == "watcher_dead"


def test_wait_ignores_cleanly_finished_watcher(tmp_path):
    runner = _make_runner(tmp_path)
    runner.orchestrator.config.evaluation_timeout = 30
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    runner.watcher_thread = t
    runner._watcher_exited_clean = True  # finished its loop normally

    runner.orchestrator.dag.submitted_milestones = set()
    runner.orchestrator.dag.get_next_runnable.return_value = []
    runner.orchestrator.dag.is_done.return_value = True

    assert runner._wait_for_evaluations(max_wait=10) == "all_done"


def test_escalation_emits_watcher_dead_before_raising(tmp_path):
    """Review finding: the escalation raise travels through the executor's
    shutdown(wait=True), which can block on a long evaluation — the event must
    already be queued when the raise starts."""
    runner = _make_runner(tmp_path)
    runner.orchestrator._get_container_tags.side_effect = RuntimeError("docker gone")

    with patch("harness.e2e.run_e2e.WATCHER_MAX_CONSECUTIVE_ERRORS", 2):
        with pytest.raises(RuntimeError, match="docker gone"):
            _run_loop(runner)

    event = runner.eval_event_queue.get_nowait()
    assert event[0] == "watcher_dead"
    assert "consecutive iteration failures" in event[4]


def test_watcher_dead_emitted_exactly_once(tmp_path):
    runner = _make_runner(tmp_path)
    runner._emit_watcher_dead("first")
    runner._emit_watcher_dead("second")
    assert runner.eval_event_queue.get_nowait()[4] == "first"
    assert runner.eval_event_queue.empty()


def test_during_capture_records_audit_before_deleting(tmp_path):
    """Review finding: deletion before recording left a crash window where the
    old commit vanished without a trace. The audit write must happen while the
    stale snapshot still exists."""
    orch = _bare_orchestrator(tmp_path)
    orch._get_tag_hash = MagicMock(return_value=OLD)
    orch._get_existing_root_files_in_git = MagicMock(return_value=set())
    orch._get_build_manifest_overlay_in_git = MagicMock()
    orch._get_existing_build_manifests_in_git = MagicMock()
    orch._get_existing_src_dirs_in_git = MagicMock(return_value={"src"})
    orch._filter_tar_archive = MagicMock()

    snapshot_file = tmp_path / "evaluation" / "M001" / "source_snapshot.tar"
    snapshot_present_at_record = []

    def capture_order(mutator):
        # _handle_submission persists other state too; only the audit write
        # (the one that appends to submission_history) matters for ordering
        probe: dict = {"resume_state": {"pending_debounce": {}}}
        mutator(probe)
        if probe.get("submission_history"):
            snapshot_present_at_record.append(snapshot_file.exists())

    orch._update_resume_state = capture_order
    orch._check_snapshot_capture_integrity = MagicMock(side_effect=_moved())

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

    assert snapshot_present_at_record == [True], "audit must be recorded before deletion"
    assert not snapshot_file.exists()


def test_before_capture_move_is_audited(tmp_path):
    orch = _bare_orchestrator(tmp_path)
    orch._get_tag_hash = MagicMock(return_value=NEW)
    recorded = []
    orch._update_resume_state = lambda mutator: recorded.append(mutator)

    with pytest.raises(SubmissionTagMoved):
        orch._handle_submission("M001", TAG, MagicMock(), {}, attempt=0, expected_tag_hash=OLD)

    assert len(recorded) == 1
    summary: dict = {}
    recorded[0](summary)
    assert summary["submission_history"][0]["phase"] == "before-capture"


def test_submission_history_is_capped(tmp_path):
    orch = object.__new__(E2EOrchestrator)
    summary: dict = {"resume_state": {"pending_debounce": {}}}
    orch._update_resume_state = lambda mutator: mutator(summary)

    for _ in range(E2EOrchestrator._SUBMISSION_HISTORY_MAX + 7):
        E2EOrchestrator._record_submission_discard(orch, "M001", _moved())

    assert len(summary["submission_history"]) == E2EOrchestrator._SUBMISSION_HISTORY_MAX


def test_discard_drops_persisted_debounce_entry(tmp_path):
    """The audit write also removes the stale pending_debounce entry, so a
    crash-resume cannot restore the OLD commit's observation window."""
    orch = object.__new__(E2EOrchestrator)
    summary: dict = {"resume_state": {"pending_debounce": {"M001": {"tag_hash": OLD}}}}
    orch._update_resume_state = lambda mutator: mutator(summary)

    E2EOrchestrator._record_submission_discard(orch, "M001", _moved())

    assert "M001" not in summary["resume_state"]["pending_debounce"]


def test_trial_end_watcher_death_beats_done_dag(tmp_path):
    """Review finding (critical): with early-unblock the DAG can be 'done'
    while the final evaluation's result was never processed by the dead
    watcher — the trial must NOT report all_done/success."""
    import itertools

    runner = _make_runner(tmp_path)
    orch = runner.orchestrator
    orch.config.max_no_progress_attempts = 3
    orch.config.recover_message_timeout_seconds = 0
    orch.config.resume_subprocess_retry_limit = 0
    orch.config.recovery_wait_seconds = 0
    orch.config.overload_backoff_base_seconds = 1
    orch.config.overload_backoff_cap_seconds = 1
    orch.config.overload_giveup_seconds = 1

    dag = orch.dag
    # while-condition sees not-done; after the agent turn early-unblock has
    # completed everything, so every later check sees done
    dag.is_done.side_effect = itertools.chain([False], itertools.repeat(True))
    dag.get_state_snapshot.return_value = {"completed": set(), "submitted": set(), "failed": set()}
    dag.completed_milestones = set()
    dag.failed_milestones = set()
    dag.skipped_milestones = set()
    dag.all_milestones = {"M001"}

    fake_agent = MagicMock()
    fake_agent.run.return_value = True
    fake_agent._last_fatal_error = None

    with patch("harness.e2e.run_e2e.E2EAgentRunner", return_value=fake_agent), patch.object(
        runner, "_wait_for_evaluations", return_value="watcher_dead"
    ):
        ok = runner.run_agent_with_recovery()

    assert ok is False, "a done DAG must not override a dead watcher"
    assert runner._last_run_summary["stop_reason"] == "watcher_dead"
