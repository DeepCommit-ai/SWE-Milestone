"""Abstract base class for agent log parsers."""

import json
import logging
import subprocess
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from harness.e2e.log_parser.models import MilestoneStats, NativeUsageUnit, SessionInfo, ToolCallRecord, TrialStats
from harness.e2e.log_parser.classify_behavior import classify_shell_command
from harness.e2e.log_parser.verification import classify_command

logger = logging.getLogger(__name__)


class AgentLogParser(ABC):
    """Abstract base class for parsing agent logs from various frameworks."""

    FRAMEWORK_NAME: str = "unknown"

    @abstractmethod
    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract detailed agent trace using agent-specific tools.

        This method calls agent-specific extraction tools (e.g., claude-extract)
        to produce detailed, human-readable trace files.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store trace files

        Returns:
            True if extraction was successful
        """

    @abstractmethod
    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract raw logs from container.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store extracted logs
            session_id: Optional session/thread ID to filter logs (Codex uses thread_id)

        Returns:
            Path to the extracted logs directory
        """

    @abstractmethod
    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from logs.

        Args:
            log_dir: Directory containing extracted logs

        Returns:
            List of tool call records, including subagent calls
        """

    @abstractmethod
    def parse_stdout_stats(self, stdout_file: Path) -> Dict:
        """Parse agent stdout file for accumulated statistics.

        Args:
            stdout_file: Path to agent_stdout.txt file

        Returns:
            Dictionary with total_cost_usd, total_turns, modelUsage, session_count
        """

    def parse_tool_results(
        self,
        log_dir: Path,
        tool_calls: List[ToolCallRecord],
    ) -> None:
        """Update tool calls with result information.

        Parses result records from logs and updates the corresponding
        tool call records with success status and output size.

        Modifies tool_calls in place.

        This is an optional method - agents that don't have separate
        result records can skip implementing this.

        Args:
            log_dir: Directory containing extracted logs
            tool_calls: List of tool call records to update
        """
        # Default implementation: no-op
        # Override in subclasses if the agent has separate result records
        pass

    def parse_native_usage_units(
        self,
        log_dir: Path,
        stdout_file: Path,
    ) -> List[NativeUsageUnit]:
        """Parse framework-native finest-grained usage units (message/turn).

        Default implementation returns empty list. Subclasses should override
        when native per-message/per-turn token usage is available.
        """
        return []

    def get_milestone_times(self, container_name: str, work_dir: str = "/testbed") -> Dict[str, Dict]:
        """Get milestone time boundaries from git tags.

        Args:
            container_name: Docker container name
            work_dir: Working directory in container

        Returns:
            Dictionary mapping milestone IDs to start/end times
        """
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "git",
                    "-C",
                    work_dir,
                    "for-each-ref",
                    "--sort=creatordate",
                    "--format=%(refname:short) %(creatordate:iso8601)",
                    "refs/tags/agent-impl-*",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to get git tags: {result.stderr}")
                return {}

            milestone_times = {}
            previous_end_time = None

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.split(" ", 1)
                if len(parts) != 2:
                    continue

                tag_name, timestamp_str = parts
                # Parse tag name: agent-impl-M002 -> M002
                if tag_name.startswith("agent-impl-"):
                    milestone_id = tag_name.replace("agent-impl-", "")
                else:
                    continue

                try:
                    # Git's %(creatordate:iso8601) uses "+0000" timezones,
                    # which datetime.fromisoformat() does not accept.
                    end_time = datetime.strptime(timestamp_str.strip(), "%Y-%m-%d %H:%M:%S %z")
                    # Remove timezone info for consistent comparison
                    if end_time.tzinfo is not None:
                        end_time = end_time.replace(tzinfo=None)
                except ValueError:
                    try:
                        # Keep the old parser as a fallback for ISO strings
                        # with colonized offsets, e.g. "+00:00".
                        end_time = datetime.fromisoformat(timestamp_str.strip())
                        if end_time.tzinfo is not None:
                            end_time = end_time.replace(tzinfo=None)
                    except ValueError as e:
                        logger.warning(f"Failed to parse timestamp '{timestamp_str}': {e}")
                        continue

                milestone_times[milestone_id] = {
                    "start_time": previous_end_time,  # Will be None for first milestone
                    "end_time": end_time,
                }
                previous_end_time = end_time

            return milestone_times

        except subprocess.TimeoutExpired:
            logger.warning("Timeout getting git tags")
            return {}
        except Exception as e:
            logger.warning(f"Error getting milestone times: {e}")
            return {}

    # Default gap threshold: 30 minutes. Gaps larger than this indicate a new session.
    SESSION_GAP_THRESHOLD_MS = 30 * 60 * 1000

    @staticmethod
    def load_session_times_from_history(session_history_path: Path) -> List[SessionInfo]:
        """Load precise session times from session_history.jsonl.

        Parses agent_exec_start/agent_exec_end event pairs to get exact
        session boundaries as recorded by agent_runner.py.

        Args:
            session_history_path: Path to session_history.jsonl

        Returns:
            List of SessionInfo with precise start/end times. Empty if file
            doesn't exist or has no exec events (old data).
        """
        import json as _json

        if not session_history_path.exists():
            return []

        try:
            events = []
            with open(session_history_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue

            # Pair agent_exec_start with agent_exec_end.
            # Handle ``extracted`` events: the harness discovers the real
            # session ID from the agent's stdout and replaces the placeholder.
            # We update pending_start's session_id so the placeholder never
            # appears in the final sessions list.
            sessions = []
            pending_start = None
            for ev in events:
                event_type = ev.get("event")
                if event_type == "agent_exec_start":
                    pending_start = ev
                elif event_type == "extracted" and pending_start:
                    # Replace placeholder session_id with the real one.
                    new_sid = ev.get("new_session_id")
                    if new_sid:
                        pending_start["session_id"] = new_sid
                elif event_type == "agent_exec_end" and pending_start:
                    start_ts = pending_start.get("ts")
                    end_ts = ev.get("ts")
                    if start_ts and end_ts:
                        start_time = datetime.fromisoformat(start_ts)
                        end_time = datetime.fromisoformat(end_ts)
                        # Remove timezone for consistent comparison
                        if start_time.tzinfo is not None:
                            start_time = start_time.replace(tzinfo=None)
                        if end_time.tzinfo is not None:
                            end_time = end_time.replace(tzinfo=None)
                        dur = max(0, int((end_time - start_time).total_seconds() * 1000))

                        sessions.append(
                            SessionInfo(
                                session_index=len(sessions),
                                start_time=start_time,
                                end_time=end_time,
                                duration_ms=dur,
                                tool_call_count=0,  # Will be enriched later if needed
                                session_id=pending_start.get("session_id"),
                            )
                        )
                    pending_start = None

            return sessions
        except Exception as e:
            logger.warning(f"Failed to load session history from {session_history_path}: {e}")
            return []

    @staticmethod
    def detect_sessions_from_tool_calls(
        tool_calls: List[ToolCallRecord],
        gap_threshold_ms: int = SESSION_GAP_THRESHOLD_MS,
    ) -> List[SessionInfo]:
        """Detect active sessions by finding gaps in tool call timestamps.

        Fallback method when session_history.jsonl doesn't have exec events (old data).
        Groups consecutive tool calls into sessions; a new session starts when the
        gap between consecutive tool calls exceeds gap_threshold_ms.

        Args:
            tool_calls: List of tool call records
            gap_threshold_ms: Minimum gap in ms to start a new session (default: 30 min)

        Returns:
            List of SessionInfo objects, ordered by time.
        """
        timed_calls = sorted(
            [tc for tc in tool_calls if tc.timestamp],
            key=lambda tc: tc.timestamp,
        )
        if not timed_calls:
            return []

        sessions: List[SessionInfo] = []
        session_start = timed_calls[0].timestamp
        session_count = 1
        prev_ts = timed_calls[0].timestamp

        for tc in timed_calls[1:]:
            gap_ms = int((tc.timestamp - prev_ts).total_seconds() * 1000)
            if gap_ms > gap_threshold_ms:
                # Finalize current session
                sessions.append(
                    SessionInfo(
                        session_index=len(sessions),
                        start_time=session_start,
                        end_time=prev_ts,
                        duration_ms=int((prev_ts - session_start).total_seconds() * 1000),
                        tool_call_count=session_count,
                    )
                )
                session_start = tc.timestamp
                session_count = 1
            else:
                session_count += 1
            prev_ts = tc.timestamp

        # Finalize last session
        sessions.append(
            SessionInfo(
                session_index=len(sessions),
                start_time=session_start,
                end_time=prev_ts,
                duration_ms=int((prev_ts - session_start).total_seconds() * 1000),
                tool_call_count=session_count,
            )
        )
        return sessions

    @staticmethod
    def _classify_behavior_detail(tool_calls: List[ToolCallRecord]) -> None:
        """Classify shell commands into behavior_detail categories."""
        for tc in tool_calls:
            if tc._bash_command:
                detail, _rule = classify_shell_command(tc._bash_command)
                tc.behavior_detail = detail

    @staticmethod
    def _build_verification_events(tool_calls: List[ToolCallRecord]) -> List[Dict[str, Any]]:
        """Classify Bash tool calls and build verification_events list.

        Args:
            tool_calls: List of tool call records with _bash_command populated

        Returns:
            List of verification event dicts (only non-"none" entries)
        """
        verification_events = []
        for tc in tool_calls:
            if tc._bash_command:
                vtype, rule = classify_command(tc._bash_command)
                if vtype != "none":
                    verification_events.append(
                        {
                            "tool_call_id": tc.id,
                            "command": tc._bash_command[:200],
                            "vtype": vtype,
                            "matched_rule": rule,
                        }
                    )
        return verification_events

    def compute_trial_stats(
        self,
        trial_name: str,
        model: str,
        tool_calls: List[ToolCallRecord],
        stdout_stats: Dict,
        milestone_times: Optional[Dict[str, Dict]] = None,
        reasoning_effort: Optional[str] = None,
        session_history_path: Optional[Path] = None,
        native_usage_units: Optional[List[NativeUsageUnit]] = None,
        trial_dir: Optional[Path] = None,
    ) -> TrialStats:
        """Compute complete trial statistics.

        Args:
            trial_name: Name of the trial
            model: Model identifier
            tool_calls: List of parsed tool calls
            stdout_stats: Statistics from agent stdout
            milestone_times: Optional milestone time boundaries
            reasoning_effort: Optional reasoning effort level (for Codex)
            session_history_path: Optional path to session_history.jsonl for precise session timing
            trial_dir: Optional trial directory for loading milestone mapping overrides

        Returns:
            Complete TrialStats object
        """
        milestone_times = milestone_times or {}
        native_usage_units = list(native_usage_units or [])

        # Assign milestones to tool calls based on timestamps
        if milestone_times:
            self._assign_milestones_to_tool_calls(tool_calls, milestone_times)
            self._assign_milestones_to_usage_units(native_usage_units, milestone_times)

        # Apply manual overrides (if present) AFTER timestamp-based assignment
        if trial_dir:
            overrides = self.load_milestone_overrides(trial_dir)
            if overrides:
                self.apply_milestone_overrides(overrides, tool_calls, native_usage_units)

        # Derive usage unit milestones from their associated tool calls.
        # This ensures usage units follow tool call overrides automatically.
        uu_proportional_shares: Dict[str, Dict[str, float]] = {}
        if native_usage_units and tool_calls:
            _, uu_proportional_shares = self._realign_usage_units_to_tool_calls(native_usage_units, tool_calls)

        self._normalize_native_usage_costs(
            native_usage_units=native_usage_units,
            total_cost=float(stdout_stats.get("total_cost_usd", 0.0) or 0.0),
        )

        # Backward-compatible fallback: if no native usage units are available,
        # distribute total usage to tool calls and aggregate from tool calls.
        if not native_usage_units:
            total_token_usage = self._extract_total_token_usage(stdout_stats.get("modelUsage", {}))
            self._distribute_usage_to_tool_calls(
                tool_calls=tool_calls,
                total_cost=float(stdout_stats.get("total_cost_usd", 0.0) or 0.0),
                total_token_usage=total_token_usage,
            )

        # Compute overall statistics
        total_subagent_calls = sum(1 for tc in tool_calls if tc.is_subagent)

        # Compute tool call breakdown
        tool_call_breakdown = defaultdict(int)
        for tc in tool_calls:
            tool_call_breakdown[tc.name] += 1

        # Determine start/end times
        start_time = None
        end_time = None
        if tool_calls:
            timestamps = [tc.timestamp for tc in tool_calls if tc.timestamp]
            if timestamps:
                start_time = min(timestamps)
                end_time = max(timestamps)

        # Override with milestone times if available
        if milestone_times:
            all_starts = [m["start_time"] for m in milestone_times.values() if m.get("start_time")]
            all_ends = [m["end_time"] for m in milestone_times.values() if m.get("end_time")]
            if all_starts:
                start_time = min(all_starts)
            if all_ends:
                end_time = max(all_ends)

        # Wall-clock duration (end - start, including idle gaps)
        wall_clock_ms = 0
        if start_time and end_time:
            wall_clock_ms = int((end_time - start_time).total_seconds() * 1000)

        # Detect sessions for active duration calculation
        # Priority: session_history.jsonl (precise) > tool call gap detection (fallback)
        sessions: List[SessionInfo] = []
        if session_history_path:
            sessions = self.load_session_times_from_history(session_history_path)
        if not sessions:
            sessions = self.detect_sessions_from_tool_calls(tool_calls)

        # Active duration = sum of session durations (excluding idle gaps)
        duration_ms = sum(s.duration_ms for s in sessions) if sessions else wall_clock_ms

        # Compute per-milestone statistics
        milestone_stats = self._compute_milestone_stats(
            milestone_times,
            tool_calls,
            stdout_stats,
            native_usage_units=native_usage_units,
            uu_proportional_shares=uu_proportional_shares,
        )

        # Get model usage and add reasoning_effort for models that support it
        model_usage = stdout_stats.get("modelUsage", {})
        if reasoning_effort:
            model_usage = self._add_reasoning_effort_to_model_usage(model_usage, reasoning_effort)

        # Classify behavior_detail for shell tool calls
        self._classify_behavior_detail(tool_calls)

        # Classify verification events from Bash tool calls (independent)
        verification_events = self._build_verification_events(tool_calls)

        # total_turns is summed from per-milestone counts, which are derived from
        # native usage units (one UU = one LLM API call). The stdout-derived
        # fallback was removed because it was unreliable: Claude Code's num_turns
        # counts user-initiated messages (not LLM calls) and is always 1 in
        # mstone trials. If there are milestones to attribute but no UUs, the
        # parser is in an unrecoverable state — fail loud rather than report
        # bogus numbers.
        if milestone_stats and not native_usage_units:
            raise RuntimeError(
                f"Cannot derive total_turns: {len(milestone_stats)} milestones "
                f"have stats but native_usage_units is empty. Most likely the "
                f"agent jsonl logs are missing or the per-agent log_parser "
                f"failed to extract per-message/turn usage."
            )
        # When milestone_stats is empty — e.g. the container was already stopped
        # at extraction time, so get_milestone_times() returned {} and no unit
        # could be attributed to a milestone window — fall back to the raw
        # parsed-unit count rather than reporting 0. total_turns equals
        # len(native_usage_units) for healthy trials anyway (every unit lands in
        # some milestone window). This guards the DeepSeek/Qwen failure mode:
        # "total_turns=0 but 743 units were parsed" because the container died
        # before stats extraction. (native_usage_units empty → 0, unchanged.)
        total_turns = (
            sum(ms.turns for ms in milestone_stats.values())
            if milestone_stats
            else len(native_usage_units)
        )

        # Derive session counts from session_history.jsonl (authoritative)
        # when available, since stdout-based counts can miss sessions whose
        # output was lost after process restart (e.g. codex extraction).
        if sessions and any(s.session_id for s in sessions):
            session_count = len(sessions)
            unique_session_count = len(set(s.session_id for s in sessions if s.session_id))
        else:
            session_count = stdout_stats.get("session_count", 0)
            unique_session_count = stdout_stats.get("unique_session_count", stdout_stats.get("session_count", 0))

        return TrialStats(
            trial_name=trial_name,
            agent_framework=self.FRAMEWORK_NAME,
            model=model,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            wall_clock_ms=wall_clock_ms,
            total_cost_usd=stdout_stats.get("total_cost_usd", 0.0),
            total_turns=total_turns,
            total_tool_calls=len(tool_calls),
            total_subagent_calls=total_subagent_calls,
            session_count=session_count,
            unique_session_count=unique_session_count,
            sessions=sessions,
            reasoning_effort=reasoning_effort,
            model_usage=model_usage,
            tool_call_breakdown=dict(tool_call_breakdown),
            milestone_stats=milestone_stats,
            native_usage_units=native_usage_units,
            all_tool_calls=tool_calls,
            verification_events=verification_events,
        )

    def _normalize_native_usage_costs(
        self,
        native_usage_units: List[NativeUsageUnit],
        total_cost: float,
    ) -> None:
        """Scale native usage unit costs so their sum matches summary total.

        Native per-message/turn usage gives the best milestone attribution, but
        framework-side cost reporting can still differ due to pricing/version
        differences. We keep attribution proportions from native units and
        normalize to the summary total for exact closure.
        """
        if not native_usage_units:
            return
        if not isinstance(total_cost, (int, float)) or total_cost <= 0:
            return

        current_total = sum(float(u.cost_usd or 0.0) for u in native_usage_units)
        if current_total <= 0:
            return

        scale = float(total_cost) / current_total
        allocated = 0.0
        for idx, unit in enumerate(native_usage_units):
            if idx == len(native_usage_units) - 1:
                unit.cost_usd = max(float(total_cost) - allocated, 0.0)
            else:
                scaled = max(float(unit.cost_usd or 0.0) * scale, 0.0)
                unit.cost_usd = scaled
                allocated += scaled

    def _assign_milestones_to_tool_calls(
        self,
        tool_calls: List[ToolCallRecord],
        milestone_times: Dict[str, Dict],
    ) -> None:
        """Assign milestone IDs to tool calls based on timestamps.

        Modifies tool calls in place.

        Args:
            tool_calls: List of tool call records
            milestone_times: Milestone time boundaries
        """
        # Sort milestones by end time
        sorted_milestones = sorted(
            [(mid, times) for mid, times in milestone_times.items() if times.get("end_time")],
            key=lambda x: x[1]["end_time"],
        )

        for tc in tool_calls:
            if not tc.timestamp:
                continue

            assigned = False
            latest_prev_mid: Optional[str] = None
            for mid, times in sorted_milestones:
                end_time = times["end_time"]
                start_time = times.get("start_time")

                if tc.timestamp > end_time:
                    latest_prev_mid = mid
                    continue

                # Tool call is in this milestone if:
                # - timestamp <= end_time AND
                # - (no start_time OR timestamp > start_time)
                if tc.timestamp <= end_time:
                    if start_time is None or tc.timestamp > start_time:
                        tc.milestone_id = mid
                        assigned = True
                        break

            # Fallback assignment: ensure post-last-tag (or otherwise unmatched)
            # tool calls still belong to the nearest milestone instead of staying
            # unassigned (which would drop cost/token from milestone aggregates).
            if not assigned and sorted_milestones:
                if latest_prev_mid is not None:
                    tc.milestone_id = latest_prev_mid
                else:
                    tc.milestone_id = sorted_milestones[0][0]

    def _assign_milestones_to_usage_units(
        self,
        usage_units: List[NativeUsageUnit],
        milestone_times: Dict[str, Dict],
    ) -> None:
        """Assign milestone IDs to native usage units based on timestamps."""
        if not usage_units or not milestone_times:
            return

        sorted_milestones = sorted(
            [(mid, times) for mid, times in milestone_times.items() if times.get("end_time")],
            key=lambda x: x[1]["end_time"],
        )
        if not sorted_milestones:
            return

        for unit in usage_units:
            if unit.milestone_id:
                continue
            if not unit.timestamp:
                continue

            assigned = False
            latest_prev_mid: Optional[str] = None
            for mid, times in sorted_milestones:
                end_time = times["end_time"]
                start_time = times.get("start_time")

                if unit.timestamp > end_time:
                    latest_prev_mid = mid
                    continue

                if start_time is None or unit.timestamp > start_time:
                    unit.milestone_id = mid
                    assigned = True
                    break

            if not assigned:
                unit.milestone_id = latest_prev_mid if latest_prev_mid is not None else sorted_milestones[0][0]

    # ------------------------------------------------------------------
    # Milestone mapping overrides
    # ------------------------------------------------------------------

    OVERRIDES_FILENAME = "milestone_mapping_overrides.json"

    @staticmethod
    def load_milestone_overrides(trial_dir: Path) -> Optional[Dict[str, Any]]:
        """Load milestone mapping overrides from trial directory.

        The overrides file contains manual corrections for tool-call-to-milestone
        mappings that the timestamp-based heuristic gets wrong (e.g., batch-tagging
        or parallel subagent execution).

        File format (milestone_mapping_overrides.json):
        {
            "tool_call_overrides": {
                "<tool_call_id>": "<correct_milestone_id>",
                ...
            },
            "usage_unit_overrides": {
                "<usage_unit_id>": "<correct_milestone_id>",
                ...
            }
        }

        Returns:
            Parsed overrides dict, or None if file doesn't exist.
        """
        path = trial_dir / AgentLogParser.OVERRIDES_FILENAME
        if not path.exists():
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"Loaded milestone overrides from {path}")
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load milestone overrides from {path}: {e}")
            return None

    @staticmethod
    def apply_milestone_overrides(
        overrides: Dict[str, Any],
        tool_calls: List[ToolCallRecord],
        native_usage_units: Optional[List[NativeUsageUnit]] = None,
    ) -> int:
        """Apply milestone mapping overrides to tool calls and usage units.

        Modifies objects in place. Should be called AFTER timestamp-based
        assignment and BEFORE milestone stats computation.

        Args:
            overrides: Parsed overrides dict with tool_call_overrides
                       and/or usage_unit_overrides.
            tool_calls: List of tool call records (modified in place).
            native_usage_units: Optional list of usage units (modified in place).

        Returns:
            Number of overrides applied.
        """
        applied = 0

        # Apply tool call overrides
        tc_overrides = overrides.get("tool_call_overrides", {})
        if tc_overrides:
            tc_index = {tc.id: tc for tc in tool_calls}
            for tc_id, new_mid in tc_overrides.items():
                tc = tc_index.get(tc_id)
                if tc is not None and tc.milestone_id != new_mid:
                    tc.milestone_id = new_mid
                    applied += 1

        # Apply usage unit overrides
        uu_overrides = overrides.get("usage_unit_overrides", {})
        if uu_overrides and native_usage_units:
            uu_index = {u.id: u for u in native_usage_units}
            for uu_id, new_mid in uu_overrides.items():
                u = uu_index.get(uu_id)
                if u is not None and u.milestone_id != new_mid:
                    u.milestone_id = new_mid
                    applied += 1

        if applied:
            logger.info(f"Applied {applied} milestone mapping overrides")
        return applied

    @staticmethod
    def _realign_usage_units_to_tool_calls(
        usage_units: List[NativeUsageUnit],
        tool_calls: List[ToolCallRecord],
    ) -> Tuple[int, Dict[str, Dict[str, float]]]:
        """Derive usage unit milestones from associated tool calls.

        Each usage unit (API message/turn) produces one or more tool calls.
        After tool call overrides are applied, usage units should follow
        their tool calls' milestone assignments rather than keeping stale
        timestamp-based assignments.

        Matching strategy:
        1. Exact timestamp match — tool calls from the same message share
           the usage unit's timestamp.
        2. Time-window fallback — for frameworks where timestamps differ
           slightly (e.g. Gemini), each usage unit "owns" tool calls from
           its timestamp up to the next usage unit's timestamp.

        The unit's milestone_id is set to the majority milestone (for
        display/serialization). For units spanning multiple milestones,
        proportional shares are returned so that cost/tokens can be split
        across milestones during stats computation.

        Args:
            usage_units: List of usage units (modified in place).
            tool_calls: List of tool calls (already overridden).

        Returns:
            Tuple of:
            - Number of usage units whose milestone_id was changed.
            - Proportional shares: {uu_id: {milestone_id: fraction}} for
              UUs whose tool calls span multiple milestones.
        """
        if not usage_units or not tool_calls:
            return 0, {}

        # Index tool calls by timestamp for exact matching
        tc_by_ts: Dict[str, List[ToolCallRecord]] = defaultdict(list)
        for tc in tool_calls:
            if tc.timestamp:
                key = tc.timestamp.isoformat()
                tc_by_ts[key].append(tc)

        # Sort usage units by timestamp for time-window fallback
        sorted_uu = sorted(
            [(i, u) for i, u in enumerate(usage_units) if u.timestamp],
            key=lambda x: x[1].timestamp,
        )

        changed = 0
        proportional_shares: Dict[str, Dict[str, float]] = {}

        for pos, (idx, unit) in enumerate(sorted_uu):
            ts_key = unit.timestamp.isoformat()

            # Strategy 1: exact timestamp match
            matched_tcs = tc_by_ts.get(ts_key, [])

            # Strategy 2: time-window fallback (this UU's ts to next UU's ts).
            # Also used when exact match captures very few tool calls — e.g.
            # Gemini gives each tool call its own timestamp, so exact match
            # may only hit the first TC while the window contains hundreds.
            next_ts = sorted_uu[pos + 1][1].timestamp if pos + 1 < len(sorted_uu) else None
            window_tcs = [
                tc
                for tc in tool_calls
                if tc.timestamp and tc.timestamp >= unit.timestamp and (next_ts is None or tc.timestamp < next_ts)
            ]
            if len(window_tcs) > len(matched_tcs):
                matched_tcs = window_tcs

            if not matched_tcs:
                continue

            # Count tool calls per milestone
            mid_counts: Dict[Optional[str], int] = defaultdict(int)
            for tc in matched_tcs:
                mid_counts[tc.milestone_id] += 1

            # Majority vote for milestone_id (display/serialization)
            best_mid = max(mid_counts, key=mid_counts.get)  # type: ignore[arg-type]
            if best_mid is not None and unit.milestone_id != best_mid:
                unit.milestone_id = best_mid
                changed += 1

            # Build proportional shares if UU spans multiple valid milestones
            valid_mids = {m: c for m, c in mid_counts.items() if m is not None}
            if len(valid_mids) > 1:
                total_tc = sum(valid_mids.values())
                proportional_shares[unit.id] = {mid: count / total_tc for mid, count in valid_mids.items()}

        if changed:
            logger.info(f"Realigned {changed} usage units to tool-call milestones")
        if proportional_shares:
            logger.info(f"Built proportional shares for {len(proportional_shares)} multi-milestone usage units")
        return changed, proportional_shares

    def _compute_milestone_stats(
        self,
        milestone_times: Dict[str, Dict],
        tool_calls: List[ToolCallRecord],
        stdout_stats: Dict,
        native_usage_units: Optional[List[NativeUsageUnit]] = None,
        uu_proportional_shares: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Dict[str, MilestoneStats]:
        """Compute per-milestone statistics.

        Args:
            milestone_times: Milestone time boundaries {mid: {start_time, end_time}}
            tool_calls: List of tool call records
            stdout_stats: Statistics from agent stdout
            native_usage_units: List of framework-native usage units
            uu_proportional_shares: For usage units spanning multiple milestones,
                {uu_id: {milestone_id: fraction}} so cost/tokens are split proportionally
                rather than winner-take-all.

        Returns:
            Dictionary mapping milestone IDs to MilestoneStats objects
        """
        if not milestone_times:
            return {}

        # Native usage units are the single source of truth for per-milestone
        # turn/cost/token attribution. The legacy fallback (proportional split
        # by tool-call count) was removed: Claude Code's stdout num_turns is
        # user-message count (not LLM calls), and codex's stdout count is
        # redundant with native UUs from the same rollout jsonl. If we got here
        # with milestones but no UUs, the agent log extraction failed — fail
        # loud rather than emit fabricated numbers.
        if not native_usage_units:
            raise RuntimeError(
                f"_compute_milestone_stats: {len(milestone_times)} milestones "
                f"to attribute but native_usage_units is empty. Per-agent "
                f"log_parser must populate native usage units (one per LLM API "
                f"call) — check that agent jsonl logs exist and are parseable."
            )

        milestone_stats = {}
        native_usage_units = list(native_usage_units)
        uu_proportional_shares = uu_proportional_shares or {}

        for mid, times in milestone_times.items():
            ms_tool_calls = [tc for tc in tool_calls if tc.milestone_id == mid]
            ms_breakdown = defaultdict(int)
            for tc in ms_tool_calls:
                ms_breakdown[tc.name] += 1

            # Use git tag times, fallback to tool call times if start_time is None
            ms_start_time = times.get("start_time")
            ms_end_time = times.get("end_time")

            # For first milestone, start_time is None - use earliest tool call time
            if ms_start_time is None and ms_tool_calls:
                ms_timestamps = [tc.timestamp for tc in ms_tool_calls if tc.timestamp]
                if ms_timestamps:
                    ms_start_time = min(ms_timestamps)

            # Wall-clock duration for this milestone
            ms_wall_clock = 0
            if ms_start_time and ms_end_time:
                ms_wall_clock = int((ms_end_time - ms_start_time).total_seconds() * 1000)

            # Active duration: detect sessions within this milestone's tool calls
            ms_sessions = self.detect_sessions_from_tool_calls(ms_tool_calls)
            ms_duration = sum(s.duration_ms for s in ms_sessions) if ms_sessions else ms_wall_clock

            # Aggregate cost/tokens from usage units, using proportional shares
            # for units that span multiple milestones.
            ms_cost = 0.0
            ms_turns_f = 0.0
            ms_token_usage: Dict[str, float] = defaultdict(float)

            for u in native_usage_units:
                shares = uu_proportional_shares.get(u.id)
                if shares is not None:
                    # This UU spans multiple milestones — use proportional share
                    share = shares.get(mid, 0.0)
                    if share <= 0:
                        continue
                    ms_cost += float(u.cost_usd or 0.0) * share
                    ms_turns_f += share
                    for key, val in (u.token_usage or {}).items():
                        if isinstance(val, (int, float)):
                            ms_token_usage[key] += val * share
                elif u.milestone_id == mid:
                    # Single-milestone UU — full attribution
                    ms_cost += float(u.cost_usd or 0.0)
                    ms_turns_f += 1.0
                    for key, val in (u.token_usage or {}).items():
                        if isinstance(val, (int, float)):
                            ms_token_usage[key] += val

            ms_turns = max(int(round(ms_turns_f)), 0)
            ms_token_usage_int: Dict[str, int] = {k: int(round(v)) for k, v in ms_token_usage.items()}

            milestone_stats[mid] = MilestoneStats(
                milestone_id=mid,
                start_time=ms_start_time,
                end_time=ms_end_time,
                duration_ms=ms_duration,
                wall_clock_ms=ms_wall_clock,
                turns=ms_turns,
                cost_usd=ms_cost,
                subagent_calls=sum(1 for tc in ms_tool_calls if tc.is_subagent),
                token_usage=dict(ms_token_usage_int),
                total_tool_calls=len(ms_tool_calls),
                tool_call_breakdown=dict(ms_breakdown),
            )

        return milestone_stats

    @staticmethod
    def _first_numeric_value(record: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
        for key in keys:
            value = record.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return default

    def _extract_total_token_usage(self, model_usage_data: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
        """Normalize and aggregate token usage totals across all models."""
        if not isinstance(model_usage_data, dict):
            return {}

        totals: Dict[str, int] = defaultdict(int)
        for usage in model_usage_data.values():
            if not isinstance(usage, dict):
                continue

            input_tokens = self._first_numeric_value(usage, ["inputTokens", "promptTokens", "input_tokens"])
            output_tokens = self._first_numeric_value(usage, ["outputTokens", "completionTokens", "output_tokens"])
            cache_read_tokens = self._first_numeric_value(
                usage,
                [
                    "cacheReadInputTokens",
                    "cacheReadTokens",
                    "cachedInputTokens",
                    "cachedTokens",
                    "cachedContentTokenCount",
                ],
            )
            cache_creation_tokens = self._first_numeric_value(
                usage, ["cacheCreationInputTokens", "cacheWriteTokens", "cacheCreationTokens"]
            )

            if input_tokens > 0:
                totals["inputTokens"] += int(round(input_tokens))
            if output_tokens > 0:
                totals["outputTokens"] += int(round(output_tokens))
            if cache_read_tokens > 0:
                totals["cacheReadInputTokens"] += int(round(cache_read_tokens))
            if cache_creation_tokens > 0:
                totals["cacheCreationInputTokens"] += int(round(cache_creation_tokens))

        return dict(totals)

    @staticmethod
    def _allocate_int_by_weights(total: int, weights: List[float]) -> List[int]:
        if total <= 0 or not weights:
            return [0] * len(weights)

        total_weight = sum(weights)
        if total_weight <= 0:
            base = total // len(weights)
            remainder = total - base * len(weights)
            alloc = [base] * len(weights)
            for i in range(remainder):
                alloc[i] += 1
            return alloc

        raw = [total * w / total_weight for w in weights]
        floored = [int(x) for x in raw]
        remainder = total - sum(floored)
        if remainder > 0:
            order = sorted(range(len(raw)), key=lambda i: raw[i] - floored[i], reverse=True)
            for i in order[:remainder]:
                floored[i] += 1
        return floored

    def _distribute_usage_to_tool_calls(
        self,
        tool_calls: List[ToolCallRecord],
        total_cost: float,
        total_token_usage: Dict[str, int],
    ) -> None:
        """Distribute total cost/tokens to individual tool calls.

        Allocation uses per-call I/O byte size as weight (fallback weight=1).
        """
        if not tool_calls:
            return

        weights: List[float] = []
        for tc in tool_calls:
            io_size = max(int(tc.input_size or 0), 0) + max(int(tc.output_size or 0), 0)
            weights.append(float(io_size if io_size > 0 else 1))

        total_weight = sum(weights)
        if total_weight <= 0:
            weights = [1.0] * len(tool_calls)
            total_weight = float(len(tool_calls))

        # Cost allocation (float)
        if total_cost > 0:
            raw_costs = [total_cost * w / total_weight for w in weights]
            allocated_sum = 0.0
            for i, tc in enumerate(tool_calls):
                if i == len(tool_calls) - 1:
                    tc.cost_usd = max(total_cost - allocated_sum, 0.0)
                else:
                    tc.cost_usd = raw_costs[i]
                    allocated_sum += tc.cost_usd
        else:
            for tc in tool_calls:
                tc.cost_usd = 0.0

        # Token allocation (integer, exact sum-preserving)
        per_key_allocations: Dict[str, List[int]] = {}
        for key, total in (total_token_usage or {}).items():
            if not isinstance(total, (int, float)):
                continue
            total_int = int(round(total))
            per_key_allocations[key] = self._allocate_int_by_weights(total_int, weights)

        for idx, tc in enumerate(tool_calls):
            usage: Dict[str, int] = {}
            for key, alloc in per_key_allocations.items():
                val = alloc[idx]
                if val > 0:
                    usage[key] = int(val)
            tc.token_usage = usage

    def _add_reasoning_effort_to_model_usage(
        self,
        model_usage: Dict[str, Dict],
        reasoning_effort: str,
    ) -> Dict[str, Dict]:
        """Add reasoning_effort to model usage for models that support it.

        Only adds reasoning_effort for models starting with "gpt-5".

        Args:
            model_usage: Original model usage dictionary
            reasoning_effort: Reasoning effort level

        Returns:
            Updated model usage dictionary
        """
        updated = {}
        for model_name, usage in model_usage.items():
            usage_copy = dict(usage)
            # Only add reasoning_effort for gpt-5* models
            if model_name.lower().startswith("gpt-5"):
                usage_copy["reasoning_effort"] = reasoning_effort
            updated[model_name] = usage_copy
        return updated


# Registry of available parsers
_PARSER_REGISTRY: Dict[str, Type[AgentLogParser]] = {}


def register_parser(framework: str):
    """Decorator to register a parser class.

    Args:
        framework: Framework name for registration

    Returns:
        Class decorator
    """

    def decorator(cls: Type[AgentLogParser]) -> Type[AgentLogParser]:
        _PARSER_REGISTRY[framework] = cls
        return cls

    return decorator


def get_parser(framework: str) -> AgentLogParser:
    """Factory function to get a parser for a specific framework.

    Args:
        framework: Framework name (e.g., "claude_code")

    Returns:
        Parser instance for the framework

    Raises:
        ValueError: If framework is not supported
    """
    # Import implementations to trigger registration
    from harness.e2e.log_parser import claude_code  # noqa: F401
    from harness.e2e.log_parser import codex  # noqa: F401
    from harness.e2e.log_parser import gemini  # noqa: F401  # registers as "gemini-cli"
    from harness.e2e.log_parser import openhands  # noqa: F401

    if framework not in _PARSER_REGISTRY:
        available = ", ".join(_PARSER_REGISTRY.keys()) or "none"
        raise ValueError(f"Unknown framework: {framework}. Available: {available}")

    return _PARSER_REGISTRY[framework]()
