"""Agent execution utilities for running agents inside Docker containers.

This module provides:
- AgentRunner: Base class with common agent execution logic
- E2EAgentRunner: Extended class for E2E continuous task queue mode

The actual agent-specific logic (command building, credential mounts) is
delegated to AgentFramework implementations via the strategy pattern.
"""

import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from harness.e2e.agents import AgentFramework, get_agent_framework
from harness.e2e.log_parser import get_parser

logger = logging.getLogger("e2e.runner")

# Prompt template directory
PROMPT_DIR = Path(__file__).parent / "prompt"


class AgentRunner:
    """Base agent runner with common functionality.

    This class provides the core agent execution logic used by both
    run_milestone.py (single milestone mode) and E2EAgentRunner (E2E mode).

    Agent-specific logic (command building, credential setup) is delegated
    to the AgentFramework implementation via the strategy pattern.
    """

    def __init__(
        self,
        container_name: str,
        workdir: str = "/testbed",
        model: str = "claude-sonnet-4-5-20250929",
        timeout_ms: int = 1800_000,
        log_dir: Optional[Path] = None,
        agent_name: str = "claude-code",
        reasoning_effort: Optional[str] = None,
        use_sdk: bool = False,
        include_directories: Optional[list[str]] = None,
        drop_params: bool = False,
        api_router: bool = False,
    ):
        """Initialize agent runner.

        Args:
            container_name: Name of the running container
            workdir: Working directory inside container
            model: Model to use (e.g., "claude-sonnet-4-5-20250929")
            timeout_ms: Execution timeout in milliseconds
            log_dir: Directory for logs (agent_prompt.txt, agent_stdout.txt, etc.)
            agent_name: Agent framework name (e.g., "claude-code")
            reasoning_effort: Reasoning effort level for Codex ("low", "medium", "high")
            use_sdk: For OpenHands, use SDK mode instead of CLI mode
            include_directories: Extra directories for agent (e.g., ["/e2e_workspace"])
            drop_params: Deprecated, use api_router instead.
            api_router: If True, don't pass *_BASE_URL env vars to docker exec
                (rely on the in-container router set up by ContainerSetup).
        """
        self.container_name = container_name
        self.workdir = workdir
        self.model = model
        self.timeout_ms = timeout_ms
        self.log_dir = Path(log_dir) if log_dir else None
        self.session_id: Optional[str] = None
        self.agent_name = agent_name
        self.api_router = api_router or drop_params

        # Build framework kwargs
        framework_kwargs = {}
        if model:
            framework_kwargs["model"] = model
        if reasoning_effort:
            framework_kwargs["reasoning_effort"] = reasoning_effort
        if use_sdk:
            framework_kwargs["use_sdk"] = use_sdk
        if include_directories:
            framework_kwargs["include_directories"] = include_directories

        # Initialize framework via strategy pattern
        self._framework: AgentFramework = get_agent_framework(agent_name, **framework_kwargs)

        self.logger = logging.getLogger(f"agent.{container_name}")
        self._last_auth_error = False  # Set by _execute_with_streaming on auth failures
        self._last_rate_limit = False  # Set by _execute_with_streaming on rate limit
        self._last_overload = False  # Set on transient server-overload (HTTP 529) responses
        self._rate_limit_reset_seconds: Optional[int] = None  # Seconds until rate limit resets
        # Set when repeated 500 errors suggest a possible model/backend compatibility issue (inferred).
        self._last_model_unavailable = False
        self._last_model_hint: Optional[str] = None  # Human-readable remediation hint
        self._last_invalid_session = False  # Set when resume session ID is invalid
        self._last_fatal_error: Optional[str] = None  # Set on fatal config errors (no retry)

    def _get_exec_env_vars(self) -> list[str]:
        """Return env var args for docker exec.

        When api_router is enabled, strips *_BASE_URL vars so the agent
        uses the in-container router URL from container initialization.
        """
        env_vars = self._framework.get_container_env_vars()
        if not self.api_router:
            return env_vars
        filtered = []
        i = 0
        while i < len(env_vars):
            if env_vars[i] == "-e" and i + 1 < len(env_vars) and "_BASE_URL=" in env_vars[i + 1]:
                i += 2
                continue
            filtered.append(env_vars[i])
            i += 1
        return filtered

    # Auth error patterns from agent CLI/API responses
    _AUTH_ERROR_PATTERNS = [
        "authentication_error",
        "OAuth token has expired",
        "Invalid API key",
        "invalid_api_key",
        "unauthorized",
        "Please run /login",
        "Failed to authenticate",
    ]

    # Rate limit / usage limit patterns
    _RATE_LIMIT_PATTERNS = [
        "you've hit your limit",
        "rate_limit_error",
        "would exceed your account's rate limit",
        "rate limit",
        "usage limit",
        "limit reached",
        "try again in",
        "retry after",
        "too many requests",
        '"code":"429"',
        '"code":429',
    ]

    # Server-side TRANSIENT overload (HTTP 529) indicators — distinct from a
    # genuine quota rate-limit. These should trigger a fast exponential backoff
    # retry rather than the long rate-limit sleep. Tokens are kept STRUCTURED
    # (the overloaded_error token + quoted-JSON 529 forms + the GLM bigmodel
    # Chinese phrases) so a bare "overloaded"/"529" in viewed source/log/prose
    # cannot match and misroute an unrelated failure into 529 backoff (#8).
    _OVERLOAD_PATTERNS = [
        "overloaded_error",
        '"code":"529"',
        '"code":529',
        '"status":529',
        '"http_status":529',
        "访问量过大",
        "访问量较大",
    ]

    # Resume/session invalidation indicators (seen in Gemini CLI resume failures)
    _INVALID_SESSION_PATTERNS = [
        "invalid session identifier",
        "use --list-sessions to see available sessions",
    ]

    # Fatal errors that should cause immediate trial termination (no retry).
    # These indicate model/context configuration issues that cannot be resolved by retrying.
    _FATAL_ERROR_PATTERNS = [
        "extra usage is required for 1m context",
        "long context beta is not yet available",
        "it may not exist or you may not have access to it",
        "string should have at least 1 character",  # empty model name
    ]

    # Known model aliases with recurring 500 patterns in some environments.
    _GEMINI_MODEL_HINTS = {
        "gemini-3-flash": (
            "Possible model/backend compatibility issue inferred for 'gemini-3-flash' from repeated 500 errors. "
            "This may also be a transient backend failure. Retry first; if it persists, "
            "try '--model gemini-3-flash-preview' or '--model gemini-2.5-flash'."
        ),
    }

    def _detect_fatal_error(self, output: str) -> Optional[str]:
        """Check if output contains a fatal configuration error that should stop the trial.

        Returns the matched pattern string if fatal, None otherwise.
        """
        output_lower = output.lower()
        for pattern in self._FATAL_ERROR_PATTERNS:
            if pattern in output_lower:
                return pattern
        return None

    def _detect_auth_error(self, output: str) -> bool:
        """Check if agent output contains authentication error indicators."""
        output_lower = output.lower()
        for pattern in self._AUTH_ERROR_PATTERNS:
            if pattern.lower() in output_lower:
                return True
        return False

    # Agents that use OAuth and may hit external rate limits.
    # API-based agents (e.g. openhands) handle rate limits internally via their SDK.
    _OAUTH_AGENTS = {"claude-code", "codex", "gemini-cli"}

    def _detect_rate_limit(self, output: str) -> bool:
        """Check if agent output contains rate limit / usage limit indicators.

        Only applies to OAuth-based agents (claude-code, codex, gemini-cli).
        API-based agents (e.g. openhands) handle rate limits internally and
        scanning their full stdout would cause false positives from file
        contents the agent viewed.
        """
        if self.agent_name not in self._OAUTH_AGENTS:
            return False
        tail = output[-5000:].lower()
        for pattern in self._RATE_LIMIT_PATTERNS:
            if pattern in tail:
                return True
        return False

    def _detect_overload(self, output: str) -> bool:
        """Check if agent output indicates a transient server overload (HTTP 529).

        Mirrors ``_detect_rate_limit``: only OAuth-based agents (claude-code,
        codex, gemini-cli) are scanned, and only the trailing ``output[-5000:]``
        tail, so source/log text the agent merely *viewed* cannot cause false
        positives. Overload is treated distinctly from a quota rate-limit so the
        recover loop can fast-backoff-retry instead of long-sleeping.
        """
        if self.agent_name not in self._OAUTH_AGENTS:
            return False
        tail = output[-5000:].lower()
        for pattern in self._OVERLOAD_PATTERNS:
            if pattern in tail:
                return True
        return False

    def _detect_invalid_session_error(self, output: str) -> bool:
        """Check if output indicates an invalid/expired local session reference."""
        output_lower = output.lower()
        return any(pattern in output_lower for pattern in self._INVALID_SESSION_PATTERNS)

    def _build_model_hint(self) -> Optional[str]:
        """Return actionable model hint, if known."""
        model_key = (self.model or "").strip().lower()
        return self._GEMINI_MODEL_HINTS.get(model_key)

    def _detect_gemini_model_compatibility_issue(self, output: str) -> bool:
        """Infer a possible Gemini model/backend compatibility issue from error patterns.

        We intentionally keep this narrow:
        - only gemini-cli
        - only known model aliases in _GEMINI_MODEL_HINTS
        - must include 500/Internal Server Error signature
        """
        if self.agent_name != "gemini-cli":
            return False

        model_key = (self.model or "").strip().lower()
        if model_key not in self._GEMINI_MODEL_HINTS:
            return False

        output_lower = output.lower()
        has_500 = any(token in output_lower for token in ["status: 500", '"code":500', '"code": 500'])
        has_internal = "internal server error" in output_lower
        return has_500 and has_internal

    def _classify_failure_signals(self, output: str, context: str) -> None:
        """Classify common failure signals and emit actionable hints."""
        fatal = self._detect_fatal_error(output)
        self._last_fatal_error = fatal
        if fatal:
            self.logger.error("⛔ Fatal configuration error detected in %s output: %s", context, fatal)

        self._last_auth_error = self._detect_auth_error(output)
        if self._last_auth_error:
            self.logger.warning("🔑 Authentication error detected in %s output", context)

        self._last_rate_limit = self._detect_rate_limit(output)
        if self._last_rate_limit:
            self._rate_limit_reset_seconds = self._parse_rate_limit_reset(output)
            self.logger.warning("⏳ Rate limit detected in %s output", context)
        else:
            self._rate_limit_reset_seconds = None

        self._last_overload = self._detect_overload(output)
        if self._last_overload:
            self.logger.warning("🌊 Server overload (HTTP 529) detected in %s output", context)

        self._last_model_unavailable = self._detect_gemini_model_compatibility_issue(output)
        self._last_model_hint = self._build_model_hint() if self._last_model_unavailable else None
        if self._last_model_unavailable:
            hint = self._last_model_hint or (
                "Repeated 500 errors observed. This may be transient; if persistent, try a different model alias."
            )
            self.logger.error(
                "❗ Possible Gemini model/backend compatibility issue inferred for model '%s' from 500 errors. %s",
                self.model,
                hint,
            )

        self._last_invalid_session = self._detect_invalid_session_error(output)
        if self._last_invalid_session:
            self.logger.warning(
                "Detected invalid session identifier in %s output. "
                "This usually means the previous run exited before a resumable session was persisted.",
                context,
            )

    # Maximum rate limit sleep: 2 hours.  Anything larger is almost certainly
    # a false positive (e.g. terminal escape ``[?2004h`` parsed as "2004h").
    _MAX_RATE_LIMIT_WAIT = 2 * 3600

    def _parse_rate_limit_reset(
        self,
        output: str,
        *,
        now_utc: Optional[datetime] = None,
        now_local: Optional[datetime] = None,
    ) -> Optional[int]:
        """Parse reset time from rate limit message, return seconds to wait.

        Handles patterns like:
        - "resets 3am (UTC)"
        - "resets in 5h"
        - "try again in 45m"
        - "retry after 3600"
        - "try again at 10:08 AM"
        """
        # Strip ANSI / terminal escape sequences to prevent false matches
        # (e.g. ``[?2004h`` being parsed as "2004 hours").
        output = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", output)
        output = re.sub(r"\[\?[0-9]+[a-zA-Z]", "", output)

        now_utc = now_utc or datetime.now(timezone.utc)
        now_local = now_local or datetime.now().astimezone()

        def _candidate_wait(reset_at: datetime, now: datetime) -> Optional[int]:
            if reset_at <= now:
                reset_at = reset_at + timedelta(days=1)
            wait = int((reset_at - now).total_seconds()) + 60  # +1min buffer
            if wait <= 0 or wait > self._MAX_RATE_LIMIT_WAIT:
                return None
            return wait

        # Absolute UTC wall-clock format (legacy behavior).
        match = re.search(r"resets\s+(\d{1,2})(am|pm)\s*\(UTC\)", output, re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            ampm = match.group(2).lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

            reset = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0)
            wait = _candidate_wait(reset, now_utc)
            if wait is not None:
                return wait

        # Absolute wall-clock format without a timezone, e.g. "try again at 10:08 AM".
        # These messages are ambiguous across providers. We treat them as "the soonest
        # plausible future wall-clock reset" across UTC and the local timezone, bounded
        # by _MAX_RATE_LIMIT_WAIT. This matches observed OAuth agent messages where the
        # printed clock time behaves like a near-future reset rather than a same-day
        # local wall time many hours away.
        match = re.search(
            r"(?:try again|retry(?:ing)?(?:\s+after)?|wait(?:ing)?)\s+at\s+"
            r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\((UTC|GMT)\))?",
            output,
            re.IGNORECASE,
        )
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            ampm = match.group(3).lower()
            zone = (match.group(4) or "").upper()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

            if zone in {"UTC", "GMT"}:
                reset = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
                wait = _candidate_wait(reset, now_utc)
                if wait is not None:
                    return wait
            else:
                candidates = []
                reset_utc = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
                wait_utc = _candidate_wait(reset_utc, now_utc)
                if wait_utc is not None:
                    candidates.append(wait_utc)

                reset_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                wait_local = _candidate_wait(reset_local, now_local)
                if wait_local is not None:
                    candidates.append(wait_local)

                if candidates:
                    return min(candidates)

        # Relative duration format, e.g. "resets in 5h", "try again in 45m".
        duration_context = re.search(
            r"(?:resets?|try again|retry(?:ing)?(?:\s+after)?|wait(?:ing)?)\D{0,20}"
            r"((?:\d+\s*(?:hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\s*){1,3})",
            output,
            re.IGNORECASE,
        )
        if duration_context:
            duration_text = duration_context.group(1)
            wait_seconds = 0
            for amount, unit in re.findall(
                r"(\d+)\s*(hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)",
                duration_text,
                re.IGNORECASE,
            ):
                value = int(amount)
                unit_lower = unit.lower()
                if unit_lower.startswith(("h", "hr")):
                    wait_seconds += value * 3600
                elif unit_lower.startswith(("m", "min")):
                    wait_seconds += value * 60
                else:
                    wait_seconds += value
            if wait_seconds > 0:
                return min(wait_seconds + 60, self._MAX_RATE_LIMIT_WAIT)

        # Retry-After style: "retry after 3600" (seconds).
        retry_after = re.search(r"retry[-_ ]?after\D{0,5}(\d{1,6})\b", output, re.IGNORECASE)
        if retry_after:
            return min(int(retry_after.group(1)) + 60, self._MAX_RATE_LIMIT_WAIT)

        return None

    def refresh_container_credentials(self) -> bool:
        """Refresh credentials for the current agent by copying host files into container."""
        if self.agent_name == "claude-code":
            return self._refresh_claude_credentials()
        if self.agent_name == "codex":
            return self._refresh_codex_credentials()

        self.logger.info("Credential refresh is not supported for agent '%s'", self.agent_name)
        return False

    def _copy_host_file_to_container(self, host_path: Path, container_path: str, chmod_mode: str = "600") -> bool:
        """Copy a host file into container and set fakeroot ownership."""
        container_dir = str(Path(container_path).parent)
        mkdir_result = subprocess.run(
            ["docker", "exec", self.container_name, "mkdir", "-p", container_dir],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if mkdir_result.returncode != 0:
            self.logger.error("Failed to create container directory %s: %s", container_dir, mkdir_result.stderr.strip())
            return False

        copy_result = subprocess.run(
            ["docker", "cp", str(host_path), f"{self.container_name}:{container_path}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if copy_result.returncode != 0:
            self.logger.error("docker cp failed for %s: %s", host_path, copy_result.stderr.strip())
            return False

        subprocess.run(
            ["docker", "exec", self.container_name, "chown", "fakeroot:fakeroot", container_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        subprocess.run(
            ["docker", "exec", self.container_name, "chmod", chmod_mode, container_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True

    def _refresh_claude_credentials(self) -> bool:
        """Copy fresh Claude OAuth credentials from host into container."""
        host_creds = Path.home() / ".claude" / ".credentials.json"
        if not host_creds.exists():
            self.logger.warning("No host credentials file found at %s", host_creds)
            return False

        try:
            creds_data = json.loads(host_creds.read_text(encoding="utf-8"))
            oauth = creds_data.get("claudeAiOauth", {})
            expires_at = oauth.get("expiresAt", 0)
            import time

            now_ms = int(time.time() * 1000)
            if expires_at and expires_at < now_ms:
                self.logger.warning(
                    "Host credentials are also expired (expiresAt=%s, now=%s)",
                    expires_at,
                    now_ms,
                )
                return False

            if not self._copy_host_file_to_container(host_creds, "/home/fakeroot/.claude/.credentials.json"):
                return False

            if expires_at:
                remaining_h = round((expires_at - now_ms) / 3600_000, 1)
                self.logger.info("🔑 Refreshed Claude credentials from host (valid for %.1fh)", remaining_h)
                self._append_session_history(
                    {"event": "credentials_refreshed", "provider": "claude", "expires_in_hours": remaining_h}
                )
            else:
                self.logger.info("🔑 Refreshed Claude credentials from host")
                self._append_session_history({"event": "credentials_refreshed", "provider": "claude"})
            return True
        except Exception as e:
            self.logger.error("Failed to refresh Claude credentials: %s", e)
            return False

    def _refresh_codex_credentials(self) -> bool:
        """Copy fresh Codex OAuth files from host into container."""
        host_codex_dir = Path.home() / ".codex"
        host_auth = host_codex_dir / "auth.json"
        host_config = host_codex_dir / "config.toml"

        if not host_auth.exists():
            self.logger.warning("No host Codex auth file found at %s", host_auth)
            return False

        try:
            auth_data = json.loads(host_auth.read_text(encoding="utf-8"))
            if not isinstance(auth_data, dict):
                self.logger.error("Host Codex auth file is not a JSON object: %s", host_auth)
                return False
            if "tokens" not in auth_data and "OPENAI_API_KEY" not in auth_data:
                self.logger.warning("Host Codex auth file has unexpected format: %s", host_auth)

            if not self._copy_host_file_to_container(host_auth, "/home/fakeroot/.codex/auth.json"):
                return False

            if host_config.exists():
                self._copy_host_file_to_container(host_config, "/home/fakeroot/.codex/config.toml")

            self.logger.info("🔑 Refreshed Codex OAuth credentials from host")
            self._append_session_history({"event": "credentials_refreshed", "provider": "codex"})
            return True
        except Exception as e:
            self.logger.error("Failed to refresh Codex credentials: %s", e)
            return False

    def _run_command(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run command with logging."""
        self.logger.debug(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}\nError: {result.stderr}")

        return result

    def _append_session_history(self, event: dict) -> None:
        """Append a single JSON event to session_history.jsonl (best-effort)."""
        if not self.log_dir:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            history_path = self.log_dir / "session_history.jsonl"
            payload = {
                "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "agent": self.agent_name,
                **event,
            }
            with open(history_path, "a", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            # Never break agent execution due to telemetry/logging issues
            self.logger.debug(f"Failed to append session history: {e}")

    def run(self, prompt: str, session_id: Optional[str] = None) -> tuple[bool, str]:
        """Execute agent with given prompt.

        Args:
            prompt: The prompt to send to the agent
            session_id: Optional session ID to use (creates new if not provided)

        Returns:
            Tuple of (success, session_id)
        """
        # Ensure log directory exists
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        # Generate or use provided session ID
        self.session_id = session_id or str(uuid.uuid4())

        # Save prompt and session ID
        if self.log_dir:
            prompt_file = self.log_dir / "agent_prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            self.logger.info(f"Prompt saved to: {prompt_file}")

            session_id_file = self.log_dir / "session_id.txt"
            session_id_file.write_text(self.session_id, encoding="utf-8")

        # Copy prompt to container
        container_prompt_path = "/tmp/agent_prompt.txt"
        if self.log_dir:
            prompt_file = self.log_dir / "agent_prompt.txt"
        else:
            # Write to temp file if no log_dir
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(prompt)
                prompt_file = Path(f.name)

        try:
            copy_cmd = ["docker", "cp", str(prompt_file), f"{self.container_name}:{container_prompt_path}"]
            self._run_command(copy_cmd, check=True)
        except RuntimeError as e:
            self.logger.error(f"Failed to copy prompt: {e}")
            return False, self.session_id

        # Build agent run command via framework
        agent_cmd = self._framework.build_run_command(
            model=self.model,
            session_id=self.session_id,
            prompt_path=container_prompt_path,
        )

        docker_exec_cmd = [
            "docker",
            "exec",
            "--user",
            "fakeroot",
            "-e",
            "HOME=/home/fakeroot",
        ]

        # Add framework-specific environment variables (e.g., CODEX_API_KEY)
        docker_exec_cmd.extend(self._get_exec_env_vars())

        docker_exec_cmd.extend(
            [
                "-w",
                self.workdir,
                self.container_name,
                "/bin/sh",
                "-c",
                agent_cmd,
            ]
        )

        self.logger.info(f"Executing agent (Session: {self.session_id[:8]}...)...")
        self.logger.info(f"Timeout: {self.timeout_ms / 1000 / 60:.1f} minutes")

        self._append_session_history({"event": "agent_exec_start", "session_id": self.session_id})
        try:
            success = self._execute_with_streaming(docker_exec_cmd, container_prompt_path)

            # For Codex: extract thread_id from stdout and update session_id
            self._update_session_id_from_output()

            self._append_session_history({"event": "agent_exec_end", "session_id": self.session_id, "success": success})
            return success, self.session_id
        except Exception as e:
            self.logger.error(f"Failed to execute agent: {e}")
            self._last_auth_error = False
            self._last_rate_limit = False
            self._last_overload = False
            self._rate_limit_reset_seconds = None
            self._last_model_unavailable = False
            self._last_model_hint = None
            self._last_invalid_session = False
            self._last_fatal_error = None
            self._append_session_history({"event": "agent_exec_end", "session_id": self.session_id, "success": False})
            self._run_command(["docker", "exec", self.container_name, "rm", "-f", container_prompt_path], check=False)
            return False, self.session_id

    def _update_session_id_from_output(self) -> None:
        """Update session_id from agent output if the framework provides extraction.

        For Codex, this extracts thread_id from stdout JSON or container rollout files.
        For Gemini, this extracts session_id from stdout JSON or container session files.
        For Claude Code, the session_id is passed in, so no update needed.

        Extraction priority:
        1. Container files (authoritative, per-session)
        2. stdout parsing (fallback, may contain stale IDs from previous runs)
        3. stderr parsing (last resort, for crash scenarios)

        Cross-validates container vs stdout when both are available.
        """
        if not self.log_dir:
            return

        container_id = None
        stdout_id = None

        # 1. Try container file extraction (authoritative)
        try:
            container_id = self._framework.extract_session_id_from_container(self.container_name)
            if container_id:
                self.logger.info(f"Extracted session_id from container files: {container_id[:8]}...")
        except Exception as e:
            self.logger.warning(f"Container session extraction failed: {e}")

        # 2. Try stdout/stderr extraction (fallback)
        if hasattr(self._framework, "extract_thread_id"):
            extract_fn = self._framework.extract_thread_id
            id_label = "thread_id"
        elif hasattr(self._framework, "extract_session_id"):
            extract_fn = self._framework.extract_session_id
            id_label = "session_id"
        else:
            extract_fn = None
            id_label = None

        if extract_fn:
            # Try stdout first
            stdout_file = self.log_dir / "agent_stdout.txt"
            if stdout_file.exists():
                stdout_content = stdout_file.read_text(encoding="utf-8")
                stdout_id = extract_fn(stdout_content)
                if stdout_id:
                    self.logger.info(f"Extracted {id_label} from stdout: {stdout_id[:8]}...")

            # Fallback to stderr if stdout extraction failed
            if not stdout_id:
                stderr_file = self.log_dir / "agent_stderr.txt"
                if stderr_file.exists():
                    stderr_content = stderr_file.read_text(encoding="utf-8")
                    stdout_id = extract_fn(stderr_content)
                    if stdout_id:
                        self.logger.info(f"Extracted {id_label} from stderr (fallback): {stdout_id[:8]}...")

        # 3. Cross-validate and select
        if container_id and stdout_id and container_id != stdout_id:
            self.logger.warning(
                f"Session ID mismatch: container={container_id[:8]}... vs stdout={stdout_id[:8]}... "
                f"Using container value (authoritative)"
            )

        extracted_id = container_id or stdout_id

        if extracted_id:
            old_id = self.session_id
            self.session_id = extracted_id
            if old_id and extracted_id != old_id:
                self._append_session_history(
                    {"event": "extracted", "old_session_id": old_id, "new_session_id": extracted_id}
                )
            # Update session_id.txt
            session_id_file = self.log_dir / "session_id.txt"
            session_id_file.write_text(self.session_id, encoding="utf-8")

    def _execute_with_streaming(self, docker_exec_cmd: list[str], container_prompt_path: str) -> bool:
        """Execute command with streaming stdout/stderr to log files.

        Args:
            docker_exec_cmd: Docker exec command to run
            container_prompt_path: Path to prompt file in container (for cleanup)

        Returns:
            True if successful
        """
        if not self.log_dir:
            # Simple execution without streaming
            result = subprocess.run(
                docker_exec_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_ms / 1000.0,
            )
            self._run_command(["docker", "exec", self.container_name, "rm", "-f", container_prompt_path], check=False)
            return result.returncode == 0

        stdout_file = open(self.log_dir / "agent_stdout.txt", "a", encoding="utf-8")
        stderr_file = open(self.log_dir / "agent_stderr.txt", "a", encoding="utf-8")

        try:
            proc = subprocess.Popen(
                docker_exec_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            stdout_lines = queue.Queue()
            stderr_lines = queue.Queue()

            def read_output(pipe, file_handle, log_prefix, line_queue):
                try:
                    for line in iter(pipe.readline, ""):
                        if line:
                            file_handle.write(line)
                            file_handle.flush()
                            self.logger.info(f"{log_prefix}{line.rstrip()}")
                            line_queue.put(line)
                finally:
                    pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(proc.stdout, stdout_file, "[stdout] ", stdout_lines)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(proc.stderr, stderr_file, "[stderr] ", stderr_lines)
            )

            stdout_thread.start()
            stderr_thread.start()

            try:
                proc.wait(timeout=self.timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.logger.error(f"Agent execution timed out after {self.timeout_ms / 1000 / 60:.1f} minutes")
                self._run_command(
                    ["docker", "exec", self.container_name, "rm", "-f", container_prompt_path], check=False
                )
                return False

            stdout_thread.join()
            stderr_thread.join()

            stdout = "".join(list(stdout_lines.queue))
            stderr = "".join(list(stderr_lines.queue))

        finally:
            stdout_file.close()
            stderr_file.close()

        # Cleanup prompt file
        self._run_command(["docker", "exec", self.container_name, "rm", "-f", container_prompt_path], check=False)

        if proc.returncode != 0:
            self.logger.error(f"Agent exited with code {proc.returncode}")
            self.logger.error(f"Check logs in: {self.log_dir}")
            output_for_detection = f"{stdout}\n{stderr}"
            self._classify_failure_signals(output_for_detection, "agent")
            return False

        self._last_auth_error = False
        self._last_rate_limit = False
        self._last_overload = False
        self._rate_limit_reset_seconds = None
        self._last_model_unavailable = False
        self._last_model_hint = None
        self._last_invalid_session = False
        self._last_fatal_error = None
        self.logger.info("Agent execution completed successfully")
        return True

    def resume_session(self, session_id: str, message: str, timeout_ms: Optional[int] = None) -> bool:
        """Resume an existing session with a message.

        Args:
            session_id: Session ID to resume
            message: Message to send
            timeout_ms: Optional timeout override for this resume call

        Returns:
            True if successful
        """
        effective_timeout_ms = timeout_ms if timeout_ms is not None else self.timeout_ms
        self.logger.info(f"Resuming session {session_id[:8]}...")
        self.logger.info(f"Resume timeout: {effective_timeout_ms / 1000 / 60:.1f} minutes")
        self._append_session_history({"event": "resume_attempt", "session_id": session_id})
        self._last_auth_error = False
        self._last_rate_limit = False
        self._last_overload = False
        self._rate_limit_reset_seconds = None
        self._last_model_unavailable = False
        self._last_model_hint = None
        self._last_invalid_session = False
        self._last_fatal_error = None

        # Write message to temp file
        if self.log_dir:
            message_file = self.log_dir / "resume_message.txt"
        else:
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write(message)
                message_file = Path(f.name)

        message_file.write_text(message, encoding="utf-8")

        # Copy to container
        container_message_path = "/tmp/resume_message.txt"
        try:
            copy_cmd = ["docker", "cp", str(message_file), f"{self.container_name}:{container_message_path}"]
            self._run_command(copy_cmd, check=True)
        except RuntimeError as e:
            self.logger.error(f"Failed to copy resume message: {e}")
            return False

        # Build agent resume command via framework
        agent_cmd = self._framework.build_resume_command(
            model=self.model,
            session_id=session_id,
            message_path=container_message_path,
        )

        docker_exec_cmd = [
            "docker",
            "exec",
            "--user",
            "fakeroot",
            "-e",
            "HOME=/home/fakeroot",
        ]

        # Add framework-specific environment variables (e.g., CODEX_API_KEY)
        docker_exec_cmd.extend(self._get_exec_env_vars())

        docker_exec_cmd.extend(
            [
                "-w",
                self.workdir,
                self.container_name,
                "/bin/sh",
                "-c",
                agent_cmd,
            ]
        )

        self._append_session_history({"event": "agent_exec_start", "session_id": session_id})
        try:
            result = subprocess.run(
                docker_exec_cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout_ms / 1000.0,
            )

            # Save output to files
            if self.log_dir:
                stdout_file = self.log_dir / "agent_stdout.txt"
                stderr_file = self.log_dir / "agent_stderr.txt"
                with open(stdout_file, "a", encoding="utf-8") as f:
                    f.write(result.stdout)
                with open(stderr_file, "a", encoding="utf-8") as f:
                    f.write(result.stderr)

            # Update session_id from output (agent may have created a new session)
            self._update_session_id_from_output()

            # Cleanup
            self._run_command(["docker", "exec", self.container_name, "rm", "-f", container_message_path], check=False)

            if result.returncode != 0:
                self.logger.error(f"Resume failed with code {result.returncode}")
                self.logger.error(f"stderr: {result.stderr}")
                output_for_detection = f"{result.stdout or ''}\n{result.stderr or ''}"
                self._classify_failure_signals(output_for_detection, "resume")
                self._append_session_history({"event": "agent_exec_end", "session_id": session_id, "success": False})
                self._append_session_history(
                    {
                        "event": "resume_failure",
                        "session_id": session_id,
                        "returncode": result.returncode,
                        "stderr_tail": (result.stderr or "")[-500:],
                    }
                )
                return False

            self.logger.info("Session resumed successfully")
            self._append_session_history({"event": "agent_exec_end", "session_id": session_id, "success": True})
            self._append_session_history({"event": "resume_success", "session_id": session_id})
            return True

        except subprocess.TimeoutExpired:
            timeout_minutes = effective_timeout_ms / 1000 / 60
            self.logger.error(f"Resume timed out after {timeout_minutes:.1f} minutes")
            self._last_auth_error = False
            self._last_rate_limit = False
            self._last_overload = False
            self._rate_limit_reset_seconds = None
            self._last_model_unavailable = False
            self._last_model_hint = None
            self._last_invalid_session = False
            self._last_fatal_error = None
            self._append_session_history({"event": "agent_exec_end", "session_id": session_id, "success": False})
            self._run_command(["docker", "exec", self.container_name, "rm", "-f", container_message_path], check=False)
            self._append_session_history(
                {
                    "event": "resume_failure",
                    "session_id": session_id,
                    "reason": "timeout",
                    "timeout_ms": effective_timeout_ms,
                }
            )
            return False
        except Exception as e:
            self.logger.error(f"Failed to resume session: {e}")
            self._last_auth_error = False
            self._last_rate_limit = False
            self._last_overload = False
            self._rate_limit_reset_seconds = None
            self._last_model_unavailable = False
            self._last_model_hint = None
            self._last_invalid_session = False
            self._last_fatal_error = None
            self._append_session_history({"event": "agent_exec_end", "session_id": session_id, "success": False})
            self._append_session_history({"event": "resume_failure", "session_id": session_id, "reason": str(e)})
            return False

    def extract_trace(self, output_dir: Optional[Path] = None) -> bool:
        """Extract agent trace using agent-specific tools.

        Delegates to the log_parser for the agent framework.

        Args:
            output_dir: Directory to save trace files (defaults to log_dir)

        Returns:
            True if successful
        """
        target_dir = output_dir or self.log_dir
        if not target_dir:
            self.logger.warning("No output directory for trace extraction")
            return False

        self.logger.info("Extracting agent trace...")

        try:
            parser = get_parser(self.agent_name)
            return parser.extract_trace(self.container_name, target_dir)
        except Exception as e:
            self.logger.warning(f"Failed to extract trace: {e}")
            return False


class E2EAgentRunner(AgentRunner):
    """Agent runner for E2E continuous task queue mode.

    Extends AgentRunner with E2E-specific functionality:
    - Prompt template loading from files
    - Session persistence across runs
    - Untagged commit detection
    - Recovery message handling
    """

    def __init__(
        self,
        container_name: str,
        output_dir: str,
        workdir: str = "/testbed",
        repo_src_dirs: Optional[list[str]] = None,
        agent_name: str = "claude-code",
        model: str = "claude-sonnet-4-5-20250929",
        timeout_ms: int = 3600_000,  # 1 hour default for E2E
        prompt_version: str = "v1",
        reasoning_effort: Optional[str] = None,
        drop_params: bool = False,
        api_router: bool = False,
    ):
        """Initialize E2E agent runner.

        Args:
            container_name: Name of the running container
            output_dir: Output directory for logs
            workdir: Working directory inside container
            repo_src_dirs: List of source code directories
            agent_name: Agent framework name (e.g., "claude-code")
            model: Model to use (e.g., "claude-sonnet-4-5-20250929")
            timeout_ms: Execution timeout in milliseconds
            prompt_version: Prompt template version (e.g., "v1")
            reasoning_effort: Reasoning effort level for GPT-5 models
            drop_params: Deprecated, use api_router instead.
            api_router: Deploy claude-code-router-py for Anthropic-to-OpenAI translation.
        """
        self.output_dir = Path(output_dir) if output_dir else None
        # Use output_dir directly as log_dir (consistent with milestone trial structure)
        log_dir = self.output_dir

        super().__init__(
            container_name=container_name,
            workdir=workdir,
            model=model,
            timeout_ms=timeout_ms,
            log_dir=log_dir,
            agent_name=agent_name,  # Pass framework name to parent
            reasoning_effort=reasoning_effort,
            include_directories=["/e2e_workspace"],
            api_router=api_router or drop_params,
        )

        self.repo_src_dirs = repo_src_dirs
        # Note: self.agent_name is now set by parent class
        self.prompt_version = prompt_version

        # Session file (persistent across runs)
        if self.output_dir:
            self.session_file = self.output_dir / ".agent_session_id"
        else:
            self.session_file = None

        # Ensure directories exist
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def _load_prompt_template(self) -> str:
        """Load prompt template from file."""
        template_file = PROMPT_DIR / f"{self.prompt_version}.md"

        if not template_file.exists():
            raise FileNotFoundError(
                f"Prompt template not found: {template_file}\n" f"Available templates: {list(PROMPT_DIR.glob('*.md'))}"
            )

        return template_file.read_text(encoding="utf-8")

    def generate_prompt(self) -> str:
        """Generate E2E continuous mode prompt.

        Loads the template and fills in variables.
        """
        template = self._load_prompt_template()

        # Format source directories
        src_dirs_str = ", ".join(f"`{d}`" for d in self.repo_src_dirs)

        # Replace template variables
        prompt = template.replace("{src_dirs}", src_dirs_str)

        return prompt

    def _get_or_create_session_id(self) -> str:
        """Get existing session ID or create new one."""
        if self.session_file and self.session_file.exists():
            session_id = self.session_file.read_text().strip()
            self.logger.info(f"Resuming existing session: {session_id[:8]}...")
            self._append_session_history({"event": "loaded", "session_id": session_id, "path": str(self.session_file)})
            return session_id

        session_id = str(uuid.uuid4())
        if self.session_file:
            self.session_file.write_text(session_id)
        self.logger.info(f"Created new session: {session_id[:8]}...")
        self._append_session_history(
            {
                "event": "created",
                "session_id": session_id,
                "path": str(self.session_file) if self.session_file else None,
            }
        )
        return session_id

    def invalidate_persistent_session(self, reason: str = "unknown") -> Optional[str]:
        """Delete the persistent session file so the next run creates a new session."""
        old_id = None
        if self.session_file and self.session_file.exists():
            try:
                old_id = self.session_file.read_text().strip()
            except Exception:
                old_id = None
            try:
                self.session_file.unlink()
            except Exception as e:
                self.logger.warning(f"Failed to delete session file {self.session_file}: {e}")
        self.session_id = None
        self._append_session_history({"event": "fallback_new_session", "old_session_id": old_id, "reason": reason})
        return old_id

    def verify_container(self) -> bool:
        """Verify the container is running."""
        self.logger.info("=" * 70)
        self.logger.info(f"Verifying container: {self.container_name}")
        self.logger.info("=" * 70)

        # Check container exists
        result = self._run_command(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{self.container_name}$"],
            check=False,
        )
        if self.container_name not in result.stdout:
            self.logger.error(f"Container '{self.container_name}' not found")
            return False

        # Check if running
        result = self._run_command(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name],
            check=False,
        )
        if result.stdout.strip() != "true":
            self.logger.info("Container exists but stopped. Starting...")
            self._run_command(["docker", "start", self.container_name])

        self.logger.info(f"Container '{self.container_name}' is running.")
        return True

    def run(self, prompt: Optional[str] = None, session_id: Optional[str] = None) -> bool:
        """Run the E2E agent.

        Args:
            prompt: Optional prompt (generates from template if not provided)
            session_id: Optional session ID (uses persistent session if not provided)

        Returns:
            True if successful, False otherwise
        """
        self.logger.info("\n" + "=" * 70)
        self.logger.info("E2E Agent Runner - Continuous Task Queue Mode")
        self.logger.info(f"Prompt Version: {self.prompt_version}")
        self.logger.info("=" * 70)

        # Verify container
        if not self.verify_container():
            return False

        # Generate prompt if not provided
        if prompt is None:
            prompt = self.generate_prompt()

        # Get/create session ID
        if session_id is None:
            session_id = self._get_or_create_session_id()

        # Call base class run
        success, self.session_id = super().run(prompt, session_id)

        # Update persistent session file if session_id changed (e.g., Codex thread_id)
        if self.session_file and self.session_id != session_id:
            self.session_file.write_text(self.session_id)
            self.logger.info(f"Updated persistent session ID: {self.session_id[:8]}...")

        if success:
            self._extract_agent_trace()

        return success

    def _generate_search_patterns(self, milestone_id: str) -> list[str]:
        """Generate search patterns for fuzzy matching a milestone ID in commit messages."""
        patterns = []
        seen = set()

        def add_pattern(p: str) -> None:
            p = p.lower().strip()
            skip_words = {"milestone", "seed", "gap", "merged", "sub", "all", "maintenance", "misc"}
            if len(p) >= 3 and p not in skip_words and p not in seen:
                seen.add(p)
                patterns.append(p)

        add_pattern(milestone_id)
        parts = re.split(r"[_\-.]", milestone_id)
        for part in parts:
            add_pattern(part)
        for i in range(len(parts) - 1):
            combo = f"{parts[i]}_{parts[i + 1]}"
            add_pattern(combo)
        no_sep = "".join(parts)
        if len(no_sep) >= 4:
            add_pattern(no_sep)

        return patterns

    def _detect_untagged_commits(self) -> dict[str, list[str]]:
        """Detect commits related to incomplete milestones that don't have submission tags."""
        try:
            # Get incomplete milestones from TASK_QUEUE.md
            queue_result = subprocess.run(
                ["docker", "exec", self.container_name, "cat", "/e2e_workspace/TASK_QUEUE.md"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            incomplete_milestones = []
            if queue_result.returncode == 0 and queue_result.stdout:
                for line in queue_result.stdout.split("\n"):
                    match = re.match(r"^-\s*(\S+):", line)
                    if match:
                        incomplete_milestones.append(match.group(1))

            if not incomplete_milestones:
                return {}

            # Get existing tags
            tag_result = subprocess.run(
                ["docker", "exec", self.container_name, "git", "-C", "/testbed", "tag", "-l", "agent-impl-*"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            existing_tags = set(tag_result.stdout.strip().split("\n")) if tag_result.stdout.strip() else set()

            milestones_without_tags = [mid for mid in incomplete_milestones if f"agent-impl-{mid}" not in existing_tags]

            if not milestones_without_tags:
                return {}

            # Get recent commits
            log_result = subprocess.run(
                ["docker", "exec", self.container_name, "git", "-C", "/testbed", "log", "--oneline", "-50"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if not log_result.stdout:
                return {}

            commits = log_result.stdout.strip().split("\n")

            # Find related commits
            milestone_commits: dict[str, list[str]] = {}
            for mid in milestones_without_tags:
                related_commits = []
                patterns = self._generate_search_patterns(mid)

                for commit in commits:
                    commit_lower = commit.lower()
                    for pattern in patterns:
                        if pattern in commit_lower:
                            if commit not in related_commits:
                                related_commits.append(commit)
                            break

                if related_commits:
                    milestone_commits[mid] = related_commits

            return milestone_commits

        except Exception as e:
            self.logger.warning(f"Failed to detect untagged commits: {e}")
            return {}

    def send_recover_message(self, has_new_tasks: bool = True, timeout_ms: Optional[int] = None) -> bool:
        """Send a recover message to wake up the agent.

        Uses the same session ID to continue the existing conversation.
        Detects untagged commits and reminds agent to create tags if needed.

        Args:
            has_new_tasks: Whether new tasks have been released in the DAG.
            timeout_ms: Optional timeout override for this recover/resume call.

        Returns:
            True if successful, False otherwise
        """
        # Load session_id from persistent file if not already set
        if not self.session_id and self.session_file and self.session_file.exists():
            self.session_id = self.session_file.read_text().strip()
            self.logger.info(f"Loaded session ID from file: {self.session_id[:8]}...")
            self._append_session_history(
                {"event": "loaded", "session_id": self.session_id, "path": str(self.session_file)}
            )

        if not self.session_id:
            self.logger.error("No session ID - cannot send recover message")
            return False

        self.logger.info(f"Sending recover message to session {self.session_id[:8]}...")
        self.logger.info(f"  has_new_tasks={has_new_tasks}")

        # Detect untagged commits
        untagged_milestones = self._detect_untagged_commits()
        if untagged_milestones:
            self.logger.warning(f"Detected untagged milestone commits: {list(untagged_milestones.keys())}")

        # Build tag reminder
        tag_reminder = ""
        if untagged_milestones:
            tag_reminder = "\n\n## IMPORTANT: Untagged Commits Detected\n\n"
            tag_reminder += "The following milestones are still marked as **incomplete**, "
            tag_reminder += "but I found commits that appear related:\n\n"

            for mid, commits in untagged_milestones.items():
                tag_reminder += f"### `{mid}` - Missing tag: `agent-impl-{mid}`\n\n"
                tag_reminder += "Related commits found:\n"
                for commit in commits[:5]:
                    tag_reminder += f"- `{commit}`\n"
                if len(commits) > 5:
                    tag_reminder += f"- ... and {len(commits) - 5} more\n"
                tag_reminder += "\n"

            tag_reminder += "**Action Required**: Create the submission tags:\n\n```bash\n"
            for mid in untagged_milestones.keys():
                tag_reminder += f"git tag agent-impl-{mid}\n"
            tag_reminder += "```\n\n"
            tag_reminder += "Without the tag, the task will NOT be marked as complete.\n"

        # Build prompt
        if has_new_tasks:
            header_title = "Task Queue Update - New Tasks Available"
            header_content = "New tasks have been added to the task queue. Please resume your work."
        else:
            if tag_reminder:
                header_title = "Task Continuation Required"
                header_content = "Your current tasks are not yet complete. Please continue working on them."
            else:
                self.logger.info("No new tasks and no untagged commits - nothing to send")
                return False

        # Load recover prompt template
        recover_prompt_file = PROMPT_DIR / f"{self.prompt_version}_recover.md.tmpl"
        if not recover_prompt_file.exists():
            recover_prompt_file = PROMPT_DIR / f"{self.prompt_version}_recover.md"
            if not recover_prompt_file.exists():
                self.logger.error(f"Recover prompt not found: {recover_prompt_file}")
                return False
            final_prompt = recover_prompt_file.read_text(encoding="utf-8")
            if tag_reminder:
                final_prompt += tag_reminder
        else:
            template = recover_prompt_file.read_text(encoding="utf-8")
            final_prompt = template.replace("{{header_title}}", header_title)
            final_prompt = final_prompt.replace("{{header_content}}", header_content)
            final_prompt = final_prompt.replace("{{tag_reminder}}", tag_reminder)

        return self.resume_session(self.session_id, final_prompt, timeout_ms=timeout_ms)

    def _extract_agent_trace(self) -> bool:
        """Extract agent trace using claude-extract inside container."""
        return self.extract_trace(self.log_dir)


# Keep backward compatibility alias
PersistentAgentRunner = E2EAgentRunner
