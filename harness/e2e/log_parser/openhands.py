"""OpenHands CLI agent log parser implementation."""

import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.e2e.log_parser.base import AgentLogParser, register_parser
from harness.e2e.log_parser.models import NativeUsageUnit, ToolCallRecord, TrialStats
from harness.e2e.pricing import resolve_pricing as _resolve_pricing_shared

logger = logging.getLogger(__name__)


@register_parser("openhands")
class OpenHandsLogParser(AgentLogParser):
    """Parser for OpenHands CLI logs.

    OpenHands CLI outputs JSONL when run with --json flag.
    Detailed logs are stored in ~/.openhands/conversations/ directory.

    JSONL output format (expected):
    ```jsonl
    {"type": "action", "action": "run", "args": {"command": "ls -la"}, ...}
    {"type": "observation", "content": "...", ...}
    {"type": "action", "action": "write", "args": {"path": "...", "content": "..."}, ...}
    ```
    """

    FRAMEWORK_NAME = "openhands"

    # OpenHands home directory in container
    OPENHANDS_HOME = "/home/fakeroot/.openhands"
    CONVERSATIONS_DIR = f"{OPENHANDS_HOME}/conversations"

    # Pricing imported from harness.e2e.pricing (single source of truth).
    # resolve_pricing() handles litellm_proxy/ prefix stripping automatically.

    # Action types that map to tool calls
    ACTION_TYPES = {
        "run": "Bash",
        "run_ipython": "IPython",
        "write": "Write",
        "read": "Read",
        "browse": "Browser",
        "browse_interactive": "BrowserInteractive",
        "message": "Message",
        "finish": "Finish",
        "delegate": "Delegate",
        "think": "Think",
    }

    # Action kinds that indicate sub-agent/micro-agent delegation
    # See: https://docs.openhands.dev/sdk/guides/agent-delegation
    DELEGATE_ACTION_KINDS = {
        "AgentDelegateAction",  # Main delegation action type
        "DelegateAction",  # Alternative name
        "MicroAgentAction",  # Micro agent invocation
    }

    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract OpenHands execution trace.

        OpenHands doesn't have a dedicated trace extraction tool.
        We copy the raw logs and rely on JSONL output captured during execution.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store trace files

        Returns:
            True if successful (always returns True as we use stdout logs)
        """
        logger.info("OpenHands trace extraction: using stdout JSONL logs and conversation files")
        # OpenHands traces are captured via --json output during execution
        # Additional extraction from ~/.openhands/conversations/ for detailed logs
        return True

    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract OpenHands logs from container.

        Copies the ~/.openhands/conversations/ contents to output directory.
        Structure: ~/.openhands/conversations/<conversation_id>/

        Args:
            container_name: Docker container name
            output_dir: Directory to store extracted logs (typically log/)
            session_id: Optional session_id (conversation_id) to filter

        Returns:
            Path to extracted logs directory
        """
        # Store in {output_dir}/openhands/ (agent name as directory)
        logs_dir = output_dir / "openhands"
        logs_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Check if conversations directory exists
            check_result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "test",
                    "-d",
                    self.CONVERSATIONS_DIR,
                ],
                capture_output=True,
                timeout=10,
            )

            if check_result.returncode != 0:
                logger.warning(f"OpenHands conversations directory not found: {self.CONVERSATIONS_DIR}")
                return logs_dir

            # If session_id is provided, only copy that conversation
            if session_id:
                # OpenHands stores conversations without hyphens in the ID
                # Try both formats: with hyphens and without
                session_id_no_hyphen = session_id.replace("-", "")
                session_ids_to_try = [session_id, session_id_no_hyphen]

                found = False
                for sid in session_ids_to_try:
                    conversation_dir = f"{self.CONVERSATIONS_DIR}/{sid}"
                    check_session = subprocess.run(
                        ["docker", "exec", container_name, "test", "-d", conversation_dir],
                        capture_output=True,
                        timeout=10,
                    )

                    if check_session.returncode == 0:
                        local_session_dir = logs_dir / sid
                        local_session_dir.mkdir(parents=True, exist_ok=True)

                        cp_result = subprocess.run(
                            ["docker", "cp", f"{container_name}:{conversation_dir}/.", str(local_session_dir)],
                            capture_output=True,
                            timeout=120,
                        )
                        if cp_result.returncode == 0:
                            logger.info(f"Extracted OpenHands conversation {sid} to {local_session_dir}")
                            found = True
                            break
                        else:
                            logger.warning(f"Failed to copy conversation {sid}: {cp_result.stderr.decode()}")

                if not found:
                    logger.warning(f"Conversation {session_id} not found (tried with/without hyphens)")

            else:
                # Copy all conversations
                # First, list conversation directories
                find_result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container_name,
                        "find",
                        self.CONVERSATIONS_DIR,
                        "-mindepth",
                        "1",
                        "-maxdepth",
                        "1",
                        "-type",
                        "d",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if find_result.returncode == 0 and find_result.stdout.strip():
                    conversation_dirs = find_result.stdout.strip().split("\n")
                    copied_count = 0

                    for conv_dir in conversation_dirs:
                        if not conv_dir:
                            continue

                        conv_id = conv_dir.split("/")[-1]
                        local_conv_dir = logs_dir / conv_id
                        local_conv_dir.mkdir(parents=True, exist_ok=True)

                        cp_result = subprocess.run(
                            ["docker", "cp", f"{container_name}:{conv_dir}/.", str(local_conv_dir)],
                            capture_output=True,
                            timeout=120,
                        )
                        if cp_result.returncode == 0:
                            copied_count += 1
                            logger.debug(f"Copied conversation: {conv_id}")

                    logger.info(f"Extracted {copied_count} OpenHands conversations to {logs_dir}")
                else:
                    # Fallback: copy entire conversations directory
                    subprocess.run(
                        ["docker", "cp", f"{container_name}:{self.CONVERSATIONS_DIR}/.", str(logs_dir)],
                        capture_output=True,
                        timeout=120,
                    )
                    logger.info(f"Extracted OpenHands conversations directory to {logs_dir}")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout extracting OpenHands logs")
        except Exception as e:
            logger.warning(f"Error extracting OpenHands logs: {e}")

        return logs_dir

    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from OpenHands logs.

        Priority:
        1. Raw event files from conversations (event-*.json)
        2. Fallback to stdout JSON events

        Args:
            log_dir: Directory containing extracted logs (e.g., /tmp/test/openhands/)

        Returns:
            List of tool call records
        """
        all_calls = []

        # First, try to parse from raw event files
        # OpenHands stores events as: events/event-00000-<uuid>.json
        event_files = sorted(log_dir.rglob("event-*.json"))
        if event_files:
            for event_file in event_files:
                try:
                    event = json.loads(event_file.read_text(encoding="utf-8"))
                    extracted = self._extract_tool_call_from_event(event, len(all_calls))
                    if extracted:
                        all_calls.append(extracted)
                except Exception as e:
                    logger.debug(f"Error parsing event file {event_file}: {e}")

            if all_calls:
                logger.info(f"Parsed {len(all_calls)} tool calls from raw event files")

        # Fallback to stdout if no raw events found
        if not all_calls:
            stdout_file = log_dir.parent / "agent_stdout.txt"
            if stdout_file.exists():
                calls = self._parse_tool_calls_from_stdout(stdout_file)
                all_calls.extend(calls)
                logger.info(f"Parsed {len(calls)} tool calls from stdout (fallback)")

        # Sort by timestamp
        all_calls.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)

        logger.info(f"Total parsed tool calls: {len(all_calls)}")
        return all_calls

    def _parse_tool_calls_from_stdout(self, stdout_file: Path) -> List[ToolCallRecord]:
        """Parse tool calls from OpenHands stdout.

        OpenHands outputs formatted JSON events with --json flag, prefixed by
        `--JSON Event--` markers. The JSON is pretty-printed (multi-line).

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            List of ToolCallRecord objects
        """
        calls = []

        try:
            content = stdout_file.read_text(encoding="utf-8")
            if not content:
                return calls

            # Extract JSON events between --JSON Event-- markers
            json_events = self._extract_json_events(content)

            for event_index, event in enumerate(json_events):
                extracted = self._extract_tool_call_from_event(event, event_index)
                if extracted:
                    calls.append(extracted)

        except Exception as e:
            logger.warning(f"Error parsing tool calls from stdout: {e}")

        return calls

    def _extract_json_events(self, content: str) -> List[Dict[str, Any]]:
        """Extract JSON events from OpenHands formatted output.

        OpenHands outputs events in format:
        ```
        --JSON Event--
        [spinner output]
        {
          "kind": "ActionEvent",
          ...
        }
        ```

        The spinner output (ANSI codes, "Agent is working", etc.) is interleaved
        with the JSON output and needs to be cleaned.

        Args:
            content: Raw stdout content

        Returns:
            List of parsed JSON objects
        """
        import re

        events = []

        # First, clean up the entire content
        # Remove ANSI escape codes
        cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", content)
        # Remove spinner lines like "[2K⠸ Agent is working" or just "⠸ Agent is working"
        cleaned = re.sub(r"^\[2K.*$", "", cleaned, flags=re.MULTILINE)
        # Remove lines that are just spinner + "Agent is working"
        cleaned = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏].*Agent is working.*$", "", cleaned, flags=re.MULTILINE)
        # Remove standalone spinner characters at start of lines
        cleaned = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*", "", cleaned, flags=re.MULTILINE)

        # Split by the JSON Event marker
        parts = cleaned.split("--JSON Event--")

        for part in parts[1:]:  # Skip content before first marker
            part = part.strip()
            if not part:
                continue

            # Find JSON object in this part
            json_start = part.find("{")
            if json_start == -1:
                continue

            # Extract JSON by finding matching braces
            json_text = part[json_start:]

            # Fix malformed JSON: OpenHands outputs multi-line strings with literal
            # newlines that need to be escaped for valid JSON
            json_text = self._fix_json_newlines(json_text)

            # Use a JSON decoder to parse
            decoder = json.JSONDecoder()
            try:
                obj, end_idx = decoder.raw_decode(json_text)
                events.append(obj)
            except json.JSONDecodeError as e:
                # If parsing fails, try to extract just the JSON portion
                # by finding the next --JSON Event-- marker or end of text
                logger.debug(f"JSON parse error at position {e.pos}: {e.msg}")
                continue

        logger.debug(f"Extracted {len(events)} JSON events from stdout")
        return events

    def _fix_json_newlines(self, json_text: str) -> str:
        """Fix malformed JSON with unescaped newlines in string values.

        OpenHands CLI outputs JSON with literal newlines inside strings,
        which is invalid JSON. This method escapes those newlines.

        Args:
            json_text: Raw JSON text with potential unescaped newlines

        Returns:
            Fixed JSON text with properly escaped newlines
        """
        result = []
        in_string = False
        escape_next = False
        i = 0

        while i < len(json_text):
            char = json_text[i]

            if escape_next:
                result.append(char)
                escape_next = False
                i += 1
                continue

            if char == "\\":
                result.append(char)
                escape_next = True
                i += 1
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                result.append(char)
                i += 1
                continue

            if in_string and char == "\n":
                # Replace literal newline with escaped newline
                result.append("\\n")
                i += 1
                continue

            if in_string and char == "\t":
                # Replace literal tab with escaped tab
                result.append("\\t")
                i += 1
                continue

            result.append(char)
            i += 1

        return "".join(result)

    def _parse_tool_calls_from_event_file(self, event_file: Path) -> List[ToolCallRecord]:
        """Parse tool calls from OpenHands event file.

        Args:
            event_file: Path to events.json or JSONL file

        Returns:
            List of ToolCallRecord objects
        """
        calls = []

        try:
            content = event_file.read_text(encoding="utf-8").strip()
            if not content:
                return calls

            # Try parsing as single JSON array first
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    for idx, event in enumerate(data):
                        extracted = self._extract_tool_call_from_event(event, idx)
                        if extracted:
                            calls.append(extracted)
                    return calls
            except json.JSONDecodeError:
                pass

            # Parse as JSONL
            for line_num, line in enumerate(content.split("\n"), 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    extracted = self._extract_tool_call_from_event(event, line_num)
                    if extracted:
                        calls.append(extracted)
                except json.JSONDecodeError:
                    continue

        except Exception as e:
            logger.warning(f"Error parsing event file {event_file}: {e}")

        return calls

    def _extract_tool_call_from_event(
        self,
        event: Dict[str, Any],
        event_index: int,
    ) -> Optional[ToolCallRecord]:
        """Extract a tool call record from an OpenHands event.

        OpenHands CLI events have structure:
        {
            "kind": "ActionEvent",
            "action": {
                "kind": "TerminalAction" | "FileEditorAction" | etc.,
                "command": "...",
                ...
            },
            "tool_name": "terminal",
            "timestamp": "...",
            ...
        }

        Args:
            event: Parsed JSON event
            event_index: Index for ID generation

        Returns:
            ToolCallRecord or None if not an action event
        """
        event_kind = event.get("kind", "")

        # Only process ActionEvent events
        if event_kind != "ActionEvent":
            return None

        # Get action details
        action = event.get("action", {})
        action_kind = action.get("kind", "unknown")

        # Map action kind to tool name
        # OpenHands action kinds: TerminalAction, FileEditorAction, etc.
        action_kind_map = {
            "TerminalAction": "Bash",
            "FileEditorAction": "Edit",
            "BrowserAction": "Browser",
            "MessageAction": "Message",
            "FinishAction": "Finish",
            "ThinkAction": "Think",
            # Delegation/micro-agent actions
            "AgentDelegateAction": "Delegate",
            "DelegateAction": "Delegate",
            "MicroAgentAction": "MicroAgent",
        }
        tool_name = event.get("tool_name", action_kind_map.get(action_kind, action_kind))

        # Generate tool call ID
        tool_id = event.get("id", f"{action_kind}_{event_index}")

        # Calculate input size from action
        input_size = len(json.dumps(action, ensure_ascii=False).encode("utf-8"))

        # Parse timestamp
        timestamp = self._parse_timestamp(event.get("timestamp"))

        # Check for success/error status
        success = True
        if event.get("error") or event.get("exception"):
            success = False

        # Check if this is a subagent/micro-agent/delegate action
        is_subagent = action_kind in self.DELEGATE_ACTION_KINDS

        # Extract raw command for terminal actions (used by verification classifier)
        bash_command = None
        if action_kind == "TerminalAction" and isinstance(action, dict):
            bash_command = action.get("command")

        return ToolCallRecord(
            id=str(tool_id),
            name=tool_name,
            timestamp=timestamp or datetime.now(),
            success=success,
            input_size=input_size,
            output_size=0,  # Will be updated by parse_tool_results
            milestone_id=None,
            is_subagent=is_subagent,
            _bash_command=bash_command,
        )

    def _parse_timestamp(self, timestamp_val: Any) -> Optional[datetime]:
        """Parse timestamp from various formats.

        Args:
            timestamp_val: Timestamp value (string, int, float, or None)

        Returns:
            datetime or None
        """
        if timestamp_val is None:
            return None

        try:
            if isinstance(timestamp_val, (int, float)):
                return datetime.fromtimestamp(timestamp_val)
            else:
                # Handle ISO format
                ts_str = str(timestamp_val)
                # Remove timezone suffix
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return ts.replace(tzinfo=None)
        except (ValueError, OSError):
            return None

    def parse_tool_results(
        self,
        log_dir: Path,
        tool_calls: List[ToolCallRecord],
    ) -> None:
        """Update tool calls with result information from observation events.

        OpenHands observation events contain results for action events.
        ObservationEvent has `action_id` field that links to ActionEvent `id`.

        Modifies tool_calls in place.

        Args:
            log_dir: Directory containing extracted logs
            tool_calls: List of tool call records to update
        """
        # Build lookup by action ID
        calls_by_id = {tc.id: tc for tc in tool_calls}

        # Parse observation events from raw event files
        event_files = sorted(log_dir.rglob("event-*.json"))

        for event_file in event_files:
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
                event_kind = event.get("kind", "")

                # Only process ObservationEvent
                if event_kind != "ObservationEvent":
                    continue

                # Find matching action by action_id
                action_id = event.get("action_id", "")
                if action_id not in calls_by_id:
                    continue

                tc = calls_by_id[action_id]
                observation = event.get("observation", {})

                # Check success status
                is_error = observation.get("is_error", False)
                tc.success = not is_error

                # Calculate output size from observation content
                content = observation.get("content", [])
                output_text = ""

                if isinstance(content, list):
                    # Content is array of {type, text} objects
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            output_text += item.get("text", "")
                elif isinstance(content, str):
                    output_text = content

                if output_text:
                    tc.output_size = len(output_text.encode("utf-8"))

                logger.debug(f"Updated tool call {action_id[:8]}: output_size={tc.output_size}")

            except Exception as e:
                logger.debug(f"Error parsing observation from {event_file}: {e}")

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
    ) -> float:
        """Calculate cost based on token usage.

        Args:
            model: Model name
            input_tokens: Number of input tokens (excluding cache reads)
            output_tokens: Number of output tokens
            cache_read_tokens: Number of cached input tokens

        Returns:
            Estimated cost in USD
        """
        rates = _resolve_pricing_shared(model)
        input_cost = (input_tokens / 1_000_000) * rates.get("input", 0)
        output_cost = (output_tokens / 1_000_000) * rates.get("output", 0)
        cache_read_cost = (cache_read_tokens / 1_000_000) * rates.get(
            "cache_read", rates.get("input", 0) * 0.2
        )
        return input_cost + output_cost + cache_read_cost

    @staticmethod
    def _parse_stdout_token_summary(stdout_file: Path) -> Optional[Dict]:
        """Parse the 'Tokens: ...' summary line from OpenHands stdout.

        Format: Tokens: ↑ input 1.63M • cache hit 90.94% • reasoning 4.11K • ↓ output 12.14K

        Returns:
            Dict with input_tokens, output_tokens, cache_hit_pct, or None if not found.
        """
        import re

        def _parse_amount(s: str) -> int:
            """Parse '1.63M', '12.14K', '378.36K' etc. to integer."""
            s = s.strip().rstrip(" •")
            m = re.match(r"([\d.]+)\s*([KMB]?)", s, re.IGNORECASE)
            if not m:
                return 0
            val = float(m.group(1))
            suffix = m.group(2).upper()
            if suffix == "K":
                return int(val * 1_000)
            elif suffix == "M":
                return int(val * 1_000_000)
            elif suffix == "B":
                return int(val * 1_000_000_000)
            return int(val)

        try:
            text = stdout_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        # Find the last "Tokens:" line
        last_tokens_line = None
        for line in text.splitlines():
            if line.startswith("Tokens:"):
                last_tokens_line = line

        if not last_tokens_line:
            return None

        result = {"input_tokens": 0, "output_tokens": 0, "cache_hit_pct": 0.0}

        # Parse input
        m = re.search(r"input\s+([\d.]+\s*[KMB]?)", last_tokens_line, re.IGNORECASE)
        if m:
            result["input_tokens"] = _parse_amount(m.group(1))

        # Parse output
        m = re.search(r"output\s+([\d.]+\s*[KMB]?)", last_tokens_line, re.IGNORECASE)
        if m:
            result["output_tokens"] = _parse_amount(m.group(1))

        # Parse cache hit percentage
        m = re.search(r"cache hit\s+([\d.]+)%", last_tokens_line)
        if m:
            result["cache_hit_pct"] = float(m.group(1))

        return result if result["input_tokens"] > 0 else None

    def parse_stdout_stats(self, stdout_file: Path, log_dir: Optional[Path] = None) -> Dict:
        """Parse statistics from raw logs or stdout.

        Priority:
        1. Raw event files from log_dir (if provided)
        2. Fallback to stdout JSON events

        Args:
            stdout_file: Path to agent_stdout.txt
            log_dir: Optional path to extracted logs directory (e.g., /tmp/test/openhands/)

        Returns:
            Dictionary with accumulated statistics including:
            - total_cost_usd, total_turns, modelUsage, session_count
            - duration_ms: total execution time (if available)
            - tool_calls: list of tool call info
            - tool_call_breakdown: {tool_name: count}
        """
        # Try to parse from raw logs first
        if log_dir and log_dir.exists():
            stats = self._parse_stats_from_raw_logs(log_dir)
            if stats and stats.get("total_turns", 0) > 0:
                logger.info(f"Parsed stats from raw logs: {stats.get('total_turns')} turns")
                return stats

        # Fallback to stdout parsing
        return self._parse_stats_from_stdout(stdout_file)

    def parse_native_usage_units(
        self,
        log_dir: Path,
        stdout_file: Path,
    ) -> List[NativeUsageUnit]:
        """Parse native turn-level usage units from OpenHands metrics events."""
        units: List[NativeUsageUnit] = []
        event_files = sorted(log_dir.rglob("event-*.json"))
        seen_ids = set()

        for idx, event_file in enumerate(event_files):
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            event_kind = event.get("kind", "")
            event_type = event.get("type", "")
            if not (event_kind == "MetricsEvent" or event_type == "metrics" or "usage" in event):
                continue

            usage = event.get("usage", event)
            if not isinstance(usage, dict):
                continue

            model = str(usage.get("model", "unknown"))
            input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
            output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
            cache_read_tokens = int(usage.get("cache_read_tokens", 0) or 0)
            if input_tokens <= 0 and output_tokens <= 0 and cache_read_tokens <= 0:
                continue

            unit_id = str(event.get("id") or event_file.name or f"event-{idx}")
            if unit_id in seen_ids:
                continue
            seen_ids.add(unit_id)

            timestamp = self._parse_timestamp(event.get("timestamp"))
            explicit_cost = event.get("cost")
            if isinstance(explicit_cost, (int, float)):
                cost = float(explicit_cost)
            else:
                cost = self._calculate_cost(model, input_tokens, output_tokens)

            units.append(
                NativeUsageUnit(
                    id=unit_id,
                    source_type="turn",
                    timestamp=timestamp,
                    model=model,
                    token_usage={
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cacheReadInputTokens": cache_read_tokens,
                    },
                    cost_usd=cost,
                )
            )

        logger.info(f"Parsed {len(units)} native usage units from OpenHands logs")
        return units

    def _parse_stats_from_raw_logs(self, log_dir: Path) -> Dict:
        """Parse statistics from raw event files and base_state.json.

        Args:
            log_dir: Path to extracted logs directory

        Returns:
            Dictionary with statistics
        """
        total_cost = 0.0
        total_turns = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 1  # We're parsing a single session's logs
        tool_calls: List[Dict[str, Any]] = []
        tool_call_breakdown: Dict[str, int] = defaultdict(int)
        first_timestamp = None
        last_timestamp = None

        # Find all event files
        event_files = sorted(log_dir.rglob("event-*.json"))
        if not event_files:
            return {}

        for event_file in event_files:
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
                event_kind = event.get("kind", "")

                # Track min/max — events are walked in lex path order, which is not
                # chronological across resumed sessions.
                timestamp = self._parse_timestamp(event.get("timestamp"))
                if timestamp:
                    if first_timestamp is None or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp

                # Count ActionEvent for tool call breakdown (not turns - turns = LLM API calls)
                if event_kind == "ActionEvent":
                    action = event.get("action", {})
                    action_kind = action.get("kind", "") if isinstance(action, dict) else ""

                    action_kind_map = {
                        "TerminalAction": "terminal",
                        "FileEditorAction": "file_editor",
                        "BrowserAction": "browser",
                        "MessageAction": "message",
                        "FinishAction": "finish",
                        "ThinkAction": "think",
                    }
                    tool_name = event.get("tool_name", action_kind_map.get(action_kind, action_kind))

                    tool_call_breakdown[tool_name] += 1
                    tool_calls.append(
                        {
                            "name": tool_name,
                            "action": action_kind,
                            "id": event.get("id", ""),
                            "success": True,
                        }
                    )

            except Exception as e:
                logger.debug(f"Error parsing event file {event_file}: {e}")

        # Parse cost and token usage from base_state.json
        # We independently recalculate cost from token_usages[] (per-call records)
        # instead of trusting accumulated_cost, because OpenHands SDK has a bug
        # where accumulated_cost stops updating after session resume.
        base_state_files = list(log_dir.rglob("base_state.json"))
        for base_state_file in base_state_files:
            try:
                base_state = json.loads(base_state_file.read_text(encoding="utf-8"))
                stats = base_state.get("stats", {})
                usage_to_metrics = stats.get("usage_to_metrics", {})

                for usage_id, metrics in usage_to_metrics.items():
                    model_name_raw = metrics.get("model_name", "unknown")
                    # Strip litellm_proxy/ prefix for display
                    model_name = model_name_raw
                    if model_name.startswith("litellm_proxy/"):
                        model_name = model_name[len("litellm_proxy/") :]

                    # Independently recalculate from token_usages[] (per-call records)
                    # This is more reliable than accumulated_cost which breaks on resume
                    token_usages = metrics.get("token_usages", [])
                    recalc_input = 0
                    recalc_output = 0
                    recalc_cache_read = 0
                    recalc_reasoning = 0
                    recalc_calls = len(token_usages)

                    for tu in token_usages:
                        recalc_input += tu.get("prompt_tokens", 0)
                        recalc_output += tu.get("completion_tokens", 0)
                        recalc_cache_read += tu.get("cache_read_tokens", 0)
                        recalc_reasoning += tu.get("reasoning_tokens", 0)

                    # Also check accumulated_token_usage as fallback
                    # (in case token_usages[] is also incomplete)
                    acc_usage = metrics.get("accumulated_token_usage", {})
                    acc_input = acc_usage.get("prompt_tokens", 0)
                    acc_output = acc_usage.get("completion_tokens", 0)
                    acc_cache = acc_usage.get("cache_read_tokens", 0)
                    acc_reasoning = acc_usage.get("reasoning_tokens", 0)

                    # Use whichever source has more data
                    if recalc_input + recalc_output >= acc_input + acc_output:
                        final_input = recalc_input
                        final_output = recalc_output
                        final_cache = recalc_cache_read
                        final_reasoning = recalc_reasoning
                        final_calls = recalc_calls
                    else:
                        final_input = acc_input
                        final_output = acc_output
                        final_cache = acc_cache
                        final_reasoning = acc_reasoning
                        # Fall back to costs[] length for call count
                        final_calls = max(recalc_calls, len(metrics.get("costs", [])))

                    total_turns += max(final_calls, len(metrics.get("costs", [])))

                    # Recalculate cost using our own pricing
                    # Input tokens for pricing = total prompt - cache_read (cache is priced separately)
                    non_cached_input = max(0, final_input - final_cache)
                    recalc_cost = self._calculate_cost(
                        model_name_raw, non_cached_input, final_output, final_cache
                    )

                    total_cost += recalc_cost

                    model_usage[model_name]["inputTokens"] += final_input
                    model_usage[model_name]["outputTokens"] += final_output
                    model_usage[model_name]["cacheReadTokens"] += final_cache
                    model_usage[model_name]["reasoningTokens"] += final_reasoning
                    model_usage[model_name]["costUSD"] += recalc_cost
                    model_usage[model_name]["apiRequests"] += final_calls

                    sdk_cost = metrics.get("accumulated_cost", 0.0)
                    if abs(recalc_cost - sdk_cost) > 0.01:
                        logger.info(
                            f"Cost recalculated for {model_name}: "
                            f"SDK=${sdk_cost:.2f} → recalc=${recalc_cost:.2f} "
                            f"(input={final_input}, output={final_output}, "
                            f"cache={final_cache}, calls={final_calls})"
                        )

                logger.info(f"Parsed base_state.json: recalculated cost=${total_cost:.4f}")

            except Exception as e:
                logger.debug(f"Error parsing base_state.json: {e}")

        # Fallback: count actual LLM calls from event files (llm_response_id)
        # and parse token totals from stdout. This handles cases where
        # base_state.json's cost/token tracking broke after session resume.
        def _has_llm_response(ef):
            try:
                return bool(json.loads(ef.read_text(encoding="utf-8")).get("llm_response_id"))
            except Exception:
                return False
        actual_llm_calls = sum(1 for ef in event_files if _has_llm_response(ef)) if event_files else 0

        if actual_llm_calls > total_turns * 2:
            # base_state.json significantly undercounts LLM calls.
            # This happens when OpenHands SDK's cost tracking breaks after session resume.
            # Use event file count for turns and estimate cost from per-turn averages.
            logger.warning(
                f"Cost tracking gap detected: base_state recorded {total_turns} calls, "
                f"but event files show {actual_llm_calls} actual LLM calls. "
                f"OpenHands SDK bug: cost tracking stops after session resume."
            )
            base_state_turns = total_turns
            total_turns = actual_llm_calls

            # Estimate tokens using per-turn averages from base_state data
            # (the base_state tokens are accurate for the calls it DID track)
            model_for_pricing = next(iter(model_usage.keys()), "glm-5")
            mu = model_usage.get(model_for_pricing, {})
            tracked_input = mu.get("inputTokens", 0)
            tracked_output = mu.get("outputTokens", 0)
            tracked_cache = mu.get("cacheReadTokens", 0)
            tracked_calls = mu.get("apiRequests", 0) or base_state_turns

            if tracked_calls > 0 and tracked_input > 0:
                # Scale up proportionally
                scale = actual_llm_calls / tracked_calls
                est_input = int(tracked_input * scale)
                est_output = int(tracked_output * scale)
                est_cache = int(tracked_cache * scale)
                est_non_cached = max(0, est_input - est_cache)

                recalc_cost = self._calculate_cost(
                    model_for_pricing, est_non_cached, est_output, est_cache
                )
                total_cost = recalc_cost

                model_usage[model_for_pricing]["inputTokens"] = est_input
                model_usage[model_for_pricing]["outputTokens"] = est_output
                model_usage[model_for_pricing]["cacheReadTokens"] = est_cache
                model_usage[model_for_pricing]["costUSD"] = recalc_cost
                model_usage[model_for_pricing]["apiRequests"] = actual_llm_calls

                logger.info(
                    f"Estimated cost for {model_for_pricing}: ${recalc_cost:.2f} "
                    f"(scaled {tracked_calls}→{actual_llm_calls} calls, "
                    f"input={est_input:,} output={est_output:,} cache={est_cache:,})"
                )

        # Calculate duration
        total_duration_ms = 0
        if first_timestamp and last_timestamp:
            total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

        logger.info(
            f"Parsed raw logs: {session_count} session, {total_turns} turns, "
            f"${total_cost:.4f}, {len(tool_calls)} tool calls, {total_duration_ms}ms"
        )

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": {k: dict(v) for k, v in model_usage.items()},
            "session_count": session_count,
            "unique_session_count": session_count,
            "duration_ms": total_duration_ms,
            "tool_calls": tool_calls,
            "tool_call_breakdown": dict(tool_call_breakdown),
        }

    def _parse_stats_from_stdout(self, stdout_file: Path) -> Dict:
        """Parse statistics from stdout (fallback method).

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            Dictionary with statistics
        """
        total_cost = 0.0
        total_turns = 0
        total_duration_ms = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 0
        tool_calls: List[Dict[str, Any]] = []
        tool_call_breakdown: Dict[str, int] = defaultdict(int)

        if not stdout_file.exists():
            logger.warning(f"stdout file not found: {stdout_file}")
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
                "unique_session_count": 0,
                "duration_ms": 0,
                "tool_calls": [],
                "tool_call_breakdown": {},
            }

        content = stdout_file.read_text(encoding="utf-8")
        if not content.strip():
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
                "unique_session_count": 0,
                "duration_ms": 0,
                "tool_calls": [],
                "tool_call_breakdown": {},
            }

        # Track session
        seen_conversation_ids = set()
        first_timestamp = None
        last_timestamp = None

        # Extract JSON events from formatted output
        json_events = self._extract_json_events(content)

        for event in json_events:

            # OpenHands events use "kind" field (e.g., "ActionEvent", "ObservationEvent")
            event_kind = event.get("kind", "")
            # Also support "type" field for alternative formats
            event_type = event.get("type", "")

            # Track conversation/session
            conv_id = event.get("conversation_id") or event.get("session_id")
            if conv_id and conv_id not in seen_conversation_ids:
                seen_conversation_ids.add(conv_id)
                session_count += 1

            # Track timestamps for duration calculation
            timestamp = self._parse_timestamp(event.get("timestamp"))
            if timestamp:
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp

            # Count ActionEvent for tool call breakdown (not turns - turns = LLM API calls)
            # OpenHands CLI uses "kind": "ActionEvent" with nested "action" object
            if event_kind == "ActionEvent" or event_type == "action":
                # Get action details from nested "action" object or directly
                action = event.get("action", {})
                action_kind = action.get("kind", "") if isinstance(action, dict) else event.get("action", "")

                # Map action kind to tool name
                action_kind_map = {
                    "TerminalAction": "Bash",
                    "FileEditorAction": "Edit",
                    "BrowserAction": "Browser",
                    "MessageAction": "Message",
                    "FinishAction": "Finish",
                    "ThinkAction": "Think",
                }
                # Use tool_name from event if available, otherwise map from action_kind
                tool_name = event.get("tool_name", action_kind_map.get(action_kind, action_kind))
                if not tool_name:
                    tool_name = self.ACTION_TYPES.get(action_kind, action_kind)

                tool_call_breakdown[tool_name] += 1
                tool_calls.append(
                    {
                        "name": tool_name,
                        "action": action_kind,
                        "success": True,  # Updated later if observation shows error
                    }
                )

            # Extract token usage from metrics events (if available)
            # Each metrics event represents one LLM API call
            if event_kind == "MetricsEvent" or event_type == "metrics" or "usage" in event:
                usage = event.get("usage", event)
                model = usage.get("model", "unknown")

                input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
                output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))

                if input_tokens or output_tokens:
                    model_usage[model]["inputTokens"] += input_tokens
                    model_usage[model]["outputTokens"] += output_tokens
                    model_usage[model]["apiRequests"] += 1
                    total_turns += 1  # Each metrics event = one LLM API call

                    # Calculate cost
                    session_cost = self._calculate_cost(model, input_tokens, output_tokens)
                    model_usage[model]["costUSD"] = model_usage[model].get("costUSD", 0.0) + session_cost
                    total_cost += session_cost

            # Extract cost directly if provided
            if "cost" in event:
                cost = event.get("cost", 0)
                if isinstance(cost, (int, float)):
                    total_cost += cost

        # Calculate duration from timestamps
        if first_timestamp and last_timestamp:
            total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

        # Ensure at least 1 session if we have any data
        if session_count == 0 and (total_turns > 0 or tool_calls):
            session_count = 1

        # Convert defaultdicts to regular dicts
        model_usage_dict = {model: dict(usage) for model, usage in model_usage.items()}

        logger.info(
            f"Parsed stdout: {session_count} sessions, {total_turns} turns, "
            f"${total_cost:.4f}, {len(tool_calls)} tool calls"
        )

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": model_usage_dict,
            "session_count": session_count,
            "unique_session_count": len(seen_conversation_ids) if seen_conversation_ids else session_count,
            "duration_ms": total_duration_ms,
            "tool_calls": tool_calls,
            "tool_call_breakdown": dict(tool_call_breakdown),
        }

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
        """Compute complete trial statistics for OpenHands.

        Args:
            trial_name: Name of the trial
            model: Model identifier
            tool_calls: List of parsed tool calls
            stdout_stats: Statistics from agent stdout (includes duration_ms)
            milestone_times: Optional milestone time boundaries
            reasoning_effort: Optional reasoning effort level

        Returns:
            Complete TrialStats object
        """
        # Use duration from stdout_stats
        duration_ms = stdout_stats.get("duration_ms", 0)

        # Calculate start/end time based on duration
        end_time = datetime.now()
        start_time = end_time - timedelta(milliseconds=duration_ms) if duration_ms > 0 else end_time

        # Compute tool call breakdown
        tool_call_breakdown = stdout_stats.get("tool_call_breakdown", {})
        if not tool_call_breakdown:
            tool_call_breakdown = {}
            for tc in tool_calls:
                tool_call_breakdown[tc.name] = tool_call_breakdown.get(tc.name, 0) + 1

        # Count subagent calls
        total_subagent_calls = sum(1 for tc in tool_calls if tc.is_subagent)

        # Strip litellm_proxy/ prefix from model name for display
        display_model = model
        if display_model.startswith("litellm_proxy/"):
            display_model = display_model[len("litellm_proxy/") :]

        # Get model usage and add reasoning_effort only for GPT models
        model_usage = stdout_stats.get("modelUsage", {})

        # Only include reasoning_effort for GPT models (gpt-4o, gpt-5, etc.)
        effective_reasoning_effort = None
        if reasoning_effort and "gpt" in display_model.lower():
            effective_reasoning_effort = reasoning_effort
            model_usage = self._add_reasoning_effort_to_model_usage(model_usage, reasoning_effort)

        # Assign milestones to tool calls and compute milestone stats
        native_usage_units = list(native_usage_units or [])
        if milestone_times:
            self._assign_milestones_to_tool_calls(tool_calls, milestone_times)
            self._assign_milestones_to_usage_units(native_usage_units, milestone_times)

        # Apply manual overrides (if present) AFTER timestamp-based assignment
        if trial_dir:
            overrides = self.load_milestone_overrides(trial_dir)
            if overrides:
                self.apply_milestone_overrides(overrides, tool_calls, native_usage_units)

        # Derive usage unit milestones from their associated tool calls
        uu_proportional_shares = {}
        if native_usage_units and tool_calls:
            _, uu_proportional_shares = self._realign_usage_units_to_tool_calls(native_usage_units, tool_calls)

        self._normalize_native_usage_costs(
            native_usage_units=native_usage_units,
            total_cost=float(stdout_stats.get("total_cost_usd", 0.0) or 0.0),
        )
        if not native_usage_units:
            total_token_usage = self._extract_total_token_usage(stdout_stats.get("modelUsage", {}))
            self._distribute_usage_to_tool_calls(
                tool_calls=tool_calls,
                total_cost=float(stdout_stats.get("total_cost_usd", 0.0) or 0.0),
                total_token_usage=total_token_usage,
            )
        milestone_stats = self._compute_milestone_stats(
            milestone_times or {},
            tool_calls,
            stdout_stats,
            native_usage_units=native_usage_units,
            uu_proportional_shares=uu_proportional_shares,
        )

        # Detect sessions: prefer session_history.jsonl (authoritative), fall back to tool call gaps
        sessions: List[SessionInfo] = []
        if session_history_path:
            sessions = self.load_session_times_from_history(session_history_path)
        if not sessions:
            sessions = self.detect_sessions_from_tool_calls(tool_calls)

        # Derive session counts from session_history (authoritative) when
        # available — stdout-based counts can miss sessions whose output was
        # lost after process restart.
        if sessions and any(s.session_id for s in sessions):
            session_count = len(sessions)
            unique_session_count = len(set(s.session_id for s in sessions if s.session_id))
        else:
            session_count = stdout_stats.get("session_count", 0)
            unique_session_count = stdout_stats.get("unique_session_count", stdout_stats.get("session_count", 0))

        # Classify behavior_detail for shell tool calls
        self._classify_behavior_detail(tool_calls)

        # Classify verification events from Bash tool calls (independent)
        verification_events = self._build_verification_events(tool_calls)

        return TrialStats(
            trial_name=trial_name,
            agent_framework=self.FRAMEWORK_NAME,
            model=display_model,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            wall_clock_ms=duration_ms,  # For OpenHands, API latency is the best we have
            total_cost_usd=stdout_stats.get("total_cost_usd", 0.0),
            total_turns=stdout_stats.get("total_turns", 0),
            total_tool_calls=len(tool_calls),
            total_subagent_calls=total_subagent_calls,
            session_count=session_count,
            unique_session_count=unique_session_count,
            sessions=sessions,
            reasoning_effort=effective_reasoning_effort,
            model_usage=model_usage,
            tool_call_breakdown=tool_call_breakdown,
            milestone_stats=milestone_stats,
            native_usage_units=native_usage_units,
            all_tool_calls=tool_calls,
            verification_events=verification_events,
        )
