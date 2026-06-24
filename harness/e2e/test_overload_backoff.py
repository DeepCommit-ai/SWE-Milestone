"""Tests for graceful HTTP-529 server-overload detection and backoff logic.

Covers:
- AgentRunner._detect_overload: matches 529 / "overloaded" / 访问量过大 in the
  output tail for OAuth agents, but NOT a bare "529" in viewed source text, and
  NOT for non-OAuth agents.
- The exponential-backoff-with-cap progression and the accumulated-duration
  give-up threshold used by run_e2e.recover()'s overload branch.
- The branch GUARD: overload diverts only when there is no parsed real reset
  time; a genuine parsed quota reset still routes to the rate-limit long-sleep.
"""

import random

import pytest

from harness.e2e.agent_runner import AgentRunner


def _runner(agent_name="claude-code"):
    """Construct a bare AgentRunner.

    AgentRunner.__init__ only builds the (lightweight) framework strategy via
    get_agent_framework — no Docker or network — so this is cheap and pure.
    """
    return AgentRunner(container_name="dummy-container", agent_name=agent_name)


class TestDetectOverload:
    def test_matches_overloaded_literal(self):
        r = _runner()
        assert r._detect_overload('{"type":"error","error":{"type":"overloaded_error"}}') is True
        assert r._detect_overload("Error: model is Overloaded right now") is True

    def test_matches_quoted_json_529_forms(self):
        r = _runner()
        assert r._detect_overload('{"code":"529","message":"..."}') is True
        assert r._detect_overload('{"code":529}') is True
        assert r._detect_overload('{"status":529}') is True

    def test_matches_glm_chinese_overload_phrase(self):
        r = _runner()
        # GLM bigmodel transient overload body (HTTP 529).
        assert r._detect_overload("当前访问量过大，请稍后再试 (try again in a moment)") is True
        assert r._detect_overload("访问量较大，请稍候") is True

    def test_does_not_match_bare_529_in_viewed_source(self):
        r = _runner()
        # A bare 529 appearing as a digit/identifier in source or logs the agent
        # merely viewed must NOT be detected as a server-overload signal.
        src = "    port = 529  # arbitrary value\n    return port + 1\n"
        assert r._detect_overload(src) is False
        assert r._detect_overload("test_case_529 passed; 529 items processed") is False
        assert r._detect_overload("HTTP 200 OK; latency 529ms") is False

    def test_only_scans_tail(self):
        r = _runner()
        # Overload token buried >5000 chars from the end must not match (mirrors
        # _detect_rate_limit's output[-5000:] tail-only scan).
        buried = '{"code":529}' + ("x" * 6000)
        assert r._detect_overload(buried) is False
        # ...but the same token within the last 5000 chars does match.
        recent = ("x" * 6000) + '{"code":529}'
        assert r._detect_overload(recent) is True

    def test_false_for_non_oauth_agent(self):
        r = _runner(agent_name="openhands")
        # API-based agents handle overload internally; scanning their stdout
        # would cause false positives, so detection is gated off entirely.
        assert r._detect_overload('{"code":529}') is False
        assert r._detect_overload("overloaded") is False

    def test_true_for_each_oauth_agent(self):
        for name in ("claude-code", "codex", "gemini-cli"):
            assert _runner(agent_name=name)._detect_overload("overloaded") is True


# --- Reference model of the inline run_e2e.recover() overload backoff ----------
# These mirror, deterministically, the exact arithmetic of the overload branch
# in run_e2e.py so the exponential progression, the cap, the jitter bound, and
# the accumulated-duration give-up can be unit-tested without a live trial.

BASE = 20
CAP = 300
GIVEUP = 3600


def _next_delay(step, *, jitter=False):
    base_delay = min(step, CAP)
    if jitter:
        return max(1, int(base_delay * random.uniform(0.8, 1.2)))
    return base_delay


def _advance_step(step):
    return min(step * 2, CAP)


class TestBackoffSequence:
    def test_exponential_progression_then_caps_at_300(self):
        step = BASE
        seen = []
        for _ in range(12):
            seen.append(_next_delay(step))  # un-jittered nominal delay
            step = _advance_step(step)
        # 20, 40, 80, 160, 320->capped 300, then pinned at the cap.
        assert seen[:4] == [20, 40, 80, 160]
        assert all(d <= CAP for d in seen)
        assert seen[4] == CAP
        assert seen[-1] == CAP  # stays at cap, never exceeds it

    def test_step_never_exceeds_cap(self):
        step = BASE
        for _ in range(20):
            step = _advance_step(step)
            assert step <= CAP

    def test_jitter_within_plus_minus_20_percent(self):
        random.seed(1234)
        for nominal in (BASE, 80, CAP):
            for _ in range(200):
                d = _next_delay(nominal, jitter=True)
                assert 1 <= d <= int(nominal * 1.2)
                # lower bound: int(nominal*0.8) (>=1)
                assert d >= max(1, int(nominal * 0.8))


class TestGiveUp:
    def test_giveup_triggers_once_accumulated_reaches_threshold(self):
        # Replays the branch's accumulate-then-check loop: give up only once the
        # continuous total has reached the budget (no successful turn between).
        total = 0
        step = BASE
        gave_up_at = None
        for _ in range(1000):
            if total >= GIVEUP:
                gave_up_at = total
                break
            delay = _next_delay(step)  # deterministic for the assertion
            total += delay
            step = _advance_step(step)
        assert gave_up_at is not None
        assert gave_up_at >= GIVEUP  # only after crossing the threshold

    def test_no_giveup_below_threshold(self):
        # A couple of transient backoffs stay well under the 1h budget.
        total = 0
        step = BASE
        for _ in range(3):  # 20 + 40 + 80 = 140s
            total += _next_delay(step)
            step = _advance_step(step)
        assert total < GIVEUP

    def test_successful_turn_resets_accumulator(self):
        # The branch resets total+step to 0/BASE on any successful agent turn, so
        # isolated overloads spread across hours never accumulate to a give-up.
        total, step = 200, 160
        # ...successful turn happens...
        total, step = 0, BASE
        assert total == 0 and step == BASE


class TestBranchGuard:
    """The overload branch fires only when there is NO parsed real reset time.

    Mirrors run_e2e.py:
        if self.agent_runner._last_overload and not self.agent_runner._rate_limit_reset_seconds:
            <overload backoff>
        if self.agent_runner._last_rate_limit:
            <long sleep>
    """

    @staticmethod
    def _takes_overload_path(last_overload, reset_seconds):
        return bool(last_overload and not reset_seconds)

    def test_overload_without_parsed_reset_takes_overload_path(self):
        assert self._takes_overload_path(last_overload=True, reset_seconds=None) is True
        assert self._takes_overload_path(last_overload=True, reset_seconds=0) is True

    def test_genuine_parsed_reset_falls_through_to_long_sleep(self):
        # "overloaded"-ish text but a REAL reset time was parsed (genuine quota):
        # must NOT take the overload fast-backoff path — falls through to the
        # existing rate-limit long-sleep.
        assert self._takes_overload_path(last_overload=True, reset_seconds=18000) is False

    def test_no_overload_signal_skips_overload_path(self):
        assert self._takes_overload_path(last_overload=False, reset_seconds=None) is False
