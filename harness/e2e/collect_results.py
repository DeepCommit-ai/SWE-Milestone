#!/usr/bin/env python3
"""
Compare milestone or e2e results across trials and print summary tables.

Usage:
    # mstone trials (default --trial-type mstone)
    python harness/e2e/collect_results.py \\
        --workspace-root DATA/harness_workspace/<repo_name>/<workspace> \\
        --trials complete_run_001 complete_run_002

    # e2e trials
    python harness/e2e/collect_results.py \\
        --workspace-root DATA/harness_workspace/<repo_name>/<workspace> \\
        --trials <trial_name> \\
        --trial-type e2e

    # aggregate across repos (reads ``analysis/extract/config.py`` unless ``--config`` is set)
    python harness/e2e/collect_results.py --multi-repo [--trials TRIAL ...] [--config-repos KEY ...]

Result files:
    Prefers ``evaluation_result_filtered.json`` when present; pass ``--non-filter`` to use
    ``evaluation_result.json`` only.

Output:
    ASCII tables (per-milestone detail, optional per-trial comparison, multi-repo summary).
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional


# Pricing is centralized in harness.e2e.pricing (single source of truth).
from harness.e2e.pricing import calculate_cost_from_model_usage as recalculate_cost_from_model_usage


def load_non_graded_milestones(workspace_root: Path) -> Set[str]:
    """Load non-graded milestone IDs from file."""
    non_graded_file = workspace_root / "non-graded_milestone_ids.txt"
    if not non_graded_file.exists():
        return set()
    try:
        with open(non_graded_file) as f:
            return {line.strip() for line in f if line.strip()}
    except Exception as e:
        print(f"Warning: Failed to load {non_graded_file}: {e}", file=sys.stderr)
        return set()


def load_milestones_from_csv(workspace_root: Path) -> Optional[Set[str]]:
    """Load milestone IDs from milestones.csv file.

    Returns None if file doesn't exist or cannot be parsed.
    """
    import csv

    csv_file = workspace_root / "milestones.csv"
    if not csv_file.exists():
        return None
    try:
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return {row["id"].strip() for row in reader if row.get("id", "").strip()}
    except Exception as e:
        print(f"Warning: Failed to load {csv_file}: {e}", file=sys.stderr)
        return None


def load_selected_milestones(workspace_root: Path) -> Tuple[Optional[Set[str]], Optional[str]]:
    """Load selected milestone IDs from file.

    First tries selected_milestone_ids.txt, then falls back to milestones.csv.
    Returns tuple of (milestone_set, source) where source is:
        - "selected_milestone_ids.txt" if loaded from that file
        - "milestones.csv" if loaded from CSV
        - None if no file found (meaning show all milestones)
    """
    selected_file = workspace_root / "selected_milestone_ids.txt"
    if selected_file.exists():
        try:
            with open(selected_file) as f:
                return {line.strip() for line in f if line.strip()}, "selected_milestone_ids.txt"
        except Exception as e:
            print(f"Warning: Failed to load {selected_file}: {e}", file=sys.stderr)

    # Fall back to milestones.csv
    csv_milestones = load_milestones_from_csv(workspace_root)
    if csv_milestones is not None:
        return csv_milestones, "milestones.csv"

    return None, None


def display_width(s: str) -> int:
    """Calculate the display width of a string, accounting for emoji and wide characters."""
    width = 0
    for char in s:
        # Emoji and some symbols take 2 display columns
        if unicodedata.east_asian_width(char) in ("F", "W"):
            width += 2
        elif ord(char) >= 0x1F300:  # Emoji range
            width += 2
        else:
            width += 1
    return width


def pad_to_width(s: str, target_width: int) -> str:
    """Pad a string to reach target display width."""
    current_width = display_width(s)
    padding = target_width - current_width
    if padding > 0:
        return s + " " * padding
    return s


def load_agent_stats(milestone_dir: Path) -> Dict:
    """Load agent_stats.json from milestone directory.

    Returns dict with cost, turns, duration, agent_framework, model or empty dict if not available.

    Duration handling:
    - New format (has wall_clock_ms): duration_ms is already active duration (session-aware)
    - Old format (no wall_clock_ms): re-compute active duration from all_tool_calls via gap detection
    """
    stats_path = milestone_dir / "agent_stats.json"
    if not stats_path.exists():
        return {}
    try:
        with open(stats_path) as f:
            stats = json.load(f)
            summary = stats.get("summary", {})
            duration_ms = summary.get("duration_ms")

            # If wall_clock_ms is present, this is new format - duration_ms is already correct
            # If wall_clock_ms is absent, this is old format - re-compute from tool calls
            if "wall_clock_ms" not in summary and duration_ms and duration_ms > 0:
                duration_ms = _recompute_active_duration(stats) or duration_ms

            # Recalculate cost using canonical family pricing if modelUsage available
            cost = recalculate_cost_from_model_usage(stats.get("modelUsage", {}))
            if cost is None:
                cost = summary.get("total_cost_usd")

            return {
                "cost": cost,
                "turns": summary.get("total_turns"),
                "duration": duration_ms if duration_ms and duration_ms > 0 else None,
                "agent_framework": stats.get("agent_framework"),
                "model": stats.get("model"),
            }
    except Exception:
        return {}


def _recompute_active_duration(stats: Dict) -> Optional[int]:
    """Re-compute active duration from all_tool_calls using gap detection.

    Used for old-format agent_stats.json that doesn't have session-aware duration.
    """
    from datetime import datetime as _dt

    GAP_THRESHOLD_MS = 30 * 60 * 1000  # 30 minutes

    tool_calls = stats.get("all_tool_calls", [])
    if not tool_calls:
        return None

    # Extract and sort timestamps
    timestamps = []
    for tc in tool_calls:
        ts_str = tc.get("timestamp")
        if ts_str:
            try:
                timestamps.append(_dt.fromisoformat(ts_str.rstrip("Z")))
            except (ValueError, TypeError):
                continue

    if len(timestamps) < 2:
        return None

    timestamps.sort()

    # Sum gaps between consecutive tool calls, capping at threshold
    active_ms = 0
    for i in range(1, len(timestamps)):
        gap = int((timestamps[i] - timestamps[i - 1]).total_seconds() * 1000)
        if gap <= GAP_THRESHOLD_MS:
            active_ms += gap

    return active_ms if active_ms > 0 else None


def load_agent_cost(milestone_dir: Path) -> Optional[float]:
    """Load cost from agent_stats.json in milestone directory.

    Returns cost in USD or None if not available.
    """
    stats = load_agent_stats(milestone_dir)
    return stats.get("cost")


def load_agent_duration_from_log(milestone_dir: Path) -> Optional[int]:
    """Load agent execution duration from milestone_runner.log.

    Parses the time difference between Phase 3 (Running agent) and Phase 4.
    Returns duration in milliseconds or None if not available.
    """
    import re
    from datetime import datetime

    # Check both possible locations for log file
    log_path = milestone_dir / "log" / "milestone_runner.log"
    if not log_path.exists():
        log_path = milestone_dir / "milestone_runner.log"
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            content = f.read()

        # Pattern: 2026-01-27 05:59:50,707 [INFO] ... Phase 3: Running agent
        # Use findall + last match to handle retries correctly
        phase3_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Phase 3: Running agent"
        phase4_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Phase 4:"

        phase3_matches = re.findall(phase3_pattern, content)
        phase4_matches = re.findall(phase4_pattern, content)

        phase3_match = phase3_matches[-1] if phase3_matches else None
        phase4_match = phase4_matches[-1] if phase4_matches else None

        if not phase3_match or not phase4_match:
            return None

        time_format = "%Y-%m-%d %H:%M:%S,%f"
        phase3_time = datetime.strptime(phase3_match, time_format)
        phase4_time = datetime.strptime(phase4_match, time_format)

        duration_ms = int((phase4_time - phase3_time).total_seconds() * 1000)
        return duration_ms if duration_ms > 0 else None
    except Exception:
        return None


def format_duration(duration_ms: Optional[int]) -> str:
    """Format duration in milliseconds as minutes with 2 decimal places."""
    if duration_ms is None:
        return "-"
    minutes = duration_ms / 1000 / 60
    return f"{minutes:.2f} min"


def load_e2e_trial_submission_counts(workspace_root: Path, trial: str) -> tuple[int, int]:
    """Load total milestone count and submitted (tagged) count from e2e trial.

    Non-graded milestones are excluded from both counts so that Total/Submit
    columns are consistent with Eval/Resolve (which already exclude non-graded).
    Retries (e.g. M003.1-retry1) are deduplicated to count unique base milestones.
    Returns (total_milestones, submitted_count). Defaults to (0, 0) if unavailable.
    """
    non_graded = load_non_graded_milestones(workspace_root)
    summary_path = workspace_root / "e2e_trial" / trial / "evaluation" / "summary.json"
    if not summary_path.exists():
        # Fall back to selected_milestone_ids.txt for total count
        selected, _ = load_selected_milestones(workspace_root)
        if selected is not None:
            graded = {m for m in selected if m not in non_graded}
            return len(graded), 0
        return 0, 0
    try:
        with open(summary_path) as f:
            summary = json.load(f)
        total = summary.get("total_milestones", 0)
        # Subtract non-graded from total
        total -= len(non_graded)
        # Deduplicate retries to count unique base milestones, excluding non-graded
        base_ids = {_strip_retry_suffix(k) for k in summary.get("results", {})
                    if _strip_retry_suffix(k) not in non_graded}
        return total, len(base_ids)
    except Exception:
        return 0, 0


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
        if "claude-code" in trial:
            cost = recalculate_cost_from_model_usage(stats.get("modelUsage", {}))
            if cost is not None:
                return cost
        return stats.get("summary", {}).get("total_cost_usd")
    except Exception:
        return None


def load_e2e_trial_turns(workspace_root: Path, trial: str) -> Optional[int]:
    """Load total turns from agent_stats.json or live agent_stdout.txt."""
    stats = _load_e2e_stats(workspace_root, trial)
    if stats is None:
        return None
    try:
        turns = stats.get("summary", {}).get("total_turns")
        if turns and turns > 0:
            return turns

        # Older Claude Code agent_stats were written with empty milestone_stats
        # when git tag timestamps failed to parse. The parser still captured
        # de-duplicated usage_units, one per LLM API call, so use them as a
        # display fallback for total turns.
        usage_units = stats.get("usage_units")
        if isinstance(usage_units, list) and usage_units:
            return len(usage_units)

        return turns
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
        model_usage = stats.get("modelUsage", {})
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
            duration_ms = stats.get("summary", {}).get("duration_ms")
            if duration_ms and duration_ms > 0:
                return duration_ms
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


def load_evaluation_result(result_path: Path, prefer_filtered: bool = True) -> Tuple[Optional[Dict], Optional[str]]:
    """Load evaluation result JSON file.

    Args:
        result_path: Path to evaluation_result.json
        prefer_filtered: If True, try to load evaluation_result_filtered.json first

    Returns:
        Tuple of (result_dict, result_type) where result_type is 'filtered', 'unfiltered', or None
    """
    if prefer_filtered:
        # Try filtered version first
        filtered_path = result_path.parent / "evaluation_result_filtered.json"
        if filtered_path.exists():
            try:
                with open(filtered_path) as f:
                    return json.load(f), "filtered"
            except Exception as e:
                print(f"Warning: Failed to load {filtered_path}: {e}", file=sys.stderr)

    # Fall back to regular evaluation_result.json
    if not result_path.exists():
        return None, None
    try:
        with open(result_path) as f:
            return json.load(f), "unfiltered"
    except Exception as e:
        print(f"Warning: Failed to load {result_path}: {e}", file=sys.stderr)
        return None, None


def check_compilation_failure(result: Dict) -> bool:
    """Check if the result indicates a compilation failure."""
    if not result:
        return False

    # Check for compilation failure in patch status
    patch_status = result.get("patch_status", {})
    compilation_success = patch_status.get("compilation_success")

    # If compilation_success is explicitly False
    if compilation_success is False:
        return True

    # Check test summary for signs of compilation failure
    test_summary = result.get("test_summary", {})
    total = test_summary.get("total", 0)

    # If no tests ran at all, it might be compilation failure
    if total == 0:
        return True

    return False


def is_resolved(result: Dict) -> bool:
    """Check if a result is resolved/passed, handling both mstone and e2e formats."""
    if not result:
        return False
    # mstone format uses "resolved"
    if "resolved" in result:
        return result.get("resolved", False)
    # e2e format uses "eval_status"
    if "eval_status" in result:
        return result.get("eval_status") == "passed"
    return False


def score_result(result: Dict) -> Tuple[int, int, int, int]:
    """
    Score a result for comparison. Higher is better.
    Returns: (resolved, f2p_achieved, n2p_achieved, p2p_achieved)
    """
    if not result:
        return (-1, -1, -1, -1)

    # Check for compilation failure
    if check_compilation_failure(result):
        return (-2, -2, -2, -2)

    resolved = 1 if is_resolved(result) else 0
    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    p2p_achieved = ts.get("pass_to_pass_achieved", 0)

    return (resolved, f2p_achieved, n2p_achieved, p2p_achieved)


def format_ratio(achieved: int, required: int) -> str:
    """Format a ratio with check mark if complete (and not 0/0)."""
    if required == 0:
        return "-"
    elif achieved == required:
        return f"✅ {achieved}/{required}"
    elif achieved == 0:
        return f"{achieved}/{required}"
    else:
        return f"⚠️ {achieved}/{required}"


def format_p2p(result: Dict) -> str:
    """Format P2P with check mark if perfect."""
    # For compilation failures, show "-"
    if check_compilation_failure(result):
        return "-"

    ts = result.get("test_summary", {})
    achieved = ts.get("pass_to_pass_achieved", 0)
    required = ts.get("pass_to_pass_required", 0)
    failed = ts.get("pass_to_pass_failed", 0)
    missing = ts.get("pass_to_pass_missing", 0)

    if achieved == required and failed == 0 and missing == 0 and required > 0:
        return f"✅ {achieved}/{required}"
    else:
        return f"{achieved}/{required}"


def get_status(result: Dict) -> str:
    """Get milestone status."""
    if not result:
        return "❌ Not run"

    # Check for e2e not_run status first (before compilation check)
    if result.get("eval_status") == "not_run":
        return "⏳ Not run"

    # Check for synthetic results (agent timeout/killed, no evaluation produced)
    if result.get("_synthetic"):
        failure_reason = result.get("_failure_reason", "unknown")
        if failure_reason == "compilation_failure":
            return "❌ Build failed"
        elif failure_reason == "no_result":
            return "❌ Run failed"
        return "❌ Unknown error"

    if check_compilation_failure(result):
        return "❌ Build failed"

    # Check for e2e error status
    if result.get("eval_status") == "error":
        return "❌ ERROR"

    if is_resolved(result):
        return "✅ RESOLVED"

    return "❌"


def get_failure_note(result: Dict, milestone_id: str = "") -> str:
    """Generate a brief note explaining the failure reason."""
    if not result:
        return "Not run"

    # Check for e2e not_run status first
    if result.get("eval_status") == "not_run":
        return "Not run"

    # Check for synthetic results
    if result.get("_synthetic"):
        failure_reason = result.get("_failure_reason", "unknown")
        if failure_reason == "compilation_failure":
            return "Build failed (no test report)"
        elif failure_reason == "no_result":
            return "Run failed"
        return "Unknown error"

    if check_compilation_failure(result):
        return "Build failed"

    # Check for e2e error status
    if result.get("eval_status") == "error":
        error_msg = result.get("error", "Eval error")
        return error_msg[:25] if len(error_msg) > 25 else error_msg

    if is_resolved(result):
        return "-"

    # Fallback to auto-generated notes
    ts = result.get("test_summary", {})
    f2p_a = ts.get("fail_to_pass_achieved", 0)
    f2p_r = ts.get("fail_to_pass_required", 0)
    n2p_a = ts.get("none_to_pass_achieved", 0)
    n2p_r = ts.get("none_to_pass_required", 0)
    p2p_failed = ts.get("pass_to_pass_failed", 0)
    p2p_missing = ts.get("pass_to_pass_missing", 0)

    issues = []

    # Check F2P
    if f2p_r > 0 and f2p_a < f2p_r:
        issues.append(f"F2P-{f2p_r - f2p_a}")

    # Check N2P
    if n2p_r > 0:
        if n2p_a == 0:
            issues.append(f"N2P incomplete")
        elif n2p_a < n2p_r:
            issues.append(f"N2P-{n2p_r - n2p_a}")

    # Check P2P
    if p2p_failed > 0:
        issues.append(f"{p2p_failed} regressed")
    if p2p_missing > 0:
        issues.append(f"{p2p_missing} missing")

    if not issues:
        return "Other"

    return ", ".join(issues)


def is_milestone_dir(item: Path) -> bool:
    """Check if a directory is a milestone directory.

    A milestone directory is identified by:
    1. Having an evaluation_result.json file, OR
    2. Having a log/ subdirectory (indicates milestone run was attempted), OR
    3. Name matches common milestone patterns (milestone_*, M###, etc.)
    """
    if not item.is_dir():
        return False

    # Check for evaluation result file
    if (item / "evaluation_result.json").exists():
        return True

    # Check for log directory (indicates milestone was run)
    if (item / "log").is_dir():
        return True

    # Check for common milestone naming patterns
    name = item.name
    # Pattern: milestone_XXX, M###, M###.#, etc.
    if name.startswith("milestone_"):
        return True
    if (
        name.startswith("M")
        and len(name) > 1
        and (name[1:].replace(".", "").replace("-", "").isdigit() or name[1:2].isdigit())
    ):
        return True

    return False


def sort_milestone_key(name: str) -> Tuple[str, int, int]:
    """Generate a sort key for milestone names to ensure proper ordering.

    Handles formats like: M001, M001.1, M001.2, M002, milestone_001, etc.
    """
    import re

    # Try to extract numeric parts for proper sorting
    # Pattern for M###.# format
    match = re.match(r"^M(\d+)(?:\.(\d+))?$", name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        return ("M", major, minor)

    # Pattern for milestone_### format
    match = re.match(r"^milestone_(\d+)(?:_sub-(\d+))?", name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        return ("milestone", major, minor)

    # Fallback: alphabetical sorting
    return (name, 0, 0)


def load_e2e_execution_order(workspace_root: Path, trial: str) -> Optional[List[str]]:
    """Load milestone execution order from e2e trial's summary.json by timestamp.

    Returns a list of milestone IDs sorted by execution time, or None if not available.
    """
    from datetime import datetime

    summary_path = workspace_root / "e2e_trial" / trial / "evaluation" / "summary.json"
    if not summary_path.exists():
        return None

    try:
        with open(summary_path) as f:
            summary = json.load(f)

        results = summary.get("results", {})
        if not results:
            return None

        # Parse timestamps and sort
        milestone_times = []
        for milestone_id, data in results.items():
            timestamp_str = data.get("timestamp")
            if timestamp_str:
                try:
                    # Format: "Tue Jan 27 07:24:26 2026"
                    timestamp = datetime.strptime(timestamp_str, "%a %b %d %H:%M:%S %Y")
                    milestone_times.append((milestone_id, timestamp))
                except ValueError:
                    # If parsing fails, use a very early time
                    milestone_times.append((milestone_id, datetime.min))
            else:
                milestone_times.append((milestone_id, datetime.min))

        # Sort by timestamp
        milestone_times.sort(key=lambda x: x[1])
        return [m[0] for m in milestone_times]

    except Exception as e:
        print(f"Warning: Failed to load execution order from {summary_path}: {e}", file=sys.stderr)
        return None


def make_custom_sort_key(custom_order: List[str]):
    """Create a sort key function based on custom order.

    Milestones in custom_order are sorted by their index.
    Milestones not in custom_order are sorted after, using default sorting.
    """

    def sort_key(name: str):
        if name in custom_order:
            return (0, custom_order.index(name), 0, 0)
        else:
            # Put non-listed milestones at the end, sorted by default
            default_key = sort_milestone_key(name)
            return (1, 0, default_key[1], default_key[2])

    return sort_key


def find_milestones(workspace_root: Path, trials: List[str]) -> List[str]:
    """Find all milestones across the given trials (mstone format)."""
    milestones = set()

    for trial in trials:
        trial_dir = workspace_root / "mstone_trial" / trial
        if trial_dir.exists():
            for item in trial_dir.iterdir():
                if is_milestone_dir(item):
                    milestones.add(item.name)

    return sorted(milestones, key=sort_milestone_key)


def _strip_retry_suffix(milestone_id: str) -> str:
    """Strip '-retry{N}' suffix from milestone ID to get the base ID.

    e.g. 'milestone_core_development.3-retry1' -> 'milestone_core_development.3'
    """
    return re.sub(r"-retry\d+$", "", milestone_id)


def _get_retry_attempt(milestone_id: str) -> int:
    """Extract retry attempt number from milestone ID. Returns 0 for base IDs."""
    m = re.search(r"-retry(\d+)$", milestone_id)
    return int(m.group(1)) if m else 0


def find_milestones_e2e(workspace_root: Path, trials: List[str]) -> List[str]:
    """Find all milestones across the given e2e trials.

    Checks both summary.json and individual evaluation directories.
    Retry suffixes (-retry1, -retry2, ...) are stripped to base milestone IDs.
    """
    milestones = set()

    for trial in trials:
        eval_dir = workspace_root / "e2e_trial" / trial / "evaluation"

        # Check summary.json
        summary_path = eval_dir / "summary.json"
        if summary_path.exists():
            try:
                with open(summary_path) as f:
                    summary = json.load(f)
                    results = summary.get("results", {})
                    milestones.update(_strip_retry_suffix(k) for k in results.keys())
            except Exception as e:
                print(f"Warning: Failed to load {summary_path}: {e}", file=sys.stderr)

        # Also check for milestone directories with evaluation_result.json
        if eval_dir.exists():
            for item in eval_dir.iterdir():
                if item.is_dir() and item.name.startswith("M"):
                    if (item / "evaluation_result.json").exists():
                        milestones.add(_strip_retry_suffix(item.name))

    return sorted(milestones, key=sort_milestone_key)


def load_e2e_results(
    workspace_root: Path, trial: str, prefer_filtered: bool = True
) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Load all milestone results from an e2e trial.

    First loads from summary.json, then checks individual evaluation_result.json
    files to supplement missing results and correct eval_status based on the
    authoritative 'resolved' field.

    Returns:
        Tuple of (results, result_type_counts) where result_type_counts
        tracks how many results were loaded from 'filtered' vs 'unfiltered' files.
    """
    results = {}
    result_type_counts = {"filtered": 0, "unfiltered": 0}
    eval_dir = workspace_root / "e2e_trial" / trial / "evaluation"

    # First, load from summary.json
    raw_results = {}
    summary_path = eval_dir / "summary.json"
    if summary_path.exists():
        try:
            with open(summary_path) as f:
                summary = json.load(f)
                raw_results = summary.get("results", {})
        except Exception as e:
            print(f"Warning: Failed to load {summary_path}: {e}", file=sys.stderr)

    # Merge retry keys into base milestone IDs, keeping only the latest attempt.
    # e.g. if both "M001" (attempt 0) and "M001-retry1" (attempt 1) exist,
    # keep only the retry1 result under key "M001".
    for raw_key, raw_val in raw_results.items():
        base_id = _strip_retry_suffix(raw_key)
        attempt = raw_val.get("attempt", _get_retry_attempt(raw_key))
        if base_id not in results or attempt > results[base_id].get("attempt", 0):
            results[base_id] = raw_val

    # Then, check ALL evaluation_result files to supplement or correct results
    # The evaluation_result.json 'resolved' field is the authoritative source
    if eval_dir.exists():
        for item in eval_dir.iterdir():
            if is_milestone_dir(item):
                dir_name = item.name
                base_id = _strip_retry_suffix(dir_name)
                result_file = item / "evaluation_result.json"

                # Try to load the result (filtered or unfiltered based on preference)
                eval_result, result_type = load_evaluation_result(result_file, prefer_filtered)

                if eval_result:
                    resolved = eval_result.get("resolved", False)
                    correct_status = "passed" if resolved else "failed"

                    if base_id not in results:
                        # Add new result from evaluation_result.json
                        results[base_id] = {
                            "eval_status": correct_status,
                            "test_summary": eval_result.get("test_summary", {}),
                            "_from_eval_result": True,
                        }
                        if result_type:
                            result_type_counts[result_type] += 1
                    else:
                        # Correct eval_status if it doesn't match resolved field
                        if results[base_id].get("eval_status") != correct_status:
                            results[base_id]["eval_status"] = correct_status
                            results[base_id]["_corrected"] = True
                        # Replace test_summary with filtered data when available
                        if result_type == "filtered":
                            results[base_id]["test_summary"] = eval_result.get("test_summary", {})
                            result_type_counts["filtered"] += 1

    return results, result_type_counts


def check_log_for_failure(log_path: Path) -> Optional[str]:
    """Check milestone runner log for failure reason.

    Returns a failure reason string if found, None otherwise.
    """
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            content = f.read()

        # Check for common failure patterns
        if "RuntimeError: No valid test report files generated" in content:
            return "compilation_failure"
        if "[ERROR]" in content and ("compilation" in content.lower() or "compile" in content.lower()):
            return "compilation_failure"
        if "BUILD FAILURE" in content:
            return "compilation_failure"

        return "unknown_failure"
    except Exception:
        return None


def compare_trials(
    workspace_root: Path, trials: List[str], prefer_filtered: bool = True
) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Compare trials and return best result for each milestone.

    Returns:
        Tuple of (best_results, result_type_counts) where result_type_counts
        tracks how many results were loaded from 'filtered' vs 'unfiltered' files.
    """
    milestones = find_milestones(workspace_root, trials)

    best_results = {}
    result_type_counts = {"filtered": 0, "unfiltered": 0, "synthetic": 0}

    for milestone in milestones:
        best_result = None
        best_trial = None
        best_score = (-999, -999, -999, -999)
        best_result_type = None
        has_any_attempt = False

        for trial in trials:
            trial_dir = workspace_root / "mstone_trial" / trial / milestone
            result_path = trial_dir / "evaluation" / "evaluation_result.json"
            result, result_type = load_evaluation_result(result_path, prefer_filtered)

            # Check if this trial was attempted (has log directory)
            log_path = trial_dir / "log" / "milestone_runner.log"
            if log_path.exists() or (trial_dir / "log").is_dir():
                has_any_attempt = True

            if result is None:
                # Check log for failure reason
                failure_reason = check_log_for_failure(log_path)
                if failure_reason == "compilation_failure":
                    # Create synthetic result for compilation failure
                    synthetic_result = {
                        "milestone_id": milestone,
                        "resolved": False,
                        "patch_status": {"compilation_success": False},
                        "test_summary": {"total": 0},
                        "_synthetic": True,
                        "_failure_reason": "compilation_failure",
                    }
                    score = score_result(synthetic_result)
                    if score > best_score:
                        best_score = score
                        best_result = synthetic_result
                        best_trial = trial
                        best_result_type = "synthetic"
                continue

            score = score_result(result)

            if score > best_score:
                best_score = score
                best_result = result
                best_trial = trial
                best_result_type = result_type

        # Include milestone if we have a result OR if there was an attempt
        if best_result:
            # Load cost, duration, and turns from the best trial
            cost = None
            duration = None
            turns = None
            agent_framework = None
            model = None
            if best_trial:
                trial_dir = workspace_root / "mstone_trial" / best_trial / milestone
                agent_stats = load_agent_stats(trial_dir)
                cost = agent_stats.get("cost")
                turns = agent_stats.get("turns")
                agent_framework = agent_stats.get("agent_framework")
                model = agent_stats.get("model")
                duration = agent_stats.get("duration") or load_agent_duration_from_log(trial_dir)
            best_results[milestone] = {
                "result": best_result,
                "trial": best_trial,
                "score": best_score,
                "cost": cost,
                "duration": duration,
                "turns": turns,
                "agent_framework": agent_framework,
                "model": model,
            }
            if best_result_type:
                result_type_counts[best_result_type] += 1
        elif has_any_attempt:
            # Milestone was attempted but no result - mark as failed
            # Try to get cost, duration, and turns from any trial that attempted this milestone
            cost = None
            duration = None
            turns = None
            agent_framework = None
            model = None
            for trial in trials:
                trial_dir = workspace_root / "mstone_trial" / trial / milestone
                agent_stats = load_agent_stats(trial_dir)
                cost = agent_stats.get("cost")
                turns = agent_stats.get("turns")
                agent_framework = agent_stats.get("agent_framework")
                model = agent_stats.get("model")
                duration = agent_stats.get("duration") or load_agent_duration_from_log(trial_dir)
                if cost is not None or duration is not None:
                    break
            best_results[milestone] = {
                "result": {
                    "milestone_id": milestone,
                    "resolved": False,
                    "test_summary": {"total": 0},
                    "_synthetic": True,
                    "_failure_reason": "no_result",
                },
                "trial": None,
                "score": (-999, -999, -999, -999),
                "cost": cost,
                "duration": duration,
                "turns": turns,
                "agent_framework": agent_framework,
                "model": model,
            }
            result_type_counts["synthetic"] += 1

    return best_results, result_type_counts


def compare_trials_e2e(
    workspace_root: Path,
    trials: List[str],
    prefer_filtered: bool = True,
    selected_milestones: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Compare e2e trials and return best result for each milestone.

    Args:
        workspace_root: Path to workspace root
        trials: List of trial names to compare
        prefer_filtered: Whether to prefer filtered results
        selected_milestones: Optional set of selected milestone IDs to include
            even if they have no results (will show as "Not run")

    Returns:
        Tuple of (best_results, result_type_counts) where result_type_counts
        tracks how many results were loaded from 'filtered' vs 'unfiltered' files.
    """
    milestones = find_milestones_e2e(workspace_root, trials)

    # Also include selected milestones even if they have no results
    if selected_milestones:
        milestones = sorted(set(milestones) | selected_milestones, key=sort_milestone_key)

    # Load all results from all trials
    trial_results: Dict[str, Dict[str, Dict]] = {}
    total_type_counts = {"filtered": 0, "unfiltered": 0, "synthetic": 0}
    for trial in trials:
        results, type_counts = load_e2e_results(workspace_root, trial, prefer_filtered)
        trial_results[trial] = results
        for k, v in type_counts.items():
            if k in total_type_counts:
                total_type_counts[k] += v

    best_results = {}

    for milestone in milestones:
        best_result = None
        best_trial = None
        best_score = (-999, -999, -999, -999)

        for trial in trials:
            result = trial_results.get(trial, {}).get(milestone)

            if result is None:
                continue

            score = score_result(result)

            if score > best_score:
                best_score = score
                best_result = result
                best_trial = trial

        if best_result:
            # Load cost from agent_stats.json for e2e trials
            cost = None
            if best_trial:
                eval_dir = workspace_root / "e2e_trial" / best_trial / "evaluation" / milestone
                cost = load_agent_cost(eval_dir)
            best_results[milestone] = {
                "result": best_result,
                "trial": best_trial,
                "score": best_score,
                "cost": cost,
            }
        elif selected_milestones and milestone in selected_milestones:
            # Milestone is in selected list but has no results - add placeholder
            best_results[milestone] = {
                "result": {
                    "milestone_id": milestone,
                    "resolved": False,
                    "eval_status": "not_run",
                    "test_summary": {},
                    "_synthetic": True,
                    "_failure_reason": "no_result",
                },
                "trial": None,
                "score": (-999, -999, -999, -999),
                "cost": None,
            }
            total_type_counts["synthetic"] += 1

    return best_results, total_type_counts


def compute_repo_summary(
    workspace_root: Path,
    trials: List[str],
    trial_type: str = "e2e",
    prefer_filtered: bool = True,
) -> Dict:
    """Compute aggregate metrics for a single repo across given trials.

    Returns dict with keys: graded, resolved, resolve_pct,
    score_1000, score_full, score_reliable, cost, duration, turns,
    total_milestones, submitted.
    """
    selected_milestones, _ = load_selected_milestones(workspace_root)
    non_graded = load_non_graded_milestones(workspace_root)

    include_selected = selected_milestones

    if trial_type == "e2e":
        results, _ = compare_trials_e2e(workspace_root, trials, prefer_filtered, include_selected)
    else:
        results, _ = compare_trials(workspace_root, trials, prefer_filtered)

    if not results:
        return {"error": True}

    # Filter by selected milestones
    if selected_milestones is not None:
        results = {k: v for k, v in results.items() if k in selected_milestones}

    resolved_count = 0
    graded_count = 0
    evaluated_count = 0
    sum_score_1000 = 0.0
    sum_score_full = 0.0
    sum_score_reliable = 0.0
    sum_precision = 0.0
    sum_recall = 0.0

    for milestone, data in results.items():
        result = data["result"]
        if milestone not in non_graded:
            graded_count += 1
            if result.get("eval_status") != "not_run":
                evaluated_count += 1
            if is_resolved(result):
                resolved_count += 1
            s1000 = calculate_score_v2(result)
            sfull = calculate_score(result)
            srel = calculate_score_reliable(result)
            prec, rec = calculate_precision_recall(result)
            if s1000 is not None:
                sum_score_1000 += s1000
            if sfull is not None:
                sum_score_full += sfull
            if srel is not None:
                sum_score_reliable += srel
            if prec is not None:
                sum_precision += prec
            if rec is not None:
                sum_recall += rec

    # Aggregate cost/duration/turns/output_tokens across trials
    total_cost = 0.0
    total_duration = 0
    total_turns = 0
    total_output_tokens = 0
    total_ms = 0
    total_submitted = 0

    if trial_type == "e2e":
        for trial in trials:
            c = load_e2e_trial_cost(workspace_root, trial)
            if c is not None:
                total_cost += c
            d = load_e2e_trial_duration(workspace_root, trial)
            if d is not None:
                total_duration += d
            t = load_e2e_trial_turns(workspace_root, trial)
            if t is not None:
                total_turns += t
            ot = load_e2e_trial_output_tokens(workspace_root, trial)
            if ot is not None:
                total_output_tokens += ot
            ms, sub = load_e2e_trial_submission_counts(workspace_root, trial)
            total_ms += ms
            total_submitted += sub

    return {
        "error": False,
        "graded": graded_count,
        "evaluated": evaluated_count,
        "resolved": resolved_count,
        "resolve_pct": resolved_count * 100 / graded_count if graded_count > 0 else 0.0,
        "score_1000": sum_score_1000 / graded_count * 100 if graded_count > 0 else 0.0,
        "score_full": sum_score_full / graded_count * 100 if graded_count > 0 else 0.0,
        "score_reliable": sum_score_reliable / graded_count * 100 if graded_count > 0 else 0.0,
        "precision": sum_precision / graded_count * 100 if graded_count > 0 else 0.0,
        "recall": sum_recall / graded_count * 100 if graded_count > 0 else 0.0,
        "cost": total_cost if total_cost > 0 else None,
        "duration": total_duration if total_duration > 0 else None,
        "turns": total_turns if total_turns > 0 else None,
        "output_tokens": total_output_tokens if total_output_tokens > 0 else None,
        "total_milestones": total_ms,
        "submitted": total_submitted,
    }


def print_multi_repo_table(summaries: List[Dict], trial_label: str = "",
                           repo_roots: Optional[Dict[str, Path]] = None):
    """Print a summary table aggregating results across multiple repos."""
    if not summaries:
        print("No results to display.")
        return

    valid = [s for s in summaries if not s.get("error")]
    if not valid:
        print("No valid results found for any repo.")
        return

    # Pre-compute display strings
    resolve_strs = {}
    done_strs = {}
    for s in summaries:
        if not s.get("error"):
            resolve_strs[s["repo"]] = f"{s['resolve_pct']:.2f}% ({s['resolved']}/{s['graded']})"
            done_strs[s["repo"]] = f"{s['evaluated']}/{s['graded']}"
        else:
            resolve_strs[s["repo"]] = "-"
            done_strs[s["repo"]] = "-"

    # Column widths
    repo_w = max(len("Repo"), max(len(s["repo"]) for s in summaries)) + 2
    resolve_w = max(10, max(len(v) for v in resolve_strs.values()) + 1)
    done_w = max(6, max(len(v) for v in done_strs.values()) + 1)

    cols = [
        ("Done", done_w),
        ("Score", 10),
        ("Resolve", resolve_w),
        ("Precision", 10),
        ("Recall", 10),
        ("Cost", 10),
        ("Time", 12),
        ("Turns", 8),
        ("OutTok(k)", 10),
    ]

    def _build_row(values: List[str]) -> str:
        """Build a table row from a list of cell values."""
        all_widths = [repo_w] + [cw for _, cw in cols]
        parts = []
        for val, w in zip(values, all_widths):
            parts.append(f" {val:<{w}} ")
        return "\u2502" + "\u2502".join(parts) + "\u2502"

    def _build_row_right(label: str, values: List[str]) -> str:
        """Build a table row with left-aligned label and right-aligned values."""
        all_widths = [repo_w] + [cw for _, cw in cols]
        parts = [f" {label:<{all_widths[0]}} "]
        for val, w in zip(values, all_widths[1:]):
            parts.append(f" {val:>{w}} ")
        return "\u2502" + "\u2502".join(parts) + "\u2502"

    # Build separators
    all_widths = [repo_w] + [cw for _, cw in cols]
    sep_top = "\u250c" + "\u252c".join("\u2500" * (w + 2) for w in all_widths) + "\u2510"
    sep_mid = "\u251c" + "\u253c".join("\u2500" * (w + 2) for w in all_widths) + "\u2524"
    sep_bot = "\u2514" + "\u2534".join("\u2500" * (w + 2) for w in all_widths) + "\u2518"

    # Header
    header_vals = ["Repo"] + [cn for cn, _ in cols]
    header = _build_row(header_vals)

    if trial_label:
        print(f"\n\U0001f3c3 Trial: {trial_label}")
        # Show agent/model/effort config
        trial_cfg = _load_trial_config(repo_roots, trial_label) if repo_roots else {}
        if trial_cfg:
            effort = trial_cfg.get("effort") or "n/a"
            agent_str = f"\033[36;1m{trial_cfg.get('agent', '?')}\033[0m"
            model_str = f"\033[33;1m{trial_cfg.get('model', '?')}\033[0m"
            effort_str = f"\033[35meffort={effort}\033[0m"
            parts = [agent_str, model_str, effort_str]
            ctx = trial_cfg.get("context_window")
            if ctx:
                ctx_label = "1M" if ctx >= 1_000_000 else f"{ctx // 1000}K"
                parts.append(f"\033[35mcontext={ctx_label}\033[0m")
            # Context auto-compaction window (trial config `auto_compact_window`).
            # claude-code compacts at min(this, model window); for a
            # pattern-unknown third-party model the window is 200K, so any value
            # >= 200K compacts at ~200K. Show configured→effective when they
            # differ so the 200K cap is visible at a glance.
            acw = trial_cfg.get("auto_compact_window")
            if acw:
                acw = int(acw)
                eff = min(acw, ctx) if ctx else acw
                lbl = f"{acw // 1000}K" if eff == acw else f"{acw // 1000}K→{eff // 1000}K"
                parts.append(f"\033[35mcompact={lbl}\033[0m")
            print(f"  {' | '.join(parts)}")
    print()
    print(sep_top)
    print(header)
    print(sep_mid)

    # Accumulators for average
    n_valid = 0
    avg_score = 0.0
    avg_prec = 0.0
    avg_rec = 0.0
    avg_resolve = 0.0
    sum_cost = 0.0
    sum_dur = 0
    sum_turns = 0
    sum_out_tok = 0

    for s in summaries:
        repo = s["repo"]
        if s.get("error"):
            print(_build_row_right(repo, ["-"] * len(cols)))
            continue

        n_valid += 1
        avg_score += s["score_reliable"]
        avg_prec += s["precision"]
        avg_rec += s["recall"]
        avg_resolve += s["resolve_pct"]
        if s["cost"] is not None:
            sum_cost += s["cost"]
        if s["duration"] is not None:
            sum_dur += s["duration"]
        if s["turns"] is not None:
            sum_turns += s["turns"]
        if s["output_tokens"] is not None:
            sum_out_tok += s["output_tokens"]

        cost_str = f"${s['cost']:.2f}" if s["cost"] is not None else "-"
        dur_str = format_duration(s["duration"]) if s["duration"] else "-"
        turns_str = str(s["turns"]) if s["turns"] else "-"
        out_tok_str = f"{s['output_tokens'] / 1000:.1f}" if s["output_tokens"] else "-"

        vals = [
            done_strs[repo],
            f"{s['score_reliable']:.2f}%",
            resolve_strs[repo],
            f"{s['precision']:.2f}%",
            f"{s['recall']:.2f}%",
            cost_str,
            dur_str,
            turns_str,
            out_tok_str,
        ]
        print(_build_row_right(repo, vals))

    # Average row
    if n_valid > 0:
        print(sep_mid)
        avg_cost_str = f"${sum_cost / n_valid:.2f}" if sum_cost > 0 else "-"
        avg_dur_str = format_duration(int(sum_dur / n_valid)) if sum_dur > 0 else "-"
        avg_turns_str = str(int(sum_turns / n_valid)) if sum_turns > 0 else "-"
        avg_out_tok_str = f"{sum_out_tok / n_valid / 1000:.1f}" if sum_out_tok > 0 else "-"

        avg_vals = [
            "",
            f"{avg_score / n_valid:.2f}%",
            f"{avg_resolve / n_valid:.2f}%",
            f"{avg_prec / n_valid:.2f}%",
            f"{avg_rec / n_valid:.2f}%",
            avg_cost_str,
            avg_dur_str,
            avg_turns_str,
            avg_out_tok_str,
        ]
        print(_build_row_right("AVERAGE", avg_vals))

    print(sep_bot)


def _detect_repo_status(workspace_root: Path, trial: str) -> str:
    """Detect repo status by checking trial directory, lock file, and docker container."""
    import subprocess

    trial_dir = workspace_root / "e2e_trial" / trial
    if not trial_dir.exists():
        return "pending"

    # Check if orchestrator process is alive via lock file
    orchestrator_alive = False
    lock_file = trial_dir / ".trial.lock"
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            subprocess.run(["kill", "-0", str(pid)], check=True, capture_output=True)
            orchestrator_alive = True
        except (ValueError, subprocess.CalledProcessError):
            pass

    if orchestrator_alive:
        return "running"

    # Orchestrator is not alive — check if trial is fully complete or just stopped.
    eval_dir = trial_dir / "evaluation"
    summary_file = eval_dir / "summary.json" if eval_dir.exists() else None
    if summary_file and summary_file.exists():
        try:
            import json as _json
            with open(summary_file) as _f:
                _summary = _json.load(_f)
            _rs = _summary.get("resume_state", {})
            _total = _summary.get("total_milestones", 0)

            # Method 1: resume_state tracks completed/failed/skipped
            _completed = set(_rs.get("completed_milestones", []))
            _failed = set(_rs.get("failed_milestones", []))
            _skipped = set(_rs.get("skipped_milestones", []))
            _accounted = len(_completed | _failed | _skipped)
            if _total > 0 and _accounted >= _total:
                return "done"

            # Method 2: check top-level results count (used by OpenHands)
            _results = _summary.get("results", {})
            if _total > 0 and len(_results) >= _total:
                return "done"

            # Method 3: check submitted_milestones in resume_state
            _submitted = len(_rs.get("submitted_milestones", []))
            if _total > 0 and _submitted >= _total:
                return "done"
        except Exception:
            pass
        # Has eval results but not all milestones done — stopped mid-run
        return "stopped"

    # No evaluation results yet — check if docker container is still running
    # (could indicate the orchestrator crashed mid-run)
    repo_name = workspace_root.name
    container_name = f"{repo_name}-{trial}"
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "true" in result.stdout.lower():
            return "stopped"
    except Exception:
        pass

    return "stopped"


def _short_repo_name(repo: str) -> str:
    """Shorten repo name: 'BurntSushi_ripgrep_14.1.1_15.0.0' → 'ripgrep'.

    Format is '{owner}_{project}_{start_ver}_{end_ver}'.
    Returns the project name (second segment).
    """
    parts = repo.split("_")
    if len(parts) >= 2:
        return parts[1]
    return parts[0]


STATUS_ICONS = {
    "running": "\033[32m●\033[0m running",
    "done": "\033[34m✓\033[0m done",
    "stopped": "\033[33m■\033[0m stopped",
    "pending": "\033[90m○\033[0m pending",
}


def _load_trial_config(repo_roots: Dict[str, Path], trial: str) -> Dict[str, str]:
    """Load agent/model/effort/context from trial metadata and agent stats."""
    for repo, ws_root in repo_roots.items():
        meta_path = ws_root / "e2e_trial" / trial / "trial_metadata.json"
        if meta_path.exists():
            try:
                import json as _json
                with open(meta_path) as f:
                    meta = _json.load(f)
                # Resolve effective reasoning effort:
                # If metadata.reasoning_effort is None it means the harness did
                # not pass --effort to the CLI — the agent uses its built-in
                # default, which depends on both agent and model.
                effort = meta.get("reasoning_effort")
                if effort is None:
                    agent = meta.get("agent_name", "")
                    model = (meta.get("model") or "").lower()
                    # Per Anthropic docs (code.claude.com/docs/en/model-config):
                    # opus-4-7 defaults to xhigh; opus-4-6/sonnet-4-6 default
                    # to high (API/Enterprise tier).
                    if agent == "claude-code":
                        if "opus-4-7" in model:
                            effort = "xhigh"
                        else:
                            effort = "high"
                    elif agent == "codex":
                        effort = "xhigh"
                    elif agent == "openhands":
                        effort = "high"  # CLI mode default
                    elif agent == "gemini-cli":
                        # gemini-cli runs thinkingLevel HIGH (dynamic budget) by
                        # default and has no graded levels (only thinking on/off),
                        # so HIGH is its built-in and maximum reasoning level.
                        effort = "high"

                # Detect context window from agent_stats.json modelUsage.
                # Codex CLI reports the *compressed* context window (95% of the
                # model's actual context).  Recover the model context window so
                # the display matches the model spec (e.g. 272K, not 258K).
                context_window = None
                stats_path = ws_root / "e2e_trial" / trial / "agent_stats.json"
                if stats_path.exists():
                    try:
                        with open(stats_path) as f:
                            stats = _json.load(f)
                        model_name = meta.get("model", "")
                        mu = stats.get("modelUsage", {})
                        # Check the primary model's contextWindow
                        if model_name in mu:
                            context_window = mu[model_name].get("contextWindow")
                        elif mu:
                            # Fallback: use the first model entry
                            context_window = next(iter(mu.values()), {}).get("contextWindow")
                        # Recover model context from Codex's 95% compressed value
                        if context_window and meta.get("agent_name") == "codex":
                            context_window = round(context_window / 0.95)
                    except Exception:
                        pass

                return {
                    "agent": meta.get("agent_name", ""),
                    "model": meta.get("model", ""),
                    "effort": effort,
                    "context_window": context_window,
                    # Native compaction window from the trial config; None unless
                    # `auto_compact_window` was set. The header shows configured
                    # vs effective (min with the model window — 200K for a
                    # pattern-unknown third-party model id).
                    "auto_compact_window": meta.get("auto_compact_window"),
                }
            except Exception:
                pass
    return {}


def print_compact_table(
    summaries: List[Dict],
    trial_label: str = "",
    repo_roots: Optional[Dict[str, Path]] = None,
    trial: str = "",
):
    """Print a compact summary table that fits in 80 columns."""
    if not summaries:
        print("No results to display.")
        return

    # Detect status for each repo
    statuses = {}
    for s in summaries:
        repo = s["repo"]
        if repo_roots and repo in repo_roots and trial:
            statuses[repo] = _detect_repo_status(repo_roots[repo], trial)
        else:
            statuses[repo] = "pending" if s.get("error") else "done"

    n_running = sum(1 for v in statuses.values() if v == "running")
    n_done = sum(1 for v in statuses.values() if v == "done")
    n_pending = sum(1 for v in statuses.values() if v == "pending")
    n_stopped = sum(1 for v in statuses.values() if v == "stopped")

    # Trial config line
    trial_cfg = _load_trial_config(repo_roots, trial) if repo_roots else {}
    config_str = ""
    if trial_cfg:
        effort = trial_cfg.get("effort") or "n/a"

        agent_str = f"\033[36;1m{trial_cfg.get('agent', '?')}\033[0m"
        model_str = f"\033[33;1m{trial_cfg.get('model', '?')}\033[0m"
        effort_str = f"\033[35meffort={effort}\033[0m"
        parts = [agent_str, model_str, effort_str]
        ctx = trial_cfg.get("context_window")
        if ctx:
            ctx_label = "1M" if ctx >= 1_000_000 else f"{ctx // 1000}K"
            parts.append(f"\033[35mcontext={ctx_label}\033[0m")
        config_str = f"\n  {' | '.join(parts)}"

    # Header
    status_parts = []
    if n_running:
        status_parts.append(f"\033[32m{n_running} running\033[0m")
    if n_done:
        status_parts.append(f"\033[34m{n_done} done\033[0m")
    if n_stopped:
        status_parts.append(f"\033[33m{n_stopped} stopped\033[0m")
    if n_pending:
        status_parts.append(f"\033[90m{n_pending} pending\033[0m")

    print(f"\n\U0001f3c3 {trial_label or 'Trial'} | {', '.join(status_parts)}{config_str}")
    print()

    # Column widths (total ~78)
    name_w = 14
    prog_w = 20  # "Total Submit Eval"
    score_w = 7
    resolve_w = 14
    status_w = 14

    # Header
    hdr = (
        f"  {'Repo':<{name_w}} {'Total Submit Eval':>{prog_w}} {'Score':>{score_w}} "
        f"{'Resolve':>{resolve_w}}  {'Status'}"
    )
    print(hdr)
    print("  " + "─" * 74)

    # Accumulators
    total_resolved = 0
    total_graded = 0
    total_score = 0.0
    total_resolve_pct = 0.0
    n_valid = 0

    for s in summaries:
        repo = s["repo"]
        short = _short_repo_name(repo)
        status = statuses.get(repo, "pending")
        status_str = STATUS_ICONS.get(status, status)

        if s.get("error"):
            prog_str = "--"
            score_str = "--"
            resolve_str = "--"
        else:
            total_ms = s.get('total_milestones') or s['graded']
            submitted = s.get('submitted', 0)
            evaluated = s['evaluated']
            prog_str = f"{total_ms:>4}   {submitted:>4}  {evaluated:>3}"
            score_str = f"{s['score_reliable']:.1f}%" if evaluated > 0 else "--"
            resolve_str = f"{s['resolve_pct']:.0f}% ({s['resolved']}/{s['graded']})"
            total_resolved += s['resolved']
            total_graded += s['graded']
            total_score += s['score_reliable']
            total_resolve_pct += s['resolve_pct']
            n_valid += 1

        print(
            f"  {short:<{name_w}} {prog_str:>{prog_w}} {score_str:>{score_w}} "
            f"{resolve_str:>{resolve_w}}  {status_str}"
        )

    # Total row — Score and Resolve both use macro average (mean of per-repo
    # rates) for consistency with print_full_table; counts in parens are the
    # underlying micro-aggregate (sum across repos).
    print("  " + "─" * 74)
    if n_valid > 0:
        avg_score = f"{total_score / n_valid:.1f}%"
        avg_resolve_pct = total_resolve_pct / n_valid
        total_resolve = f"{avg_resolve_pct:.0f}% ({total_resolved}/{total_graded})"
    else:
        avg_score = "--"
        total_resolve = "--"
    print(f"  {'AVERAGE':<{name_w}} {'':>{prog_w}} {avg_score:>{score_w}} {total_resolve:>{resolve_w}}")
    print()


def print_detail_table(
    workspace_root: Path,
    trials: List[str],
    trial_type: str = "e2e",
    prefer_filtered: bool = True,
    trial_label: str = "",
    repo_roots: Optional[Dict[str, Path]] = None,
):
    """Print per-milestone detail table for a single repo, compact for 80 cols."""
    selected_milestones, _ = load_selected_milestones(workspace_root)
    non_graded = load_non_graded_milestones(workspace_root)

    include_selected = selected_milestones
    if trial_type == "e2e":
        results, _ = compare_trials_e2e(workspace_root, trials, prefer_filtered, include_selected)
    else:
        results, _ = compare_trials(workspace_root, trials, prefer_filtered)

    if not results:
        return False

    if selected_milestones is not None:
        results = {k: v for k, v in results.items() if k in selected_milestones}

    # Sort by e2e execution order if available
    custom_order = load_e2e_execution_order(workspace_root, trials[0]) if trials else None
    if custom_order:
        sort_key = make_custom_sort_key(custom_order)
        sorted_milestones = sorted(results.keys(), key=sort_key)
    else:
        sorted_milestones = sorted(results.keys())

    repo_name = _short_repo_name(workspace_root.name)

    # Count stats
    n_graded = sum(1 for m in sorted_milestones if m not in non_graded)
    n_evaluated = 0
    n_resolved = 0
    total_score = 0.0
    for m in sorted_milestones:
        if m in non_graded:
            continue
        r = results[m]["result"]
        if r.get("eval_status") != "not_run":
            n_evaluated += 1
        if is_resolved(r):
            n_resolved += 1
        s = calculate_score_reliable(r)
        if s is not None:
            total_score += s

    avg_score_str = f"{total_score / n_graded * 100:.1f}%" if n_graded > 0 else "--"
    print(f"\n\U0001f4cb {repo_name} ({n_evaluated}/{n_graded} evaluated, score: {avg_score_str})")
    print()

    # Column widths
    m_w = 22
    f2p_w = 5
    n2p_w = 5
    p2p_w = 4
    score_w = 6
    prec_w = 6
    rec_w = 6
    status_w = 14

    hdr = (
        f"  {'Milestone':<{m_w}} {'F2P':>{f2p_w}} {'N2P':>{n2p_w}} "
        f"{'P2P':>{p2p_w}} {'Score':>{score_w}} {'Prec':>{prec_w}} {'Rec':>{rec_w}}  {'Status'}"
    )
    print(hdr)
    print("  " + "─" * 80)

    for milestone in sorted_milestones:
        data = results[milestone]
        result = data["result"]
        ts = result.get("test_summary", {})

        f2p_a = ts.get("fail_to_pass_achieved", 0)
        f2p_r = ts.get("fail_to_pass_required", 0)
        n2p_a = ts.get("none_to_pass_achieved", 0)
        n2p_r = ts.get("none_to_pass_required", 0)

        if result.get("eval_status") == "not_run":
            f2p_str = "--"
            n2p_str = "--"
            p2p_str = "--"
            score_str = "--"
            prec_str = "--"
            rec_str = "--"
            if milestone in non_graded:
                status = "\033[90m🚫 non-graded\033[0m"
            else:
                status = "\033[90m⏳ pending\033[0m"
        else:
            f2p_str = f"{f2p_a}/{f2p_r}"
            n2p_str = f"{n2p_a}/{n2p_r}"
            p2p_achieved = ts.get("pass_to_pass_achieved", 0)
            p2p_required = ts.get("pass_to_pass_required", 0)
            p2p_failed = p2p_required - p2p_achieved
            p2p_str = "✓" if p2p_failed == 0 else f"✗{p2p_failed}"

            prec, rec = calculate_precision_recall(result)
            prec_str = f"{prec * 100:.0f}%" if prec is not None else "--"
            rec_str = f"{rec * 100:.0f}%" if rec is not None else "--"

            if milestone in non_graded:
                score_str = "n/a"
                status = "\033[90m🚫 non-graded\033[0m"
            else:
                s = calculate_score_reliable(result)
                score_str = f"{s * 100:.1f}%" if s is not None else "--"
                if is_resolved(result):
                    status = "\033[32m✅ resolved\033[0m"
                elif check_compilation_failure(result):
                    status = "\033[31m💥 compile\033[0m"
                else:
                    status = "\033[31m❌ failed\033[0m"

        # Truncate milestone name if too long
        m_display = milestone if len(milestone) <= m_w else milestone[:m_w - 2] + ".."

        print(
            f"  {m_display:<{m_w}} {f2p_str:>{f2p_w}} {n2p_str:>{n2p_w}} "
            f"{p2p_str:>{p2p_w}} {score_str:>{score_w}} {prec_str:>{prec_w}} {rec_str:>{rec_w}}  {status}"
        )

    print("  " + "─" * 80)

    # Cost/time/turns footer
    cost = None
    duration = None
    turns = None
    out_tok = None
    for trial in trials:
        c = load_e2e_trial_cost(workspace_root, trial)
        if c:
            cost = (cost or 0) + c
        d = load_e2e_trial_duration(workspace_root, trial)
        if d:
            duration = (duration or 0) + d
        t = load_e2e_trial_turns(workspace_root, trial)
        if t:
            turns = (turns or 0) + t
        ot = load_e2e_trial_output_tokens(workspace_root, trial)
        if ot:
            out_tok = (out_tok or 0) + ot

    parts = []
    if cost is not None:
        parts.append(f"Cost: ${cost:.2f}")
    if duration is not None:
        parts.append(f"Time: {format_duration(duration)}")
    if turns is not None:
        parts.append(f"Turns: {turns}")
    if out_tok is not None:
        parts.append(f"OutTok: {out_tok / 1000:.0f}k")
    if parts:
        print(f"  {' | '.join(parts)}")
    print()
    return True


def format_cost(cost: Optional[float]) -> str:
    """Format cost in USD."""
    if cost is None:
        return "-"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def calculate_score(result: Dict) -> Optional[float]:
    """Calculate milestone score (Algorithm V1).

    Formula: (F2P_achieved + N2P_achieved) / (F2P_required + N2P_required) * (P2P_achieved / P2P_required)

    Uses ratio-based P2P penalty instead of absolute numbers, which is fairer for
    projects with large test suites.

    Returns None if the result is invalid, 0.0 if compilation failure.
    """
    if not result:
        return None

    if check_compilation_failure(result):
        return 0.0

    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    f2p_required = ts.get("fail_to_pass_required", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    n2p_required = ts.get("none_to_pass_required", 0)
    p2p_achieved = ts.get("pass_to_pass_achieved", 0)
    p2p_required = ts.get("pass_to_pass_required", 0)

    # Calculate the first part: (F2P + N2P) ratio
    total_required = f2p_required + n2p_required
    if total_required == 0:
        # If no F2P or N2P required, treat as 100% achieved
        first_part = 1.0
    else:
        first_part = (f2p_achieved + n2p_achieved) / total_required

    # Calculate the second part: P2P ratio penalty
    # Uses ratio instead of absolute numbers for fairness across different project sizes
    if p2p_required > 0:
        second_part = p2p_achieved / p2p_required
    else:
        second_part = 1.0

    return first_part * second_part


def calculate_score_v2(result: Dict) -> Optional[float]:
    """Calculate milestone score (Algorithm V2).

    Formula: (F2P_achieved + N2P_achieved) / (F2P_required + N2P_required) * max(0, 1 - P2P_missed / min(1000, P2P_required))

    This algorithm:
    - Uses F2P + N2P for the main score (same as V1)
    - Caps P2P penalty denominator at 1000 to avoid over-penalizing large test suites
    - P2P_missed = P2P_required - P2P_achieved

    Returns None if the result is invalid, 0.0 if compilation failure.
    """
    if not result:
        return None

    if check_compilation_failure(result):
        return 0.0

    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    f2p_required = ts.get("fail_to_pass_required", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    n2p_required = ts.get("none_to_pass_required", 0)
    p2p_achieved = ts.get("pass_to_pass_achieved", 0)
    p2p_required = ts.get("pass_to_pass_required", 0)

    # Calculate the first part: (F2P + N2P) ratio
    total_required = f2p_required + n2p_required
    if total_required == 0:
        # If no F2P or N2P required, treat as 100% achieved
        first_part = 1.0
    else:
        first_part = (f2p_achieved + n2p_achieved) / total_required

    # Calculate the second part: P2P penalty with capped denominator
    # P2P_missed = P2P_required - P2P_achieved
    p2p_missed = p2p_required - p2p_achieved
    if p2p_required > 0:
        capped_p2p = min(1000, p2p_required)
        second_part = max(0.0, 1.0 - p2p_missed / capped_p2p)
    else:
        second_part = 1.0

    return first_part * second_part


def calculate_score_reliable(result: Dict) -> Optional[float]:
    """Calculate milestone score_reliable (PR-F1 over fix vs regression counts).

    Definitions:
      N_target = F2P_required + N2P_required
      N_fixed = F2P_achieved + N2P_achieved
      N_broken = P2P_failed + P2P_missing

    Edge handling:
      - If N_target == 0 and N_fixed == 0: Recall = 1
      - Precision uses epsilon smoothing with ε=1:
          Precision = (N_fixed + 1) / (N_fixed + N_broken + 1)
      - If Precision == Recall == 0: score = 0

    Returns None if result is invalid, 0.0 if compilation failure.
    """
    if not result:
        return None

    if check_compilation_failure(result):
        return 0.0

    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    f2p_required = ts.get("fail_to_pass_required", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    n2p_required = ts.get("none_to_pass_required", 0)
    p2p_failed = ts.get("pass_to_pass_failed", 0)
    p2p_missing = ts.get("pass_to_pass_missing", 0)

    n_target = f2p_required + n2p_required
    n_fixed = f2p_achieved + n2p_achieved
    n_broken = p2p_failed + p2p_missing

    if n_target == 0:
        recall = 1.0 if n_fixed == 0 else 0.0
    else:
        recall = n_fixed / n_target

    # epsilon smoothing on precision to avoid overly hard 0 when N_fixed == 0
    epsilon = 1.0
    precision = (n_fixed + epsilon) / (n_fixed + n_broken + epsilon)

    if precision == 0 and recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def calculate_precision_recall(result: Dict) -> Tuple[Optional[float], Optional[float]]:
    """Calculate precision and recall components of score_reliable.

    Returns (precision, recall) tuple, or (None, None) if result is invalid.
    """
    if not result:
        return None, None

    if check_compilation_failure(result):
        return 0.0, 0.0

    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    f2p_required = ts.get("fail_to_pass_required", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    n2p_required = ts.get("none_to_pass_required", 0)
    p2p_failed = ts.get("pass_to_pass_failed", 0)
    p2p_missing = ts.get("pass_to_pass_missing", 0)

    n_target = f2p_required + n2p_required
    n_fixed = f2p_achieved + n2p_achieved
    n_broken = p2p_failed + p2p_missing

    if n_target == 0:
        recall = 1.0 if n_fixed == 0 else 0.0
    else:
        recall = n_fixed / n_target

    epsilon = 1.0
    precision = (n_fixed + epsilon) / (n_fixed + n_broken + epsilon)

    return precision, recall


def format_score(score: Optional[float]) -> str:
    """Format score as percentage for display."""
    if score is None:
        return "-"
    return f"{score * 100:.2f}%"


def print_comparison_table(
    best_results: Dict[str, Dict],
    non_graded_milestones: Set[str] = None,
    show_cost_column: bool = True,
    total_cost: Optional[float] = None,
    show_time_column: bool = False,
    custom_sort_key=None,
    trial_names: List[str] = None,
    workspace_root: Path = None,
    trial_type: str = None,
    total_duration: Optional[int] = None,
    total_turns: Optional[int] = None,
):
    """Print comparison table.

    Args:
        best_results: Dictionary of milestone results
        non_graded_milestones: Set of milestone IDs that are not graded
        show_cost_column: Whether to show per-milestone Cost column (default True)
        total_cost: Total cost to display in summary (if None, sum from per-milestone costs)
        show_time_column: Whether to show per-milestone Time column (default False, only for mstone)
        custom_sort_key: Optional custom sort key function for milestone ordering
        trial_names: List of trial names being compared
        workspace_root: Path to workspace root for extracting repo info
        trial_type: Type of trial ('mstone' or 'e2e')
        total_duration: Total duration in ms for e2e trials (from orchestrator.log)
        total_turns: Total turns for e2e trials (from agent_stats.json)
    """
    if non_graded_milestones is None:
        non_graded_milestones = set()

    # Sort milestones by custom order if provided, otherwise by name
    sort_key = custom_sort_key if custom_sort_key else sort_milestone_key
    sorted_milestones = sorted(best_results.keys(), key=sort_key)

    # Pre-calculate all notes, scores and stats
    notes = {}
    scores = {}
    scores_v2 = {}
    scores_reliable = {}
    resolved_count = 0
    graded_count = 0  # Count of milestones that are graded (not in non_graded_milestones)
    sum_cost = 0.0
    sum_duration = 0  # Total duration in milliseconds
    sum_turns = 0  # Total turns
    sum_score = 0.0
    sum_score_v2 = 0.0
    sum_score_reliable = 0.0
    score_count = 0  # Count of milestones with valid scores
    agent_framework = None
    model = None

    for milestone in sorted_milestones:
        data = best_results[milestone]
        result = data["result"]
        cost = data.get("cost")
        duration = data.get("duration")
        turns = data.get("turns")
        notes[milestone] = get_failure_note(result, milestone)
        scores[milestone] = calculate_score(result)
        scores_v2[milestone] = calculate_score_v2(result)
        scores_reliable[milestone] = calculate_score_reliable(result)

        # Extract agent_framework and model from first available
        if agent_framework is None and data.get("agent_framework"):
            agent_framework = data.get("agent_framework")
        if model is None and data.get("model"):
            model = data.get("model")

        # Only count graded milestones for pass rate and score
        if milestone not in non_graded_milestones:
            graded_count += 1
            if is_resolved(result):
                resolved_count += 1
            # Sum scores for graded milestones only
            if scores[milestone] is not None:
                sum_score += scores[milestone]
            if scores_v2[milestone] is not None:
                sum_score_v2 += scores_v2[milestone]
                score_count += 1
            if scores_reliable[milestone] is not None:
                sum_score_reliable += scores_reliable[milestone]

        # Cost includes all milestones
        if cost is not None:
            sum_cost += cost

        # Duration includes all milestones
        if duration is not None:
            sum_duration += duration

        # Turns includes all milestones
        if turns is not None:
            sum_turns += turns

    # Use provided total_cost or sum from per-milestone costs
    display_cost = total_cost if total_cost is not None else sum_cost

    # Print trial info at top
    if workspace_root:
        # Extract repo range from workspace root path (last 2 components before workspace_root)
        repo_range = "/".join(workspace_root.parts[-2:]) if len(workspace_root.parts) >= 2 else str(workspace_root)
        print(f"📁 Repo: {repo_range}")
    if trial_names:
        trial_str = (
            ", ".join(trial_names)
            if len(trial_names) <= 3
            else f"{', '.join(trial_names[:3])}... ({len(trial_names)} trials)"
        )
        print(f"🏃 Trial: {trial_str}")
    if trial_type:
        print(f"📊 Type: {trial_type}")

    # Print agent info
    if agent_framework or model:
        agent_info = f"🤖 Agent: {agent_framework or 'unknown'} | Model: {model or 'unknown'}"
        print(agent_info)
    print()

    # Print summary at top (pass rate based on graded milestones only)
    non_graded_count = len(sorted_milestones) - graded_count
    pass_rate = resolved_count * 100 / graded_count if graded_count > 0 else 0.0

    # Use total_duration/total_turns if provided (e2e), otherwise use summed values (mstone)
    display_duration = total_duration if total_duration is not None else sum_duration
    display_turns = total_turns if total_turns is not None else sum_turns

    # Show duration and turns in summary (for both mstone and e2e when available)
    time_suffix = (
        f" | Duration: {format_duration(display_duration)}" if display_duration and display_duration > 0 else ""
    )
    turns_suffix = f" | Turns: {display_turns}" if display_turns and display_turns > 0 else ""
    avg_score = sum_score / graded_count if graded_count > 0 else 0.0
    avg_score_v2 = sum_score_v2 / graded_count if graded_count > 0 else 0.0
    avg_score_reliable = sum_score_reliable / graded_count if graded_count > 0 else 0.0

    if non_graded_count > 0:
        summary_text = f"Score-1000: {avg_score_v2 * 100:.2f}% | Score-full: {avg_score * 100:.2f}% | Score-reliable: {avg_score_reliable * 100:.2f}% | Resolve: {pass_rate:.2f}% ({resolved_count}/{graded_count}, excl. {non_graded_count} non-graded) | Cost: ${display_cost:.2f}{time_suffix}{turns_suffix}"
    else:
        summary_text = f"Score-1000: {avg_score_v2 * 100:.2f}% | Score-full: {avg_score * 100:.2f}% | Score-reliable: {avg_score_reliable * 100:.2f}% | Resolve: {pass_rate:.2f}% ({resolved_count}/{graded_count}) | Cost: ${display_cost:.2f}{time_suffix}{turns_suffix}"

    # Print summary with prominent border
    summary_width = display_width(summary_text) + 4
    print("┏" + "━" * summary_width + "┓")
    print("┃  " + summary_text + "  ┃")
    print("┗" + "━" * summary_width + "┛")
    print()

    # Calculate dynamic column widths
    milestone_width = max(len("Milestone"), max(display_width(m) for m in sorted_milestones)) + 2

    # Fixed widths for other columns
    f2p_width = 10
    n2p_width = 11
    p2p_width = 14
    status_width = 15
    score_1000_width = 12  # "Score-1000"
    score_full_width = 12  # "Score-full"
    score_reliable_width = 16  # "Score-reliable"
    cost_width = 10
    time_width = 10
    turns_width = 6
    # Dynamic note width based on content
    note_width = max(len("Note") + 2, max(display_width(n) for n in notes.values()) + 2)

    # Build table border strings
    def make_border(left: str, mid: str, right: str) -> str:
        parts = [
            f"{left}{'─' * (milestone_width + 2)}{mid}{'─' * (f2p_width + 2)}{mid}",
            f"{'─' * (n2p_width + 2)}{mid}{'─' * (p2p_width + 2)}{mid}",
            f"{'─' * (status_width + 2)}{mid}{'─' * (score_1000_width + 2)}{mid}{'─' * (score_full_width + 2)}{mid}{'─' * (score_reliable_width + 2)}{mid}",
        ]
        if show_cost_column:
            parts.append(f"{'─' * (cost_width + 2)}{mid}")
        if show_time_column:
            parts.append(f"{'─' * (time_width + 2)}{mid}{'─' * (turns_width + 2)}{mid}")
        parts.append(f"{'─' * (note_width + 2)}{right}")
        return "".join(parts)

    top_border = make_border("┌", "┬", "┐")
    mid_border = make_border("├", "┼", "┤")
    bot_border = make_border("└", "┴", "┘")

    # Print header
    print(top_border)
    header_milestone = pad_to_width("Milestone", milestone_width)
    header_f2p = pad_to_width("F2P", f2p_width)
    header_n2p = pad_to_width("N2P", n2p_width)
    header_p2p = pad_to_width("P2P", p2p_width)
    header_status = pad_to_width("Status", status_width)
    header_score_1000 = pad_to_width("Score-1000", score_1000_width)
    header_score_full = pad_to_width("Score-full", score_full_width)
    header_score_reliable = pad_to_width("Score-reliable", score_reliable_width)
    header_cost = pad_to_width("Cost", cost_width)
    header_time = pad_to_width("Time", time_width)
    header_turns = pad_to_width("Turns", turns_width)
    header_note = pad_to_width("Note", note_width)

    # Build header row based on which columns are shown
    header_parts = [
        f"│ {header_milestone} │ {header_f2p} │ {header_n2p} │ {header_p2p} │ {header_status} │ {header_score_1000} │ {header_score_full} │ {header_score_reliable} │"
    ]
    if show_cost_column:
        header_parts.append(f" {header_cost} │")
    if show_time_column:
        header_parts.append(f" {header_time} │ {header_turns} │")
    header_parts.append(f" {header_note} │")
    print("".join(header_parts))
    print(mid_border)

    for i, milestone in enumerate(sorted_milestones):
        data = best_results[milestone]
        result = data["result"]
        cost = data.get("cost")
        duration = data.get("duration")
        turns = data.get("turns")
        ts = result.get("test_summary", {})

        # Format columns
        f2p_a = ts.get("fail_to_pass_achieved", 0)
        f2p_r = ts.get("fail_to_pass_required", 0)
        n2p_a = ts.get("none_to_pass_achieved", 0)
        n2p_r = ts.get("none_to_pass_required", 0)

        f2p_str = format_ratio(f2p_a, f2p_r)
        n2p_str = format_ratio(n2p_a, n2p_r)
        p2p_str = format_p2p(result)
        # Use non-graded status if milestone is in non-graded list
        if milestone in non_graded_milestones:
            status = "🚫 Non-graded"
        else:
            status = get_status(result)
        note = notes[milestone]  # Use pre-calculated note
        score_full_str = format_score(scores[milestone])  # Use pre-calculated score (full)
        score_1000_str = format_score(scores_v2[milestone])  # Use pre-calculated score (1000)
        score_reliable_str = format_score(scores_reliable[milestone])  # Use pre-calculated score (reliable)
        cost_str = format_cost(cost)
        time_str = format_duration(duration)
        turns_str = str(turns) if turns is not None else "-"

        # Pad each column to correct display width
        milestone_col = pad_to_width(milestone, milestone_width)
        f2p_col = pad_to_width(f2p_str, f2p_width)
        n2p_col = pad_to_width(n2p_str, n2p_width)
        p2p_col = pad_to_width(p2p_str, p2p_width)
        status_col = pad_to_width(status, status_width)
        score_1000_col = pad_to_width(score_1000_str, score_1000_width)
        score_full_col = pad_to_width(score_full_str, score_full_width)
        score_reliable_col = pad_to_width(score_reliable_str, score_reliable_width)
        cost_col = pad_to_width(cost_str, cost_width)
        time_col = pad_to_width(time_str, time_width)
        turns_col = pad_to_width(turns_str, turns_width)
        note_col = pad_to_width(note, note_width)

        # Build data row based on which columns are shown
        row_parts = [
            f"│ {milestone_col} │ {f2p_col} │ {n2p_col} │ {p2p_col} │ {status_col} │ {score_1000_col} │ {score_full_col} │ {score_reliable_col} │"
        ]
        if show_cost_column:
            row_parts.append(f" {cost_col} │")
        if show_time_column:
            row_parts.append(f" {time_col} │ {turns_col} │")
        row_parts.append(f" {note_col} │")
        print("".join(row_parts))

        # Print separator or bottom border
        if i < len(sorted_milestones) - 1:
            print(mid_border)
        else:
            print(bot_border)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare mstone or e2e results across trials and print tables. "
            "Use --multi-repo to aggregate repos (see module docstring)."
        )
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Path to workspace root (e.g., DATA/harness_workspace/...)",
    )
    parser.add_argument(
        "--trials",
        nargs="+",
        help="List of trial names to compare (e.g., complete_run_001 complete_run_002)",
    )
    parser.add_argument(
        "--multi-repo",
        action="store_true",
        help="Aggregate results across multiple repos. "
             "By default reads analysis/extract/config.py; use --config to override.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config module (e.g., analysis/extract/config.py or analysis.extract.config). "
             "Must define DATA_ROOT, WORKSPACE_MAPPING, and E2E_TRIAL_NAMES.",
    )
    parser.add_argument(
        "--config-repos",
        nargs="*",
        default=None,
        help="Subset of repo keys from WORKSPACE_MAPPING to include (default: all). "
             "E.g., --config-repos dubbo ripgrep navidrome",
    )
    parser.add_argument(
        "--trial-type",
        choices=["mstone", "e2e"],
        default="mstone",
        help="Type of trials to compare: 'mstone' (default) or 'e2e'",
    )
    parser.add_argument(
        "--non-filter",
        action="store_true",
        help="Use unfiltered results (evaluation_result.json) instead of filtered results",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Show all milestones, not just those in selected_milestone_ids.txt",
    )
    parser.add_argument(
        "--sort-by-e2e",
        type=str,
        metavar="TRIAL",
        help="Sort milestones by e2e execution order from specified trial (e.g., _claude-code_sonnet-4.5-run_001_001)",
    )
    parser.add_argument(
        "--sort-order",
        type=str,
        help="Custom sort order: comma-separated milestone IDs (e.g., 'M06,M11,M01') or path to file with one ID per line",
    )
    parser.add_argument(
        "--merge-best",
        action="store_true",
        help="Merge multiple trials by taking the best score per milestone (default: show each trial separately)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Show compact summary table (fits 80 columns). Default for --multi-repo.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        default=False,
        help="Show full wide table with all columns (original format).",
    )
    parser.add_argument(
        "--detail",
        type=str,
        default=None,
        metavar="REPO",
        help="Show per-milestone detail for a specific repo (substring match).",
    )

    args = parser.parse_args()

    # Determine whether to prefer filtered results (default is True, --non-filter sets to False)
    prefer_filtered = not args.non_filter

    # === Multi-repo mode ===
    if args.multi_repo:
        if args.config:
            # Load user-specified config module
            import importlib.util

            config_path = Path(args.config)
            if config_path.suffix == ".py" and config_path.exists():
                # File path: analysis/extract/config.py
                spec = importlib.util.spec_from_file_location("_user_config", config_path)
                _cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(_cfg)
            else:
                # Module path: analysis.extract.config
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
                _cfg = importlib.import_module(args.config)
            DATA_ROOT = _cfg.DATA_ROOT
            WORKSPACE_MAPPING = _cfg.WORKSPACE_MAPPING
            E2E_TRIAL_NAMES = _cfg.E2E_TRIAL_NAMES
        else:
            # Default: analysis/extract/config.py
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            from analysis.extract.config import DATA_ROOT, WORKSPACE_MAPPING, E2E_TRIAL_NAMES

        repo_keys = args.config_repos if args.config_repos else list(WORKSPACE_MAPPING.keys())
        trial_list = args.trials if args.trials else E2E_TRIAL_NAMES
        trial_type = args.trial_type if args.trial_type != "mstone" else "e2e"

        # Validate repo keys
        valid_repo_keys = []
        repo_roots = {}
        for repo_key in repo_keys:
            if repo_key not in WORKSPACE_MAPPING:
                print(f"Warning: unknown repo key '{repo_key}', skipping", file=sys.stderr)
                continue
            repo_cfg = WORKSPACE_MAPPING[repo_key]
            ws_root = Path(DATA_ROOT) / repo_cfg["path"]
            if not ws_root.exists():
                print(f"Warning: workspace not found for {repo_key}: {ws_root}", file=sys.stderr)
                continue
            valid_repo_keys.append(repo_key)
            repo_roots[repo_key] = ws_root

        # Handle --detail mode
        if args.detail is not None:
            # Show trial/model/agent header
            trial_cfg = _load_trial_config(repo_roots, trial_list[0]) if repo_roots else {}
            if trial_cfg:
                effort = trial_cfg.get("effort") or "n/a"
                agent_str = f"\033[36;1m{trial_cfg.get('agent', '?')}\033[0m"
                model_str = f"\033[33;1m{trial_cfg.get('model', '?')}\033[0m"
                effort_str = f"\033[35meffort={effort}\033[0m"
                parts = [agent_str, model_str, effort_str]
                ctx = trial_cfg.get("context_window")
                if ctx:
                    ctx_label = "1M" if ctx >= 1_000_000 else f"{ctx // 1000}K"
                    parts.append(f"\033[35mcontext={ctx_label}\033[0m")
                print(f"\n\U0001f3c3 {trial_list[0]} | {' | '.join(parts)}")

            if args.detail == "":
                # No repo specified: show all repos that have evaluation results
                found_any = False
                for repo_key in valid_repo_keys:
                    # Skip repos with no trial directory
                    trial_dir = repo_roots[repo_key] / "e2e_trial" / trial_list[0]
                    if not trial_dir.exists():
                        continue
                    result = print_detail_table(
                        repo_roots[repo_key], trial_list, trial_type, prefer_filtered,
                        trial_label=trial_list[0], repo_roots=repo_roots,
                    )
                    if result:
                        found_any = True
                if not found_any:
                    print("No results found for any repo.")
            else:
                # Specific repo: substring match
                matched_roots = []
                for repo_key in valid_repo_keys:
                    if args.detail.lower() in repo_key.lower():
                        matched_roots.append(repo_roots[repo_key])
                if not matched_roots:
                    print(f"Error: no repo matching '{args.detail}' found.", file=sys.stderr)
                    sys.exit(1)
                for root in matched_roots:
                    print_detail_table(root, trial_list, trial_type, prefer_filtered,
                                       trial_label=trial_list[0], repo_roots=repo_roots)
            sys.exit(0)

        # Print one table per trial
        for ti, trial in enumerate(trial_list):
            if ti > 0:
                print()
            summaries = []
            for repo_key in valid_repo_keys:
                repo_cfg = WORKSPACE_MAPPING[repo_key]
                summary = compute_repo_summary(repo_roots[repo_key], [trial], trial_type, prefer_filtered)
                summary["repo"] = repo_cfg.get("display_name", repo_key)
                summaries.append(summary)

            if args.full:
                print_multi_repo_table(summaries, trial, repo_roots)
            else:
                # Default: compact mode
                print_compact_table(summaries, trial, repo_roots, trial)

        sys.exit(0)

    # === Single-repo mode: validate required args ===
    if not args.workspace_root:
        parser.error("--workspace-root is required unless --multi-repo is used")
    if not args.trials:
        parser.error("--trials is required unless --multi-repo is used")

    if not args.workspace_root.exists():
        print(f"Error: Workspace root does not exist: {args.workspace_root}", file=sys.stderr)
        sys.exit(1)

    # Load selected milestones early (needed for e2e to include unrun milestones)
    selected_milestones, milestones_source = load_selected_milestones(args.workspace_root)

    # Shared state computed once
    include_selected = selected_milestones if not args.show_all else None
    non_graded_milestones = load_non_graded_milestones(args.workspace_root)

    # Determine custom sort order (shared across all display modes)
    custom_sort_key = None
    custom_order = None
    sort_by_e2e_trial = args.sort_by_e2e
    if not args.sort_by_e2e and not args.sort_order:
        if args.trial_type == "e2e":
            sort_by_e2e_trial = args.trials[0]
        else:
            for trial in args.trials:
                e2e_trial_dir = args.workspace_root / "e2e_trial" / trial
                if e2e_trial_dir.exists():
                    sort_by_e2e_trial = trial
                    break
    if sort_by_e2e_trial:
        custom_order = load_e2e_execution_order(args.workspace_root, sort_by_e2e_trial)
        if custom_order:
            custom_sort_key = make_custom_sort_key(custom_order)
        else:
            print(f"Warning: Could not load execution order from e2e trial '{sort_by_e2e_trial}'", file=sys.stderr)
    elif args.sort_order:
        sort_order_path = Path(args.sort_order)
        if sort_order_path.exists():
            try:
                with open(sort_order_path) as f:
                    custom_order = [line.strip() for line in f if line.strip()]
            except Exception as e:
                print(f"Warning: Failed to load sort order from {args.sort_order}: {e}", file=sys.stderr)
        else:
            custom_order = [m.strip() for m in args.sort_order.split(",") if m.strip()]
        if custom_order:
            custom_sort_key = make_custom_sort_key(custom_order)

    # Helper: load results for a list of trials, filter, and print table
    def _load_filter_and_print(trial_list: list, print_header_info: bool = True):
        """Load results for given trials, apply filters, and print table."""
        if args.trial_type == "e2e":
            best_results, result_type_counts = compare_trials_e2e(
                args.workspace_root, trial_list, prefer_filtered, include_selected
            )
        else:
            best_results, result_type_counts = compare_trials(args.workspace_root, trial_list, prefer_filtered)

        if not best_results:
            print("No results found for any milestone.", file=sys.stderr)
            return

        # Filter by selected milestones
        if selected_milestones is not None and not args.show_all:
            best_results = {k: v for k, v in best_results.items() if k in selected_milestones}

        if not best_results:
            print("No results found for any milestone after filtering.", file=sys.stderr)
            return

        if print_header_info:
            # Print result source information
            print()
            if prefer_filtered:
                print("📋 Source: prefer evaluation_result_filtered.json (filtered)")
            else:
                print("📋 Source: using evaluation_result.json (unfiltered)")

            filtered_count = result_type_counts.get("filtered", 0)
            unfiltered_count = result_type_counts.get("unfiltered", 0)
            synthetic_count = result_type_counts.get("synthetic", 0)
            if filtered_count > 0 or unfiltered_count > 0:
                source_info = []
                if filtered_count > 0:
                    source_info.append(f"filtered: {filtered_count}")
                if unfiltered_count > 0:
                    source_info.append(f"unfiltered: {unfiltered_count}")
                if synthetic_count > 0:
                    source_info.append(f"synthetic: {synthetic_count}")
                print(f"   Load stats: {', '.join(source_info)}")

            non_graded_in_results = len(non_graded_milestones & best_results.keys())
            non_graded_suffix = f", {non_graded_in_results} non-graded" if non_graded_in_results > 0 else ""
            if selected_milestones is not None:
                selected_in_results = sum(1 for k in best_results.keys() if k in selected_milestones)
                if args.show_all:
                    total_milestones = len(best_results)
                    print(
                        f"📌 Scope: all {total_milestones} milestones, {selected_in_results} selected{non_graded_suffix}"
                    )
                else:
                    print(f"📌 Scope: {len(best_results)} selected milestones{non_graded_suffix}")
            else:
                print(f"📌 Scope: all {len(best_results)} milestones{non_graded_suffix}")

            if custom_order and sort_by_e2e_trial:
                print(f"📊 Sort: by e2e trial '{sort_by_e2e_trial}' execution order")
                print(f"   Execution order: {', '.join(custom_order)}")
            elif custom_order and args.sort_order:
                sort_order_path = Path(args.sort_order)
                if sort_order_path.exists():
                    print(f"📊 Sort: by order in file '{args.sort_order}'")
                else:
                    print(f"📊 Sort: by specified order")
                print(f"   Sort order: {', '.join(custom_order)}")

            print()

        # Print table
        if args.trial_type == "e2e":
            total_cost = 0.0
            total_duration = 0
            total_turns = 0
            for trial in trial_list:
                trial_cost = load_e2e_trial_cost(args.workspace_root, trial)
                if trial_cost is not None:
                    total_cost += trial_cost
                trial_duration = load_e2e_trial_duration(args.workspace_root, trial)
                if trial_duration is not None:
                    total_duration += trial_duration
                trial_turns = load_e2e_trial_turns(args.workspace_root, trial)
                if trial_turns is not None:
                    total_turns += trial_turns
            print_comparison_table(
                best_results,
                non_graded_milestones,
                show_cost_column=False,
                total_cost=total_cost,
                show_time_column=False,
                custom_sort_key=custom_sort_key,
                trial_names=trial_list,
                workspace_root=args.workspace_root,
                trial_type=args.trial_type,
                total_duration=total_duration if total_duration > 0 else None,
                total_turns=total_turns if total_turns > 0 else None,
            )
        else:
            print_comparison_table(
                best_results,
                non_graded_milestones,
                show_cost_column=True,
                show_time_column=True,
                custom_sort_key=custom_sort_key,
                trial_names=trial_list,
                workspace_root=args.workspace_root,
                trial_type=args.trial_type,
            )

    # Helper: compute summary stats for a single trial (used in comparison table)
    def _compute_trial_summary(trial: str) -> dict:
        """Compute summary metrics for a single trial."""
        if args.trial_type == "e2e":
            results, _ = compare_trials_e2e(args.workspace_root, [trial], prefer_filtered, include_selected)
        else:
            results, _ = compare_trials(args.workspace_root, [trial], prefer_filtered)

        if not results:
            return {"trial": trial, "error": True}

        # Filter by selected milestones
        if selected_milestones is not None and not args.show_all:
            results = {k: v for k, v in results.items() if k in selected_milestones}

        resolved_count = 0
        graded_count = 0
        sum_score_1000 = 0.0
        sum_score_full = 0.0
        sum_score_reliable = 0.0
        score_count = 0

        for milestone, data in results.items():
            result = data["result"]
            if milestone not in non_graded_milestones:
                graded_count += 1
                if is_resolved(result):
                    resolved_count += 1
                s1000 = calculate_score_v2(result)
                sfull = calculate_score(result)
                srel = calculate_score_reliable(result)
                if s1000 is not None:
                    sum_score_1000 += s1000
                    score_count += 1
                if sfull is not None:
                    sum_score_full += sfull
                if srel is not None:
                    sum_score_reliable += srel

        cost = load_e2e_trial_cost(args.workspace_root, trial) if args.trial_type == "e2e" else None
        duration = load_e2e_trial_duration(args.workspace_root, trial) if args.trial_type == "e2e" else None
        turns = load_e2e_trial_turns(args.workspace_root, trial) if args.trial_type == "e2e" else None
        total_ms, submitted_ms = (
            load_e2e_trial_submission_counts(args.workspace_root, trial) if args.trial_type == "e2e" else (0, 0)
        )

        return {
            "trial": trial,
            "error": False,
            "graded": graded_count,
            "resolved": resolved_count,
            "resolve_pct": resolved_count * 100 / graded_count if graded_count > 0 else 0.0,
            "score_1000": sum_score_1000 / graded_count * 100 if graded_count > 0 else 0.0,
            "score_full": sum_score_full / graded_count * 100 if graded_count > 0 else 0.0,
            "score_reliable": sum_score_reliable / graded_count * 100 if graded_count > 0 else 0.0,
            "cost": cost,
            "duration": duration,
            "turns": turns,
            "total_milestones": total_ms,
            "submitted": submitted_ms,
        }

    # === Main display logic ===
    if len(args.trials) > 1 and not args.merge_best:
        # Separate mode: show each trial independently
        print()
        print(f"📊 Mode: separate display ({len(args.trials)} trials)")

        # Print summary comparison table at the top
        summaries = [_compute_trial_summary(trial) for trial in args.trials]

        # Build comparison table
        # Pre-compute resolve and submitted strings to determine column widths
        resolve_strs = {}
        submitted_strs = {}
        for s in summaries:
            if not s["error"]:
                resolve_strs[s["trial"]] = f"{s['resolve_pct']:.2f}% ({s['resolved']}/{s['graded']})"
                total_ms = s.get("total_milestones", 0)
                sub_ms = s.get("submitted", 0)
                submitted_strs[s["trial"]] = f"{sub_ms}/{total_ms}" if total_ms > 0 else "-"
            else:
                resolve_strs[s["trial"]] = "(no data)"
                submitted_strs[s["trial"]] = "-"

        trial_col_w = max(len("Trial"), max(len(s["trial"]) for s in summaries)) + 2
        resolve_col_w = max(10, max(len(v) for v in resolve_strs.values()) + 1)
        submitted_col_w = max(9, max(len(v) for v in submitted_strs.values()) + 1)
        num_cols = [
            ("Submitted", submitted_col_w),
            ("*Resolve*", resolve_col_w),
            ("*Score-rel*", 12),
            ("Score-1000", 12),
            ("Score-full", 12),
            ("Cost", 10),
            ("Time", 12),
            ("Turns", 8),
        ]

        # Header
        header = f"│ {'Trial':<{trial_col_w}}"
        for col_name, col_w in num_cols:
            header += f"│ {col_name:>{col_w}} "
        header += "│"
        sep_top = "┌" + "─" * (trial_col_w + 1)
        sep_mid = "├" + "─" * (trial_col_w + 1)
        sep_bot = "└" + "─" * (trial_col_w + 1)
        for _, col_w in num_cols:
            sep_top += "┬" + "─" * (col_w + 2)
            sep_mid += "┼" + "─" * (col_w + 2)
            sep_bot += "┴" + "─" * (col_w + 2)
        sep_top += "┐"
        sep_mid += "┤"
        sep_bot += "┘"

        print()
        print("  (* = key metrics)")
        print(sep_top)
        print(header)
        print(sep_mid)
        for s in summaries:
            if s["error"]:
                row = f"│ {s['trial']:<{trial_col_w}}"
                row += f"│ {submitted_strs[s['trial']]:>{num_cols[0][1]}} "
                row += f"│ {resolve_strs[s['trial']]:>{num_cols[1][1]}} "
                for _, col_w in num_cols[2:]:
                    row += f"│ {'-':>{col_w}} "
                row += "│"
            else:
                cost_str = f"${s['cost']:.2f}" if s["cost"] is not None else "-"
                dur_str = format_duration(s["duration"]) if s["duration"] else "-"
                turns_str = str(s["turns"]) if s["turns"] else "-"
                srel = f"{s['score_reliable']:.2f}%"
                s1k = f"{s['score_1000']:.2f}%"
                sfull = f"{s['score_full']:.2f}%"
                row = f"│ {s['trial']:<{trial_col_w}}"
                row += f"│ {submitted_strs[s['trial']]:>{num_cols[0][1]}} "
                row += f"│ {resolve_strs[s['trial']]:>{num_cols[1][1]}} "
                row += f"│ {srel:>{num_cols[2][1]}} "
                row += f"│ {s1k:>{num_cols[3][1]}} "
                row += f"│ {sfull:>{num_cols[4][1]}} "
                row += f"│ {cost_str:>{num_cols[5][1]}} "
                row += f"│ {dur_str:>{num_cols[6][1]}} "
                row += f"│ {turns_str:>{num_cols[7][1]}} "
                row += "│"
            print(row)
        print(sep_bot)

        for i, trial in enumerate(args.trials, 1):
            print()
            print("━" * 55)
            print(f"  Trial {i}/{len(args.trials)}: {trial}")
            print("━" * 55)
            # Each trial uses its own execution order
            if args.trial_type == "e2e" and not args.sort_order:
                trial_order = load_e2e_execution_order(args.workspace_root, trial)
                if trial_order:
                    custom_sort_key = make_custom_sort_key(trial_order)
                    custom_order = trial_order
                    sort_by_e2e_trial = trial
                else:
                    custom_sort_key = None
                    custom_order = None
                    sort_by_e2e_trial = None
            _load_filter_and_print([trial])
            print()
    else:
        # Merged mode (--merge-best) or single trial
        if len(args.trials) > 1:
            print()
            print(f"📊 Mode: merge-best (best score per milestone, {len(args.trials)} trials)")
        _load_filter_and_print(args.trials)


if __name__ == "__main__":
    main()
