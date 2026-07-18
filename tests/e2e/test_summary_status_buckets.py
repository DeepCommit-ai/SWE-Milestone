"""Regression tests for classify_unevaluated_milestones.

Root incident (kimi-k3 dubbo M025, 2026-07-18): in early-unlock mode a
submission calls dag.mark_complete (not mark_submitted), so a milestone whose
evaluation was still in flight — or crashed after the runner exited — vanished
from every DAG pending set and the summary labeled it "blocked" even though
the agent demonstrably submitted it (tag + snapshot on disk). The bucket split
is now a pure function taking explicit submission evidence.
"""

from harness.e2e.orchestrator import classify_unevaluated_milestones


def _classify(**overrides):
    defaults = dict(
        all_milestones={"M1", "M2", "M3", "M4"},
        evaluated={"M1"},
        runnable=[],
        skipped=set(),
        dag_submitted=set(),
        was_submitted=lambda m: False,
    )
    defaults.update(overrides)
    return classify_unevaluated_milestones(**defaults)


class TestEarlyUnlockSubmissionEvidence:
    def test_early_unlock_in_flight_eval_is_submitted_not_blocked(self):
        # M2 submitted via early unlock: absent from dag_submitted, not
        # runnable (DAG already marks it complete), evidence says submitted.
        available, submitted, blocked = _classify(
            was_submitted=lambda m: m == "M2",
        )
        assert "M2" in submitted
        assert "M2" not in blocked

    def test_no_evidence_and_unmet_deps_stays_blocked(self):
        available, submitted, blocked = _classify()
        assert blocked == ["M2", "M3", "M4"]
        assert submitted == []

    def test_snapshot_evidence_callback_receives_pending_only(self):
        seen = []

        def evidence(m):
            seen.append(m)
            return False

        _classify(evaluated={"M1", "M2"}, runnable=["M3"], was_submitted=evidence)
        # M1/M2 evaluated, M3 available: only M4 needs an evidence check.
        assert seen == ["M4"]


class TestClassicBuckets:
    def test_dag_submitted_set_still_maps_to_submitted(self):
        available, submitted, blocked = _classify(dag_submitted={"M3"})
        assert "M3" in submitted
        assert "M3" not in blocked

    def test_evaluated_milestones_never_reappear(self):
        available, submitted, blocked = _classify(
            evaluated={"M1", "M2"},
            dag_submitted={"M1"},  # stale DAG state must not resurface M1
            was_submitted=lambda m: True,
        )
        for bucket in (available, submitted, blocked):
            assert "M1" not in bucket and "M2" not in bucket

    def test_available_and_skipped_take_precedence_over_evidence(self):
        available, submitted, blocked = _classify(
            runnable=["M2"],
            skipped={"M3"},
            was_submitted=lambda m: True,  # evidence must not steal these
        )
        assert available == ["M2"]
        assert "M3" not in submitted and "M3" not in blocked
        assert submitted == ["M4"]
        assert blocked == []

    def test_runnable_entries_outside_pending_are_ignored(self):
        available, _, _ = _classify(runnable=["M1", "M2"])  # M1 evaluated
        assert available == ["M2"]
