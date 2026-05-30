"""Google Gemini CLI agent log parser implementation."""

import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.e2e.log_parser.base import AgentLogParser, register_parser
from harness.e2e.log_parser.models import NativeUsageUnit, ToolCallRecord, TrialStats
from harness.e2e.pricing import has_tiered_pricing as _has_tiered_pricing_shared
from harness.e2e.pricing import resolve_pricing as _resolve_pricing_shared

logger = logging.getLogger(__name__)


@register_parser("gemini-cli")
class GeminiLogParser(AgentLogParser):
    """Parser for Google Gemini CLI logs.

    Gemini CLI outputs JSON when run with --output-format json flag.
    Detailed logs are stored in ~/.gemini/tmp/<project_hash>/ directory.
    """

    FRAMEWORK_NAME = "gemini-cli"

    # Gemini home directory in container
    GEMINI_HOME = "/home/fakeroot/.gemini"
    TMP_DIR = f"{GEMINI_HOME}/tmp"

    # Pricing imported from harness.e2e.pricing (single source of truth).

    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract Gemini execution trace.

        Gemini doesn't have a dedicated trace extraction tool like claude-extract.
        We copy the raw logs and rely on JSON output captured during execution.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store trace files

        Returns:
            True if successful (always returns True as we use stdout logs)
        """
        logger.info("Gemini trace extraction: using stdout JSON logs and raw log files")
        # Gemini traces are captured via --output-format json output during execution
        # Additional extraction from ~/.gemini/tmp/ for detailed logs
        return True

    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract Gemini logs from container.

        Copies the ~/.gemini/tmp/ contents to output directory.
        Structure: ~/.gemini/tmp/<project_hash>/chats/<session_id>/

        Args:
            container_name: Docker container name
            output_dir: Directory to store extracted logs (typically log/)
            session_id: Optional session_id to filter - only extract matching session

        Returns:
            Path to extracted logs directory
        """
        # Store in {output_dir}/gemini/ (agent name as directory)
        logs_dir = output_dir / "gemini"
        logs_dir.mkdir(parents=True, exist_ok=True)

        try:
            # First, find the most recent project hash directory in tmp
            # Enumerate ~/.gemini/tmp/*/ directories
            find_result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "find",
                    self.TMP_DIR,
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

            if find_result.returncode != 0 or not find_result.stdout.strip():
                logger.warning(f"No Gemini tmp directories found: {find_result.stderr}")
                return logs_dir

            project_dirs = find_result.stdout.strip().split("\n")

            # Get modification times to find most recent project directory
            # (exclude 'bin' which is for internal use)
            newest_dir = None
            newest_mtime = 0

            for project_dir in project_dirs:
                if not project_dir:
                    continue
                # Skip the bin directory
                if project_dir.endswith("/bin"):
                    continue

                stat_result = subprocess.run(
                    ["docker", "exec", container_name, "stat", "-c", "%Y", project_dir],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if stat_result.returncode == 0:
                    try:
                        mtime = int(stat_result.stdout.strip())
                        if mtime > newest_mtime:
                            newest_mtime = mtime
                            newest_dir = project_dir
                    except ValueError:
                        pass

            if not newest_dir:
                logger.warning("Could not determine newest Gemini project directory")
                return logs_dir

            logger.info(f"Found Gemini project directory: {newest_dir}")

            # Find all relevant files in the project directory
            find_files_result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "find",
                    newest_dir,
                    "-type",
                    "f",
                    "-name",
                    "*.json*",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            logger.debug(f"Find files result: {find_files_result.stdout}")

            if find_files_result.returncode == 0 and find_files_result.stdout.strip():
                files = find_files_result.stdout.strip().split("\n")
                copied_count = 0

                for remote_path in files:
                    if not remote_path:
                        continue

                    # Filter by session_id if provided
                    # Note: session_id format in path is different from the provided one
                    # Path format: session-2026-01-24T00-02-39c5b8f6.json
                    # So we DON'T filter by session_id here - always copy all session files
                    # The session_id parameter is for future use if needed

                    # Create local path preserving relative structure from project dir
                    rel_path = remote_path.replace(newest_dir + "/", "")
                    local_path = logs_dir / rel_path
                    local_path.parent.mkdir(parents=True, exist_ok=True)

                    cp_result = subprocess.run(
                        ["docker", "cp", f"{container_name}:{remote_path}", str(local_path)],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if cp_result.returncode == 0:
                        copied_count += 1
                        logger.debug(f"Copied: {remote_path} -> {local_path}")
                    else:
                        logger.warning(f"Failed to copy {remote_path}: {cp_result.stderr}")

                logger.info(f"Extracted {copied_count} Gemini log files to {logs_dir}")
            else:
                # Fallback: copy entire tmp directory
                subprocess.run(
                    ["docker", "cp", f"{container_name}:{newest_dir}/.", str(logs_dir)],
                    capture_output=True,
                    timeout=120,
                )
                logger.info(f"Extracted Gemini logs directory to {logs_dir}")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout extracting Gemini logs")
        except Exception as e:
            logger.warning(f"Error extracting Gemini logs: {e}")

        return logs_dir

    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from Gemini logs.

        Priority:
        1. Session log files (contain full input/output details)
        2. Fallback to stdout stats (only counts, no input/output sizes)

        Args:
            log_dir: Directory containing extracted logs

        Returns:
            List of tool call records
        """
        all_calls = []

        # First, try to parse from session log files (preferred - has input/output details)
        session_files = list(log_dir.rglob("session-*.json"))
        if session_files:
            for session_file in session_files:
                try:
                    calls = self._parse_tool_calls_from_session_log(session_file)
                    all_calls.extend(calls)
                    logger.info(f"Parsed {len(calls)} tool calls from {session_file.name}")
                except Exception as e:
                    logger.warning(f"Error parsing session log {session_file}: {e}")

        # If no session logs found, fallback to stdout
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
        """Parse tool calls from Gemini stdout JSON (fallback method).

        This is a fallback when detailed session logs are not available.
        Extracts tool call information from stats.tools.byName section.

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            List of ToolCallRecord objects
        """
        calls = []

        try:
            content = stdout_file.read_text(encoding="utf-8").strip()
            if not content:
                return calls

            # Parse JSON (single object or multiple)
            json_objects = []
            try:
                data = json.loads(content)
                json_objects.append(data)
            except json.JSONDecodeError:
                for line in content.split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            json_objects.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

            for data in json_objects:
                stats = data.get("stats", {})
                tools_stats = stats.get("tools", {})
                by_name = tools_stats.get("byName", {})

                for tool_name, tool_info in by_name.items():
                    if not isinstance(tool_info, dict):
                        continue

                    count = tool_info.get("count", 1)
                    success_count = tool_info.get("success", count)

                    # Create a ToolCallRecord for each invocation
                    for i in range(count):
                        calls.append(
                            ToolCallRecord(
                                id=f"{tool_name}_{i}",
                                name=tool_name,
                                timestamp=datetime.now(),
                                success=i < success_count,
                                input_size=0,
                                output_size=0,
                                milestone_id=None,
                                is_subagent=False,
                            )
                        )

        except Exception as e:
            logger.warning(f"Error parsing tool calls from stdout: {e}")

        return calls

    def _parse_tool_calls_from_session_log(self, session_file: Path) -> List[ToolCallRecord]:
        """Parse tool calls from Gemini session log file.

        Session logs contain detailed tool call information including
        args (input) and result (output).

        Args:
            session_file: Path to session JSON file

        Returns:
            List of ToolCallRecord objects with input/output sizes
        """
        calls = []

        try:
            with open(session_file, encoding="utf-8") as f:
                data = json.load(f)

            messages = data.get("messages", [])

            for message in messages:
                if message.get("type") != "gemini":
                    continue

                tool_calls = message.get("toolCalls", [])
                for tc in tool_calls:
                    tool_id = tc.get("id", "")
                    tool_name = tc.get("name", "unknown")
                    args = tc.get("args", {})
                    status = tc.get("status", "")
                    timestamp_str = tc.get("timestamp")

                    # Calculate input size from args
                    input_size = len(json.dumps(args, ensure_ascii=False).encode("utf-8"))

                    # Calculate output size from result
                    output_size = 0
                    results = tc.get("result", [])
                    for result in results:
                        if isinstance(result, dict):
                            func_response = result.get("functionResponse", {})
                            response = func_response.get("response", {})
                            output = response.get("output", "")
                            if isinstance(output, str):
                                output_size += len(output.encode("utf-8"))
                            elif isinstance(output, (dict, list)):
                                output_size += len(json.dumps(output, ensure_ascii=False).encode("utf-8"))

                    # Parse timestamp
                    timestamp = None
                    if timestamp_str:
                        try:
                            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            timestamp = timestamp.replace(tzinfo=None)
                        except ValueError:
                            timestamp = datetime.now()
                    else:
                        timestamp = datetime.now()

                    # Extract raw command for shell tool calls (used by verification classifier)
                    bash_command = None
                    if tool_name == "run_shell_command" and isinstance(args, dict):
                        bash_command = args.get("command")

                    calls.append(
                        ToolCallRecord(
                            id=tool_id,
                            name=tool_name,
                            timestamp=timestamp,
                            success=status == "success",
                            input_size=input_size,
                            output_size=output_size,
                            milestone_id=None,
                            is_subagent=False,
                            _bash_command=bash_command,
                        )
                    )

        except Exception as e:
            logger.warning(f"Error parsing session log {session_file}: {e}")

        return calls

    def _parse_json_file(self, json_path: Path) -> List[ToolCallRecord]:
        """Parse a single JSON/JSONL file for tool calls.

        Args:
            json_path: Path to JSON file

        Returns:
            List of tool call records
        """
        calls = []

        with open(json_path, encoding="utf-8") as f:
            content = f.read().strip()

            # Try parsing as single JSON first
            try:
                data = json.loads(content)
                calls.extend(self._extract_tool_calls_from_event(data))
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
                    calls.extend(self._extract_tool_calls_from_event(event))
                except json.JSONDecodeError as e:
                    logger.debug(f"Invalid JSON at {json_path}:{line_num}: {e}")

        return calls

    def _extract_tool_calls_from_event(
        self,
        event: Dict[str, Any],
    ) -> List[ToolCallRecord]:
        """Extract tool call records from a Gemini JSON event.

        Gemini events may contain:
        - "type": "tool_use" or "function_call"
        - Tool information in content blocks
        - Nested in "parts" for Gemini API format

        Args:
            event: Parsed JSON event

        Returns:
            List of tool call records
        """
        calls = []

        event_type = event.get("type", "")

        # Handle tool_use events (similar to Claude format)
        if event_type in ("tool_use", "function_call", "tool_call"):
            call = self._create_tool_call_record(event)
            if call:
                calls.append(call)

        # Handle Gemini API response format with parts
        if "parts" in event:
            for part in event.get("parts", []):
                if isinstance(part, dict):
                    if "functionCall" in part:
                        call = self._create_tool_call_from_function_call(part["functionCall"], event)
                        if call:
                            calls.append(call)
                    elif part.get("type") == "tool_use":
                        call = self._create_tool_call_record(part)
                        if call:
                            calls.append(call)

        # Handle nested content blocks (similar to Claude format)
        if "content" in event and isinstance(event["content"], list):
            for item in event["content"]:
                if isinstance(item, dict) and item.get("type") in ("tool_use", "function_call"):
                    call = self._create_tool_call_record(item)
                    if call:
                        calls.append(call)

        # Handle tool_calls array (OpenAI-compatible format)
        if "tool_calls" in event:
            for tc in event.get("tool_calls", []):
                call = self._create_tool_call_record(tc)
                if call:
                    calls.append(call)

        # Handle Gemini-specific events with functionCalls
        if "functionCalls" in event:
            for fc in event.get("functionCalls", []):
                call = self._create_tool_call_from_function_call(fc, event)
                if call:
                    calls.append(call)

        return calls

    def _create_tool_call_record(
        self,
        data: Dict[str, Any],
    ) -> Optional[ToolCallRecord]:
        """Create a ToolCallRecord from tool call data.

        Args:
            data: Tool call data

        Returns:
            ToolCallRecord or None if invalid
        """
        tool_id = data.get("id", data.get("call_id", ""))
        tool_name = data.get("name", data.get("function", {}).get("name", "unknown"))
        tool_input = data.get("input", data.get("args", data.get("arguments", {})))

        # Handle function object format
        if "function" in data and isinstance(data["function"], dict):
            tool_name = data["function"].get("name", tool_name)
            tool_input = data["function"].get("arguments", tool_input)

        # Parse input if it's a string
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                tool_input = {"raw": tool_input}

        # Calculate input size
        input_size = len(json.dumps(tool_input, ensure_ascii=False).encode("utf-8"))

        # Parse timestamp
        timestamp = self._parse_timestamp(data.get("timestamp", data.get("created_at")))

        # Extract raw command for shell tool calls (used by verification classifier)
        bash_command = None
        if tool_name == "run_shell_command" and isinstance(tool_input, dict):
            bash_command = tool_input.get("command")

        return ToolCallRecord(
            id=tool_id,
            name=tool_name,
            timestamp=timestamp,
            success=not data.get("is_error", False),
            input_size=input_size,
            output_size=0,
            milestone_id=None,
            is_subagent=False,
            _bash_command=bash_command,
        )

    def _create_tool_call_from_function_call(
        self,
        function_call: Dict[str, Any],
        parent_event: Dict[str, Any],
    ) -> Optional[ToolCallRecord]:
        """Create a ToolCallRecord from Gemini functionCall format.

        Args:
            function_call: Function call data from Gemini format
            parent_event: Parent event for timestamp

        Returns:
            ToolCallRecord or None if invalid
        """
        tool_name = function_call.get("name", "unknown")
        tool_args = function_call.get("args", {})

        # Calculate input size
        input_size = len(json.dumps(tool_args, ensure_ascii=False).encode("utf-8"))

        # Parse timestamp from parent event
        timestamp = self._parse_timestamp(parent_event.get("timestamp"))

        # Extract raw command for shell tool calls (used by verification classifier)
        bash_command = None
        if tool_name == "run_shell_command" and isinstance(tool_args, dict):
            bash_command = tool_args.get("command")

        return ToolCallRecord(
            id=function_call.get("id", ""),
            name=tool_name,
            timestamp=timestamp,
            success=True,  # Default to true, updated by parse_tool_results
            input_size=input_size,
            output_size=0,
            milestone_id=None,
            is_subagent=False,
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
                ts = datetime.fromisoformat(str(timestamp_val).replace("Z", "+00:00"))
                return ts.replace(tzinfo=None)
        except (ValueError, OSError):
            return None

    def _parse_concatenated_json(self, content: str) -> List[Dict[str, Any]]:
        """Parse multiple concatenated JSON objects from a string.

        Gemini CLI outputs one JSON object per run. When using resume,
        multiple JSON objects may be concatenated in stdout.

        Args:
            content: String containing one or more JSON objects

        Returns:
            List of parsed JSON objects
        """
        json_objects = []
        decoder = json.JSONDecoder()
        content = content.strip()
        idx = 0

        while idx < len(content):
            # Skip whitespace
            while idx < len(content) and content[idx] in " \t\n\r":
                idx += 1

            if idx >= len(content):
                break

            try:
                obj, end_idx = decoder.raw_decode(content, idx)
                json_objects.append(obj)
                idx += end_idx
            except json.JSONDecodeError:
                # If we can't parse, try to find the next '{' and continue
                next_brace = content.find("{", idx + 1)
                if next_brace == -1:
                    break
                idx = next_brace

        logger.debug(f"Parsed {len(json_objects)} JSON objects from stdout")
        return json_objects

    def _has_tiered_pricing(self, model: str) -> bool:
        """Check if a model uses tiered pricing."""
        return _has_tiered_pricing_shared(model)

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        prompt_tokens: int | None = None,
    ) -> float:
        """Calculate cost based on token usage.

        Args:
            model: Model name
            input_tokens: Number of new input tokens (excluding cached)
            output_tokens: Number of output tokens
            cached_tokens: Number of cached input tokens (charged at reduced rate)
            prompt_tokens: Total prompt tokens for this request (input + cached),
                used to select pricing tier for models with tiered pricing.
                If None, the lowest tier is used.

        Returns:
            Estimated cost in USD
        """
        rates = _resolve_pricing_shared(model, prompt_tokens=prompt_tokens)
        input_cost = (input_tokens / 1_000_000) * rates.get("input", 0)
        cached_cost = (cached_tokens / 1_000_000) * rates.get("cache_read", rates.get("input", 0) * 0.1)
        output_cost = (output_tokens / 1_000_000) * rates.get("output", 0)
        return input_cost + cached_cost + output_cost

    def _merge_stats(self, stdout_stats: Dict, session_stats: Dict) -> Dict:
        """Merge stdout and session JSON stats for accurate cost calculation.

        Strategy per model:
        - Tiered-pricing models with session data: use session JSON per-message cost
          (accurate tiering) plus residual cost for requests not in session JSON.
        - Flat-pricing models or models not in session JSON: use stdout cost as-is.
        - Models only in session JSON (not in stdout): include session data.

        Token totals in modelUsage stay from stdout (more complete, includes sub-model
        calls). Only costUSD gets replaced for tiered models.

        Args:
            stdout_stats: Stats from stdout parsing (modelUsage, total_cost_usd, etc.)
            session_stats: Stats from _parse_session_logs (per_model breakdown)

        Returns:
            Dict with merged modelUsage and total_cost_usd
        """
        model_usage = dict(stdout_stats.get("modelUsage", {}))
        per_model_session = session_stats.get("per_model", {})
        merged_total_cost = 0.0

        # Process models present in stdout
        for model, usage in model_usage.items():
            stdout_cost = usage.get("costUSD", 0.0)
            stdout_reqs = usage.get("apiRequests", 0)

            if model in per_model_session:
                # Session logs are the complete per-call record (stdout under-
                # counts across resumes), and _calculate_cost handles both flat
                # and tiered pricing. Use session cost + residual for any model
                # that has session data, not just tiered ones.
                session_data = per_model_session[model]
                session_cost = session_data["cost_usd"]
                session_turns = session_data["turns"]

                residual_reqs = max(0, stdout_reqs - session_turns)
                if residual_reqs > 0 and stdout_reqs > 0:
                    # Estimate residual tokens proportionally from stdout totals
                    fraction = residual_reqs / stdout_reqs
                    residual_input = int(usage.get("inputTokens", 0) * fraction)
                    residual_output = int(usage.get("outputTokens", 0) * fraction)
                    residual_thoughts = int(usage.get("thoughtsTokens", 0) * fraction)
                    residual_cached = int(usage.get("cachedTokens", 0) * fraction)
                    residual_total_output = residual_output + residual_thoughts
                    # Use avg prompt for residual (best we can do)
                    avg_prompt = (residual_input + residual_cached) // max(residual_reqs, 1)
                    residual_cost = self._calculate_cost(
                        model,
                        residual_input,
                        residual_total_output,
                        residual_cached,
                        prompt_tokens=avg_prompt,
                    )
                    merged_cost = session_cost + residual_cost
                else:
                    merged_cost = session_cost

                usage["costUSD"] = merged_cost
                merged_total_cost += merged_cost
                logger.debug(
                    f"Merge [{model}]: session=${session_cost:.4f} + "
                    f"residual({residual_reqs} reqs)=${merged_cost - session_cost:.4f} "
                    f"= ${merged_cost:.4f} (stdout was ${stdout_cost:.4f})"
                )
            else:
                # Flat pricing or not in session JSON — use stdout cost
                merged_total_cost += stdout_cost

        # Add models only in session JSON (not in stdout)
        for model, session_data in per_model_session.items():
            if model not in model_usage:
                tu = session_data["token_usage"]
                model_usage[model] = {
                    "inputTokens": tu["new_input_tokens"],
                    "outputTokens": tu["output_tokens"],
                    "thoughtsTokens": tu["thoughts_tokens"],
                    "cachedTokens": tu["cached_tokens"],
                    "apiRequests": session_data["turns"],
                    "costUSD": session_data["cost_usd"],
                }
                merged_total_cost += session_data["cost_usd"]
                logger.debug(f"Merge [{model}]: session-only, ${session_data['cost_usd']:.4f}")

        return {
            "total_cost_usd": merged_total_cost,
            "modelUsage": model_usage,
        }

    def parse_stdout_stats(self, stdout_file: Path, logs_dir: Optional[Path] = None) -> Dict:
        """Parse agent_stdout.txt for accumulated statistics.

        Gemini JSON output includes stats in its response when using --output-format json.

        Actual format from gemini CLI 0.25.1:
        {
          "session_id": "...",
          "response": "...",
          "stats": {
            "models": {
              "gemini-3-flash-preview": {
                "api": {"totalRequests": 3, "totalErrors": 0, "totalLatencyMs": 9387},
                "tokens": {"input": 15601, "prompt": 21712, "candidates": 201,
                           "total": 22072, "cached": 6111, "thoughts": 159, "tool": 0}
              }
            },
            "tools": {"totalCalls": 2, "totalSuccess": 2, "totalFail": 0,
                      "totalDurationMs": 11, "byName": {...}}
          }
        }

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            Dictionary with accumulated statistics including:
            - total_cost_usd, total_turns, modelUsage, session_count
            - duration_ms: total API latency
            - tool_calls: list of tool call info from stats.tools.byName
            - tool_call_breakdown: {tool_name: count}
        """
        total_cost = 0.0
        total_turns = 0
        total_duration_ms = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 0
        unique_session_ids: set[str] = set()
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

        # Read entire file and try to parse as JSON (Gemini outputs multi-line JSON)
        content = stdout_file.read_text(encoding="utf-8").strip()
        if not content:
            # stdout is empty, try to get stats from session logs
            if logs_dir:
                session_stats = self._parse_session_logs(logs_dir)
                if session_stats.get("total_turns", 0) > 0:
                    logger.info(
                        f"Parsed session logs: {session_stats['total_turns']} turns, "
                        f"${session_stats['total_cost_usd']:.2f} (stdout was empty)"
                    )
                    model_usage_from_session = {}
                    for model_name, model_data in session_stats.get("per_model", {}).items():
                        tu = model_data["token_usage"]
                        model_usage_from_session[model_name] = {
                            "inputTokens": tu["new_input_tokens"],
                            "outputTokens": tu["output_tokens"],
                            "thoughtsTokens": tu["thoughts_tokens"],
                            "cachedTokens": tu["cached_tokens"],
                            "apiRequests": model_data["turns"],
                            "costUSD": model_data["cost_usd"],
                        }
                    return {
                        "total_cost_usd": session_stats.get("total_cost_usd", 0.0),
                        "total_turns": session_stats.get("total_turns", 0),
                        "modelUsage": model_usage_from_session,
                        "session_count": 1,
                        "unique_session_count": 1,
                        "duration_ms": 0,
                        "tool_calls": [],
                        "tool_call_breakdown": {},
                    }
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

        # Parse JSON objects from content
        # Gemini may output multiple JSON objects (one per run/resume)
        json_objects = self._parse_concatenated_json(content)

        for data in json_objects:
            # Skip non-dict objects
            if not isinstance(data, dict):
                continue
            # Check for Gemini stats in response
            stats = data.get("stats", {})
            models_stats = stats.get("models", {})

            if models_stats:
                session_count += 1
                session_id = data.get("session_id") or data.get("conversation_id") or data.get("id")
                if isinstance(session_id, str) and session_id:
                    unique_session_ids.add(session_id)

                for model, usage in models_stats.items():
                    if not isinstance(usage, dict):
                        continue

                    # Extract token counts from nested structure
                    tokens = usage.get("tokens", {})
                    api_info = usage.get("api", {})

                    # Get token counts
                    input_tokens = tokens.get("input", tokens.get("prompt", 0))
                    output_tokens = tokens.get("candidates", tokens.get("output", 0))
                    thoughts_tokens = tokens.get("thoughts", 0)
                    cached_tokens = tokens.get("cached", 0)
                    total_tokens = tokens.get("total", 0)
                    api_requests = api_info.get("totalRequests", 1)
                    latency_ms = api_info.get("totalLatencyMs", 0)

                    # Fallback to old format if new format not present
                    if not tokens:
                        input_tokens = usage.get("promptTokens", usage.get("input_tokens", 0))
                        output_tokens = usage.get("responseTokens", usage.get("output_tokens", 0))
                        cached_tokens = usage.get("cachedTokens", 0)
                        api_requests = usage.get("apiRequests", 1)

                    # Accumulate
                    model_usage[model]["inputTokens"] += input_tokens
                    model_usage[model]["outputTokens"] += output_tokens
                    model_usage[model]["thoughtsTokens"] = model_usage[model].get("thoughtsTokens", 0) + thoughts_tokens
                    model_usage[model]["cachedTokens"] += cached_tokens
                    model_usage[model]["apiRequests"] += api_requests
                    model_usage[model]["latencyMs"] += latency_ms
                    total_duration_ms += latency_ms
                    if total_tokens:
                        model_usage[model]["totalTokens"] += total_tokens

                    # Count LLM API calls (total_turns = number of API requests)
                    total_turns += api_requests

                    # Calculate cost (thoughts tokens charged at output rate)
                    # Estimate avg prompt tokens per request for tiered pricing.
                    # stdout input_tokens = non-cached, so total prompt = input + cached.
                    total_output_tokens = output_tokens + thoughts_tokens
                    avg_prompt = (input_tokens + cached_tokens) // max(api_requests, 1)
                    session_cost = self._calculate_cost(
                        model,
                        input_tokens,
                        total_output_tokens,
                        cached_tokens,
                        prompt_tokens=avg_prompt,
                    )
                    model_usage[model]["costUSD"] = model_usage[model].get("costUSD", 0.0) + session_cost
                    total_cost += session_cost

            # Extract tool calls from stats.tools (for tool_call_breakdown, not turns)
            tools_stats = stats.get("tools", {})
            if tools_stats:
                # Extract individual tool calls from byName
                by_name = tools_stats.get("byName", {})
                for tool_name, tool_info in by_name.items():
                    if not isinstance(tool_info, dict):
                        continue

                    count = tool_info.get("count", 1)
                    success = tool_info.get("success", count)
                    fail = tool_info.get("fail", 0)
                    duration_ms = tool_info.get("durationMs", 0)

                    tool_call_breakdown[tool_name] += count

                    # Create tool call entries (one per count)
                    for i in range(count):
                        tool_calls.append(
                            {
                                "name": tool_name,
                                "success": i < success,  # First 'success' calls succeeded
                                "duration_ms": duration_ms // count if count > 0 else 0,
                            }
                        )

            # Also count num_turns if present (similar to Claude format)
            # Only use num_turns when stats.models is absent to avoid double-counting
            # (stats.models.*.api.totalRequests already contributes to total_turns above)
            if not models_stats and "num_turns" in data:
                total_turns += data.get("num_turns", 0)

        # Convert defaultdicts to regular dicts
        model_usage_dict = {model: dict(usage) for model, usage in model_usage.items()}

        # Reconcile with the session JSON logs — the complete per-call record.
        # stdout under-counts across resumes (and may label the model
        # differently, e.g. "gemini-3-flash-preview" vs "gemini-3.5-flash"),
        # which makes a naive merge double-count. When the session logs captured
        # at least as many turns as stdout, treat them as authoritative.
        if logs_dir:
            session_stats = self._parse_session_logs(logs_dir)
            session_turns = session_stats.get("total_turns", 0)
            if session_turns > 0 and session_turns >= total_turns:
                model_usage_dict = {}
                for model_name, md in session_stats.get("per_model", {}).items():
                    tu = md["token_usage"]
                    model_usage_dict[model_name] = {
                        "inputTokens": tu["new_input_tokens"],
                        "outputTokens": tu["output_tokens"],
                        "thoughtsTokens": tu["thoughts_tokens"],
                        "cachedTokens": tu["cached_tokens"],
                        "apiRequests": md["turns"],
                        "costUSD": md["cost_usd"],
                    }
                total_cost = session_stats.get("total_cost_usd", 0.0)
                total_turns = session_turns
            elif model_usage_dict and session_turns > 0:
                # Session less complete than stdout — merge (session cost for
                # accurate per-message pricing + residual for stdout-only calls).
                merged = self._merge_stats(
                    {"modelUsage": model_usage_dict, "total_cost_usd": total_cost},
                    session_stats,
                )
                total_cost = merged["total_cost_usd"]
                for model_name in merged["modelUsage"]:
                    if model_name not in model_usage_dict:
                        total_turns += merged["modelUsage"][model_name].get("apiRequests", 0)
                model_usage_dict = merged["modelUsage"]

        logger.info(
            f"Parsed stdout: {session_count} sessions, {len(unique_session_ids) if unique_session_ids else session_count} unique sessions, "
            f"{total_turns} turns, ${total_cost:.4f}, {len(tool_calls)} tool calls"
        )

        unique_session_count = len(unique_session_ids) if unique_session_ids else session_count

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": model_usage_dict,
            "session_count": session_count,
            "unique_session_count": unique_session_count,
            "duration_ms": total_duration_ms,
            "tool_calls": tool_calls,
            "tool_call_breakdown": dict(tool_call_breakdown),
        }

    def _count_turns_from_session_logs(self, logs_dir: Path) -> int:
        """Count turns from Gemini session log files.

        A turn is defined as a model response (message with type="gemini").

        Args:
            logs_dir: Directory containing extracted logs (typically log/)

        Returns:
            Total number of turns across all session files
        """
        stats = self._parse_session_logs(logs_dir)
        return stats.get("total_turns", 0)

    def _iter_session_messages(self, logs_dir: Path) -> List[Dict[str, Any]]:
        """Return de-duplicated gemini assistant messages (with per-call token
        usage) from the session logs — the single source of truth for
        turns/tokens/cost.

        Current gemini-cli writes JSONL append-logs (`session-*.jsonl`): the real
        per-call usage lives on bare top-level lines `{"type":"gemini","id":...,
        "tokens":{...},...}`. The interleaved `{"$set":{"messages":[...]}}`
        snapshots carry `tokens:null` (ignored), and each call is emitted twice
        (with/without `toolCalls`) sharing one `id` — so we dedup by `id`,
        preferring the copy that carries `toolCalls` (needed for milestone
        attribution). Older single-document `session-*.json` files (top-level
        `messages` array with populated tokens) are still supported.
        """
        gemini_dir = logs_dir / "gemini"
        if not gemini_dir.exists():
            gemini_dir = logs_dir
        files = sorted(gemini_dir.rglob("session-*.jsonl")) + sorted(gemini_dir.rglob("session-*.json"))

        by_id: Dict[str, Dict[str, Any]] = {}

        def consider(msg: Any, fallback_id: str) -> None:
            if not isinstance(msg, dict) or msg.get("type") != "gemini":
                return
            tok = msg.get("tokens")
            if not isinstance(tok, dict):
                return
            if not (tok.get("input") or tok.get("output") or tok.get("cached") or tok.get("thoughts")):
                return
            mid = str(msg.get("id") or fallback_id)
            prev = by_id.get(mid)
            if prev is None or (not prev.get("toolCalls") and msg.get("toolCalls")):
                by_id[mid] = msg

        for sf in files:
            try:
                if sf.suffix == ".jsonl":
                    with open(sf, encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f):
                            if '"tokens"' not in line:  # fast-skip heartbeat/snapshot lines
                                continue
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue
                            consider(obj, f"{sf.name}:{i}")
                else:
                    doc = json.loads(open(sf, encoding="utf-8", errors="replace").read())
                    if isinstance(doc, dict):
                        for i, m in enumerate(doc.get("messages", []) or []):
                            consider(m, f"{sf.name}:{i}")
            except Exception as e:
                logger.warning(f"Error reading session log {sf}: {e}")

        return list(by_id.values())

    def _parse_session_logs(self, logs_dir: Path) -> Dict:
        """Parse Gemini session log files for turns, tokens, and cost.

        Returns per-model breakdown with per-message tiered cost calculation.

        Args:
            logs_dir: Directory containing extracted logs (typically log/)

        Returns:
            Dictionary with total_turns, total_cost_usd, and per_model breakdown:
            {
                "total_turns": int,
                "total_cost_usd": float,
                "per_model": {
                    "model-name": {
                        "turns": int,
                        "cost_usd": float,
                        "token_usage": {
                            "input_tokens": int,    # total incl cached
                            "cached_tokens": int,
                            "new_input_tokens": int,
                            "output_tokens": int,
                            "thoughts_tokens": int,
                        },
                    },
                },
            }
        """
        total_turns = 0
        total_cost = 0.0
        per_model: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "turns": 0,
                "cost_usd": 0.0,
                "token_usage": {
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "new_input_tokens": 0,
                    "output_tokens": 0,
                    "thoughts_tokens": 0,
                },
            }
        )
        fallback_model = "gemini-3-flash-preview"

        for msg in self._iter_session_messages(logs_dir):
            msg_model = msg.get("model") or fallback_model
            total_turns += 1
            entry = per_model[msg_model]
            entry["turns"] += 1

            tokens = msg.get("tokens", {})
            if tokens:
                msg_input = tokens.get("input", 0)
                msg_output = tokens.get("output", tokens.get("candidates", 0))
                msg_thoughts = tokens.get("thoughts", 0)
                msg_cached = tokens.get("cached", 0)
                msg_new_input = max(0, msg_input - msg_cached)

                tu = entry["token_usage"]
                tu["input_tokens"] += msg_input
                tu["cached_tokens"] += msg_cached
                tu["new_input_tokens"] += msg_new_input
                tu["output_tokens"] += msg_output
                tu["thoughts_tokens"] += msg_thoughts

                # Per-message cost for accurate tiered pricing
                msg_total_output = msg_output + msg_thoughts
                msg_cost = self._calculate_cost(
                    msg_model,
                    msg_new_input,
                    msg_total_output,
                    msg_cached,
                    prompt_tokens=msg_input,
                )
                entry["cost_usd"] += msg_cost
                total_cost += msg_cost

        # Log summary
        for model_name, data in per_model.items():
            tu = data["token_usage"]
            logger.info(
                f"Session logs [{model_name}]: {data['turns']} turns, "
                f"{tu['input_tokens']:,} input, {tu['cached_tokens']:,} cached, "
                f"{tu['output_tokens'] + tu['thoughts_tokens']:,} output, "
                f"${data['cost_usd']:.2f}"
            )

        logger.info(f"Session logs total: {total_turns} turns, ${total_cost:.2f}")

        return {
            "total_turns": total_turns,
            "total_cost_usd": total_cost,
            "per_model": dict(per_model),
        }

    def parse_native_usage_units(
        self,
        log_dir: Path,
        stdout_file: Path,
    ) -> List[NativeUsageUnit]:
        """Parse native message-level usage units from Gemini session logs."""
        units: List[NativeUsageUnit] = []
        for mi, msg in enumerate(self._iter_session_messages(log_dir)):
            tokens = msg.get("tokens", {})
            if not isinstance(tokens, dict):
                continue

            msg_input = int(tokens.get("input", 0) or 0)
            msg_output = int(tokens.get("output", tokens.get("candidates", 0)) or 0)
            msg_thoughts = int(tokens.get("thoughts", 0) or 0)
            msg_cached = int(tokens.get("cached", 0) or 0)
            if msg_input <= 0 and msg_output <= 0 and msg_thoughts <= 0 and msg_cached <= 0:
                continue

            model = str(msg.get("model") or "gemini-3-flash-preview")
            msg_new_input = max(0, msg_input - msg_cached)
            msg_total_output = msg_output + msg_thoughts
            msg_cost = self._calculate_cost(
                model,
                msg_new_input,
                msg_total_output,
                msg_cached,
                prompt_tokens=msg_input,
            )

            timestamp = None
            ts_val = msg.get("timestamp")
            if isinstance(ts_val, str) and ts_val:
                try:
                    timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    timestamp = None

            msg_id = str(msg.get("id") or f"msg{mi}")
            msg_token_usage = {
                "inputTokens": msg_input,
                "outputTokens": msg_total_output,
                "cacheReadInputTokens": msg_cached,
                "thoughtsTokens": msg_thoughts,
            }

            # Expand multi-toolCall messages (v0.29.5+) into one NativeUsageUnit
            # per tool call so each tool call counts as one turn and gets
            # assigned to the correct milestone via its own timestamp.
            tool_calls = msg.get("toolCalls", [])
            if len(tool_calls) > 1:
                n_tc = len(tool_calls)
                split_tokens = {k: v // n_tc for k, v in msg_token_usage.items()}
                split_cost = msg_cost / n_tc
                for tc_idx, tc in enumerate(tool_calls):
                    tc_ts = None
                    tc_ts_val = tc.get("timestamp") if isinstance(tc, dict) else None
                    if isinstance(tc_ts_val, str) and tc_ts_val:
                        try:
                            tc_ts = datetime.fromisoformat(tc_ts_val.replace("Z", "+00:00")).replace(tzinfo=None)
                        except ValueError:
                            tc_ts = None
                    units.append(
                        NativeUsageUnit(
                            id=f"{msg_id}:tc{tc_idx}",
                            source_type="tool_call",
                            timestamp=tc_ts or timestamp,
                            model=model,
                            token_usage=dict(split_tokens),
                            cost_usd=split_cost,
                        )
                    )
            else:
                units.append(
                    NativeUsageUnit(
                        id=msg_id,
                        source_type="message",
                        timestamp=timestamp,
                        model=model,
                        token_usage=msg_token_usage,
                        cost_usd=msg_cost,
                    )
                )

        logger.info(f"Parsed {len(units)} native usage units from Gemini logs")
        return units

    def parse_tool_results(
        self,
        log_dir: Path,
        tool_calls: List[ToolCallRecord],
    ) -> None:
        """Update tool calls with result information from Gemini logs.

        Parses result events from JSON/JSONL files and updates corresponding
        tool call records with success status and output size.

        Modifies tool_calls in place.

        Args:
            log_dir: Directory containing extracted logs
            tool_calls: List of tool call records to update
        """
        # Build lookup map by tool call ID
        calls_by_id = {tc.id: tc for tc in tool_calls if tc.id}

        if not calls_by_id:
            return

        # Find all JSON/JSONL files
        json_files = list(log_dir.rglob("*.json")) + list(log_dir.rglob("*.jsonl"))

        for json_file in json_files:
            try:
                with open(json_file, encoding="utf-8") as f:
                    content = f.read().strip()

                # Try parsing as single JSON first
                try:
                    data = json.loads(content)
                    self._update_tool_results_from_event(data, calls_by_id)
                    continue
                except json.JSONDecodeError:
                    pass

                # Parse as JSONL
                for line in content.split("\n"):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                        self._update_tool_results_from_event(event, calls_by_id)
                    except json.JSONDecodeError:
                        continue

            except Exception as e:
                logger.debug(f"Error parsing tool results from {json_file}: {e}")

    def _update_tool_results_from_event(
        self,
        event: Dict[str, Any],
        calls_by_id: Dict[str, ToolCallRecord],
    ) -> None:
        """Update tool call records from a result event.

        Args:
            event: Parsed JSON event
            calls_by_id: Mapping of tool call ID to ToolCallRecord
        """
        event_type = event.get("type", "")

        # Handle tool_result events (similar to Claude format)
        if event_type in ("tool_result", "function_call_result"):
            call_id = event.get("tool_use_id", event.get("call_id", event.get("id", "")))
            if call_id and call_id in calls_by_id:
                tc = calls_by_id[call_id]
                tc.success = not event.get("is_error", False)

                # Calculate output size
                output = event.get("output", event.get("content", event.get("result", "")))
                if isinstance(output, str):
                    tc.output_size = len(output.encode("utf-8"))
                elif isinstance(output, (dict, list)):
                    tc.output_size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))

        # Handle Gemini functionResponse format
        if "parts" in event:
            for part in event.get("parts", []):
                if isinstance(part, dict) and "functionResponse" in part:
                    response = part["functionResponse"]
                    # Match by name if no ID available
                    name = response.get("name", "")
                    result = response.get("response", {})

                    # Try to find matching call
                    for tc in calls_by_id.values():
                        if tc.name == name and tc.output_size == 0:
                            tc.success = not result.get("is_error", False)
                            output = result.get("output", result.get("content", ""))
                            if isinstance(output, str):
                                tc.output_size = len(output.encode("utf-8"))
                            elif isinstance(output, (dict, list)):
                                tc.output_size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))
                            break

        # Handle nested tool_results
        if "tool_results" in event:
            for result in event.get("tool_results", []):
                call_id = result.get("call_id", result.get("id", ""))
                if call_id and call_id in calls_by_id:
                    tc = calls_by_id[call_id]
                    tc.success = not result.get("is_error", False)

                    output = result.get("output", result.get("content", ""))
                    if isinstance(output, str):
                        tc.output_size = len(output.encode("utf-8"))
                    elif isinstance(output, (dict, list)):
                        tc.output_size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))

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
        """Compute complete trial statistics for Gemini.

        Overrides base class to use duration_ms from Gemini's API latency stats.

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
        # Derive start/end time from tool call timestamps (accurate).
        # Falls back to datetime.now() if no tool calls are available.
        timed = [tc for tc in tool_calls if tc.timestamp]
        if timed:
            start_time = min(tc.timestamp for tc in timed)
            end_time = max(tc.timestamp for tc in timed)
        else:
            end_time = datetime.now()
            start_time = end_time

        # API latency from Gemini stats (used only as a fallback for duration).
        api_latency_ms = stdout_stats.get("duration_ms", 0)

        # Compute tool call breakdown from actual tool call records
        # (stdout stats.tools.byName systematically underreports)
        tool_call_breakdown: Dict[str, int] = {}
        for tc in tool_calls:
            tool_call_breakdown[tc.name] = tool_call_breakdown.get(tc.name, 0) + 1

        # Count subagent calls
        total_subagent_calls = sum(1 for tc in tool_calls if tc.is_subagent)

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

        # Get model usage and add reasoning_effort for gpt-5* models
        model_usage = stdout_stats.get("modelUsage", {})
        if reasoning_effort:
            model_usage = self._add_reasoning_effort_to_model_usage(model_usage, reasoning_effort)

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

        # Compute duration from sessions (sum of active session durations).
        # Wall clock = total elapsed from start to end (including gaps).
        wall_clock_ms = int((end_time - start_time).total_seconds() * 1000) if start_time and end_time else 0
        if sessions:
            session_duration_ms = sum(s.duration_ms for s in sessions)
        else:
            session_duration_ms = wall_clock_ms or api_latency_ms

        return TrialStats(
            trial_name=trial_name,
            agent_framework=self.FRAMEWORK_NAME,
            model=model,
            start_time=start_time,
            end_time=end_time,
            duration_ms=session_duration_ms,
            wall_clock_ms=wall_clock_ms,
            total_cost_usd=stdout_stats.get("total_cost_usd", 0.0),
            total_turns=stdout_stats.get("total_turns", 0),
            total_tool_calls=len(tool_calls),
            total_subagent_calls=total_subagent_calls,
            session_count=session_count,
            unique_session_count=unique_session_count,
            sessions=sessions,
            reasoning_effort=reasoning_effort,
            model_usage=model_usage,
            tool_call_breakdown=tool_call_breakdown,
            milestone_stats=milestone_stats,
            native_usage_units=native_usage_units,
            all_tool_calls=tool_calls,
            verification_events=verification_events,
        )
