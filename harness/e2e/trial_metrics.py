#!/usr/bin/env python3
"""Authoritative trial-level metric caliber, shared by monitor and analysis.

This module is the *single source of truth* for how a trial's headline effort
numbers (cost / turns / output tokens / duration) are derived from a parsed
``agent_stats.json``.  Both the monitor (``collect_results.py`` / ``monitor.sh``)
and downstream analysis import from here, so their numbers agree by construction
rather than by hand-maintained convention.

Two layers:

* **Pure caliber functions** — ``trial_cost`` / ``trial_turns`` /
  ``trial_output_tokens`` / ``trial_duration_ms``.  Input is the parsed
  ``agent_stats.json`` dict; output is the metric.  This is THE caliber.
  Analysis, which already loads ``agent_stats.json`` into a dict, calls these
  directly so its extraction matches monitor field-for-field.

* **File-loading wrappers** — ``load_e2e_trial_*``.  Read ``agent_stats.json``
  (with a live ``agent_stdout.txt`` fallback while a trial is still running),
  then delegate to the pure functions.  ``collect_results.py`` uses these.

The cost caliber lives in ``harness.e2e.pricing`` (canonical family pricing);
we import it here rather than duplicating the pricing table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from harness.e2e.pricing import calculate_cost_from_model_usage


# ─────────────────────────────────────────────────────────────────────────────
# Pure caliber functions — input: parsed agent_stats.json dict
# ─────────────────────────────────────────────────────────────────────────────

def trial_cost(agent_stats: Dict, *, is_claude_code: bool) -> Optional[float]:
    """Total trial cost in USD.

    claude-code trials recalculate from ``modelUsage`` with canonical pricing
    (the Claude Code CLI mis-prices non-Claude models proxied through it); every
    other agent trusts ``summary.total_cost_usd``.
    """
    if is_claude_code:
        cost = calculate_cost_from_model_usage(agent_stats.get("modelUsage", {}))
        if cost is not None:
            return cost
    return agent_stats.get("summary", {}).get("total_cost_usd")


def trial_turns(agent_stats: Dict) -> Optional[int]:
    """Total turns.

    Prefers ``summary.total_turns``; falls back to ``len(usage_units)`` (one per
    LLM API call) for older Claude Code stats whose ``milestone_stats`` were
    empty because git-tag timestamps failed to parse.
    """
    turns = agent_stats.get("summary", {}).get("total_turns")
    if turns and turns > 0:
        return turns

    usage_units = agent_stats.get("usage_units")
    if isinstance(usage_units, list) and usage_units:
        return len(usage_units)

    return turns


def trial_output_tokens(agent_stats: Dict) -> Optional[int]:
    """Total output tokens across all models in ``modelUsage``.

    Sums ``outputTokens`` + ``thoughtsTokens`` (Gemini) + ``reasoningOutputTokens``
    (Codex) + ``reasoningTokens`` (OpenHands) so reasoning-heavy models report
    their full output work.
    """
    model_usage = agent_stats.get("modelUsage", {})
    if not model_usage:
        return None
    total = 0
    for m in model_usage.values():
        if not isinstance(m, dict):
            continue
        total += (
            m.get("outputTokens", 0)
            + m.get("thoughtsTokens", 0)
            + m.get("reasoningOutputTokens", 0)
            + m.get("reasoningTokens", 0)
        )
    return total if total > 0 else None


def trial_duration_ms(agent_stats: Dict) -> Optional[int]:
    """Active agent working time in milliseconds.

    ``summary.duration_ms`` is the sum of session durations (excludes idle gaps
    such as resume delays).  Returns ``None`` when unavailable — callers that
    also want the orchestrator.log wall-clock fallback should use
    :func:`load_e2e_trial_duration`.
    """
    duration_ms = agent_stats.get("summary", {}).get("duration_ms")
    if duration_ms and duration_ms > 0:
        return duration_ms
    return None


# ─────────────────────────────────────────────────────────────────────────────
# File-loading wrappers — monitor use: (workspace_root, trial) → read files
# ─────────────────────────────────────────────────────────────────────────────

def _load_e2e_stats(workspace_root: Path, trial: str) -> Optional[Dict]:
    """Load stats from agent_stats.json, falling back to live agent_stdout.txt.

    agent_stats.json is written once at trial cleanup.  While a trial is still
    running it does not exist, so we fall back to parsing agent_stdout.txt
    (which receives one JSON line per completed session) for live cost/turns.
    """
    trial_dir = workspace_root / "e2e_trial" / trial
    stats_path = trial_dir / "agent_stats.json"

    if stats_path.exists():
        try:
            with open(stats_path) as f:
                return json.load(f)
        except Exception:
            pass

    # Fallback: parse agent_stdout.txt for live stats
    stdout_path = trial_dir / "log" / "agent_stdout.txt"
    if not stdout_path.exists():
        return None
    try:
        total_cost = 0.0
        total_turns = 0
        model_usage: Dict[str, Dict] = {}
        with open(stdout_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "total_cost_usd" not in data and "num_turns" not in data:
                    continue
                total_cost += data.get("total_cost_usd", 0)
                total_turns += data.get("num_turns", 0)
                for model, usage in data.get("modelUsage", {}).items():
                    if not isinstance(usage, dict):
                        continue
                    if model not in model_usage:
                        model_usage[model] = {}
                    for key, val in usage.items():
                        if isinstance(val, (int, float)):
                            model_usage[model][key] = model_usage[model].get(key, 0) + val
        if total_turns == 0 and not model_usage:
            return None
        return {
            "summary": {"total_cost_usd": total_cost, "total_turns": total_turns},
            "modelUsage": model_usage,
            "_live": True,  # marker: parsed from stdout, not agent_stats
        }
    except Exception:
        return None


def load_e2e_trial_cost(workspace_root: Path, trial: str) -> Optional[float]:
    """Load total cost from agent_stats.json or live agent_stdout.txt.

    For claude-code trials: recalculates from modelUsage with canonical
    pricing (corrects Claude Code CLI's wrong rates for non-Claude models).

    Returns total cost in USD or None if not available.
    """
    stats = _load_e2e_stats(workspace_root, trial)
    if stats is None:
        return None
    try:
        return trial_cost(stats, is_claude_code=("claude-code" in trial))
    except Exception:
        return None


def load_e2e_trial_turns(workspace_root: Path, trial: str) -> Optional[int]:
    """Load total turns from agent_stats.json or live agent_stdout.txt."""
    stats = _load_e2e_stats(workspace_root, trial)
    if stats is None:
        return None
    try:
        return trial_turns(stats)
    except Exception:
        return None


def load_e2e_trial_output_tokens(workspace_root: Path, trial: str) -> Optional[int]:
    """Load total output tokens from agent_stats.json or live agent_stdout.txt.

    Sums outputTokens + thoughtsTokens (Gemini) + reasoningOutputTokens (Codex)
    + reasoningTokens (OpenHands) across all models in modelUsage.
    """
    stats = _load_e2e_stats(workspace_root, trial)
    if stats is None:
        return None
    try:
        return trial_output_tokens(stats)
    except Exception:
        return None


def load_e2e_trial_duration(workspace_root: Path, trial: str) -> Optional[int]:
    """Load e2e trial duration from agent_stats.json.

    Uses the sum of all session durations (duration_ms) which represents
    actual agent working time, excluding gaps between sessions (e.g. resume delays).
    Falls back to orchestrator.log wall-clock time if agent_stats.json is unavailable.
    Returns duration in milliseconds or None if not available.
    """
    # Primary: read from agent_stats.json
    stats_path = workspace_root / "e2e_trial" / trial / "agent_stats.json"
    if stats_path.exists():
        try:
            with open(stats_path) as f:
                stats = json.load(f)
            d = trial_duration_ms(stats)
            if d is not None:
                return d
        except Exception:
            pass

    # Fallback: parse orchestrator.log wall-clock time
    import re
    from datetime import datetime

    log_path = workspace_root / "e2e_trial" / trial / "orchestrator.log"
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            content = f.read()

        time_format = "%Y-%m-%d %H:%M:%S,%f"

        start_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Agent started \(first run\)"
        start_match = re.search(start_pattern, content)

        end_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*E2E Trial (?:COMPLETED|INCOMPLETE)"
        end_matches = re.findall(end_pattern, content)

        if not start_match or not end_matches:
            return None

        start_time = datetime.strptime(start_match.group(1), time_format)
        end_time = datetime.strptime(end_matches[-1], time_format)

        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        return duration_ms if duration_ms > 0 else None
    except Exception:
        return None
