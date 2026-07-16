#!/usr/bin/env python3
"""
E2E Agent Trial Runner - Continuous Task Queue Mode with Recovery

This script runs an E2E trial where:
1. Watcher thread monitors git tags and runs evaluations (background)
2. Agent manager runs Claude and handles recovery when new tasks appear

Architecture:
- Watcher runs in background thread, updates shared DAG state
- Agent manager in main thread coordinates agent lifecycle:
  1. Run agent until it exits (queue empty)
  2. If pending evaluations exist, wait for them
  3. If new tasks appear, send recover message to wake agent
  4. Repeat until DAG is complete

Features:
- Debounce: Wait for tag hash to stabilize before starting evaluation
- Retry: Allow re-evaluation when tag changes after initial evaluation
"""

from harness.e2e.env_guard import reject_legacy_env
import argparse
import fcntl
import json
import logging
import os
import queue
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Optional

import yaml

from harness.e2e.orchestrator import E2EOrchestrator
from harness.e2e.agent_runner import E2EAgentRunner
from harness.e2e.agents.claude_code import validate_claude_code_version
from harness.e2e.log_parser import get_parser
from harness.e2e.repo_config_binding import (
    freeze_repo_config,
    load_trial_repo_config_binding,
    resolve_repo_config,
)
from harness.e2e.runtime_policy_binding import (
    RUNTIME_POLICY_ENV_KEYS,
    RUNTIME_POLICY_MODE_PROTECTED,
    TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING,
    RuntimePolicyBindingError,
    freeze_runtime_policy,
    load_trial_runtime_policy_binding,
    resolve_runtime_policy,
    runtime_policy_coverage_errors,
    verify_expected_runtime_policy,
)
from harness.e2e.trial_lock import acquire_trial_lock

logger = logging.getLogger("e2e.runner")
orchestrator_logger = logging.getLogger("e2e.orchestrator")


def _activate_runtime_policy(binding) -> None:
    """Atomically replace all harness-managed runtime-policy environment."""
    env = dict(binding.env)
    unexpected = set(env) - set(RUNTIME_POLICY_ENV_KEYS)
    if unexpected:
        raise RuntimePolicyBindingError(
            f"runtime policy derived unmanaged environment keys: {sorted(unexpected)}"
        )
    for key in RUNTIME_POLICY_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.pop("SWE_MILESTONE_UNPROTECTED", None)
    os.environ.update(env)
    if binding.mode == "unprotected":
        os.environ["SWE_MILESTONE_UNPROTECTED"] = "1"


def _resolve_trial_relative_path(
    trial_root: Path,
    value: str,
    *,
    field_name: str,
) -> Path:
    """Resolve a persisted resume path without allowing trial-root escape."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{field_name} must be a safe relative POSIX path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe {field_name}: {value!r}")
    root = Path(trial_root).resolve()
    resolved = root.joinpath(*relative.parts).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes trial root: {value!r}") from exc
    return resolved


@dataclass
class DebounceState:
    """Track debounce state for a milestone tag.

    When a tag is first detected, we don't immediately start evaluation.
    Instead, we wait for the tag hash to stabilize (no changes for debounce_seconds).
    This handles the case where an agent creates a tag, then quickly amends the commit.
    """

    tag: str  # Tag name (e.g., "agent-impl-M001.1")
    hash: str  # Current commit hash the tag points to
    first_seen: float  # Timestamp when tag was first detected
    last_updated: float  # Timestamp of last hash change
    milestone_id: str  # Milestone ID (e.g., "M001.1")


def load_workspace_metadata(
    workspace_root: Path,
    repo_config: Optional[dict] = None,
) -> dict:
    """Load workspace metadata from metadata.json.

    Args:
        workspace_root: Path to workspace root (e.g., DATA/harness_workspace/repo/version)

    Returns:
        Dictionary with metadata values

    Raises:
        FileNotFoundError: If metadata.json doesn't exist
        KeyError: If required fields are missing
    """
    metadata_path = workspace_root / "metadata.json"

    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata.json not found at {metadata_path}")

    with open(metadata_path, "r") as f:
        metadata = json.load(f)

    # Validate required fields
    required_fields = ["repo_src_dirs", "test_dirs", "exclude_patterns"]
    missing_fields = [f for f in required_fields if f not in metadata]
    if missing_fields:
        raise KeyError(f"Missing required fields in metadata.json: {missing_fields}")

    logger.info(f"Loaded workspace metadata from {metadata_path}")
    logger.info(f"  repo_src_dirs: {metadata['repo_src_dirs']}")
    logger.info(f"  test_dirs: {metadata['test_dirs']}")
    logger.info(f"  exclude_patterns: {metadata['exclude_patterns']}")

    # Fallback to config YAML for optional patterns if not in metadata
    # workspace_root: DATA/harness_workspace/navidrome_navidrome_v0.57.0_v0.58.0/baseline_004_v4
    # config_path:    config/navidrome_navidrome_v0.57.0_v0.58.0.yaml
    if (
        repo_config is not None
        and (
            "generated_patterns" not in metadata
            or "modifiable_test_patterns" not in metadata
        )
    ):
        if "generated_patterns" not in metadata and "generated_patterns" in repo_config:
            metadata["generated_patterns"] = repo_config["generated_patterns"]
        if (
            "modifiable_test_patterns" not in metadata
            and "modifiable_test_patterns" in repo_config
        ):
            metadata["modifiable_test_patterns"] = repo_config[
                "modifiable_test_patterns"
            ]
    elif "generated_patterns" not in metadata or "modifiable_test_patterns" not in metadata:
        config_name = workspace_root.parent.name  # e.g., navidrome_navidrome_v0.57.0_v0.58.0
        config_path = Path("config") / f"{config_name}.yaml"
        if config_path.exists():
            logger.info(f"Loading optional patterns from config: {config_path}")
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            if "generated_patterns" not in metadata and "generated_patterns" in config:
                metadata["generated_patterns"] = config["generated_patterns"]
                logger.info(f"  loaded generated_patterns from config: {metadata['generated_patterns']}")
            if "modifiable_test_patterns" not in metadata and "modifiable_test_patterns" in config:
                metadata["modifiable_test_patterns"] = config["modifiable_test_patterns"]
                logger.info(f"  loaded modifiable_test_patterns from config: {metadata['modifiable_test_patterns']}")
        else:
            logger.debug(f"Config file not found: {config_path}, using defaults for optional patterns")

    return metadata


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_next_trial_name(base_name: str, result_dir: Path) -> str:
    """Generate next trial name with auto-incrementing suffix.

    If base_name already ends with a numeric suffix (e.g., "trial_001"),
    it is returned as-is without auto-incrementing.

    Args:
        base_name: Base name for the trial (e.g., 'agent_trial') or
                   fixed name with suffix (e.g., 'trial_001')
        result_dir: Parent directory where trials are stored

    Returns:
        Trial name with suffix (e.g., 'agent_trial_001', 'agent_trial_002')
        or the original base_name if it already has a numeric suffix
    """
    # If base_name already ends with _NNN (numeric suffix), use it directly
    if re.match(r".*_\d{3}$", base_name):
        return base_name

    trial_path = Path(base_name)
    parent_dir = result_dir / trial_path.parent if trial_path.parent != Path(".") else result_dir
    short_name = trial_path.name

    if not parent_dir.exists():
        return f"{base_name}_001"

    max_num = 0
    found_existing = False

    exact = parent_dir / short_name
    if exact.exists():
        found_existing = True

    for entry in parent_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if not name.startswith(f"{short_name}_"):
            continue
        suffix = name[len(short_name) + 1 :]
        if len(suffix) != 3 or not suffix.isdigit():
            continue
        found_existing = True
        max_num = max(max_num, int(suffix))

    if found_existing:
        new_short_name = f"{short_name}_{max_num + 1:03d}"
        if trial_path.parent != Path("."):
            return str(trial_path.parent / new_short_name)
        return new_short_name
    return f"{base_name}_001"


class E2ETrialRunner:
    """Coordinates watcher and agent with recovery support."""

    def __init__(
        self,
        orchestrator: E2EOrchestrator,
        agent_output_dir: Path,
        workdir: str,
        repo_src_dirs: list[str],
        agent_name: str,
        model: str,
        timeout_ms: int,
        prompt_version: str,
        copy_testbed: bool = True,
        remove_container: bool = False,
        reasoning_effort: Optional[str] = None,
        force: bool = False,
    ):
        self.orchestrator = orchestrator
        self.agent_output_dir = agent_output_dir
        self.workdir = workdir
        self.repo_src_dirs = repo_src_dirs
        self.agent_name = agent_name
        self.model = model
        self.timeout_ms = timeout_ms
        self.prompt_version = prompt_version
        self.copy_testbed = copy_testbed
        self.remove_container = remove_container
        self.reasoning_effort = reasoning_effort
        self.force = force

        self.watcher_thread = None
        self.watcher_stop_event = threading.Event()
        self.agent_runner = None
        self._trial_lock_file = None  # File handle for trial-level process lock

        # Event queue for watcher -> main loop communication
        # Event format: (event_type, milestone_id, dag_status, eval_status, error_msg)
        # - ("eval_complete", mid, dag_status, eval_status, error_msg)  # error_msg is None for normal pass/fail
        # - ("eval_error", mid, "unlocked", "error", error_msg)  # System-level error (process killed, OOM, etc.)
        # - ("watcher_done", None, None, None, None)
        self.eval_event_queue: queue.Queue = queue.Queue()

        # Lock for shared state between watcher thread and main thread
        self._state_lock = threading.RLock()

        # Shared state for debounce tracking (accessed by watcher thread and main thread)
        # This allows _wait_for_evaluations to know if there are items waiting in debounce
        # NOTE: Access should be protected by self._state_lock
        self.pending_debounce: Dict[str, DebounceState] = {}

        # Shared state for running evaluations (accessed by watcher thread and main thread)
        # This tracks milestones currently being evaluated, independent of DAG state.
        # In early unlock mode, DAG marks milestones as "completed" immediately,
        # but we still need to wait for evaluations to finish for summary updates.
        # NOTE: Access should be protected by self._state_lock
        self.running_evaluations: set = set()  # Set of (mid, attempt) tuples

        # Resume priming state (only used in --resume-trial)
        self._resume_pending_debounce: Dict[str, dict] = {}
        self._resume_pending_evaluations: Dict[str, dict] = {}
        self._resume_retry_state_path = self.orchestrator.trial_root / "resume_retry_state.json"
        self._last_run_summary: Dict[str, object] = {}

        # Setup runner logger to write to the same orchestrator.log file
        self._setup_runner_logger()

    def _acquire_trial_lock(self):
        """Acquire exclusive trial-level lock to prevent concurrent processes.

        Uses fcntl.flock on a .lock file in trial_root. If another process
        already holds the lock, logs an error and exits immediately.
        """
        lock_path = self.orchestrator.trial_root / ".trial.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._trial_lock_file = open(lock_path, "a+")
        try:
            fcntl.flock(self._trial_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID for debugging
            self._trial_lock_file.seek(0)
            self._trial_lock_file.truncate()
            self._trial_lock_file.write(str(os.getpid()))
            self._trial_lock_file.flush()
        except OSError:
            # Another process holds the lock
            try:
                self._trial_lock_file.seek(0)
                existing_pid = self._trial_lock_file.read().strip()
            except Exception:
                existing_pid = "unknown"
            logger.error(
                f"Another process (PID {existing_pid}) is already running on this trial. " f"Lock file: {lock_path}"
            )
            self._trial_lock_file.close()
            self._trial_lock_file = None
            sys.exit(1)

    def _release_trial_lock(self):
        """Release the trial-level lock."""
        if self._trial_lock_file is not None:
            try:
                fcntl.flock(self._trial_lock_file.fileno(), fcntl.LOCK_UN)
                self._trial_lock_file.close()
            except Exception:
                pass
            self._trial_lock_file = None

    def _setup_runner_logger(self):
        """Add file handler to e2e.runner logger to write to orchestrator.log."""
        log_file = self.orchestrator.trial_root / "orchestrator.log"
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        # Use [runner] prefix to distinguish from orchestrator logs
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [runner] %(message)s"))
        logger.addHandler(file_handler)

    def _default_resume_retry_state(self) -> dict:
        return {
            "version": 1,
            "total_resume_runs": 0,
            "total_no_progress_exits": 0,
            "consecutive_no_progress_exits": 0,
            "last_updated": None,
            "last_policy_decision": None,
            "last_resume_run": None,
        }

    def _load_resume_retry_state(self) -> dict:
        state = self._default_resume_retry_state()
        path = self._resume_retry_state_path
        if not path.exists():
            return state
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in state:
                    if key in loaded:
                        state[key] = loaded[key]
        except Exception as e:
            logger.warning(f"Failed to load resume retry state from {path}: {e}")

        for key in ["total_resume_runs", "total_no_progress_exits", "consecutive_no_progress_exits"]:
            try:
                state[key] = max(0, int(state.get(key, 0)))
            except Exception:
                state[key] = 0
        return state

    def _save_resume_retry_state(self, state: dict) -> None:
        path = self._resume_retry_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"Failed to save resume retry state to {path}: {e}")

    def _apply_resume_no_progress_policy(self, resume_session: bool) -> tuple[bool, bool]:
        """Apply persisted no-progress resume policy.

        Returns:
            (allow_resume_run, effective_resume_session)
        """
        if not resume_session:
            return True, False

        state = self._load_resume_retry_state()
        config = self.orchestrator.config
        limit = int(getattr(config, "resume_no_progress_retry_limit", 1) or 0)
        policy = str(getattr(config, "resume_no_progress_policy", "exit") or "exit").strip().lower()
        if policy not in {"exit", "start_new_session"}:
            logger.warning(f"Unknown resume_no_progress_policy '{policy}', fallback to 'exit'")
            policy = "exit"

        consecutive = int(state.get("consecutive_no_progress_exits", 0) or 0)
        if limit <= 0 or consecutive < limit:
            return True, True

        decision = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "policy": policy,
            "limit": limit,
            "consecutive_no_progress_exits": consecutive,
        }
        state["last_policy_decision"] = decision
        self._save_resume_retry_state(state)

        if policy == "start_new_session":
            logger.warning(
                "Resume no-progress limit reached (%s/%s); policy=start_new_session, will clear session and continue.",
                consecutive,
                limit,
            )
            orchestrator_logger.warning(
                "⚠️ Resume no-progress limit reached (%s/%s); starting a fresh agent session",
                consecutive,
                limit,
            )
            return True, False

        logger.warning(
            "Resume no-progress limit reached (%s/%s); policy=exit, skipping this resume-trial run.",
            consecutive,
            limit,
        )
        orchestrator_logger.warning("⚠️ Resume skipped by policy: no-progress limit reached (%s/%s)", consecutive, limit)
        return False, True

    def _record_resume_run_outcome(
        self,
        *,
        resume_session_requested: bool,
        resume_session_used: bool,
        success: bool,
    ) -> None:
        """Persist resume-run outcome for next --resume-trial decision."""
        state = self._load_resume_retry_state()
        state["total_resume_runs"] = int(state.get("total_resume_runs", 0) or 0) + 1

        summary = dict(self._last_run_summary or {})
        stopped_by_no_progress = bool(summary.get("stopped_by_no_progress_limit", False))
        made_any_progress = bool(summary.get("made_any_progress", False))

        if stopped_by_no_progress:
            state["total_no_progress_exits"] = int(state.get("total_no_progress_exits", 0) or 0) + 1
            state["consecutive_no_progress_exits"] = int(state.get("consecutive_no_progress_exits", 0) or 0) + 1
        elif made_any_progress or success:
            state["consecutive_no_progress_exits"] = 0

        state["last_resume_run"] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "success": bool(success),
            "resume_session_requested": bool(resume_session_requested),
            "resume_session_used": bool(resume_session_used),
            "summary": summary,
        }
        self._save_resume_retry_state(state)

    def start_watcher_thread(self):
        """Start watcher in background thread.

        Note: setup_environment() is now called synchronously in run() before this.
        This thread only monitors for agent tags and runs evaluations.
        """

        def watcher_loop():
            try:
                logger.info("Watcher thread started (monitoring for tags)")
                # Run watcher loop (non-blocking version)
                self._run_watcher_loop()
            except Exception as e:
                logger.error(f"Watcher thread died: {e}", exc_info=True)

        self.watcher_thread = threading.Thread(target=watcher_loop, daemon=True)
        self.watcher_thread.start()

    def _run_watcher_loop(self):
        """Watcher loop that checks for tags and runs evaluations.

        This version implements debounce and retry logic:
        - Debounce: Wait for tag hash to stabilize before starting evaluation
        - Retry: Allow re-evaluation when tag changes after initial evaluation

        Pushes events to eval_event_queue when evaluations complete.
        """
        import concurrent.futures
        from harness.e2e.orchestrator import run_evaluation_task

        dag = self.orchestrator.dag
        config = self.orchestrator.config

        # Get config values
        debounce_seconds = config.debounce_seconds
        max_debounce_wait = config.max_debounce_wait
        max_retries = config.max_retries

        # State tracking
        # Note: self.pending_debounce is shared with main thread for _wait_for_evaluations
        pending_debounce = self.pending_debounce  # Use instance variable
        retry_counts: Dict[str, int] = {}  # mid -> retry count (for tag updates after eval)
        submission_failures: Dict[str, int] = {}  # mid -> submission failure count

        # Initialize evaluated_hashes from orchestrator (for resume mode)
        # In fresh mode, _evaluated_hashes is empty; in resume mode, it contains previous hashes
        evaluated_hashes: Dict[str, str] = dict(self.orchestrator._evaluated_hashes)
        if evaluated_hashes:
            logger.info(f"Restored {len(evaluated_hashes)} evaluated hashes from previous run")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            pending_futures: Dict[concurrent.futures.Future, tuple] = {}  # future -> (mid, attempt)
            stale_futures: set = set()  # Futures whose results should be ignored (superseded by newer eval)

            def cancel_existing_evaluation(milestone_id: str) -> bool:
                """Cancel any existing evaluation for a milestone.

                Returns True if an evaluation was found and handled (cancelled or marked stale).
                """
                # Find existing future for this milestone
                existing_future = None
                existing_info = None
                for f, (m, a) in list(pending_futures.items()):
                    if m == milestone_id:
                        existing_future = f
                        existing_info = (m, a)
                        break

                if existing_future is None:
                    return False

                mid, attempt = existing_info

                # Try to cancel the future
                if existing_future.cancel():
                    # Successfully cancelled (was still in queue)
                    logger.info(f"🚫 {mid}: Cancelled pending evaluation (attempt {attempt})")
                    pending_futures.pop(existing_future, None)
                    with self._state_lock:
                        self.running_evaluations.discard((mid, attempt))
                else:
                    # Already running, mark as stale so result will be ignored
                    logger.info(f"🚫 {mid}: Marking running evaluation as stale (attempt {attempt})")
                    stale_futures.add(existing_future)

                return True

            # === Resume priming: restore pending debounce / evaluations without re-scanning tags ===
            dropped_debounce: set[str] = set()
            if self._resume_pending_debounce:
                priming_now = time.time()
                logger.info(f"Resume priming: restoring {len(self._resume_pending_debounce)} pending debounce items")
                try:
                    current_tags = self.orchestrator._get_container_tags()
                except Exception as e:
                    logger.warning(f"Resume priming: failed to list tags, will fall back to normal scan: {e}")
                    current_tags = set()

                for mid, payload in list(self._resume_pending_debounce.items()):
                    if mid in dag.completed_milestones or mid in dag.failed_milestones or mid in dag.skipped_milestones:
                        dropped_debounce.add(mid)
                        continue

                    if not isinstance(payload, dict):
                        dropped_debounce.add(mid)
                        continue

                    tag = payload.get("tag") or f"agent-impl-{mid}"
                    if current_tags and tag not in current_tags:
                        dropped_debounce.add(mid)
                        continue

                    tag_hash = payload.get("tag_hash")
                    if not isinstance(tag_hash, str) or not tag_hash:
                        try:
                            tag_hash = self.orchestrator._get_tag_hash(tag)
                        except Exception:
                            tag_hash = ""

                    first_seen_ts = payload.get("first_seen_ts", payload.get("first_seen"))
                    last_updated_ts = payload.get("last_updated_ts", payload.get("last_updated"))
                    if not isinstance(first_seen_ts, (int, float)):
                        first_seen_ts = priming_now
                    if not isinstance(last_updated_ts, (int, float)):
                        last_updated_ts = priming_now

                    # Clamp to "now" to avoid negative durations if clocks differ
                    first_seen_ts = min(float(first_seen_ts), priming_now)
                    last_updated_ts = min(float(last_updated_ts), priming_now)
                    last_updated_ts = max(last_updated_ts, first_seen_ts)

                    with self._state_lock:
                        pending_debounce[mid] = DebounceState(
                            tag=tag,
                            hash=tag_hash,
                            first_seen=first_seen_ts,
                            last_updated=last_updated_ts,
                            milestone_id=mid,
                        )

                self._resume_pending_debounce = {}

            dropped_eval_keys: set[str] = set()
            if self._resume_pending_evaluations:
                logger.info(f"Resume priming: restoring {len(self._resume_pending_evaluations)} pending evaluations")
                for key, payload in list(self._resume_pending_evaluations.items()):
                    if not isinstance(key, str) or not isinstance(payload, dict):
                        dropped_eval_keys.add(str(key))
                        continue

                    mid = payload.get("milestone_id")
                    if not isinstance(mid, str) or not mid:
                        if "#" in key:
                            mid = key.split("#", 1)[0]
                        else:
                            dropped_eval_keys.add(key)
                            continue

                    attempt = payload.get("attempt", 0)
                    try:
                        attempt = int(attempt)
                    except Exception:
                        attempt = 0

                    snapshot_rel = payload.get("snapshot_path")
                    result_rel = payload.get("result_dir")
                    if not isinstance(snapshot_rel, str) or not snapshot_rel:
                        dropped_eval_keys.add(key)
                        continue

                    try:
                        snapshot_path = _resolve_trial_relative_path(
                            self.orchestrator.trial_root,
                            snapshot_rel,
                            field_name="resume snapshot_path",
                        )
                    except ValueError as exc:
                        logger.warning("Resume priming: dropping %s: %s", key, exc)
                        dropped_eval_keys.add(key)
                        continue
                    if not snapshot_path.exists():
                        logger.warning(f"Resume priming: snapshot missing for {key}: {snapshot_path}")
                        dropped_eval_keys.add(key)
                        continue

                    if isinstance(result_rel, str) and result_rel:
                        try:
                            result_dir = _resolve_trial_relative_path(
                                self.orchestrator.trial_root,
                                result_rel,
                                field_name="resume result_dir",
                            )
                        except ValueError as exc:
                            logger.warning("Resume priming: dropping %s: %s", key, exc)
                            dropped_eval_keys.add(key)
                            continue
                    else:
                        result_dir = snapshot_path.parent
                    result_dir.mkdir(parents=True, exist_ok=True)

                    with self._state_lock:
                        self.running_evaluations.add((mid, attempt))

                    future = executor.submit(
                        run_evaluation_task,
                        milestone_id=mid,
                        snapshot_path=snapshot_path,
                        result_dir=result_dir,
                        workspace_root=self.orchestrator.workspace_root,
                        fail_to_pass_threshold=config.fail_to_pass_threshold,
                        pass_to_pass_threshold=config.pass_to_pass_threshold,
                        none_to_pass_threshold=config.none_to_pass_threshold,
                        agent_attempt=attempt,
                        build_failure_fail_closed=(
                            self.orchestrator.build_failure_fail_closed
                        ),
                        repo_config_path=(
                            self.orchestrator.repo_config_binding.path
                            if self.orchestrator.repo_config_binding is not None
                            else None
                        ),
                        repo_config_sha256=(
                            self.orchestrator.repo_config_binding.sha256
                            if self.orchestrator.repo_config_binding is not None
                            else None
                        ),
                        runtime_policy_path=(
                            self.orchestrator.runtime_policy_binding.path
                            if self.orchestrator.runtime_policy_binding is not None
                            else None
                        ),
                        runtime_policy_sha256=(
                            self.orchestrator.runtime_policy_binding.sha256
                            if self.orchestrator.runtime_policy_binding is not None
                            else None
                        ),
                        runtime_policy_mode=(
                            self.orchestrator.runtime_policy_binding.mode
                            if self.orchestrator.runtime_policy_binding is not None
                            else None
                        ),
                    )
                    pending_futures[future] = (mid, attempt)

                    # Seed dedupe hash to avoid re-debounce/rescan; still allows retry on hash change
                    tag_hash = payload.get("tag_hash")
                    if isinstance(tag_hash, str) and tag_hash:
                        evaluated_hashes[mid] = tag_hash

                    if attempt > 0:
                        retry_counts[mid] = max(retry_counts.get(mid, 0), attempt)

                self._resume_pending_evaluations = {}

            # Best-effort cleanup: remove invalid persisted pending entries to avoid infinite growth
            if dropped_debounce or dropped_eval_keys:

                def _cleanup(summary: dict) -> None:
                    rs = summary.get("resume_state", {})
                    pd = rs.get("pending_debounce")
                    if isinstance(pd, dict):
                        for mid in dropped_debounce:
                            pd.pop(mid, None)
                    pe = rs.get("pending_evaluations")
                    if isinstance(pe, dict):
                        for k in dropped_eval_keys:
                            pe.pop(k, None)

                try:
                    self.orchestrator._update_resume_state(_cleanup)
                except Exception as e:
                    logger.warning(f"Resume priming: failed to cleanup stale resume_state entries: {e}")

            while not self.watcher_stop_event.is_set():
                now = time.time()

                # Step 1: Check for completed evaluations
                done_futures = [f for f in pending_futures if f.done()]
                for f in done_futures:
                    mid, attempt = pending_futures.pop(f)
                    # Remove from running evaluations tracking (thread-safe)
                    with self._state_lock:
                        self.running_evaluations.discard((mid, attempt))

                    # Check if this evaluation was marked as stale (superseded by newer evaluation)
                    if f in stale_futures:
                        stale_futures.discard(f)
                        logger.info(f"🗑️  {mid}: Discarding stale evaluation result (attempt {attempt})")
                        continue

                    try:
                        result = f.result()
                        # Unpack result tuple: (milestone_id, is_resolved, actual_passed, eval_res, error_msg)
                        # - is_resolved: Whether milestone passed threshold checks (for DAG)
                        # - actual_passed: Whether tests actually passed 100% (for eval_status)
                        _, is_resolved, actual_passed, eval_res, err_msg = result
                        # Pass to result processing - returns (dag_status, eval_status, error_msg)
                        dag_status, eval_status, error_msg = self.orchestrator._process_evaluation_result(
                            mid, is_resolved, actual_passed, eval_res, err_msg, attempt=attempt
                        )
                        # Notify main loop with dual-dimension status
                        self.eval_event_queue.put(("eval_complete", mid, dag_status, eval_status, error_msg))
                        if error_msg:
                            logger.info(
                                f"Pushed eval_complete event for {mid}: dag={dag_status}, eval={eval_status} (error)"
                            )
                        else:
                            logger.info(f"Pushed eval_complete event for {mid}: dag={dag_status}, eval={eval_status}")
                    except Exception as e:
                        logger.error(f"Error processing evaluation for {mid}: {e}")
                        self.eval_event_queue.put(("eval_error", mid, "unlocked", "error", str(e)))

                # Check if done (thread-safe check of pending state)
                # Must ensure:
                # 1. DAG is complete (all milestones in terminal state)
                # 2. No pending evaluations (pending_futures)
                # 3. No pending debounce (tags waiting to stabilize)
                # 4. No running evaluations (important for final milestone in early unlock mode)
                # 5. No error evaluations that still need re-evaluation
                with self._state_lock:
                    is_done = (
                        dag.is_done() and not pending_futures and not pending_debounce and not self.running_evaluations
                    )
                if is_done:
                    # Check for error evaluations that need re-evaluation
                    summary = self.orchestrator._load_summary_or_init()
                    error_mids = [
                        mid for mid, r in summary.get("results", {}).items()
                        if r.get("eval_status") == "error" and mid not in evaluated_hashes
                    ]
                    if error_mids:
                        logger.info(f"DAG done but {len(error_mids)} error evaluation(s) pending re-scan: {error_mids}")
                    else:
                        logger.info("All milestones processed and evaluated, watcher exiting")
                        self.eval_event_queue.put(("watcher_done", None, None, None, None))
                        break

                # Step 2: Check pending debounce items
                for mid in list(pending_debounce.keys()):
                    state = pending_debounce[mid]
                    current_hash = self.orchestrator._get_tag_hash(f"agent-impl-{mid}")

                    if current_hash != state.hash:
                        # Hash changed, update state
                        logger.info(f"⏳ {mid}: Tag hash changed during debounce, resetting timer...")
                        state.hash = current_hash
                        state.last_updated = now
                        try:

                            def _mutate(summary: dict) -> None:
                                rs = summary["resume_state"]
                                rs["pending_debounce"][mid] = {
                                    "tag": state.tag,
                                    "tag_hash": current_hash,
                                    "first_seen_ts": state.first_seen,
                                    "last_updated_ts": now,
                                }

                            self.orchestrator._update_resume_state(_mutate)
                        except Exception as e:
                            logger.debug(f"Failed to persist debounce update for {mid}: {e}")
                        continue

                    time_since_last_update = now - state.last_updated
                    time_since_first_seen = now - state.first_seen

                    if time_since_last_update >= debounce_seconds:
                        # Stable for debounce period, start evaluation
                        with self._state_lock:
                            del pending_debounce[mid]
                            self.running_evaluations.add((mid, 0))
                        logger.info(f"✓ {mid}: Debounce complete ({debounce_seconds}s stable), starting evaluation...")
                        success = self.orchestrator._handle_submission(
                            mid, state.tag, executor, pending_futures, attempt=0
                        )
                        if success:
                            evaluated_hashes[mid] = current_hash
                            retry_counts[mid] = 0
                            submission_failures.pop(mid, None)  # Clear failure count on success
                        else:
                            # Submission failed (e.g., snapshot extraction error), clean up tracking
                            with self._state_lock:
                                self.running_evaluations.discard((mid, 0))
                            submission_failures[mid] = submission_failures.get(mid, 0) + 1
                            if submission_failures[mid] >= max_retries:
                                # Max submission failures reached, mark as evaluated to skip future attempts
                                evaluated_hashes[mid] = current_hash
                                logger.error(f"⛔ {mid}: Max submission failures ({max_retries}) reached, giving up")
                            else:
                                # Will re-enter debounce on next scan (tag still exists, not in evaluated_hashes)
                                logger.warning(
                                    f"⚠️ {mid}: Submission failed ({submission_failures[mid]}/{max_retries}), "
                                    f"will retry after re-debounce"
                                )
                    elif time_since_first_seen >= max_debounce_wait:
                        # Max wait exceeded, force evaluation
                        with self._state_lock:
                            del pending_debounce[mid]
                            self.running_evaluations.add((mid, 0))
                        logger.warning(
                            f"⚠️ {mid}: Max debounce wait ({max_debounce_wait}s) exceeded, forcing evaluation..."
                        )
                        success = self.orchestrator._handle_submission(
                            mid, state.tag, executor, pending_futures, attempt=0
                        )
                        if success:
                            evaluated_hashes[mid] = current_hash
                            retry_counts[mid] = 0
                            submission_failures.pop(mid, None)  # Clear failure count on success
                        else:
                            # Submission failed, clean up tracking
                            with self._state_lock:
                                self.running_evaluations.discard((mid, 0))
                            submission_failures[mid] = submission_failures.get(mid, 0) + 1
                            if submission_failures[mid] >= max_retries:
                                # Max submission failures reached, mark as evaluated to skip future attempts
                                evaluated_hashes[mid] = current_hash
                                logger.error(f"⛔ {mid}: Max submission failures ({max_retries}) reached, giving up")
                            else:
                                # Will re-enter debounce on next scan
                                logger.warning(
                                    f"⚠️ {mid}: Submission failed ({submission_failures[mid]}/{max_retries}), "
                                    f"will retry after re-debounce"
                                )

                # Step 3: Scan for new/changed tags
                current_tags = self.orchestrator._get_container_tags()

                for mid in dag.all_milestones:
                    tag = f"agent-impl-{mid}"
                    if tag not in current_tags:
                        continue

                    current_hash = self.orchestrator._get_tag_hash(tag)

                    if mid in pending_debounce:
                        # Already in debounce, handled above
                        continue

                    # Skip if already completed in DAG (for resume mode without hash),
                    # UNLESS the previous evaluation errored (infrastructure failure) —
                    # in that case, re-evaluate to get a proper result.
                    if mid in dag.completed_milestones and mid not in evaluated_hashes:
                        summary = self.orchestrator._load_summary_or_init()
                        prev_eval = summary.get("results", {}).get(mid, {}).get("eval_status")
                        if prev_eval == "error":
                            logger.info(f"🔄 {mid}: Previously errored (eval_status=error), will re-evaluate")
                        else:
                            logger.info(f"⏭️  {mid}: Already completed in DAG, skipping evaluation")
                            evaluated_hashes[mid] = current_hash  # Record hash to prevent re-checking
                            continue

                    if mid not in evaluated_hashes:
                        # First time seeing this tag, start debounce
                        logger.info(f"🔍 {mid}: New tag detected, starting debounce ({debounce_seconds}s)...")
                        with self._state_lock:
                            pending_debounce[mid] = DebounceState(
                                tag=tag,
                                hash=current_hash,
                                first_seen=now,
                                last_updated=now,
                                milestone_id=mid,
                            )
                        try:

                            def _mutate(summary: dict) -> None:
                                rs = summary["resume_state"]
                                rs["pending_debounce"][mid] = {
                                    "tag": tag,
                                    "tag_hash": current_hash,
                                    "first_seen_ts": now,
                                    "last_updated_ts": now,
                                }

                            self.orchestrator._update_resume_state(_mutate)
                        except Exception as e:
                            logger.debug(f"Failed to persist pending_debounce for {mid}: {e}")
                    elif current_hash != evaluated_hashes[mid]:
                        # Hash changed after evaluation - this is a RETRY
                        current_retry_count = retry_counts.get(mid, 0)
                        if current_retry_count >= max_retries:
                            logger.warning(f"⛔ {mid}: Max retries ({max_retries}) exceeded, ignoring tag update")
                            continue

                        # Cancel any existing evaluation for this milestone before starting retry
                        cancel_existing_evaluation(mid)

                        # Start retry immediately (no debounce for retries)
                        retry_counts[mid] = current_retry_count + 1
                        attempt = retry_counts[mid]
                        logger.info(
                            f"🔄 {mid}: Tag updated after evaluation, starting retry {attempt}/{max_retries}..."
                        )
                        with self._state_lock:
                            self.running_evaluations.add((mid, attempt))
                        success = self.orchestrator._handle_submission(
                            mid, tag, executor, pending_futures, attempt=attempt
                        )
                        if success:
                            evaluated_hashes[mid] = current_hash
                        else:
                            # Submission failed, clean up tracking and revert retry count
                            with self._state_lock:
                                self.running_evaluations.discard((mid, attempt))
                            retry_counts[mid] = current_retry_count  # Revert retry count
                            logger.warning(f"⚠️ {mid}: Retry submission failed, cleaned up running_evaluations")

                time.sleep(2)

    def _drain_pending_events(self) -> str:
        """Drain any pending events from the queue without blocking.

        Returns:
            "all_done" if watcher_done signal received, "continue" otherwise
        """
        while True:
            try:
                event = self.eval_event_queue.get_nowait()
                result = self._process_queue_event(event)
                if result == "all_done":
                    return "all_done"
            except queue.Empty:
                return "continue"

    def _process_queue_event(self, event) -> str | None:
        """Process a single event from the queue.

        Args:
            event: Tuple of (event_type, mid, dag_status, eval_status, error_msg)

        Returns:
            "all_done" if watcher_done signal received, None otherwise
        """
        # Event format: (event_type, mid, dag_status, eval_status, error_msg)
        event_type = event[0]
        mid = event[1] if len(event) > 1 else None

        if event_type == "eval_complete":
            dag_status = event[2] if len(event) > 2 else "unlocked"
            eval_status = event[3] if len(event) > 3 else "unknown"
            error_msg = event[4] if len(event) > 4 else None
            if error_msg:
                # Evaluation execution error (e.g., compile error, test didn't run)
                logger.warning(f"📬 Received eval_complete for {mid}: dag={dag_status}, eval={eval_status} (error)")
                logger.warning(f"   Error: {error_msg[:200]}..." if len(error_msg) > 200 else f"   Error: {error_msg}")
            else:
                logger.info(f"📬 Received eval_complete for {mid}: dag={dag_status}, eval={eval_status}")
        elif event_type == "eval_error":
            error_msg = event[4] if len(event) > 4 else "Unknown error"
            logger.error(f"📬 Received eval_error for {mid}: {error_msg}")
        elif event_type == "watcher_done":
            logger.info("📬 Received watcher_done signal")
            return "all_done"

        return None

    def _wait_for_evaluations(self, max_wait: int = None) -> str:
        """Wait for pending evaluations using event queue.

        Instead of polling with sleep, this waits on the event queue for
        notifications from the watcher thread.

        Also waits for items in debounce period - these are tags that have been
        detected but haven't been submitted yet (waiting for hash to stabilize).

        Args:
            max_wait: Maximum wait time in seconds (default: config.evaluation_timeout, 3600s)

        Returns:
            "new_tasks" - new tasks available for agent
            "all_done" - DAG completed AND all evaluations finished
            "agent_incomplete" - agent didn't complete all tasks (no pending work, but DAG not done)
            "timeout" - max wait time exceeded
        """
        dag = self.orchestrator.dag
        config = self.orchestrator.config

        # Use config value if max_wait not specified
        if max_wait is None:
            max_wait = config.evaluation_timeout

        start_time = time.time()

        # First, drain any pending events that accumulated while agent was running
        if self._drain_pending_events() == "all_done":
            # Even if watcher says done, verify DAG is actually complete
            if dag.is_done():
                return "all_done"

        # Wait for:
        # - submitted_milestones: milestones in normal evaluation mode
        # - pending_debounce: tags waiting for hash to stabilize
        # - running_evaluations: evaluations in progress (important for early unlock mode!)
        while True:
            with self._state_lock:
                has_pending = bool(dag.submitted_milestones or self.pending_debounce or self.running_evaluations)

            if not has_pending:
                # Nothing pending - check if there are still runnable tasks
                runnable = dag.get_next_runnable()
                if runnable:
                    logger.info(f"New tasks available: {runnable}")
                    return "new_tasks"
                # No runnable tasks and nothing pending - check if DAG is done
                if dag.is_done():
                    return "all_done"
                else:
                    # Agent didn't complete all milestones
                    logger.warning(f"Agent incomplete: DAG not done but no pending work")
                    logger.warning(f"  Completed: {sorted(dag.completed_milestones)}")
                    logger.warning(f"  Failed: {sorted(dag.failed_milestones)}")
                    logger.warning(
                        f"  Remaining: {sorted(dag.all_milestones - dag.completed_milestones - dag.failed_milestones - dag.skipped_milestones)}"
                    )
                    return "agent_incomplete"

            # Check if new tasks are already available (excluding those in debounce)
            # Tasks in pending_debounce already have tags submitted, so agent can't make more progress on them
            runnable = dag.get_next_runnable()
            with self._state_lock:
                runnable_excluding_debounce = [m for m in runnable if m not in self.pending_debounce]
            if runnable_excluding_debounce:
                logger.info(f"New tasks available: {runnable_excluding_debounce}")
                return "new_tasks"
            elif runnable and self.pending_debounce:
                # All runnable tasks are in debounce - wait for debounce to complete
                logger.info(f"Waiting for debounce: {list(self.pending_debounce.keys())}")

            if dag.is_done() and not self.running_evaluations:
                return "all_done"

            # Calculate remaining time
            elapsed = time.time() - start_time
            remaining = max_wait - elapsed
            if remaining <= 0:
                logger.warning(f"Timeout after {max_wait}s waiting for evaluations")
                return "timeout"

            # Wait for event from watcher (with timeout)
            try:
                event = self.eval_event_queue.get(timeout=min(5.0, remaining))
                result = self._process_queue_event(event)
                if result == "all_done":
                    # Verify DAG is actually done AND all evaluations have finished
                    # This ensures we wait for the final milestone's evaluation to complete
                    # even in early unlock mode where DAG might be "done" before eval finishes
                    with self._state_lock:
                        evals_done = not self.running_evaluations
                    if dag.is_done() and evals_done:
                        return "all_done"
                    elif dag.is_done() and not evals_done:
                        logger.info("DAG complete, waiting for final evaluation(s) to finish...")
                        # Continue waiting for running evaluations
            except queue.Empty:
                # Timeout on queue.get, log status and continue
                pending = len(dag.submitted_milestones)
                debounce = len(self.pending_debounce)
                running = len(self.running_evaluations)
                logger.info(
                    f"Waiting... pending={pending}, debounce={debounce}, running={running} ({int(elapsed)}s elapsed)"
                )

        return "all_done"

    def run_agent_with_recovery(self, resume_session_first: bool = False) -> bool:
        """Run agent with automatic recovery when new tasks appear.

        Note: Environment setup and task queue population is now done synchronously
        in run() before this method is called, so no sleep is needed.

        Progress tracking:
        - Tracks DAG state (completed + submitted milestones) before and after each run
        - If progress is made (new milestones completed or submitted), no_progress_count resets
        - If no progress is made, no_progress_count increments
        - Exits after max_no_progress_attempts (3) consecutive attempts without progress

        Returns:
            True if all tasks completed successfully
        """
        dag = self.orchestrator.dag
        config = self.orchestrator.config
        first_run = True
        recover_count = 0
        no_progress_count = 0
        made_any_progress = False
        max_no_progress_attempts = config.max_no_progress_attempts

        # Track progress by monitoring DAG state changes
        def get_dag_progress_state():
            """Get current DAG progress state for comparison."""
            # Use get_state_snapshot() for atomic read of all state
            snapshot = dag.get_state_snapshot()
            return {
                "completed": snapshot["completed"],
                "submitted": snapshot["submitted"],
                "failed": snapshot["failed"],
            }

        def has_progress(prev_state, curr_state):
            """Check if any progress was made between states."""
            # Progress = new completions, new submissions, or new failures (which unlock dependents)
            new_completed = curr_state["completed"] - prev_state["completed"]
            new_submitted = curr_state["submitted"] - prev_state["submitted"]
            new_failed = curr_state["failed"] - prev_state["failed"]
            return bool(new_completed or new_submitted or new_failed)

        logger.info("=" * 70)
        logger.info("Starting E2E Agent with Recovery Support")
        logger.info("=" * 70)

        def _set_last_run_summary(stop_reason: str):
            self._last_run_summary = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "stop_reason": stop_reason,
                "recover_attempts": recover_count,
                "final_no_progress_count": no_progress_count,
                "max_no_progress_attempts": max_no_progress_attempts,
                "stopped_by_no_progress_limit": stop_reason == "no_progress_limit",
                "made_any_progress": made_any_progress,
                "dag_done": dag.is_done(),
                "completed_count": len(dag.completed_milestones),
                "failed_count": len(dag.failed_milestones),
                "skipped_count": len(dag.skipped_milestones),
            }

        # Create agent runner
        self.agent_runner = E2EAgentRunner(
            container_name=self.orchestrator.container_name,
            output_dir=str(self.agent_output_dir),
            workdir=self.workdir,
            repo_src_dirs=self.repo_src_dirs,
            agent_name=self.agent_name,
            model=self.model,
            timeout_ms=self.timeout_ms,
            prompt_version=self.prompt_version,
            reasoning_effort=self.reasoning_effort,
        )

        def invoke_agent(callable_):
            # A model turn may have deleted its disposable cache or an old
            # resumed container may predate the immutable Go split. Verify the
            # exact shared runtime and reset only reproducible COW module state
            # before every fresh/resume/recover subprocess.
            self.orchestrator.container_setup.prepare_agent_invocation()
            return callable_()

        # Capture initial state
        prev_state = get_dag_progress_state()
        has_new_tasks = True  # First run always has tasks
        configured_recover_timeout = int(getattr(config, "recover_message_timeout_seconds", 0) or 0)
        if configured_recover_timeout > 0:
            recover_timeout_ms = min(self.timeout_ms, configured_recover_timeout * 1000)
        else:
            recover_timeout_ms = self.timeout_ms
        logger.info(
            "Recover message timeout configured to %.1f minutes",
            recover_timeout_ms / 1000 / 60,
        )

        # Generic-failure retry budget for the initial resume_session subprocess
        # (handles transient TCP wedge / externally-killed claude before giving
        # up on the session). See e2e_config.yaml: resume_subprocess_retry_limit.
        resume_subprocess_retry_limit = int(getattr(config, "resume_subprocess_retry_limit", 0) or 0)
        recovery_wait_seconds = int(getattr(config, "recovery_wait_seconds", 60) or 0)

        # Graceful HTTP-529 server-overload backoff config. Server overload is a
        # TRANSIENT condition distinct from a genuine quota rate-limit: instead of
        # a long sleep we exponentially back off (base → cap) and only give up,
        # gracefully and resumably, once we've spent `giveup` continuous wall-clock
        # seconds backing off without any successful agent turn in between.
        overload_backoff_base = int(getattr(config, "overload_backoff_base_seconds", 20) or 20)
        overload_backoff_cap = int(getattr(config, "overload_backoff_cap_seconds", 300) or 300)
        overload_giveup_seconds = int(getattr(config, "overload_giveup_seconds", 3600) or 3600)
        # Accumulated continuous-overload wall-clock seconds; reset to 0 on any
        # successful agent turn so isolated overloads don't accumulate over hours.
        overload_backoff_total = 0
        # Current backoff step (the un-jittered delay to use on the NEXT overload).
        overload_backoff_step = overload_backoff_base

        while not dag.is_done() and no_progress_count < max_no_progress_attempts:
            if first_run:
                if resume_session_first:
                    resume_attempt = 0
                    fall_back_to_fresh = False
                    success = False

                    while True:
                        attempt_label = (
                            "first attempt"
                            if resume_attempt == 0
                            else f"retry {resume_attempt}/{resume_subprocess_retry_limit}"
                        )
                        logger.info(f"Attempting to resume previous agent session ({attempt_label})...")
                        orchestrator_logger.info(f"🔁 Agent resume {attempt_label}")
                        self.orchestrator._update_task_queue_file(self.orchestrator.trial_root)
                        success = invoke_agent(
                            lambda: self.agent_runner.send_recover_message(
                                has_new_tasks=True, timeout_ms=recover_timeout_ms
                            )
                        )
                        if success:
                            break

                        # Fatal config error - abort immediately, no retry
                        if self.agent_runner._last_fatal_error:
                            trial_path = self.orchestrator.trial_root
                            logger.error(
                                "⛔ Fatal error: %s\n"
                                "   Fix the model/config, then resume:\n"
                                "     python -m harness.e2e.run_e2e --resume-trial %s",
                                self.agent_runner._last_fatal_error,
                                trial_path,
                            )
                            _set_last_run_summary("fatal_error")
                            return False
                        # DAG completed during the resume attempt - skip fallback
                        if dag.is_done():
                            logger.info("Resume failed but DAG is complete - skipping fallback")
                            break
                        if self.agent_runner._last_model_unavailable:
                            hint = self.agent_runner._last_model_hint or (
                                f"Repeated 500 errors observed for model '{self.model}'. "
                                "This may be transient; if persistent, try a different model alias."
                            )
                            logger.error(
                                "❗ Resume failed with repeated 500 errors; possible model/backend "
                                "compatibility issue (inferred). %s",
                                hint,
                            )
                            orchestrator_logger.error(
                                "❗ Resume failed with repeated 500 errors; possible model/backend "
                                "compatibility issue (inferred): %s",
                                hint,
                            )
                            _set_last_run_summary("model_unavailable")
                            return False
                        if self.agent_runner._last_rate_limit or self.agent_runner._last_auth_error:
                            # Rate limit / auth error - don't destroy session, let the main failure
                            # handler downstream deal with sleep/retry. Don't burn retry budget on it.
                            logger.warning(
                                "Resume failed due to rate limit / auth error - "
                                "skipping fallback to preserve session"
                            )
                            orchestrator_logger.info(
                                "⏳ Resume hit rate limit / auth error - will sleep and retry"
                            )
                            break
                        if self.agent_runner._last_invalid_session:
                            # Session genuinely doesn't exist anymore — retrying same id won't help.
                            logger.warning(
                                "Resume session ID is invalid/expired, clearing persistent session "
                                "and starting fresh."
                            )
                            orchestrator_logger.info("🧹 Invalid session ID detected; starting new agent session")
                            fall_back_to_fresh = True
                            break

                        # Generic failure (non-zero subprocess exit, no specific signal).
                        # Possibly a transient TCP wedge / externally-killed claude.
                        # Retry the SAME session id if budget remains.
                        if resume_attempt < resume_subprocess_retry_limit:
                            resume_attempt += 1
                            if recovery_wait_seconds > 0:
                                logger.warning(
                                    "Resume attempt failed (no specific error); sleeping %ds "
                                    "before retry %d/%d...",
                                    recovery_wait_seconds,
                                    resume_attempt,
                                    resume_subprocess_retry_limit,
                                )
                                orchestrator_logger.warning(
                                    "⏳ Resume retry %d/%d in %ds",
                                    resume_attempt,
                                    resume_subprocess_retry_limit,
                                    recovery_wait_seconds,
                                )
                                time.sleep(recovery_wait_seconds)
                            continue

                        # Retry budget exhausted → fall back to a fresh session.
                        logger.warning(
                            "Resume retries exhausted (%d/%d), falling back to a new agent session...",
                            resume_attempt,
                            resume_subprocess_retry_limit,
                        )
                        orchestrator_logger.info(
                            "🧹 Resume retries exhausted (%d/%d); fallback to new session",
                            resume_attempt,
                            resume_subprocess_retry_limit,
                        )
                        fall_back_to_fresh = True
                        break

                    if fall_back_to_fresh:
                        try:
                            self.agent_runner.invalidate_persistent_session(reason="resume_failure")
                        except Exception as e:
                            logger.warning(f"Failed to invalidate persistent session: {e}")
                        orchestrator_logger.info("🚀 Agent started (fallback new session)")
                        success = invoke_agent(self.agent_runner.run)

                    resume_session_first = False  # Only attempt once
                else:
                    logger.info("Running agent (first run)...")
                    orchestrator_logger.info("🚀 Agent started (first run)")
                    success = invoke_agent(self.agent_runner.run)
                first_run = False
                orchestrator_logger.info("Agent first run completed" + (" ✓" if success else " ✗ (failed)"))
            else:
                logger.info(
                    f"Sending recover message (recover #{recover_count}, has_new_tasks={has_new_tasks}, no_progress={no_progress_count}/{max_no_progress_attempts})..."
                )
                orchestrator_logger.info(f"🔄 Agent recover message sent (recover #{recover_count})")
                self.orchestrator._update_task_queue_file(self.orchestrator.trial_root)
                success = invoke_agent(
                    lambda: self.agent_runner.send_recover_message(
                        has_new_tasks=has_new_tasks, timeout_ms=recover_timeout_ms
                    )
                )
                orchestrator_logger.info(
                    f"Agent recover {recover_count} completed" + (" ✓" if success else " ✗ (failed)")
                )

            if success:
                # A successful agent turn means the endpoint is healthy again;
                # forget any accumulated continuous-overload time so isolated 529s
                # spread across hours never add up to a spurious give-up.
                overload_backoff_total = 0
                overload_backoff_step = overload_backoff_base

            if not success:
                # Check for fatal configuration errors - no point retrying
                if self.agent_runner._last_fatal_error:
                    trial_path = self.orchestrator.trial_root
                    logger.error(
                        "⛔ Fatal error: %s\n"
                        "   Fix the model/config and resume with:\n"
                        "     python -m harness.e2e.run_e2e --resume-trial %s [--model <model>]",
                        self.agent_runner._last_fatal_error,
                        trial_path,
                    )
                    orchestrator_logger.error(
                        "⛔ Fatal error: %s. Aborting trial.", self.agent_runner._last_fatal_error
                    )
                    _set_last_run_summary("fatal_error")
                    return False

                # Check if DAG is already complete - no need to retry/recover
                if dag.is_done():
                    logger.info("Agent failed but DAG is complete - no recovery needed")
                    break
                logger.error("Agent execution failed")
                if self.agent_runner._last_model_unavailable:
                    hint = self.agent_runner._last_model_hint or (
                        f"Repeated 500 errors observed for model '{self.model}'. "
                        "This may be transient; if persistent, try a different model alias."
                    )
                    logger.error(
                        "❗ Aborting trial to avoid futile retries: repeated 500 errors suggest a possible "
                        "model/backend compatibility issue (inferred). %s",
                        hint,
                    )
                    orchestrator_logger.error(
                        "❗ Aborting trial to avoid futile retries: repeated 500 errors suggest a possible "
                        "model/backend compatibility issue (inferred): %s",
                        hint,
                    )
                    _set_last_run_summary("model_unavailable")
                    return False
                if self.agent_runner._last_invalid_session:
                    logger.warning(
                        "Detected invalid session identifier; will invalidate persistent session before next recovery."
                    )
                    orchestrator_logger.warning("⚠️ Invalid session identifier detected; forcing fresh session")
                    try:
                        self.agent_runner.invalidate_persistent_session(reason="invalid_session")
                    except Exception as e:
                        logger.warning(f"Failed to invalidate persistent session: {e}")
                # Transient server overload (HTTP 529): fast exponential backoff
                # instead of a long rate-limit sleep. Only when there is NO parsed
                # real reset time — a genuine quota with a parsed reset falls
                # through to the rate-limit long-sleep below (CRITICAL #2).
                if self.agent_runner._last_overload and not self.agent_runner._rate_limit_reset_seconds:
                    # Give up gracefully once we've spent the whole continuous-overload
                    # budget backing off without a single successful turn in between.
                    if overload_backoff_total >= overload_giveup_seconds:
                        logger.warning(
                            "🛑 ENDPOINT_UNAVAILABLE - gave up after %ds of continuous server "
                            "overload; resume with run_all --repos when endpoint recovers",
                            overload_backoff_total,
                        )
                        orchestrator_logger.warning(
                            "🛑 ENDPOINT_UNAVAILABLE - gave up after %ds of continuous server "
                            "overload; resume with run_all --repos when endpoint recovers",
                            overload_backoff_total,
                        )
                        _set_last_run_summary("endpoint_unavailable")
                        return False
                    # Exponential step capped at the cap, with ±20% jitter so two
                    # concurrent repos hitting 529 don't re-synchronize their retries.
                    base_delay = min(overload_backoff_step, overload_backoff_cap)
                    delay = max(1, int(base_delay * random.uniform(0.8, 1.2)))
                    logger.warning(
                        "🌊 Server overload (HTTP 529) - backing off %ds (continuous %ds/%ds)...",
                        delay, overload_backoff_total, overload_giveup_seconds,
                    )
                    orchestrator_logger.info(
                        "🌊 Server overload (HTTP 529) - backing off %ds (continuous %ds/%ds)",
                        delay, overload_backoff_total, overload_giveup_seconds,
                    )
                    # Backoff waits must not count as "no progress" (mirror rate-limit).
                    no_progress_count = max(0, no_progress_count - 1)
                    # Best-effort credential refresh, but never skip the backoff.
                    if self.agent_runner.refresh_container_credentials():
                        logger.info("🔑 Credentials refreshed from host (overload backoff still required)")
                        orchestrator_logger.info("🔑 Credentials refreshed from host (overload backoff still required)")
                    time.sleep(delay)
                    overload_backoff_total += delay
                    overload_backoff_step = min(overload_backoff_step * 2, overload_backoff_cap)
                    # Session is still valid - resume it (overload is external, not a session problem).
                    continue
                # Check if this was a rate limit - sleep until reset
                if self.agent_runner._last_rate_limit:
                    reset_secs = self.agent_runner._rate_limit_reset_seconds
                    if reset_secs and reset_secs > 0:
                        reset_mins = reset_secs / 60
                        logger.warning(f"⏳ Rate limit hit - sleeping {reset_mins:.0f}m until reset...")
                        orchestrator_logger.info(f"⏳ Rate limit hit - sleeping {reset_mins:.0f}m until reset")
                    else:
                        reset_secs = 3600  # default 1h if we can't parse reset time
                        logger.warning("⏳ Rate limit hit - sleeping 60m (default)...")
                        orchestrator_logger.info("⏳ Rate limit hit - sleeping 60m (default)")
                    # Don't count rate limits as "no progress"
                    no_progress_count = max(0, no_progress_count - 1)
                    # Best-effort credential refresh, but never skip rate-limit wait.
                    if self.agent_runner.refresh_container_credentials():
                        logger.info("🔑 Credentials refreshed from host (rate limit wait still required)")
                        orchestrator_logger.info("🔑 Credentials refreshed from host (rate limit wait still required)")
                    time.sleep(reset_secs)
                    # Session is still valid after rate limit - resume it, don't invalidate
                    # (rate limit is an external constraint, not a session problem)
                    continue
                # Check if this was an auth error - refresh credentials before retry
                elif self.agent_runner._last_auth_error:
                    logger.warning("🔑 Auth error detected - attempting credential refresh from host...")
                    orchestrator_logger.info("🔑 Auth error detected - refreshing credentials from host")
                    if self.agent_runner.refresh_container_credentials():
                        logger.info("🔑 Credentials refreshed - will resume existing session")
                        orchestrator_logger.info("🔑 Credentials refreshed successfully")
                        # Don't count auth failures as "no progress"
                        no_progress_count = max(0, no_progress_count - 1)
                        # Session is still valid - just retry with refreshed credentials
                    else:
                        logger.error("🔑 Credential refresh failed - host token may also be expired")
                        orchestrator_logger.error("🔑 Credential refresh failed")
                # Wait before recovery to give API time to recover from rate limits/overload
                wait_secs = config.recovery_wait_seconds
                logger.info(f"Waiting {wait_secs}s before recovery (API cooldown)...")
                time.sleep(wait_secs)

            # Agent exited - check if we need to wait or recover
            logger.info("Agent exited, checking state...")

            # Wait for pending evaluations using event queue
            wait_result = self._wait_for_evaluations()

            # Check progress after waiting
            curr_state = get_dag_progress_state()
            made_progress = has_progress(prev_state, curr_state)

            if made_progress:
                logger.info(
                    f"Progress detected: completed={len(curr_state['completed'])}, submitted={len(curr_state['submitted'])}"
                )
                made_any_progress = True
                no_progress_count = 0  # Reset no-progress counter
            else:
                no_progress_count += 1
                logger.warning(f"No progress detected ({no_progress_count}/{max_no_progress_attempts})")

            # Update previous state for next iteration
            prev_state = curr_state

            if wait_result == "new_tasks":
                logger.info("New tasks available, recovering agent...")
                has_new_tasks = True
                recover_count += 1
                continue
            elif wait_result == "all_done":
                logger.info("All evaluations complete!")
                break
            elif wait_result == "agent_incomplete":
                # Agent didn't complete all milestones - try to recover
                # No new tasks, but may have untagged commits to remind about
                logger.warning("Agent incomplete, attempting recovery...")
                has_new_tasks = False
                recover_count += 1
                continue
            else:  # timeout
                logger.warning("Timeout waiting for evaluations, attempting recovery...")
                has_new_tasks = False
                recover_count += 1

        # Before stopping watcher, check if there are error evaluations to retry.
        # When DAG is done but some milestones have eval_status=error (infrastructure
        # failure), the watcher should re-evaluate them before we declare completion.
        if dag.is_done():
            summary = self.orchestrator._load_summary_or_init()
            error_milestones = [
                mid for mid, r in summary.get("results", {}).items()
                if r.get("eval_status") == "error"
            ]
            if error_milestones:
                # Agent is done and can't fix these anymore. The watcher won't
                # re-evaluate without a new tag push, so waiting is pointless.
                # Log and continue — treat eval errors as failed milestones.
                logger.warning(f"DAG complete. {len(error_milestones)} milestone(s) have eval errors "
                               f"(infrastructure failures), treating as failed: {error_milestones}")

        # Stop watcher
        self.watcher_stop_event.set()

        if dag.is_done():
            _set_last_run_summary("all_done")
            logger.info("=" * 70)
            logger.info("E2E Trial COMPLETED")
            logger.info(f"  Completed: {len(dag.completed_milestones)}")
            logger.info(f"  Failed: {len(dag.failed_milestones)}")
            logger.info(f"  Skipped: {len(dag.skipped_milestones)}")
            logger.info(f"  Total recover attempts: {recover_count}")
            logger.info("=" * 70)
            return True
        else:
            stop_reason = "no_progress_limit" if no_progress_count >= max_no_progress_attempts else "incomplete"
            _set_last_run_summary(stop_reason)
            remaining = dag.all_milestones - dag.completed_milestones - dag.failed_milestones - dag.skipped_milestones
            logger.warning("=" * 70)
            logger.warning("E2E Trial INCOMPLETE")
            logger.warning(f"  Completed: {len(dag.completed_milestones)}")
            logger.warning(f"  Failed: {len(dag.failed_milestones)}")
            logger.warning(f"  Remaining: {len(remaining)} - {sorted(remaining)}")
            logger.warning(f"  Total recover attempts: {recover_count}")
            logger.warning(f"  Stopped after {no_progress_count} consecutive attempts without progress")
            logger.warning("=" * 70)
            return False

    def _clear_stale_log_files(self):
        """Clear stale log files after --force recreates the container.

        When --force destroys and recreates the container, the OpenHands conversation
        persistence directory inside the container is wiped.  Host-side log files
        (agent_stdout.txt, .agent_session_id, etc.) still reference the old session,
        so we must clear them to prevent extract_session_id from returning stale IDs.
        """
        stale_files = [
            "agent_stdout.txt",
            "agent_stderr.txt",
            ".agent_session_id",
            "session_id.txt",
            "session_history.jsonl",
            "resume_message.txt",
        ]
        cleared = []
        for name in stale_files:
            path = self.agent_output_dir / name
            if path.exists():
                try:
                    path.unlink()
                    cleared.append(name)
                except Exception as e:
                    logger.warning(f"Failed to remove stale log file {name}: {e}")
        if cleared:
            logger.info(f"Cleared stale log files after --force: {', '.join(cleared)}")

    def cleanup(self):
        """Cleanup after trial: copy testbed and optionally remove container."""
        import subprocess
        import shutil

        # Ignore SIGTERM during cleanup so that only kill -9 can interrupt it
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        container_name = self.orchestrator.container_name
        trial_root = self.orchestrator.trial_root

        logger.info("=" * 70)
        logger.info("Running cleanup...")
        logger.info("=" * 70)

        # Extract agent stats BEFORE removing container
        self._extract_agent_stats()

        # Copy testbed from container to trial_root
        if self.copy_testbed:
            testbed_dest = trial_root / "testbed"
            logger.info(f"Copying /testbed from container to {testbed_dest}...")
            try:
                # Create destination directory
                testbed_dest.mkdir(parents=True, exist_ok=True)

                # Use docker cp to copy testbed contents
                result = subprocess.run(
                    ["docker", "cp", f"{container_name}:/testbed/.", str(testbed_dest)],
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout for large repos
                )
                if result.returncode == 0:
                    logger.info(f"✓ Testbed copied to {testbed_dest}")
                else:
                    logger.warning(f"Failed to copy testbed: {result.stderr}")
            except subprocess.TimeoutExpired:
                logger.warning("Timeout copying testbed")
            except Exception as e:
                logger.warning(f"Error copying testbed: {e}")

        # Optionally remove container
        if self.remove_container:
            logger.info(f"Removing container {container_name}...")
            try:
                result = subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    logger.info(f"✓ Container {container_name} removed")
                else:
                    logger.warning(f"Failed to remove container: {result.stderr}")
            except Exception as e:
                logger.warning(f"Error removing container: {e}")
        else:
            logger.info(f"Container {container_name} kept running (use --remove-container to remove)")

        # Release trial lock
        self._release_trial_lock()

        logger.info("Cleanup complete.")

    def _extract_agent_stats(self):
        """Extract and parse agent logs to compute trial statistics.

        This extracts Claude Code JSONL logs from the container and computes
        detailed statistics including tool calls, costs, and token usage.
        """
        container_name = self.orchestrator.container_name
        trial_root = self.orchestrator.trial_root

        logger.info("Extracting agent statistics...")
        try:
            parser = get_parser(self.agent_name)

            # 1. Extract JSONL logs from container (to agent_logs/{agent_name}/)
            logs_dir = parser.extract_raw_logs(container_name, self.agent_output_dir)

            # 2. Parse tool calls
            tool_calls = parser.parse_tool_calls(logs_dir)

            # 3. Update tool calls with result information
            parser.parse_tool_results(logs_dir, tool_calls)

            # 4. Parse agent_stdout.txt statistics (pass logs_dir for raw log parsing)
            stdout_file = self.agent_output_dir / "agent_stdout.txt"
            stdout_stats = parser.parse_stdout_stats(stdout_file, logs_dir)

            # 5. Parse framework-native finest-grained usage units (message/turn)
            native_usage_units = parser.parse_native_usage_units(logs_dir, stdout_file)

            # 6. Get milestone times from git tags
            milestone_times = parser.get_milestone_times(container_name)

            # 7. Compute complete trial statistics
            trial_name = trial_root.name
            model = self.agent_runner.model if hasattr(self.agent_runner, "model") else "unknown"
            session_history_path = self.agent_output_dir / "session_history.jsonl"
            stats = parser.compute_trial_stats(
                trial_name=trial_name,
                model=model,
                tool_calls=tool_calls,
                stdout_stats=stdout_stats,
                milestone_times=milestone_times,
                reasoning_effort=self.reasoning_effort,
                session_history_path=session_history_path,
                native_usage_units=native_usage_units,
                trial_dir=trial_root,
            )

            # 8. Save to agent_stats.json
            stats_path = trial_root / "agent_stats.json"
            stats.to_json(stats_path)
            logger.info(f"✓ Agent stats saved to {stats_path}")
            logger.info(
                f"  {stats.total_tool_calls} tool calls, " f"{stats.total_turns} turns, " f"${stats.total_cost_usd:.2f}"
            )

        except BaseException as e:
            logger.warning(f"Failed to extract agent stats: {e}")

    def _install_sigterm_handler(self):
        """Install SIGTERM handler to convert SIGTERM into KeyboardInterrupt.

        This ensures that `kill <pid>` triggers the existing
        except-KeyboardInterrupt / finally-cleanup flow instead of
        terminating the process immediately.
        """

        def _sigterm_handler(signum, frame):
            logger.info("Received SIGTERM, raising KeyboardInterrupt for graceful shutdown...")
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _sigterm_handler)

    def run(self) -> bool:
        """Run the complete E2E trial."""
        self._install_sigterm_handler()
        self._acquire_trial_lock()
        success = False
        try:
            # Setup environment synchronously BEFORE starting agent
            # This ensures container is ready and task queue is populated
            logger.info("Setting up E2E environment (synchronous)...")
            self.orchestrator.setup_environment(force=self.force)

            # When --force recreates the container, clear stale host-side log files.
            # These files (especially agent_stdout.txt) contain old Conversation IDs
            # that would pollute session_id extraction for the new session.
            if self.force:
                self._clear_stale_log_files()

            self.orchestrator._update_task_queue_file(self.orchestrator.trial_root)
            logger.info("E2E environment ready, task queue populated")

            # Start watcher in background (only monitors for tags now)
            self.start_watcher_thread()

            # Run agent with recovery
            success = self.run_agent_with_recovery()
            return success

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            self.watcher_stop_event.set()
            return False

        finally:
            # Always run cleanup
            self.cleanup()

    def run_resume(self, trial_state, resume_session: bool = True) -> bool:
        """Run the E2E trial in resume mode.

        Args:
            trial_state: TrialState object with restored state
            resume_session: If True, attempt to resume the previous agent session first.
                            If False, force creation of a new agent session.

        Returns:
            True if trial completed successfully
        """
        requested_resume_session = bool(resume_session)
        allow_run, resume_session = self._apply_resume_no_progress_policy(requested_resume_session)
        if not allow_run:
            logger.warning("Resume-trial stopped by persisted no-progress policy before agent startup.")
            return False

        self._install_sigterm_handler()
        self._acquire_trial_lock()
        success = False
        try:
            if not resume_session:
                # Force creation of new session by deleting persistent session ID
                old_session_file = self.agent_output_dir / ".agent_session_id"
                if old_session_file.exists():
                    old_session_id = old_session_file.read_text().strip()
                    logger.info(f"Removing old session ID file ({old_session_id[:8]}...) - will create new session")
                    old_session_file.unlink()
            else:
                logger.info("Resume mode: will attempt to resume previous agent session (fallback to new if needed)")

            # Prime watcher state (avoid re-scan/debounce when possible)
            self._resume_pending_debounce = dict(getattr(trial_state, "pending_debounce", {}) or {})
            self._resume_pending_evaluations = dict(getattr(trial_state, "pending_evaluations", {}) or {})

            # Setup environment for resume (reuse container, restore state)
            logger.info("Setting up E2E environment for RESUME...")
            self.orchestrator.setup_environment_for_resume(
                completed_milestones=trial_state.completed_milestones,
                failed_milestones=trial_state.failed_milestones,
                skipped_milestones=trial_state.skipped_milestones,
                early_unlocked_milestones=trial_state.early_unlocked_milestones,
                submitted_milestones=getattr(trial_state, "submitted_milestones", set()),
                evaluated_hashes=trial_state.evaluated_hashes,
            )
            self.orchestrator._update_task_queue_file(self.orchestrator.trial_root)
            logger.info("E2E environment ready for resume, task queue updated")

            # Start watcher in background
            self.start_watcher_thread()

            # Run agent with recovery
            success = self.run_agent_with_recovery(resume_session_first=resume_session)
            self._record_resume_run_outcome(
                resume_session_requested=requested_resume_session,
                resume_session_used=resume_session,
                success=success,
            )
            return success

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            self.watcher_stop_event.set()
            return False

        finally:
            # Always run cleanup
            self.cleanup()


def _run_resume_mode(args):
    """Run in resume mode - restore state from previous trial and continue.

    Args:
        args: Parsed command line arguments (must have resume_trial set)
    """
    from harness.e2e.resume import TrialStateLoader, verify_container_for_resume

    trial_root = args.resume_trial.resolve()
    logger.info(f"Resuming trial from: {trial_root}")

    # Acquire exclusive lock before loading state (works without metadata —
    # workspace_root is derived from trial_root path: <ws>/e2e_trial/<name>).
    trial_name = trial_root.name
    workspace_root_for_lock = trial_root.parent.parent
    _trial_lock = acquire_trial_lock(workspace_root_for_lock, trial_name, force=args.force)

    # Load and validate trial state
    loader = TrialStateLoader(trial_root)
    is_valid, errors = loader.validate()
    if not is_valid:
        logger.error("Cannot resume trial - validation failed:")
        for err in errors:
            logger.error(f"  - {err}")
        sys.exit(1)

    trial_state = loader.load()

    # Verify container is available
    is_valid, issues = verify_container_for_resume(trial_state.container_name)
    if not is_valid:
        logger.error("Cannot resume trial - container issues:")
        for issue in issues:
            logger.error(f"  - {issue}")
        sys.exit(1)

    # Extract config from original metadata, allow CLI overrides
    metadata = trial_state.original_config
    metadata_schema = metadata.get("trial_metadata_schema_version", 1)
    runtime_policy_binding = None
    if (
        "runtime_policy_binding" in metadata
        or (
            isinstance(metadata_schema, int)
            and not isinstance(metadata_schema, bool)
            and metadata_schema
            >= TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING
        )
    ):
        runtime_policy_binding = load_trial_runtime_policy_binding(
            trial_root,
            metadata,
            expected_repo_name=metadata.get("repo_name"),
        )
        _activate_runtime_policy(runtime_policy_binding)
        logger.info(
            "Resume: restored trial-pinned runtime policy %s (%s)",
            runtime_policy_binding.sha256,
            runtime_policy_binding.mode,
        )
    else:
        # Explicit legacy compatibility only.  These results remain labelled
        # legacy-live and are not promotion-grade.
        from harness.e2e.quarantine import metadata_wants_unprotected

        if metadata_wants_unprotected(metadata):
            os.environ["SWE_MILESTONE_UNPROTECTED"] = "1"
            logger.info(
                "Resume: legacy trial was launched --unprotected; keeping it open"
            )
        logger.warning(
            "Resume: legacy trial has no pinned runtime policy; live policy "
            "recovery is forensic-only and must not be mixed into a promotion"
        )
    # --model override for resume (e.g., fix a wrong model after fatal error)
    if getattr(args, '_model_explicitly_set', False):
        logger.info(f"Overriding model: {metadata.get('model')} → {args.model}")
        metadata["model"] = args.model
    requested_agent_version = metadata.get("requested_agent_version")
    workspace_root = Path(metadata["workspace_root"]).resolve()

    repo_config_binding = load_trial_repo_config_binding(
        trial_root,
        metadata,
        expected_repo_name=metadata.get("repo_name"),
    )
    if repo_config_binding is None:
        logger.warning(
            "Resume: legacy trial has no pinned repository config; results are "
            "forensic/legacy-unbound and must not be mixed into a pinned promotion"
        )

    # New trials persist the complete snapshot-authority filter.  Resume must
    # not rebuild it from a live metadata.json, because a dataset edit between
    # fresh and resume would otherwise change which submitted files exist in
    # later snapshots.  Legacy trials retain their historical fallback.
    if repo_config_binding is not None:
        required_capture_fields = ("repo_src_dirs", "test_dirs", "exclude_patterns")
        missing_capture_fields = [
            field for field in required_capture_fields if field not in metadata
        ]
        if missing_capture_fields:
            raise RuntimeError(
                "Pinned trial metadata is missing snapshot capture field(s): "
                + ", ".join(missing_capture_fields)
            )
        repo_src_dirs = metadata["repo_src_dirs"]
        test_dirs = metadata["test_dirs"]
        exclude_patterns = metadata["exclude_patterns"]
        generated_patterns = metadata.get("generated_patterns", [])
        modifiable_test_patterns = metadata.get("modifiable_test_patterns", [])
        logger.info("Resume: using trial-persisted snapshot capture filter")
    else:
        workspace_metadata = load_workspace_metadata(workspace_root)
        repo_src_dirs = workspace_metadata["repo_src_dirs"]
        test_dirs = workspace_metadata["test_dirs"]
        exclude_patterns = workspace_metadata["exclude_patterns"]
        generated_patterns = workspace_metadata.get("generated_patterns", [])
        modifiable_test_patterns = workspace_metadata.get("modifiable_test_patterns", [])

    # Resolve dag_path from original metadata or default
    dag_path_str = metadata.get("dag_path")
    if dag_path_str:
        dag_path = Path(dag_path_str)
    else:
        dag_path = workspace_root / "dependencies.csv"

    if not dag_path.exists():
        logger.error(f"DAG file not found: {dag_path}")
        sys.exit(1)

    # Use trial-level config
    trial_config_path = trial_root / "e2e_config.yaml"
    if not trial_config_path.exists():
        trial_config_path = None

    logger.info(f"Resuming trial: {trial_name}")
    logger.info(f"  Completed: {len(trial_state.completed_milestones)}")
    logger.info(f"  Failed: {len(trial_state.failed_milestones)}")
    logger.info(f"  Skipped: {len(trial_state.skipped_milestones)}")
    logger.info(f"  Container: {trial_state.container_name}")

    # Initialize Orchestrator
    orchestrator = E2EOrchestrator(
        repo_name=metadata["repo_name"],
        milestone_version=metadata.get("milestone_version", "test_multi_stage_v2"),
        image_name=metadata["image"],
        dag_path=dag_path,
        srs_root=Path(metadata["srs_root"]),
        trial_root=trial_root,
        workspace_root=workspace_root,
        agent_name=metadata.get("agent_name", "claude-code"),
        model=metadata.get("model", "claude-sonnet-4-5-20250929"),
        config_path=trial_config_path,
        repo_src_dirs=repo_src_dirs,
        test_dirs=test_dirs,
        exclude_patterns=exclude_patterns,
        generated_patterns=generated_patterns,
        modifiable_test_patterns=modifiable_test_patterns,
        reasoning_effort=metadata.get("reasoning_effort"),
        agent_version=requested_agent_version,
        build_failure_fail_closed=metadata.get("build_failure_fail_closed"),
        repo_config_binding=repo_config_binding,
        runtime_policy_binding=runtime_policy_binding,
    )

    # Prepare agent output directory (reuse existing)
    agent_output_dir = trial_root / "log"
    agent_output_dir.mkdir(parents=True, exist_ok=True)

    # Create trial runner
    trial = E2ETrialRunner(
        orchestrator=orchestrator,
        agent_output_dir=agent_output_dir,
        workdir="/testbed",
        repo_src_dirs=repo_src_dirs,
        agent_name=metadata.get("agent_name", "claude-code"),
        model=metadata.get("model", "claude-sonnet-4-5-20250929"),
        timeout_ms=metadata.get("timeout_seconds", 3600) * 1000,
        prompt_version=metadata.get("prompt_version", "v2"),
        copy_testbed=not args.skip_testbed_copy,
        remove_container=args.remove_container,
        reasoning_effort=metadata.get("reasoning_effort"),
    )

    # Run with resume mode
    success = trial.run_resume(trial_state, resume_session=not args.no_resume_session)
    sys.exit(0 if success else 1)


def main():
    reject_legacy_env()  # legacy EVOCLAW_* -> hard error with rename map
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Run End-to-End Agent Trial (Continuous Task Queue Mode with Recovery)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python run_e2e.py \\
    --repo-name urllib3_urllib3_2.0.6_2.3.0 \\
    --image urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/base:latest \\
    --dag-path DATA/.../dependencies.csv \\
    --srs-root DATA/.../srs/v1 \\
    --workspace-root DATA/... \\
    --prompt-version v1
        """,
    )

    # Project Config (required for fresh start, optional for resume)
    parser.add_argument("--repo-name", default=None, help="Repository name (required for fresh start)")
    parser.add_argument("--milestone-version", default="test_multi_stage_v2", help="Milestone version string")
    parser.add_argument("--image", default=None, help="Base docker image for agent (required for fresh start)")
    parser.add_argument(
        "--unprotected",
        action="store_true",
        help="Bypass the quarantine fail-closed guard (run a repo that has a "
        "policy WITHOUT applying it — scores may be tainted). Normally you launch "
        "via scripts/run_all.py, which applies the policy and runs the gate.",
    )
    parser.add_argument(
        "--expected-runtime-policy-sha256",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--expected-runtime-policy-mode",
        choices=["protected", "absent", "unprotected"],
        default=None,
        help=argparse.SUPPRESS,
    )

    # Paths (required for fresh start, optional for resume)
    parser.add_argument(
        "--dag-path",
        type=Path,
        default=None,
        help="Path to dependencies.csv (default: {workspace-root}/dependencies.csv)",
    )
    parser.add_argument(
        "--srs-root", type=Path, default=None, help="Root directory containing SRS folders (required for fresh start)"
    )
    parser.add_argument(
        "--workspace-root", type=Path, default=None, help="Harness workspace root (required for fresh start)"
    )
    # Note: --trial-root is now auto-generated under workspace-root/e2e_trial/

    # Agent Config
    parser.add_argument(
        "--agent",
        default="claude-code",
        choices=["claude-code", "codex", "gemini-cli", "openhands"],
        help="Agent framework to use (default: claude-code)",
    )
    parser.add_argument("--model", default="claude-sonnet-4-5-20250929", help="Claude model ID")
    parser.add_argument(
        "--agent-version",
        type=validate_claude_code_version,
        default=None,
        help="Claude Code CLI version to install: an exact version (for example 2.1.158), stable, or latest.",
    )
    parser.add_argument("--prompt-version", default="v2", help="Prompt template version (e.g., v1 or v2)")
    parser.add_argument(
        "--milestones",
        default=None,
        help=(
            "Run only a dependency-closed prefix of the milestone DAG. Accepts a count "
            "('10') or a percentage ('50%%'). Selection is the first N in topological order "
            "(ascending-ID tiebreak), so prerequisites are always included; percentages round "
            "UP (ceil). Writes the chosen IDs to <trial_root>/milestone_selection.txt without "
            "touching the dataset's selected_milestone_ids.txt."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Agent execution timeout in seconds (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        choices=["low", "medium", "high", "xhigh", "max", "none", "off"],
        help="Reasoning effort (low|medium|high|xhigh|max). Use 'none' or 'off' to explicitly disable for upstreams that reject reasoning_effort (e.g. Moonshot kimi via OpenRouter returns HTTP 400 UnsupportedParamsError). Default: unset → agent uses model's built-in default (e.g. opus-4-7=xhigh, sonnet/opus-4-6=high; OpenHands auto-disables for kimi/moonshot). For claude-code, also passed via CLAUDE_CODE_EFFORT_LEVEL env to work around upstream bug #41028.",
    )

    # Config
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to e2e_config.yaml (default: search in workspace-root, then harness/e2e/)",
    )
    build_failure_policy = parser.add_mutually_exclusive_group()
    build_failure_policy.add_argument(
        "--allow-partial-build-reports",
        dest="build_failure_fail_closed",
        action="store_false",
        help=(
            "On deterministic compilation/build failures, score reports from "
            "packages/modules that completed. Timeouts and nonzero outer-runner "
            "exits remain fail-closed."
        ),
    )
    build_failure_policy.add_argument(
        "--fail-closed-build-reports",
        dest="build_failure_fail_closed",
        action="store_true",
        help=(
            "Strict opt-in: reject partial reports after deterministic "
            "compilation/build failures."
        ),
    )
    parser.set_defaults(build_failure_fail_closed=None)

    # Trial naming
    parser.add_argument(
        "--trial-name",
        type=str,
        default=None,
        help="Custom trial name base (e.g., 'v2_sonnet_urllib3'). Auto-increments if exists.",
    )

    # Cleanup options
    parser.add_argument(
        "--skip-testbed-copy",
        action="store_true",
        help="Skip copying /testbed from container to trial directory (default: copy testbed)",
    )
    parser.add_argument(
        "--remove-container",
        action="store_true",
        help="Remove container after trial completes (default: keep container running)",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Force remove existing container with the same name before starting a fresh trial.",
    )

    # Resume mode
    parser.add_argument(
        "--resume-trial",
        type=Path,
        default=None,
        help="Resume from existing trial directory (container must exist). Mutually exclusive with fresh start options.",
    )
    parser.add_argument(
        "--no-resume-session",
        action="store_true",
        help="In --resume-trial mode, do not attempt to resume the previous agent session (force a new session).",
    )

    args = parser.parse_args()

    if (args.expected_runtime_policy_sha256 is None) != (
        args.expected_runtime_policy_mode is None
    ):
        parser.error(
            "--expected-runtime-policy-sha256 and "
            "--expected-runtime-policy-mode must be provided together"
        )

    # --unprotected: signal ContainerSetup NOT to recover quarantine from policy
    # (operator explicitly wants an open baseline run) — covers fresh AND resume,
    # both of which construct ContainerSetup (F2-b).
    if getattr(args, "unprotected", False):
        os.environ["SWE_MILESTONE_UNPROTECTED"] = "1"

    # Track whether --model was explicitly provided (vs default)
    args._model_explicitly_set = '--model' in sys.argv

    # Handle resume mode
    if args.resume_trial:
        if args.expected_runtime_policy_sha256 is not None:
            parser.error(
                "expected runtime-policy identity is fresh-worker-only; resume "
                "uses the trial-frozen binding"
            )
        if args.agent_version:
            parser.error(
                "--agent-version cannot change an existing container during --resume-trial; "
                "the version recorded by the original trial is reused"
            )
        _run_resume_mode(args)
        return

    # Fresh start mode - validate required arguments
    missing_args = []
    if not args.repo_name:
        missing_args.append("--repo-name")
    if not args.image:
        missing_args.append("--image")
    if not args.srs_root:
        missing_args.append("--srs-root")
    if not args.workspace_root:
        missing_args.append("--workspace-root")

    if missing_args:
        parser.error(f"the following arguments are required for fresh start: {', '.join(missing_args)}")
    if args.agent_version and args.agent != "claude-code":
        parser.error("--agent-version is currently supported only with --agent claude-code")

    # Resolve the complete runtime policy once, before any container exists.
    # This replaces any partial/stale inherited marker environment and gives a
    # direct run_e2e launch the same policy as scripts/run_all.py.
    project_root = Path(__file__).resolve().parent.parent.parent
    resolved_runtime_policy = resolve_runtime_policy(
        args.repo_name,
        project_root,
        unprotected=args.unprotected,
    )
    verify_expected_runtime_policy(
        resolved_runtime_policy,
        expected_sha256=args.expected_runtime_policy_sha256,
        expected_mode=args.expected_runtime_policy_mode,
    )
    if resolved_runtime_policy.mode == RUNTIME_POLICY_MODE_PROTECTED:
        policy_errors = runtime_policy_coverage_errors(resolved_runtime_policy)
        if policy_errors:
            for error in policy_errors:
                logger.error(error)
            raise RuntimePolicyBindingError(
                "runtime policy failed quarantine coverage validation"
            )
    _activate_runtime_policy(resolved_runtime_policy)

    from harness.e2e.quarantine import quarantine_guard_error_from_config

    _guard = quarantine_guard_error_from_config(
        args.repo_name,
        (
            resolved_runtime_policy.policy
            if resolved_runtime_policy.source_path is not None
            else None
        ),
        quarantine_active=bool(os.environ.get("SWE_MILESTONE_QUARANTINE")),
        unprotected=args.unprotected,
    )
    if _guard:
        logger.error(_guard)
        sys.exit(1)

    # Setup Paths
    workspace_root = args.workspace_root.resolve()
    resolved_repo_config = resolve_repo_config(args.repo_name, workspace_root)

    # Load workspace metadata (repo_src_dirs, test_dirs, exclude_patterns)
    # These fields are required and will raise an error if missing
    workspace_metadata = load_workspace_metadata(
        workspace_root,
        repo_config=dict(resolved_repo_config.config),
    )
    repo_src_dirs = workspace_metadata["repo_src_dirs"]
    test_dirs = workspace_metadata["test_dirs"]
    exclude_patterns = workspace_metadata["exclude_patterns"]
    generated_patterns = workspace_metadata.get("generated_patterns", [])  # Optional
    modifiable_test_patterns = workspace_metadata.get("modifiable_test_patterns", [])  # Optional

    # Resolve dag_path: CLI > default (workspace_root/dependencies.csv)
    if args.dag_path is None:
        dag_path = workspace_root / "dependencies.csv"
        if not dag_path.exists():
            logger.error(f"dependencies.csv not found at {dag_path}")
            logger.error("Please provide --dag-path explicitly or ensure dependencies.csv exists in workspace-root")
            sys.exit(1)
        logger.info(f"Using default dag_path: {dag_path}")
    else:
        dag_path = args.dag_path.resolve()
        if not dag_path.exists():
            logger.error(f"dependencies.csv not found at {dag_path}")
            sys.exit(1)

    # Create e2e_trial directory for all trials
    e2e_trial_dir = workspace_root / "e2e_trial"
    e2e_trial_dir.mkdir(parents=True, exist_ok=True)

    # Benchmark-version gate (docs/versioning.md): verify the data checkout
    # (workspace_root lives inside the data repo) and the image tag against
    # the pinned version BEFORE any trial state exists — an explicit-pin
    # refusal must not leave behind a populated trial dir that blocks the
    # retry. The verdicts are persisted into trial_metadata below so the
    # version a score belongs to is recorded, not inferred.
    from harness.e2e.data_version import (
        check_data_version,
        check_image_tag_consistency,
    )

    version_meta = check_data_version(Path(args.workspace_root), context="run_e2e")
    image_check_meta = (
        check_image_tag_consistency(str(args.image), context="run_e2e")
        if args.image
        else None
    )

    # Generate next trial name with auto-incrementing suffix
    trial_base_name = args.trial_name if args.trial_name else "agent_run"
    trial_name = get_next_trial_name(trial_base_name, e2e_trial_dir)
    trial_root = e2e_trial_dir / trial_name

    # Acquire exclusive lock on (workspace_root, trial_name) before any
    # state-modifying work. Bound to a local so the fd stays open for the
    # life of the process; the kernel releases the flock on exit.
    _trial_lock = acquire_trial_lock(workspace_root, trial_name, force=args.force)

    # Refuse to overwrite an existing trial directory (prevents silent data loss)
    if trial_root.exists() and any(trial_root.iterdir()):
        if args.force:
            logger.warning(f"--force: wiping existing trial directory '{trial_root}'")
            import shutil
            try:
                shutil.rmtree(trial_root)
            except (PermissionError, OSError) as exc:
                # Native rmtree can fail on:
                #   - PermissionError: docker-copied files with root ownership
                #   - OSError ENOTEMPTY: Python 3.12's _rmtree_safe_fd is
                #     sensitive to deeply-nested trees (e.g., testbed/
                #     ui/node_modules) and can race itself on some filesystems
                # Fall back to an alpine container running as root, which is
                # robust against both. We deliberately *never* abort --force
                # here: even if the host-side dir can't be fully cleaned, the
                # docker rm + new container later will reset the in-container
                # state, which is what matters. Host-side residue is logged
                # for manual cleanup but doesn't block the trial.
                logger.warning(
                    f"Native rmtree failed ({exc.__class__.__name__}: {exc}); "
                    f"falling back to alpine container"
                )
                import subprocess as _sp
                # Clear CONTENTS via find -mindepth 1 -delete: robust against
                # empty dirs, hidden files, and weird filenames; doesn't touch
                # the mount point itself (can't unlink an active bind target).
                # 300s accommodates 400MB+ node_modules trees with many
                # thousands of tiny files.
                try:
                    result = _sp.run(
                        [
                            "docker", "run", "--rm",
                            "-v", f"{trial_root}:/data",
                            "alpine", "find", "/data", "-mindepth", "1", "-delete",
                        ],
                        capture_output=True, timeout=300,
                    )
                    if result.returncode != 0:
                        logger.warning(
                            f"Alpine rm exited {result.returncode}; "
                            f"stderr: {result.stderr.decode('utf-8', errors='replace')[:500]}"
                        )
                except _sp.TimeoutExpired:
                    logger.warning(
                        "Alpine rm timed out after 300s; residual files may remain"
                    )
                except Exception as alpine_exc:
                    logger.warning(f"Alpine rm raised {alpine_exc.__class__.__name__}: {alpine_exc}")

                # After alpine cleanup, the shell dir may still be there
                # (mount point can't self-delete). Try to drop it; if residue
                # remains, proceed with a loud warning rather than aborting.
                if trial_root.exists() and any(trial_root.iterdir()):
                    logger.warning(
                        f"Trial dir {trial_root} has residual files after alpine "
                        f"cleanup — proceeding with --force anyway (container "
                        f"will be fresh). Consider `rm -rf {trial_root}` manually "
                        f"after this trial."
                    )
                elif trial_root.exists():
                    try:
                        trial_root.rmdir()
                    except OSError:
                        pass  # benign; will be re-mkdir'd below
        else:
            logger.error(
                f"Trial directory already exists and is not empty: {trial_root}\n"
                f"  To resume this trial:  python -m harness.e2e.run_e2e --resume-trial {trial_root}\n"
                f"  To start fresh:        add --force to remove existing data\n"
                f"  To create a new trial: use a different --trial-name"
            )
            sys.exit(1)

    logger.info(f"Creating new trial: {trial_name}")
    logger.info(f"Trial artifacts path: {trial_root}")
    trial_root.mkdir(parents=True, exist_ok=True)

    # Freeze the exact repository evaluation config before any milestone is
    # captured.  Every worker and resume path consumes this byte-identical
    # copy, so a live data-root YAML edit cannot switch semantics mid-trial.
    repo_config_binding = freeze_repo_config(trial_root, resolved_repo_config)
    runtime_policy_binding = freeze_runtime_policy(
        trial_root,
        resolved_runtime_policy,
    )
    _activate_runtime_policy(runtime_policy_binding)

    # Copy config and selected_milestone_ids to trial directory
    import shutil

    # Determine config source path (priority: --config > workspace > harness/e2e)
    if args.config and args.config.exists():
        config_source = args.config.resolve()
    elif (workspace_root / "e2e_config.yaml").exists():
        config_source = workspace_root / "e2e_config.yaml"
    else:
        config_source = Path(__file__).parent / "e2e_config.yaml"

    # Copy config to trial root
    trial_config_path = trial_root / "e2e_config.yaml"
    if config_source.exists():
        shutil.copy(config_source, trial_config_path)
        logger.info(f"Copied config from {config_source} to trial directory")
    else:
        logger.warning(f"Config not found at {config_source}, will use defaults")
        trial_config_path = None

    # Copy selected_milestone_ids.txt if exists (don't overwrite pre-existing trial-level file)
    selected_milestones_dst = trial_root / "selected_milestone_ids.txt"
    selected_milestones_src = workspace_root / "selected_milestone_ids.txt"
    if selected_milestones_dst.exists():
        logger.info(f"Using existing selected_milestone_ids.txt in trial directory")
    elif selected_milestones_src.exists():
        shutil.copy(selected_milestones_src, selected_milestones_dst)
        logger.info(f"Copied selected_milestone_ids.txt to trial directory")

    # Copy milestones.csv and dependencies.csv to trial directory (don't overwrite pre-existing)
    for csv_name in ["milestones.csv", "dependencies.csv", "additional_dependencies.csv"]:
        dst = trial_root / csv_name
        src = workspace_root / csv_name
        if dst.exists():
            logger.info(f"Using existing {csv_name} in trial directory")
        elif src.exists():
            shutil.copy(src, dst)
            logger.info(f"Copied {csv_name} to trial directory")

    # --milestones: write a dependency-closed prefix to a NEW trial-level file
    # (milestone_selection.txt), read by the orchestrator in preference to
    # selected_milestone_ids.txt. The dataset's selected_milestone_ids.txt is never modified.
    if getattr(args, "milestones", None):
        from harness.e2e.milestone_selection import (
            select_prefix, read_base_ids, write_selection,
        )
        deps_csv = trial_root / "dependencies.csv"
        ms_csv = trial_root / "milestones.csv"
        base_ids = read_base_ids(trial_root / "selected_milestone_ids.txt")  # dataset copy, if any
        selected = select_prefix(
            deps_csv, args.milestones,
            milestones_csv=ms_csv if ms_csv.exists() else None,
            base_ids=base_ids,
        )
        sel_path = write_selection(selected, trial_root / "milestone_selection.txt")
        logger.info(
            f"--milestones {args.milestones}: selected {len(selected)} milestone(s) "
            f"(dependency-closed) -> {sel_path.name}: {selected}"
        )

    # Save trial metadata (including model info)
    from datetime import datetime
    from harness.e2e.agents import get_agent_framework

    # Resolve the effective reasoning effort from the agent framework
    _framework_kwargs = {}
    if args.reasoning_effort:
        _framework_kwargs["reasoning_effort"] = args.reasoning_effort
    _tmp_framework = get_agent_framework(args.agent, **_framework_kwargs)
    effective_reasoning_effort = _tmp_framework.get_effective_reasoning_effort()

    # Resolve once and persist so resume cannot silently change scoring policy
    # when the workspace/default config changes later.
    from harness.e2e.config import E2EConfig

    effective_build_failure_fail_closed = args.build_failure_fail_closed
    if effective_build_failure_fail_closed is None:
        effective_build_failure_fail_closed = E2EConfig(trial_config_path).build_failure_fail_closed

    trial_metadata = {
        "trial_metadata_schema_version": (
            TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING
        ),
        "trial_name": trial_name,
        "repo_name": args.repo_name,
        "milestone_version": args.milestone_version,
        # Benchmark version this trial claims to run on (vX.Y, the image tag —
        # docs/versioning.md), plus the verified state of the data checkout
        # and of the image tag (a :latest fallback shows up here as a
        # structured mismatch, not just a transient launch warning).
        "benchmark_version": version_meta["benchmark_version"],
        "data_version": version_meta["data_version"],
        "image_tag_check": image_check_meta,
        "image": args.image,
        "model": args.model,
        "agent_name": args.agent,
        # The requested selector is known before container startup; the actual
        # numeric version is detected and filled after initialization.
        "requested_agent_version": args.agent_version,
        "agent_version": None,
        # Filled from docker container inspect after launch. A full image ID,
        # unlike the user-facing tag, cannot be silently retargeted.
        "agent_image_id": None,
        "prompt_version": args.prompt_version,
        "timeout_seconds": args.timeout,
        "reasoning_effort": effective_reasoning_effort,
        "build_failure_fail_closed": effective_build_failure_fail_closed,
        # Native claude-code context-compaction window in tokens. Set from the
        # trial config `auto_compact_window` (propagated via the
        # SWE_MILESTONE_AUTO_COMPACT_WINDOW env var); recorded here so the monitor can
        # display it. None when the config doesn't set it.
        "auto_compact_window": os.environ.get("SWE_MILESTONE_AUTO_COMPACT_WINDOW"),
        "repo_src_dirs": repo_src_dirs,
        "test_dirs": test_dirs,
        "exclude_patterns": exclude_patterns,
        "generated_patterns": generated_patterns,
        "modifiable_test_patterns": modifiable_test_patterns,
        "start_time": datetime.now().isoformat(),
        "dag_path": str(dag_path),
        "srs_root": str(args.srs_root),
        "workspace_root": str(args.workspace_root),
        # Persist --unprotected so a resumed open baseline stays open (isn't
        # silently re-hardened by ContainerSetup's policy recovery on resume).
        "unprotected": bool(args.unprotected),
        "repo_config_binding": repo_config_binding.to_metadata(trial_root),
        "runtime_policy_binding": runtime_policy_binding.to_metadata(trial_root),
    }
    metadata_path = trial_root / "trial_metadata.json"
    with open(metadata_path, "w") as f:
        json.dump(trial_metadata, f, indent=2)
    logger.info(f"Saved trial metadata to {metadata_path}")

    # Initialize Orchestrator (use trial-level config)
    orchestrator = E2EOrchestrator(
        repo_name=args.repo_name,
        milestone_version=args.milestone_version,
        image_name=args.image,
        dag_path=dag_path,
        srs_root=args.srs_root,
        trial_root=trial_root,
        workspace_root=args.workspace_root,
        agent_name=args.agent,
        model=args.model,
        config_path=trial_config_path,  # Use trial-level config
        repo_src_dirs=repo_src_dirs,  # Pure source directories (for SrcFileFilter)
        test_dirs=test_dirs,  # Test directory patterns for SrcFileFilter
        exclude_patterns=exclude_patterns,  # Exclude patterns for SrcFileFilter
        generated_patterns=generated_patterns,  # Generated code patterns for snapshot inclusion
        modifiable_test_patterns=modifiable_test_patterns,  # Test files agent can modify
        reasoning_effort=args.reasoning_effort,
        agent_version=args.agent_version,
        build_failure_fail_closed=effective_build_failure_fail_closed,
        repo_config_binding=repo_config_binding,
        runtime_policy_binding=runtime_policy_binding,
    )

    # Prepare agent output directory
    agent_output_dir = trial_root / "log"
    agent_output_dir.mkdir(parents=True, exist_ok=True)

    # Create and run trial
    trial = E2ETrialRunner(
        orchestrator=orchestrator,
        agent_output_dir=agent_output_dir,
        workdir="/testbed",
        repo_src_dirs=repo_src_dirs,
        agent_name=args.agent,
        model=args.model,
        timeout_ms=args.timeout * 1000,
        prompt_version=args.prompt_version,
        copy_testbed=not args.skip_testbed_copy,
        remove_container=args.remove_container,
        reasoning_effort=args.reasoning_effort,
        force=args.force,
    )

    success = trial.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
