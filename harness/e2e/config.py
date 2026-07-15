from pathlib import Path
from typing import Dict, List, Optional
import yaml
import logging

logger = logging.getLogger("e2e.config")

# =============================================================================
# Unified Tool Category Mapping
# =============================================================================
# Maps raw tool names from each agent framework to a unified category.
#
# Unified categories:
#   read     - Read file contents
#   edit     - Targeted text replacement within a file
#   write    - Create or overwrite an entire file
#   shell    - Execute shell commands
#   search   - Search/find files or content (grep, glob, ls)
#   plan     - Task planning / TODO tracking
#   subagent - Sub-agent orchestration
#
# Notes:
#   - codex only exposes shell_command; all file ops go through shell commands,
#     so they cannot be disaggregated into read/edit/write/search.
#   - gemini-cli renamed grep_search -> search_file_content across versions;
#     both are mapped to "search".

TOOL_CATEGORY_MAP: Dict[str, Dict[str, str]] = {
    "claude-code": {
        "Read": "read",
        "Edit": "edit",
        "Write": "write",
        "Bash": "shell",
        "Grep": "search",
        "Glob": "search",
        "TodoWrite": "plan",
        "Task": "subagent",
        "TaskOutput": "subagent",
    },
    "codex": {
        "shell_command": "shell",
        "update_plan": "plan",
    },
    "gemini-cli": {
        "read_file": "read",
        "replace": "edit",
        "file_editor": "edit",
        "write_file": "write",
        "run_shell_command": "shell",
        "terminal": "shell",
        "search_file_content": "search",
        "grep_search": "search",
        "list_directory": "search",
        "glob": "search",
        "task_tracker": "plan",
    },
}

UNIFIED_TOOL_CATEGORIES: List[str] = [
    "read",
    "edit",
    "write",
    "shell",
    "search",
    "plan",
    "subagent",
]


def map_tool_breakdown(agent_name: str, raw_breakdown: Dict[str, int]) -> Dict[str, int]:
    """Aggregate a raw tool_call_breakdown into unified categories.

    Args:
        agent_name: Agent framework name (e.g. "claude-code", "codex", "gemini-cli").
        raw_breakdown: {raw_tool_name: count, ...} from agent_stats.json.

    Returns:
        {unified_category: count, ...} with an extra "other" key for unmapped tools.
    """
    mapping = TOOL_CATEGORY_MAP.get(agent_name, {})
    result: Dict[str, int] = {cat: 0 for cat in UNIFIED_TOOL_CATEGORIES}
    result["other"] = 0

    for tool_name, count in raw_breakdown.items():
        category = mapping.get(tool_name)
        if category:
            result[category] += count
        else:
            result["other"] += count
            logger.debug(f"Unmapped tool '{tool_name}' for agent '{agent_name}', counted as 'other'")

    return result


DEFAULT_CONFIG = {
    "dag_unlock": {
        "early_unblock": True,
        "ignore_weak_dependencies": False,
        "strict_threshold": {
            "fail_to_pass": 1.0,
            "pass_to_pass": 1.0,
            "none_to_pass": 1.0,
        },
    },
    "evaluation": {
        "include_new_tests": False,
        "build_failure_fail_closed": False,
    },
    "retry_and_timing": {
        "debounce_seconds": 120,
        "max_debounce_wait": 360,
        "max_retries": 2,
        "evaluation_timeout": 3600,
        "max_no_progress_attempts": 3,
        "recovery_wait_seconds": 60,
        "recover_message_timeout_seconds": 18000,
        # After THREE worker-level no-progress exits (3 consecutive failed
        # resumes), rotate the session on the next --resume-trial. The inner
        # loop already retries max_no_progress_attempts (=3) times with the
        # same session jsonl, so each outer failure represents 3 failed
        # inner attempts. 3 outer = 9 total inner attempts — errs on the
        # side of preserving session memory to maximize agent context
        # continuity across transient stops. Once 9 attempts on the same
        # jsonl haven't produced progress, rotating is the safer bet
        # (covers bloated-context cases like kimi-k2.6 on nushell where
        # the jsonl grew past the model's 200K window).
        "resume_no_progress_retry_limit": 3,
        "resume_no_progress_policy": "start_new_session",
        "resume_subprocess_retry_limit": 3,
    },
}


class E2EConfig:
    def __init__(self, config_path: Path = None):
        self.config = self._deep_copy(DEFAULT_CONFIG)
        if config_path and config_path.exists():
            self._load_config(config_path)
        elif config_path:
            logger.warning(f"Config file {config_path} not found, using defaults.")

    def _deep_copy(self, d: Dict) -> Dict:
        """Deep copy a nested dictionary."""
        result = {}
        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = self._deep_copy(v)
            else:
                result[k] = v
        return result

    def _deep_merge(self, base: Dict, update: Dict) -> Dict:
        """Deep merge update into base."""
        for k, v in update.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def _load_config(self, path: Path):
        try:
            with open(path, "r") as f:
                user_config = yaml.safe_load(f)
                if user_config:
                    self._deep_merge(self.config, user_config)
            logger.info(f"Loaded E2E config from {path}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}, using defaults")

    # === DAG Unlock Configuration ===

    @property
    def early_unblock(self) -> bool:
        """If True, DAG unlocks immediately on submission without waiting for test results."""
        return self.config.get("dag_unlock", {}).get("early_unblock", True)

    @property
    def ignore_weak_dependencies(self) -> bool:
        """If True, weak dependencies don't block even when not started."""
        return self.config.get("dag_unlock", {}).get("ignore_weak_dependencies", False)

    @property
    def fail_to_pass_threshold(self) -> float:
        """Threshold for F2P tests (only used when early_unblock=False)."""
        return self.config.get("dag_unlock", {}).get("strict_threshold", {}).get("fail_to_pass", 1.0)

    @property
    def pass_to_pass_threshold(self) -> float:
        """Threshold for P2P tests (only used when early_unblock=False)."""
        return self.config.get("dag_unlock", {}).get("strict_threshold", {}).get("pass_to_pass", 1.0)

    @property
    def none_to_pass_threshold(self) -> float:
        """Threshold for N2P tests (only used when early_unblock=False)."""
        return self.config.get("dag_unlock", {}).get("strict_threshold", {}).get("none_to_pass", 1.0)

    # === Evaluation Configuration ===

    @property
    def include_new_tests(self) -> bool:
        """If True, treat new_tests from classification as fail_to_pass tests."""
        return self.config.get("evaluation", {}).get("include_new_tests", False)

    @property
    def build_failure_fail_closed(self) -> bool:
        """Whether a detected build failure rejects partial test reports.

        False is the global compatibility default: an otherwise completed test
        command may contribute results from packages/modules that did run.
        Runner timeouts and nonzero outer exits are controlled separately and
        always remain fail-closed. Set true explicitly for strict scoring.
        """
        value = self.config.get("evaluation", {}).get("build_failure_fail_closed", False)
        if not isinstance(value, bool):
            raise ValueError("evaluation.build_failure_fail_closed must be a boolean")
        return value

    # === Retry and Timing Configuration ===

    @property
    def debounce_seconds(self) -> int:
        """Wait time for tag hash to stabilize before starting evaluation."""
        return self.config.get("retry_and_timing", {}).get("debounce_seconds", 120)

    @property
    def max_debounce_wait(self) -> int:
        """Maximum wait time even if hash keeps changing."""
        return self.config.get("retry_and_timing", {}).get("max_debounce_wait", 360)

    @property
    def max_retries(self) -> int:
        """Maximum number of retry attempts when tag changes after evaluation starts."""
        return self.config.get("retry_and_timing", {}).get("max_retries", 2)

    @property
    def evaluation_timeout(self) -> int:
        """Maximum wait time for pending evaluations to complete."""
        return self.config.get("retry_and_timing", {}).get("evaluation_timeout", 3600)

    @property
    def max_no_progress_attempts(self) -> int:
        """Maximum consecutive recovery attempts without progress before giving up."""
        return self.config.get("retry_and_timing", {}).get("max_no_progress_attempts", 3)

    @property
    def recovery_wait_seconds(self) -> int:
        """Wait time before sending recovery message after agent failure."""
        return self.config.get("retry_and_timing", {}).get("recovery_wait_seconds", 60)

    @property
    def recover_message_timeout_seconds(self) -> int:
        """Timeout for a single recover/resume message execution.

        This is intentionally shorter than the full agent timeout to prevent a
        single recover call from blocking the orchestrator for hours.
        """
        return self.config.get("retry_and_timing", {}).get("recover_message_timeout_seconds", 18000)

    @property
    def resume_no_progress_retry_limit(self) -> int:
        """Consecutive no-progress resume exits allowed before policy is applied."""
        return self.config.get("retry_and_timing", {}).get("resume_no_progress_retry_limit", 1)

    @property
    def resume_no_progress_policy(self) -> str:
        """Behavior when no-progress resume retry limit is reached.

        Supported values:
        - "exit": stop resume-trial immediately (default)
        - "start_new_session": invalidate persisted session and continue with a new one
        """
        return self.config.get("retry_and_timing", {}).get("resume_no_progress_policy", "exit")

    @property
    def resume_subprocess_retry_limit(self) -> int:
        """How many extra times to retry the initial `claude --resume <id>`
        subprocess call after a generic failure, before invalidating the session
        and falling back to a fresh one.

        Helps recover from transient issues (TCP wedge, externally-killed
        claude, brief proxy outage) where the session itself is fine but the
        subprocess returned non-zero. Each retry sleeps `recovery_wait_seconds`
        first to give the API a chance to settle.

        Total attempts = 1 + this limit. Set to 0 for legacy behavior
        (one attempt then immediate fallback).

        Distinct from `resume_no_progress_retry_limit`, which counts CONSECUTIVE
        worker invocations that exited with no progress (persistent across
        worker restarts). This one is per-worker, per-subprocess.
        """
        return self.config.get("retry_and_timing", {}).get("resume_subprocess_retry_limit", 3)
