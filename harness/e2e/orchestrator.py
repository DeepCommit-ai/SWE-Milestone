import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
import traceback
import concurrent.futures
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from harness.e2e.dag import DAGManager
from harness.e2e.evaluator import InfrastructureFailureError, PatchEvaluator, EvaluationResult
from harness.e2e.config import E2EConfig
from harness.e2e.container_setup import ContainerSetup, inspect_docker_image_id
from harness.e2e.repo_config_binding import RepoConfigBinding, RepoConfigBindingError
from harness.e2e.runtime_policy_binding import (
    RuntimePolicyBinding,
    RuntimePolicyBindingError,
)
from harness.e2e.residue_prune import (
    capture_filter_config as _capture_filter_config,
    check_snapshot_integrity,
    classify_capture_loss,
    normalize_tar_members,
    parse_status_porcelain_z,
)
from harness.utils.src_filter import SrcFileFilter
from harness.utils.snapshot import (
    ManifestOverlay,
    ROOT_BUILD_FILES,
    expand_atomic_manifest_overlay,
    find_build_manifests,
    get_snapshot_paths,
    make_snapshot_metadata,
    should_include_snapshot_file,
)
from harness.test_runner.core.milestone_attempt import RunnerInfrastructureError

logger = logging.getLogger("e2e.orchestrator")


def _is_transient_error(e: BaseException) -> bool:
    """Errors worth an evaluation retry: environment/infrastructure trouble,
    not deterministic evaluator bugs. InfrastructureFailureError (F-2a) is
    transient by definition — the runtime may recover between attempts."""
    return (
        isinstance(e, (InfrastructureFailureError, RunnerInfrastructureError))
        or "Broken pipe" in str(e)
        or "Connection reset" in str(e)
        or "Temporary failure" in str(e)
        or isinstance(e, (OSError, IOError, BrokenPipeError))
    )


def _run_evaluation_once(
    milestone_id: str,
    snapshot_path: Path,
    result_dir: Path,
    workspace_root: Path,
    fail_to_pass_threshold: float,
    pass_to_pass_threshold: float,
    none_to_pass_threshold: float,
    baseline_json: Path,
    eval_result_path: Path,
    agent_attempt: int = 0,
    build_failure_fail_closed: bool = False,
    repo_config_path: Optional[Path] = None,
    repo_config_sha256: Optional[str] = None,
    runtime_policy_path: Optional[Path] = None,
    runtime_policy_sha256: Optional[str] = None,
    runtime_policy_mode: Optional[str] = None,
) -> Tuple[str, bool, Optional[EvaluationResult], Optional[str]]:
    """Single evaluation attempt. Returns (milestone_id, is_resolved, eval_res, error_msg)."""
    evaluator = PatchEvaluator(
        workspace_root=workspace_root,
        milestone_id=milestone_id,
        patch_file=snapshot_path,
        baseline_classification=baseline_json,
        output_dir=result_dir,
        agent_attempt=agent_attempt,
        build_failure_fail_closed=build_failure_fail_closed,
        repo_config_path=repo_config_path,
        repo_config_sha256=repo_config_sha256,
        runtime_policy_path=runtime_policy_path,
        runtime_policy_sha256=runtime_policy_sha256,
        runtime_policy_mode=runtime_policy_mode,
    )
    eval_res = evaluator.evaluate()

    with open(eval_result_path, "w") as f:
        import json

        json.dump(eval_res.to_dict(), f, indent=2)

    # F-2a: an infrastructure failure poisoned the test run. The flagged JSON
    # is already on disk as evidence; raise so the transient-retry loop in
    # run_evaluation_task re-runs the evaluation instead of scoring it.
    if eval_res.infrastructure_failure:
        raise InfrastructureFailureError(
            f"{milestone_id}: infrastructure failure during evaluation: "
            f"{eval_res.infrastructure_failure}"
        )

    # Use config thresholds to determine resolution
    # Calculate rates for each test category
    if eval_res.fail_to_pass_required > 0:
        f2p_rate = eval_res.fail_to_pass_achieved / eval_res.fail_to_pass_required
    else:
        f2p_rate = 1.0  # If no F2P tests, assume 100% success

    # P2P rate: use pass_to_pass_required as denominator to account for missing tests
    # Missing tests (tests that were expected but not found in results) should be treated as failures
    p2p_required = eval_res.pass_to_pass_required
    if p2p_required > 0:
        p2p_rate = eval_res.pass_to_pass_success_count / p2p_required
    else:
        p2p_rate = 1.0  # If no P2P tests, assume 100% success

    if eval_res.none_to_pass_required > 0:
        n2p_rate = eval_res.none_to_pass_achieved / eval_res.none_to_pass_required
    else:
        n2p_rate = 1.0  # If no N2P tests, assume 100% success

    # Check all thresholds - milestone is resolved only if all pass.
    # F1 fail-closed: never let the threshold recompute overturn a scoring-
    # untrusted verdict (residue prune requested but did not complete -> the
    # eval tree may be the additive GT solution). AND in the safety flag.
    is_resolved = (
        f2p_rate >= fail_to_pass_threshold
        and p2p_rate >= pass_to_pass_threshold
        and n2p_rate >= none_to_pass_threshold
        and not eval_res.resolution_locked_false
    )
    if eval_res.resolution_locked_false:
        logger.warning(
            f"🚫 {milestone_id}: resolution locked false "
            f"(residue: {eval_res.residue_prune_skipped_reason or 'ok'}, "
            f"infra: {eval_res.infrastructure_failure or 'none'}, "
            f"validity: {eval_res.infra_invalid_reason or 'ok'}, "
            f"production: {bool(eval_res.go_module_production_compile_error)}, "
            f"test-graph: {bool(eval_res.go_module_test_graph_contract_error)}) "
            f"— resolution locked False (fail-closed)"
        )

    # Actual test result: did tests actually pass 100%? (ignoring thresholds)
    # This is used for eval_status reporting, independent of DAG resolution
    actual_passed = (
        f2p_rate == 1.0
        and p2p_rate == 1.0
        and n2p_rate == 1.0
        and not eval_res.resolution_locked_false
    )

    # Update eval_res.resolved to reflect Config decision
    eval_res.resolved = is_resolved
    return milestone_id, is_resolved, actual_passed, eval_res, None


def run_evaluation_task(
    milestone_id: str,
    snapshot_path: Path,
    result_dir: Path,
    workspace_root: Path,
    fail_to_pass_threshold: float,
    pass_to_pass_threshold: float,
    none_to_pass_threshold: float,
    max_retries: int = 1,
    agent_attempt: int = 0,
    build_failure_fail_closed: bool = False,
    repo_config_path: Optional[Path] = None,
    repo_config_sha256: Optional[str] = None,
    runtime_policy_path: Optional[Path] = None,
    runtime_policy_sha256: Optional[str] = None,
    runtime_policy_mode: Optional[str] = None,
) -> Tuple[str, bool, bool, Optional[EvaluationResult], Optional[str]]:
    """Run evaluation in a separate process with retry logic for transient failures.

    Args:
        milestone_id: The milestone being evaluated
        snapshot_path: Path to the source snapshot tar file
        result_dir: Directory to store evaluation results
        workspace_root: Root of the workspace
        fail_to_pass_threshold: Threshold for F2P test success rate
        pass_to_pass_threshold: Threshold for P2P test success rate
        none_to_pass_threshold: Threshold for N2P test success rate
        max_retries: Number of retry attempts for transient failures (default: 1)
        agent_attempt: Agent-level retry attempt number (0=first, 1=retry1, etc.)
        build_failure_fail_closed: Reject partial reports after deterministic
            build/setup failures when True; score completed package/module
            reports when False.

    Returns:
        Tuple of (milestone_id, is_resolved, actual_passed, eval_res, error_msg)
        - is_resolved: Whether milestone passed threshold checks (for DAG)
        - actual_passed: Whether tests actually passed 100% (for eval_status)
    """
    eval_result_path = result_dir / "evaluation_result.json"

    # Try test_results first, then fallback to test_data
    baseline_json = workspace_root / "test_results" / milestone_id / f"{milestone_id}_classification.json"
    if not baseline_json.exists():
        baseline_json = workspace_root / "test_data" / milestone_id / f"{milestone_id}_classification.json"

    last_error = None
    last_traceback = None

    for attempt in range(max_retries + 1):
        try:
            return _run_evaluation_once(
                milestone_id=milestone_id,
                snapshot_path=snapshot_path,
                result_dir=result_dir,
                workspace_root=workspace_root,
                fail_to_pass_threshold=fail_to_pass_threshold,
                pass_to_pass_threshold=pass_to_pass_threshold,
                none_to_pass_threshold=none_to_pass_threshold,
                baseline_json=baseline_json,
                eval_result_path=eval_result_path,
                agent_attempt=agent_attempt,
                build_failure_fail_closed=build_failure_fail_closed,
                repo_config_path=repo_config_path,
                repo_config_sha256=repo_config_sha256,
                runtime_policy_path=runtime_policy_path,
                runtime_policy_sha256=runtime_policy_sha256,
                runtime_policy_mode=runtime_policy_mode,
            )
        except Exception as e:
            last_error = e
            last_traceback = traceback.format_exc()

            # Log detailed error info
            error_type = type(e).__name__
            print(f"⚠️  Evaluation attempt {attempt + 1}/{max_retries + 1} failed for {milestone_id}")
            print(f"   Error type: {error_type}")
            print(f"   Error message: {e}")
            print(f"   Traceback:\n{last_traceback}")

            # Check if this is a transient error worth retrying
            is_transient = _is_transient_error(e)

            if attempt < max_retries and is_transient:
                print(f"   🔄 Retrying in 5 seconds (transient error detected)...")
                time.sleep(5)
            elif attempt < max_retries:
                # Non-transient error, don't retry
                print(f"   ❌ Not retrying (non-transient error)")
                break

    # All attempts failed
    error_msg = f"{type(last_error).__name__}: {last_error}"
    if last_traceback:
        # Save detailed traceback to file for debugging
        traceback_file = result_dir / "evaluation_error.log"
        try:
            with open(traceback_file, "w") as f:
                f.write(f"Milestone: {milestone_id}\n")
                f.write(f"Attempts: {max_retries + 1}\n")
                f.write(f"Error: {error_msg}\n\n")
                f.write("Full Traceback:\n")
                f.write(last_traceback)
            print(f"   📝 Detailed error log saved to: {traceback_file}")
        except Exception:
            pass  # Ignore errors when saving traceback

    return milestone_id, False, False, None, error_msg


class E2EOrchestrator:
    def __init__(
        self,
        repo_name: str,
        milestone_version: str,
        image_name: str,
        dag_path: Path,
        srs_root: Path,
        trial_root: Path,
        workspace_root: Path,  # For evaluation (test data)
        repo_src_dirs: list[str],  # Source directories for snapshot extraction (required)
        test_dirs: list[str],  # Test directory patterns for filtering (required)
        agent_name: str = "claude-code",
        model: str = "claude-sonnet-4-5-20250929",
        config_path: Optional[Path] = None,  # Trial-level config path
        exclude_patterns: Optional[list[str]] = None,  # Exclude patterns for filtering
        generated_patterns: Optional[list[str]] = None,  # Generated code patterns for snapshot inclusion
        modifiable_test_patterns: Optional[list[str]] = None,  # Test files agent can modify
        main_branch: str = "main",  # Main branch name from repo config
        reasoning_effort: Optional[str] = None,  # For framework env var injection
        agent_version: Optional[str] = None,
        build_failure_fail_closed: Optional[bool] = None,
        repo_config_binding: Optional[RepoConfigBinding] = None,
        runtime_policy_binding: Optional[RuntimePolicyBinding] = None,
    ):
        self.repo_name = repo_name
        self.milestone_version = milestone_version
        self.image_name = image_name
        self.dag_path = dag_path
        self.srs_root = srs_root
        self.trial_root = trial_root
        self.workspace_root = workspace_root
        self.agent_name = agent_name
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.agent_version = agent_version
        self.repo_src_dirs = repo_src_dirs
        self.test_dirs = test_dirs
        self.main_branch = main_branch
        self.repo_config_binding = repo_config_binding
        self.runtime_policy_binding = runtime_policy_binding
        if (
            self.repo_config_binding is not None
            and self.repo_config_binding.repo_name != self.repo_name
        ):
            raise RepoConfigBindingError(
                "orchestrator repo config binding mismatch: "
                f"expected {self.repo_name!r}, got "
                f"{self.repo_config_binding.repo_name!r}"
            )
        if (
            self.runtime_policy_binding is not None
            and self.runtime_policy_binding.repo_name != self.repo_name
        ):
            raise RuntimePolicyBindingError(
                "orchestrator runtime policy binding mismatch: "
                f"expected {self.repo_name!r}, got "
                f"{self.runtime_policy_binding.repo_name!r}"
            )

        # Create SrcFileFilter for filtering test and excluded files from snapshots
        # All filtering (test_dirs, exclude_patterns) is done through SrcFileFilter
        # generated_patterns allows including generated code in snapshots for compilation
        # modifiable_test_patterns allows test files that agent needs to modify
        self.src_filter = SrcFileFilter(
            src_dirs=self.repo_src_dirs,
            test_dirs=self.test_dirs,
            exclude_patterns=exclude_patterns,
            generated_patterns=generated_patterns,
            modifiable_test_patterns=modifiable_test_patterns,
        )

        # Note: No testbed_path - we use the image's built-in /testbed
        self.e2e_workspace_path = self.trial_root / "e2e_workspace"  # Mounted to /e2e_workspace in container
        # Use trial_name in container name to support parallel runs
        # Docker container names only allow [a-zA-Z0-9][a-zA-Z0-9_.-], so replace colons
        trial_name = self.trial_root.name  # e.g., agent_run_001
        safe_repo_name = self.repo_name.replace(":", "_")
        self.container_name = f"{safe_repo_name}-{trial_name}"

        # Initialize ContainerSetup for container initialization and management
        self.container_setup = ContainerSetup(
            container_name=self.container_name,
            image_name=self.image_name,
            workdir="/testbed",
            agent_name=self.agent_name,
            e2e_workspace_path=self.e2e_workspace_path,
            agent_framework_name=self.agent_name,  # agent_name is the framework (e.g., "gemini-cli")
            reasoning_effort=self.reasoning_effort,
            agent_version=self.agent_version,
            repo_name=self.repo_name,  # authoritative repo id for F2 policy recovery
            runtime_policy_binding=self.runtime_policy_binding,
        )

        # Load config (priority: config_path > trial_root > workspace_root > harness/e2e default)
        effective_config_path = self._resolve_config_path(config_path)
        self.config = E2EConfig(effective_config_path)
        self.build_failure_fail_closed = (
            self.config.build_failure_fail_closed
            if build_failure_fail_closed is None
            else build_failure_fail_closed
        )
        if not isinstance(self.build_failure_fail_closed, bool):
            raise ValueError("build_failure_fail_closed must be a boolean")
        logger.info(
            "Build failure policy: %s",
            "fail-closed"
            if self.build_failure_fail_closed
            else "score completed package/module reports",
        )

        # Initialize DAG with config
        # Trial-level overrides: selected_milestone_ids.txt and additional_dependencies.csv
        # (run_e2e.py copies workspace-level selected_milestone_ids.txt to trial_root on creation,
        #  but the trial copy can be manually edited to run a subset of milestones)
        # --milestones writes a dependency-closed subset to milestone_selection.txt; it takes
        # precedence over selected_milestone_ids.txt (the dataset's curated set) when present.
        trial_milestone_selection = self.trial_root / "milestone_selection.txt"
        trial_selected_ids = self.trial_root / "selected_milestone_ids.txt"
        if trial_milestone_selection.exists():
            selected_ids_file = trial_milestone_selection
        elif trial_selected_ids.exists():
            selected_ids_file = trial_selected_ids
        else:
            selected_ids_file = None
        additional_deps = self.trial_root / "additional_dependencies.csv"
        self.dag = DAGManager(
            dag_path,
            selected_ids_file=selected_ids_file,
            ignore_weak_dependencies=self.config.ignore_weak_dependencies,
            additional_dependencies_csv=additional_deps if additional_deps.exists() else None,
        )

        # Ensure trial root exists
        self.trial_root.mkdir(parents=True, exist_ok=True)

        # Setup logging
        self._setup_logger()

        self._last_eval_res = None

        # Track milestones that were early-unlocked (for early unlock mode)
        self._early_unlocked_milestones: Set[str] = set()

        # Resume mode state
        self._is_resumed: bool = False
        self._evaluated_hashes: Dict[str, str] = {}  # milestone_id -> tag_hash (for deduplication)
        # Exact commit handed to the agent. Build-manifest deltas are always
        # computed against this BASE and the value is persisted for resume.
        self._snapshot_baseline_commit: Optional[str] = None

        # Serialize summary.json reads/writes (watcher thread + agent thread can both update state)
        self._summary_lock = threading.RLock()

        # Log early unblock mode status
        if self.config.early_unblock:
            logger.info("⚡ EARLY UNBLOCK MODE ENABLED: Tasks will be unlocked immediately after source extraction")

    # === Summary.json helpers (atomic write + resume_state persistence) ===

    def _summary_file_path(self) -> Path:
        return self.trial_root / "evaluation" / "summary.json"

    @staticmethod
    def _write_json_atomic(path: Path, data: dict) -> None:
        """Write JSON atomically (tmp + os.replace) to avoid half-written files."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            import json

            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def _ensure_resume_state(self, summary: dict) -> dict:
        resume_state = summary.get("resume_state")
        if not isinstance(resume_state, dict):
            resume_state = {}
            summary["resume_state"] = resume_state

        resume_state.setdefault("schema_version", 1)
        resume_state.setdefault("updated_at", None)
        dag_state = resume_state.get("dag")
        if not isinstance(dag_state, dict):
            dag_state = {}
            resume_state["dag"] = dag_state
        dag_state.setdefault("completed", [])
        dag_state.setdefault("failed", [])
        dag_state.setdefault("skipped", [])
        dag_state.setdefault("submitted", [])
        dag_state.setdefault("early_unlocked", [])

        pending_debounce = resume_state.get("pending_debounce")
        if not isinstance(pending_debounce, dict):
            pending_debounce = {}
            resume_state["pending_debounce"] = pending_debounce

        pending_evals = resume_state.get("pending_evaluations")
        if not isinstance(pending_evals, dict):
            pending_evals = {}
            resume_state["pending_evaluations"] = pending_evals

        return resume_state

    def _load_summary_or_init(self) -> dict:
        summary_file = self._summary_file_path()
        if summary_file.exists():
            import json

            try:
                with open(summary_file, "r", encoding="utf-8") as f:
                    summary = json.load(f)
                if not isinstance(summary, dict):
                    summary = {}
            except json.JSONDecodeError as e:
                logger.warning(f"summary.json is corrupted ({summary_file}): {e}. Re-initializing minimal structure.")
                summary = {}
        else:
            summary = {}

        summary.setdefault("repo_name", self.repo_name)
        summary.setdefault("milestone_version", self.milestone_version)
        summary.setdefault("agent_name", self.agent_name)
        summary.setdefault("total_milestones", len(self.dag.all_milestones))
        if not isinstance(summary.get("results"), dict):
            summary["results"] = {}
        if not isinstance(summary.get("statistics"), dict):
            summary["statistics"] = {
                "passed": 0,
                "failed": 0,
                "error": 0,
                "available": 0,
                "submitted": 0,
                "blocked": 0,
                "skipped": 0,
                "early_unlocked": 0,
            }

        self._ensure_resume_state(summary)
        return summary

    def _refresh_resume_state(self, summary: dict) -> None:
        resume_state = self._ensure_resume_state(summary)
        resume_state["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if self._snapshot_baseline_commit:
            resume_state["snapshot_baseline_commit"] = self._snapshot_baseline_commit

        snapshot = self.dag.get_state_snapshot()
        resume_state["dag"] = {
            "completed": sorted(snapshot["completed"]),
            "failed": sorted(snapshot["failed"]),
            "skipped": sorted(snapshot["skipped"]),
            "submitted": sorted(snapshot["submitted"]),
            "early_unlocked": sorted(self._early_unlocked_milestones),
        }

    def _update_resume_state(self, mutator_fn: Callable[[dict], None]) -> None:
        """Atomically update summary.json resume_state.

        mutator_fn receives the loaded summary dict and may mutate it (typically
        summary["resume_state"][...]).
        """
        with self._summary_lock:
            summary = self._load_summary_or_init()
            mutator_fn(summary)
            self._refresh_resume_state(summary)
            self._write_json_atomic(self._summary_file_path(), summary)

    def _resolve_config_path(self, config_path: Optional[Path]) -> Optional[Path]:
        """Resolve config path with priority: config_path > trial_root > workspace_root > harness/e2e default.

        Args:
            config_path: Explicitly provided config path (highest priority)

        Returns:
            Resolved config path, or None if no config found
        """
        # Priority 1: Explicitly provided config path
        if config_path and config_path.exists():
            logger.info(f"Using config from provided path: {config_path}")
            return config_path

        # Priority 2: Trial-level config (in trial_root)
        trial_config = self.trial_root / "e2e_config.yaml"
        if trial_config.exists():
            logger.info(f"Using trial-level config: {trial_config}")
            return trial_config

        # Priority 3: Workspace-level config
        workspace_config = self.workspace_root / "e2e_config.yaml"
        if workspace_config.exists():
            logger.info(f"Using workspace-level config: {workspace_config}")
            return workspace_config

        # Priority 4: Default config in harness/e2e/
        default_config = Path(__file__).parent / "e2e_config.yaml"
        if default_config.exists():
            logger.info(f"Using default config: {default_config}")
            return default_config

        logger.warning("No config file found, using built-in defaults")
        return None

    def _setup_logger(self):
        """Setup file logger for the orchestrator."""
        log_file = self.trial_root / "orchestrator.log"
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)

        # Also log to console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("[E2E] %(message)s"))
        logger.addHandler(console)

    def setup_environment(self, force: bool = False):
        """Start the persistent container (uses image's built-in /testbed).

        Creates a fresh container to ensure clean state.
        Refuses to proceed if a container with the same name already exists,
        unless force=True is specified.
        Uses ContainerSetup for container initialization and git history truncation.

        Args:
            force: If True, remove existing container before creating a new one.
        """
        logger.info("Setting up E2E environment...")

        # Safety check: refuse to overwrite an existing container unless --force
        if self._container_exists():
            if force:
                logger.warning(f"--force specified: removing existing container '{self.container_name}'")
            else:
                raise RuntimeError(
                    f"Container '{self.container_name}' already exists. "
                    "Refusing to overwrite to prevent data loss. "
                    "To resume an existing trial, use --resume-trial. "
                    "To force remove and start fresh, use --force. "
                    "Or manually remove the container: "
                    f"docker rm -f {self.container_name}"
                )

        # Note: No testbed cloning - the container uses its built-in /testbed from the Docker image.
        # This ensures Python version compatibility (image was built with compatible code).

        self.container_setup.start_container(force=force)
        self._record_agent_image_provenance()
        self._record_agent_version()

        # Truncate git history to prevent agent from seeing future commits
        self.container_setup.truncate_git_history(self.main_branch)

        # Capture the BASE before the agent can create a commit. Inferring this
        # later from whichever submission tags happen to remain is ambiguous and
        # makes deletion semantics impossible to audit reliably.
        baseline = self._docker_exec_git("rev-parse", "HEAD")
        if baseline.returncode != 0 or not baseline.stdout.strip():
            detail = (baseline.stderr or baseline.stdout or "unknown Git error").strip()
            raise RuntimeError(f"Could not record snapshot baseline commit: {detail}")
        self._snapshot_baseline_commit = baseline.stdout.strip()
        logger.info(f"Snapshot manifest BASE: {self._snapshot_baseline_commit}")

        # Apply whitelist-based network lockdown (blocks code hosting, removes sudo)
        self.container_setup.lock_network()

        # Initialize summary.json early so resume_state can be persisted even before evaluations complete
        self._update_resume_state(lambda _summary: None)

    def setup_environment_for_resume(
        self,
        completed_milestones: Set[str],
        failed_milestones: Set[str],
        skipped_milestones: Set[str],
        early_unlocked_milestones: Set[str],
        evaluated_hashes: Dict[str, str],
        submitted_milestones: Optional[Set[str]] = None,
    ):
        """Setup environment for resumed trial (reuse existing container).

        Unlike setup_environment(), this does NOT:
        - Remove existing container
        - Truncate git history (preserves agent tags)

        Args:
            completed_milestones: Set of completed milestone IDs
            failed_milestones: Set of failed milestone IDs
            skipped_milestones: Set of skipped milestone IDs
            early_unlocked_milestones: Set of early-unlocked milestone IDs
            evaluated_hashes: Dict of milestone_id -> tag_hash for deduplication
        """
        logger.info("Setting up E2E environment for RESUME...")

        # Check container exists
        if not self._container_exists():
            raise RuntimeError(
                f"Container {self.container_name} not found. " "Cannot resume - original container must exist."
            )

        # Start container if stopped
        if not self._is_container_running():
            logger.info(f"Starting stopped container {self.container_name}...")
            result = subprocess.run(
                ["docker", "start", self.container_name],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start container: {result.stderr}")
            logger.info(f"Container {self.container_name} started successfully")
        else:
            logger.info(f"Container {self.container_name} is already running")

        # Resume used to bypass cache/toolchain/plugin probes entirely because
        # it started Docker directly instead of going through ContainerSetup.
        # Re-run the same fail-closed gate used for a fresh launch before the
        # model receives another turn.
        # Also backfill legacy trial metadata when a resumed container predates
        # agent-version recording. A pinned version is re-validated here.
        self._record_agent_image_provenance()
        self.container_setup.verify_runtime_environment()
        self._record_agent_version()

        # Verify network lockdown is still active (iptables persists across stop/start)
        try:
            self.container_setup.verify_network_lockdown()
            logger.info("Network lockdown verified on resume")
        except RuntimeError as e:
            logger.warning(f"Network lockdown not active on resume: {e}")
            logger.info("Re-applying network lockdown...")
            self.container_setup.lock_network()

        # Restore DAG state
        self.dag.restore_state(
            completed=completed_milestones,
            failed=failed_milestones,
            skipped=skipped_milestones,
            submitted=submitted_milestones,
        )

        # Restore early-unlocked milestones
        self._early_unlocked_milestones = early_unlocked_milestones & self.dag.all_milestones

        # Set evaluated hashes for deduplication
        self._evaluated_hashes = evaluated_hashes
        self._is_resumed = True

        # New trials persist the exact pre-agent BASE. Legacy trials fall back
        # to the earliest submission's parent inside manifest discovery, but
        # never to the mutable current HEAD.
        persisted = (
            self._load_summary_or_init()
            .get("resume_state", {})
            .get("snapshot_baseline_commit")
        )
        if isinstance(persisted, str) and persisted.strip():
            check = self._docker_exec_git("cat-file", "-e", f"{persisted.strip()}^{{commit}}")
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or "missing commit").strip()
                raise RuntimeError(
                    f"Persisted snapshot baseline commit is unavailable: {persisted} ({detail})"
                )
            self._snapshot_baseline_commit = persisted.strip()
            logger.info(f"Restored snapshot manifest BASE: {self._snapshot_baseline_commit}")

        # Refresh persisted resume_state (might be missing in old trials)
        self._update_resume_state(lambda _summary: None)

        # Log git tags in container for verification
        tags = self._get_container_tags()
        agent_tags = [t for t in tags if t.startswith("agent-impl-")]
        logger.info(f"Found {len(agent_tags)} agent submission tags in container")

        logger.info(f"Resume setup complete:")
        logger.info(f"  Completed: {len(self.dag.completed_milestones)}")
        logger.info(f"  Failed: {len(self.dag.failed_milestones)}")
        logger.info(f"  Skipped: {len(self.dag.skipped_milestones)}")
        logger.info(f"  Early-unlocked: {len(self._early_unlocked_milestones)}")
        logger.info(f"  Evaluated hashes: {len(self._evaluated_hashes)}")

    def _record_agent_version(self) -> Optional[str]:
        """Persist the actual in-container agent CLI version to trial metadata."""
        actual = self.container_setup.get_agent_version(verify_requested=True)
        if not actual:
            return None

        metadata_path = self.trial_root / "trial_metadata.json"
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            metadata["agent_version"] = actual
            tmp_path = metadata_path.with_suffix(".json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(metadata, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, metadata_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to record agent version in {metadata_path}: {exc}") from exc

        logger.info(f"Agent version: {actual} (recorded in trial_metadata.json)")
        return actual

    def _record_agent_image_provenance(self) -> str:
        """Persist and verify the exact image ID backing the agent container."""
        actual = inspect_docker_image_id(self.container_name, container=True)
        metadata_path = self.trial_root / "trial_metadata.json"
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            recorded = metadata.get("agent_image_id")
            if recorded is not None and recorded != actual:
                raise RuntimeError(
                    "Agent container image differs from the trial's persisted image: "
                    f"recorded={recorded}, container={actual}"
                )
            metadata["agent_image_id"] = actual
            tmp_path = metadata_path.with_suffix(".json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(metadata, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, metadata_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to record agent image in {metadata_path}: {exc}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Failed to record agent image in {metadata_path}: {exc}"
            ) from exc
        logger.info(
            "Agent image ID: %s (recorded in trial_metadata.json)", actual
        )
        return actual

    def _container_exists(self) -> bool:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{self.container_name}$"],
            capture_output=True,
            text=True,
        )
        return self.container_name in result.stdout.strip()

    def _is_container_running(self) -> bool:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name], capture_output=True, text=True
        )
        return result.stdout.strip() == "true"

    def _get_existing_root_files_in_git(self, tag_name: str, files: list[str]) -> set[str]:
        """Check which files exist in git at the given tag (batch check).

        Args:
            tag_name: Git tag to check
            files: List of file paths to check (relative to repo root)

        Returns:
            Set of files that exist
        """
        if not files:
            return set()

        # Use git ls-tree with all files at once
        result = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                "-w",
                "/testbed",
                self.container_name,
                "git",
                "ls-tree",
                "--name-only",
                tag_name,
                "--",
            ]
            + files,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot root-manifest discovery failed for {tag_name}"
                + (f": {detail}" if detail else "")
            )

        # Parse output: each line is a file that exists
        existing = set()
        for line in result.stdout.strip().split("\n"):
            if line:
                existing.add(line)
        return existing

    def _infer_legacy_snapshot_baseline(self, tag_name: str) -> str:
        """Recover the pre-agent BASE for a legacy trial with no persisted value.

        New trials never use this path: setup records HEAD before the agent runs.
        It remains solely so a pre-fix trial can be resumed and re-captured.  The
        inference is fail-closed and resolves the parent of the earliest reachable
        submission tag, never the mutable worktree HEAD.
        """
        tags_result = self._docker_exec_git("tag", "-l", "agent-impl-*")
        if tags_result.returncode != 0:
            detail = (tags_result.stderr or tags_result.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot manifest discovery could not list agent tags for {tag_name}"
                + (f": {detail}" if detail else "")
            )

        reachable: List[Tuple[int, str]] = []
        for candidate in (t for t in tags_result.stdout.splitlines() if t.strip()):
            ancestor = self._docker_exec_git("merge-base", "--is-ancestor", candidate, tag_name)
            if ancestor.returncode == 1:
                continue
            if ancestor.returncode != 0:
                detail = (ancestor.stderr or ancestor.stdout or "").strip()
                raise RuntimeError(
                    f"Snapshot manifest discovery could not compare {candidate} to {tag_name}"
                    + (f": {detail}" if detail else "")
                )
            distance = self._docker_exec_git("rev-list", "--count", f"{candidate}..{tag_name}")
            if distance.returncode != 0:
                detail = (distance.stderr or distance.stdout or "").strip()
                raise RuntimeError(
                    f"Snapshot manifest discovery could not measure {candidate}..{tag_name}"
                    + (f": {detail}" if detail else "")
                )
            try:
                reachable.append((int(distance.stdout.strip()), candidate))
            except ValueError as e:
                raise RuntimeError(
                    f"Snapshot manifest discovery received an invalid rev-list count "
                    f"for {candidate}..{tag_name}: {distance.stdout!r}"
                ) from e

        earliest_tag = max(reachable, default=(0, tag_name))[1]
        base_result = self._docker_exec_git("rev-parse", f"{earliest_tag}^")
        if base_result.returncode != 0:
            detail = (base_result.stderr or base_result.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot manifest discovery could not resolve the pre-agent parent "
                f"of {earliest_tag} for {tag_name}"
                + (f": {detail}" if detail else "")
            )

        baseline = base_result.stdout.strip()
        if not baseline:
            raise RuntimeError(
                f"Snapshot manifest discovery resolved an empty pre-agent parent for {tag_name}"
            )
        logger.warning(
            "Legacy trial has no persisted snapshot BASE; inferred %s from %s^",
            baseline,
            earliest_tag,
        )
        return baseline

    def _get_build_manifest_overlay_in_git(self, tag_name: str) -> ManifestOverlay:
        """Return cumulative manifest upserts/deletes relative to explicit BASE."""
        baseline = getattr(self, "_snapshot_baseline_commit", None) or self._infer_legacy_snapshot_baseline(tag_name)

        ancestor = self._docker_exec_git("merge-base", "--is-ancestor", baseline, tag_name)
        if ancestor.returncode != 0:
            detail = (ancestor.stderr or ancestor.stdout or "not an ancestor").strip()
            raise RuntimeError(
                f"Snapshot manifest BASE {baseline} is not an ancestor of {tag_name}"
                + (f": {detail}" if detail else "")
            )

        # Disable rename detection deliberately: a manifest rename is precisely
        # one tombstone plus one upsert in the three-way overlay.
        upsert_result = self._docker_exec_git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=ACMT",
            baseline,
            tag_name,
            "--",
        )
        if upsert_result.returncode != 0:
            detail = (upsert_result.stderr or upsert_result.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot manifest discovery could not diff {baseline}..{tag_name}"
                + (f": {detail}" if detail else "")
            )
        delete_result = self._docker_exec_git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=D",
            baseline,
            tag_name,
            "--",
        )
        if delete_result.returncode != 0:
            detail = (delete_result.stderr or delete_result.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot manifest deletion discovery could not diff {baseline}..{tag_name}"
                + (f": {detail}" if detail else "")
            )
        return ManifestOverlay.create(
            baseline,
            find_build_manifests(
                upsert_result.stdout.split("\0"), getattr(self, "src_filter", None)
            ),
            find_build_manifests(
                delete_result.stdout.split("\0"), getattr(self, "src_filter", None)
            ),
        )

    def _get_changed_build_manifests_in_git(self, tag_name: str) -> set[str]:
        """Compatibility wrapper returning only manifest upserts."""
        return set(self._get_build_manifest_overlay_in_git(tag_name).upserts)

    def _get_existing_build_manifests_in_git(self, tag_name: str) -> set[str]:
        """Return every submitted non-test build manifest, including Go modules."""
        result = self._docker_exec_git(
            "-c", "core.quotePath=false", "ls-tree", "-r", "-z",
            "--name-only", tag_name,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot manifest inventory could not list {tag_name}"
                + (f": {detail}" if detail else "")
            )
        return find_build_manifests(
            result.stdout.split("\0"), getattr(self, "src_filter", None)
        )

    def _get_existing_src_dirs_in_git(self, tag_name: str, dirs: list[str]) -> set[str]:
        """Check which source directories exist in git at the given tag.

        Args:
            tag_name: Git tag to check
            dirs: List of directory paths to check (relative to repo root)

        Returns:
            Set of directories that exist
        """
        if not dirs:
            return set()

        existing = set()
        for dir_path in dirs:
            # Normalize path (remove trailing slash for ls-tree check)
            check_path = dir_path.rstrip("/")

            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "fakeroot",
                    "-e",
                    "HOME=/home/fakeroot",
                    "-w",
                    "/testbed",
                    self.container_name,
                    "git",
                    "ls-tree",
                    "-d",  # Only match directories
                    "--name-only",
                    tag_name,
                    "--",
                    check_path,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"Snapshot source-directory discovery failed for {check_path} at {tag_name}"
                    + (f": {detail}" if detail else "")
                )

            if result.stdout.strip():
                # Directory exists, add original path (with trailing slash if present)
                existing.add(dir_path)

        return existing

    def start_watcher(self):
        """Start the watcher loop (Daemon Mode).

        This method runs indefinitely, monitoring for trigger files/tags,
        running evaluations, and updating the global status.
        """
        self.setup_environment()
        self._update_task_queue_file(self.trial_root)  # Initial task queue

        logger.info(f"Watcher started for container {self.container_name}")
        logger.info("Waiting for triggers (git tags like 'agent-impl-Mxxx')...")

        processed_tags = set()

        # Use ProcessPoolExecutor for parallel evaluation
        # We use a max_workers limit to avoid overloading the system
        with concurrent.futures.ProcessPoolExecutor(max_workers=8) as executor:
            pending_futures = {}  # {future: milestone_id}

            try:
                while not self.dag.is_done() or pending_futures:
                    # 0. Check for completed evaluations
                    done_futures = [f for f in pending_futures if f.done()]
                    for f in done_futures:
                        mid = pending_futures.pop(f)
                        try:
                            result = f.result()
                            self._process_evaluation_result(*result)
                        except Exception as e:
                            logger.error(f"Error processing evaluation for {mid}: {e}", exc_info=True)
                            self._update_evaluation_summary(
                                mid, dag_status="error", eval_status="error", error_msg=str(e)
                            )

                    if self.dag.is_done() and not pending_futures:
                        break

                    # 1. Scan for new tags (The Trigger)
                    # We look for agent-impl-{mid} tags that we haven't processed yet
                    current_tags = self._get_container_tags()

                    # Check for new agent tags corresponding to known milestones
                    # We check ALL milestones (runnable or not, maybe agent jumped ahead)
                    for mid in self.dag.all_milestones:
                        tag = f"agent-impl-{mid}"
                        if tag in current_tags:
                            tag_hash = self._get_tag_hash(tag)
                            unique_id = f"{tag}_{tag_hash}"

                            if unique_id in processed_tags:
                                continue

                            logger.info(f"⚡ Trigger detected: New submission {tag}")
                            processed_tags.add(unique_id)

                            # 2. Silent mode: No status update shown to agent
                            # Just process the submission silently

                            # 3. Async Evaluation (Submitted to pool)
                            try:
                                self._handle_submission(
                                    mid,
                                    tag,
                                    executor,
                                    pending_futures,
                                    expected_tag_hash=tag_hash,
                                )
                            except Exception as e:
                                error_msg = f"Snapshot capture failed for {tag}: {e}"
                                logger.error(error_msg, exc_info=True)
                                self.dag.mark_failed(mid)
                                self._update_task_queue_file(self.trial_root)
                                self._process_evaluation_result(
                                    mid,
                                    False,
                                    False,
                                    None,
                                    error_msg,
                                )

                    # Sleep briefly
                    time.sleep(2)

            except KeyboardInterrupt:
                logger.info("Watcher stopped by user.")
                # Cancel pending futures? ProcessPoolExecutor handles cleanup on exit

    def _docker_exec_git(self, *git_args) -> subprocess.CompletedProcess:
        """Execute git command in container as fakeroot user."""
        cmd = [
            "docker",
            "exec",
            "--user",
            "fakeroot",
            "-e",
            "HOME=/home/fakeroot",
            "-w",
            "/testbed",
            self.container_name,
            "git",
            *git_args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    def _get_container_tags(self) -> Set[str]:
        res = self._docker_exec_git("tag", "-l")
        return set(res.stdout.strip().split("\n"))

    def _get_tag_hash(self, tag: str) -> str:
        res = self._docker_exec_git("rev-parse", f"{tag}^{{commit}}")
        return res.stdout.strip()

    def _filter_tar_archive(
        self,
        tar_path: Path,
        extra_build_manifests: Optional[Set[str]] = None,
    ) -> int:
        """Filter a tar archive to remove test files (but keep generated code).

        Uses SrcFileFilter.should_include_in_snapshot() which includes:
        - Regular source files (not test, not excluded)
        - Generated code files (e.g., .pb.go) even if in exclude_patterns

        Creates a new filtered tar in-place (replaces original).

        Args:
            tar_path: Path to the tar archive to filter

        Returns:
            Number of files filtered out
        """
        import tarfile
        import tempfile

        # Never skip this pass merely because test/exclude patterns are empty.
        # It also enforces manifest three-way authority; a broad src_dir can
        # contain stale POMs even in such a repo.

        filtered_count = 0
        temp_tar_path = tar_path.with_suffix(".filtered.tar")

        try:
            with tarfile.open(tar_path, "r") as src_tar:
                with tarfile.open(temp_tar_path, "w") as dst_tar:
                    for member in src_tar.getmembers():
                        # Keep directory entries as-is.
                        if member.isdir():
                            dst_tar.addfile(member)
                            continue

                        # Apply the existing path authority policy uniformly.
                        # Symlinks/hardlinks and other archive-native entries
                        # are part of the submitted tree; preserve their tar
                        # metadata instead of silently turning them into
                        # deletions merely because they are not regular files.
                        if should_include_snapshot_file(
                            member.name,
                            self.src_filter,
                            extra_build_manifests=extra_build_manifests,
                        ):
                            if member.isfile():
                                fileobj = src_tar.extractfile(member)
                                if fileobj is None:
                                    raise tarfile.TarError(
                                        f"cannot read regular tar member {member.name!r}"
                                    )
                                dst_tar.addfile(member, fileobj)
                            else:
                                dst_tar.addfile(member)
                        else:
                            # Filter out this file
                            filtered_count += 1
                            logger.debug(f"  Filtered out: {member.name}")

            # Replace original with filtered version
            temp_tar_path.replace(tar_path)
            if filtered_count > 0:
                logger.info(f"Filtered out {filtered_count} test/excluded members from snapshot")

        except Exception as e:
            logger.error(f"Error filtering tar archive: {e}")
            # Clean up temp file if it exists
            if temp_tar_path.exists():
                temp_tar_path.unlink()
            # Do NOT fall through to an unfiltered tar — that would ship GT
            # test files into the eval tree.
            # Signal failure so the caller can refuse to grade this snapshot.
            raise

        return filtered_count

    def _repo_config_sidecar_fields(self) -> Dict[str, Any]:
        """Return relocatable config/policy identities for snapshot metadata."""
        result: Dict[str, Any] = {}
        repo_binding = getattr(self, "repo_config_binding", None)
        if repo_binding is not None:
            result["repo_config_binding"] = repo_binding.identity.to_dict()
        runtime_binding = getattr(self, "runtime_policy_binding", None)
        if runtime_binding is not None:
            result["runtime_policy_binding"] = runtime_binding.identity.to_dict()
        return result

    def _check_snapshot_capture_integrity(
        self,
        agent_tag: str,
        snapshot_file: Path,
        snapshot_paths: List[str],
        manifest_overlay: ManifestOverlay,
        agent_commit: Optional[str] = None,
    ) -> None:
        """Capture-side snapshot loss check (residue-prune spec §11.4-H).

        Three loss channels:
        1. pipeline loss — includable files of the agent tag tree missing from
           the filtered tar (archive/filter bug). This is a hard error: scoring
           a partial capture would turn an evaluator failure into an agent
           failure;
        2. uncommitted loss — dirty work-tree paths at capture time: the agent
           never committed them, so they are absent from the tag tree AND the
           tar (invisible to channel 1). This remains diagnostic because the
           work may already belong to the next milestone;
        3. out-of-scope loss — paths committed since the previous submission
           that no archive pathspec covers (work in the wrong location; can
           never reach the tar). This remains diagnostic. The milestone START
           tag does not exist in the
           agent container (all tags are stripped at setup, anti-leak), so the
           previous agent-impl tag is the closest available baseline; the
           trial's first milestone has none and skips this channel.
        """
        capture_ref = agent_commit or agent_tag
        res = self._docker_exec_git(
            "-c", "core.quotePath=false", "ls-tree", "-r", "-z", "--name-only", capture_ref
        )
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(
                f"Snapshot integrity could not list the tree for {agent_tag}"
                + (f": {detail}" if detail else "")
            )
        tag_files = {p for p in res.stdout.split("\0") if p}
        try:
            with tarfile.open(snapshot_file) as tf:
                tar_files = normalize_tar_members(tf.getnames())
        except (tarfile.TarError, OSError) as e:
            raise RuntimeError(f"Snapshot integrity could not read {snapshot_file}: {e}") from e

        # Capture is deterministic for a tag. After applying the same filter,
        # even one missing expected file is a pipeline bug, not a tolerance
        # decision. Use an exact gate here; the looser default remains available
        # for legacy eval-side diagnostics.
        report = check_snapshot_integrity(
            tag_files,
            tar_files,
            self.src_filter,
            max_missing=0,
            max_missing_frac=0.0,
            extra_build_manifests=set(manifest_overlay.upserts),
        )

        # A manifest must have exactly one authority channel. Any unchanged POM
        # in the tar would overwrite END; any missing upsert would drop an agent
        # change. Tombstones must never be present as archive members.
        tar_manifests = find_build_manifests(tar_files)
        if tar_manifests != set(manifest_overlay.upserts):
            unexpected = sorted(tar_manifests - set(manifest_overlay.upserts))
            missing = sorted(set(manifest_overlay.upserts) - tar_manifests)
            raise RuntimeError(
                "Snapshot manifest overlay mismatch "
                f"(unexpected={unexpected[:10]}, missing={missing[:10]})"
            )
        deleted_present = set(manifest_overlay.deletes) & tar_files
        if deleted_present:
            raise RuntimeError(
                f"Snapshot contains manifest tombstone path(s): {sorted(deleted_present)}"
            )

        # Channel 2: uncommitted work at capture time. May include work the
        # agent already started for the NEXT milestone (tag detection is
        # async) — heuristic warning, not an error.
        uncommitted_in: List[str] = []
        uncommitted_out: List[str] = []
        status = self._docker_exec_git(
            "-c", "core.quotePath=false", "status", "--porcelain", "-z"
        )
        if status.returncode == 0:
            dirty = parse_status_porcelain_z(status.stdout)
            uncommitted_in, uncommitted_out = classify_capture_loss(
                dirty, self.src_filter, snapshot_paths
            )
        else:
            logger.warning("snapshot capture diagnostics: git status failed for %s", agent_tag)

        # Channel 3: committed outside the capture scope since the previous
        # submission (--diff-filter=d: deletions are not lost work).
        committed_out: List[str] = []
        diff_base: Optional[str] = None
        tags_res = self._docker_exec_git("tag", "-l", "agent-impl-*", "--sort=creatordate")
        if tags_res.returncode == 0:
            tags = [t for t in tags_res.stdout.splitlines() if t.strip()]
            if agent_tag in tags and tags.index(agent_tag) > 0:
                diff_base = tags[tags.index(agent_tag) - 1]
        else:
            logger.warning("snapshot capture diagnostics: git tag listing failed for %s", agent_tag)
        if diff_base:
            diff = self._docker_exec_git(
                "-c", "core.quotePath=false", "diff", "--name-only", "-z",
                "--diff-filter=d", diff_base, capture_ref,
            )
            if diff.returncode == 0:
                changed = [p for p in diff.stdout.split("\0") if p]
                _, committed_out = classify_capture_loss(
                    changed, self.src_filter, snapshot_paths
                )
            else:
                logger.warning(
                    "snapshot capture diagnostics: git diff failed for %s..%s",
                    diff_base,
                    agent_tag,
                )

        if not report.ok:
            sidecar = snapshot_file.parent / (snapshot_file.stem + ".integrity.json")
            sidecar.write_text(
                json.dumps(
                    make_snapshot_metadata(
                        tag=agent_tag,
                        snapshot_file=snapshot_file,
                        manifest_overlay=manifest_overlay,
                        extra={
                            "ok": False,
                            "expected_count": report.expected_count,
                            "missing_count": report.missing_count,
                            "missing_sample": report.missing_sample,
                            "capture_filter": _capture_filter_config(self.src_filter),
                            **self._repo_config_sidecar_fields(),
                        },
                    ),
                    indent=2,
                )
            )
            raise RuntimeError(
                f"Snapshot capture integrity failed for {agent_tag}: "
                f"{report.missing_count}/{report.expected_count} expected files are missing "
                f"(sample: {report.missing_sample[:5]})"
            )

        image_id = inspect_docker_image_id(self.container_name, container=True)
        agent_tag_commit = agent_commit or self._get_tag_hash(agent_tag)
        if not re.fullmatch(r"[0-9a-f]{40,64}", agent_tag_commit):
            raise RuntimeError(
                f"Snapshot provenance could not resolve {agent_tag} commit"
            )
        current_tag_commit = self._get_tag_hash(agent_tag)
        if current_tag_commit != agent_tag_commit:
            raise RuntimeError(
                f"Submission tag moved during snapshot capture: {agent_tag} "
                f"was {agent_tag_commit}, now {current_tag_commit or '?'}"
            )

        sidecar = snapshot_file.parent / (snapshot_file.stem + ".integrity.json")
        sidecar.write_text(
            json.dumps(
                make_snapshot_metadata(
                    tag=agent_tag,
                    snapshot_file=snapshot_file,
                    manifest_overlay=manifest_overlay,
                    extra={
                    "tag": agent_tag,
                    "ok": report.ok,
                    "expected_count": report.expected_count,
                    "missing_count": report.missing_count,
                    "missing_sample": report.missing_sample,
                    # F2 witness (codex F4): record the capture-time filter
                    # config, not a file list. The eval side rebuilds the
                    # filter and applies it to the START tree, so it protects
                    # even GT tests the agent deleted (absent from the tag
                    # tree) — and survives eval-side test_dirs drift.
                    "capture_filter": _capture_filter_config(self.src_filter),
                    **self._repo_config_sidecar_fields(),
                    "uncommitted_lost_count": len(uncommitted_in),
                    "uncommitted_lost_sample": uncommitted_in[:20],
                    "uncommitted_outside_count": len(uncommitted_out),
                    "uncommitted_outside_sample": uncommitted_out[:20],
                    "committed_outside_count": len(committed_out),
                    "committed_outside_sample": committed_out[:20],
                    "outside_diff_base": diff_base,
                    "agent_base_image_id": image_id,
                    "agent_tag_commit": agent_tag_commit,
                    },
                ),
                indent=2,
            )
        )
        if uncommitted_in or uncommitted_out:
            logger.warning(
                f"⚠️  snapshot capture: {len(uncommitted_in) + len(uncommitted_out)} uncommitted "
                f"path(s) at capture of {agent_tag} — not in the snapshot "
                f"(in-scope: {uncommitted_in[:3]}, out-of-scope: {uncommitted_out[:3]})"
            )
        if committed_out:
            logger.warning(
                f"⚠️  snapshot capture: {len(committed_out)} path(s) committed since {diff_base} "
                f"lie outside the capture scope and will never reach the snapshot: {committed_out[:3]}"
            )

    def _handle_submission(
        self,
        mid: str,
        agent_tag: str,
        executor,
        pending_futures,
        attempt: int = 0,
        expected_tag_hash: Optional[str] = None,
    ) -> bool:
        """Process a detected submission (Async).

        Args:
            mid: Milestone ID
            agent_tag: Git tag to evaluate
            executor: ThreadPoolExecutor for async evaluation
            pending_futures: Dict to track pending evaluation futures
            attempt: Attempt number (0=first, 1=retry1, 2=retry2, etc.)

        Returns:
            True if evaluation task was successfully submitted, False otherwise.
            Caller should clean up running_evaluations tracking if False is returned.
        """
        attempt_str = f" (retry {attempt})" if attempt > 0 else ""
        logger.info(f"Preparing evaluation for {mid}{attempt_str}...")
        agent_commit = expected_tag_hash or self._get_tag_hash(agent_tag)
        if not re.fullmatch(r"[0-9a-f]{40,64}", agent_commit):
            raise RuntimeError(f"Cannot resolve immutable commit for {agent_tag}")
        if self._get_tag_hash(agent_tag) != agent_commit:
            raise RuntimeError(f"Submission tag moved before capture: {agent_tag}")

        # Mark as submitted immediately and update task queue
        # This removes the task from agent's view right away (silent mode)
        self.dag.mark_submitted(mid)
        self._update_task_queue_file(self.trial_root)
        # Persist submitted state as early as possible to make resume robust to crashes
        self._update_resume_state(lambda _summary: None)

        # Create output dir under evaluation/{mid}/ or evaluation/{mid}-retry{attempt}/
        eval_root = self.trial_root / "evaluation"
        eval_root.mkdir(exist_ok=True)
        if attempt > 0:
            result_dir = eval_root / f"{mid}-retry{attempt}"
        else:
            result_dir = eval_root / mid
        result_dir.mkdir(exist_ok=True)

        # Extract Snapshot (source directories + root build files)
        # We use 'git archive' to grab the state of source dirs at agent_tag
        snapshot_file = result_dir / "source_snapshot.tar"

        # Include root build files (Cargo.toml, go.mod, etc.) to preserve agent's dependency config
        # Batch check which root files exist (single git ls-tree call)
        existing_root_files = self._get_existing_root_files_in_git(agent_commit, ROOT_BUILD_FILES)
        manifest_overlay = expand_atomic_manifest_overlay(
            self._get_build_manifest_overlay_in_git(agent_commit),
            self._get_existing_build_manifests_in_git(agent_commit),
            self.repo_src_dirs,
        )
        changed_build_manifests = set(manifest_overlay.upserts)

        # Check which source directories exist (tolerate missing directories)
        existing_src_dirs = self._get_existing_src_dirs_in_git(agent_commit, self.repo_src_dirs)
        if self.repo_src_dirs and not existing_src_dirs:
            raise RuntimeError(
                f"No configured source directories exist at {agent_tag}: {self.repo_src_dirs}"
            )
        if existing_src_dirs != set(self.repo_src_dirs):
            missing_dirs = set(self.repo_src_dirs) - existing_src_dirs
            logger.warning(f"Some source directories do not exist in {agent_tag}: {missing_dirs}")

        snapshot_paths = get_snapshot_paths(
            self.repo_src_dirs,
            existing_root_files=existing_root_files,
            existing_src_dirs=existing_src_dirs,
            extra_build_manifests=changed_build_manifests,
        )
        if not snapshot_paths:
            raise RuntimeError(f"Snapshot path discovery produced no paths for {agent_tag}")
        logger.info(f"Creating source snapshot from tag {agent_tag} for paths: {snapshot_paths}...")
        with open(snapshot_file, "wb") as f:
            # Build git archive command with all source directories and root build files
            # Use fakeroot user for git operations (required for safe.directory)
            git_archive_cmd = [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                "-w",
                "/testbed",
                self.container_name,
                "git",
                "archive",
                "--format=tar",
                agent_commit,
            ] + snapshot_paths  # Add source directories + root build files

            res = subprocess.run(
                git_archive_cmd,
                stdout=f,
                stderr=subprocess.PIPE,
            )

            if res.returncode != 0:
                snapshot_file.unlink(missing_ok=True)
                detail = res.stderr.decode(errors="replace").strip()
                raise RuntimeError(
                    f"git archive failed for {agent_tag}"
                    + (f": {detail}" if detail else "")
                )

        # Filter out test files and excluded files from snapshot
        self._filter_tar_archive(
            snapshot_file,
            extra_build_manifests=changed_build_manifests,
        )

        # Snapshot capture loss check (residue-prune spec §11.4-H): pipeline
        # loss is fail-closed; uncommitted/out-of-scope work remains diagnostic.
        self._check_snapshot_capture_integrity(
            agent_tag,
            snapshot_file,
            snapshot_paths,
            manifest_overlay,
            agent_commit,
        )

        # EARLY UNBLOCK MODE: If enabled, unlock dependent tasks immediately
        # after source extraction, without waiting for evaluation to complete.
        # This allows the agent to proceed to dependent tasks faster.
        if self.config.early_unblock:
            logger.info(f"⚡ Early unblock mode: Marking {mid} as complete for DAG progression")
            self._early_unlocked_milestones.add(mid)
            self.dag.mark_complete(mid)
            self._update_task_queue_file(self.trial_root)
            # Persist early-unlocked DAG progression immediately (critical for resume correctness)
            self._update_resume_state(lambda _summary: None)
            logger.info(f"⚡ {mid}: Dependent tasks unlocked. Evaluation continues in background for reporting.")

        # Submit evaluation task to process pool
        logger.info(f"Submitting evaluation task for {mid} to background process...")
        try:
            future = executor.submit(
                run_evaluation_task,
                milestone_id=mid,
                snapshot_path=snapshot_file,
                result_dir=result_dir,
                workspace_root=self.workspace_root,
                fail_to_pass_threshold=self.config.fail_to_pass_threshold,
                pass_to_pass_threshold=self.config.pass_to_pass_threshold,
                none_to_pass_threshold=self.config.none_to_pass_threshold,
                agent_attempt=attempt,
                build_failure_fail_closed=self.build_failure_fail_closed,
                repo_config_path=(
                    self.repo_config_binding.path
                    if self.repo_config_binding is not None
                    else None
                ),
                repo_config_sha256=(
                    self.repo_config_binding.sha256
                    if self.repo_config_binding is not None
                    else None
                ),
                runtime_policy_path=(
                    self.runtime_policy_binding.path
                    if self.runtime_policy_binding is not None
                    else None
                ),
                runtime_policy_sha256=(
                    self.runtime_policy_binding.sha256
                    if self.runtime_policy_binding is not None
                    else None
                ),
                runtime_policy_mode=(
                    self.runtime_policy_binding.mode
                    if self.runtime_policy_binding is not None
                    else None
                ),
            )
        except Exception:
            # Ensure we don't leave ghost pending state on executor failures
            self._update_resume_state(
                lambda summary: summary["resume_state"]["pending_evaluations"].pop(f"{mid}#{attempt}", None)
            )
            raise

        # Persist pending evaluation so resume can restart without re-scanning tags/debounce
        tag_hash = agent_commit
        submitted_ts = time.time()

        def _mutate(summary: dict) -> None:
            rs = summary["resume_state"]
            pe = rs["pending_evaluations"]
            pe[f"{mid}#{attempt}"] = {
                "milestone_id": mid,
                "attempt": attempt,
                "tag": agent_tag,
                "tag_hash": tag_hash,
                "snapshot_path": str(snapshot_file.relative_to(self.trial_root)),
                "result_dir": str(result_dir.relative_to(self.trial_root)),
                "early_unblocked": mid in self._early_unlocked_milestones,
                "submitted_ts": submitted_ts,
            }
            # Once submitted, debounce is no longer pending
            rs["pending_debounce"].pop(mid, None)

        self._update_resume_state(_mutate)

        pending_futures[future] = (mid, attempt)
        return True

    def _process_evaluation_result(
        self,
        mid: str,
        is_resolved: bool,
        actual_resolved: bool,
        eval_res: Optional[EvaluationResult],
        error_msg: Optional[str],
        attempt: int = 0,
    ) -> tuple:
        """Handle completion of an evaluation task.

        In early unlock mode, DAG state is already updated in _handle_submission,
        so we skip DAG updates here but still record evaluation results for reporting.

        Args:
            mid: Milestone ID
            is_resolved: Whether the milestone passed threshold checks
            actual_resolved: The actual evaluation result (for early unlock reporting)
            eval_res: Evaluation result object
            error_msg: Error message if evaluation failed
            attempt: Attempt number (0=first, 1=retry1, etc.)

        Returns:
            Tuple of (dag_status, eval_status, error_msg)
            - dag_status: "unlocked", "completed", "failed" (DAG state)
            - eval_status: "passed", "failed", "error" (actual evaluation result)
        """
        # For retries, use a different result directory
        if attempt > 0:
            result_dir = self.trial_root / "evaluation" / f"{mid}-retry{attempt}"
            result_dir.mkdir(exist_ok=True)
        else:
            result_dir = self.trial_root / "evaluation" / mid

        was_early_unlocked = mid in self._early_unlocked_milestones

        if error_msg:
            logger.error(f"Evaluation task for {mid} failed: {error_msg}")
            self._generate_feedback(mid, None, result_dir, False, error_msg=error_msg)
            # In early unlock mode, DAG already shows complete but eval failed
            dag_status = "unlocked" if was_early_unlocked else "error"
            eval_status = "error"
            self._update_evaluation_summary(
                mid, dag_status=dag_status, eval_status=eval_status, error_msg=error_msg, attempt=attempt
            )
            return (dag_status, eval_status, error_msg)

        self._last_eval_res = eval_res

        # Determine eval_status from actual test result
        eval_status = "passed" if actual_resolved else "failed"

        # Update DAG state (skip if already early-unlocked or retry)
        if was_early_unlocked or attempt > 0:
            # DAG already updated in _handle_submission, just log the actual result
            actual_result = "PASSED" if actual_resolved else "FAILED"
            if attempt > 0:
                logger.info(f"🔄 {mid} retry {attempt} complete: Actual result = {actual_result}")
            else:
                logger.info(f"⚡ {mid} evaluation complete (early-unlocked): Actual result = {actual_result}")
            dag_status = "unlocked"
        else:
            # Normal mode: update DAG based on evaluation result
            if is_resolved:
                self.dag.mark_complete(mid)
                logger.info(f"✅ {mid} Passed!")
                dag_status = "completed"
            else:
                self.dag.mark_failed(mid)
                logger.info(f"❌ {mid} Failed.")
                dag_status = "failed"

        # Generate Feedback (local only, not pushed to container)
        self._generate_feedback(mid, eval_res, result_dir, actual_resolved)

        # Update evaluation summary (local only, not visible to agent)
        self._update_evaluation_summary(
            mid, dag_status=dag_status, eval_status=eval_status, eval_res=eval_res, attempt=attempt
        )

        # Update Task Queue (only needed in normal mode, early unlock already updated)
        if not was_early_unlocked and attempt == 0:
            self._update_task_queue_file(self.trial_root)

        return (dag_status, eval_status, None)

    def _generate_feedback(
        self, milestone_id: str, eval_res, step_dir: Path, is_resolved: bool, error_msg: Optional[str] = None
    ):
        """Generate feedback report for local debugging (NOT pushed to container).

        Silent mode: Agent does not see any test feedback or evaluation results.
        Reports are stored locally in trial_root for debugging purposes only.
        """
        # 1. Generate Feedback Report (local only)
        report_content = f"# Feedback for {milestone_id}\n"
        report_content += f"**Date**: {time.ctime()}\n\n"

        if error_msg:
            report_content += f"**Result**: ❌ ERROR\n\n{error_msg}\n"
        elif eval_res:
            report_content += f"**Result**: {'✅ PASSED' if is_resolved else '❌ FAILED'}\n\n"
            report_content += "## Test Summary\n"
            report_content += f"- Passed: {eval_res.passed_tests}\n"
            report_content += f"- Failed: {eval_res.failed_tests}\n\n"

            if eval_res.fail_to_pass_failure:
                report_content += "## ⚠️ Required Tests Still Failing\n"
                for t in eval_res.fail_to_pass_failure:
                    report_content += f"- {t}\n"
                report_content += "\n"

            if eval_res.pass_to_pass_failure:
                report_content += "## ❌ Regressions (Tests that passed before but fail now)\n"
                for t in eval_res.pass_to_pass_failure:
                    report_content += f"- {t}\n"
                report_content += "\n"
        else:
            report_content += "**Result**: ❌ FAILED (No evaluation results produced)\n"

        # Save report locally for debugging (NOT pushed to container)
        report_file = step_dir / "feedback_report.md"
        with open(report_file, "w") as f:
            f.write(report_content)

        logger.info(f"Feedback report saved locally: {report_file}")
        # Note: In silent mode, we do NOT push reports to container.
        # Agent should not see any test feedback.

    def _update_evaluation_summary(
        self,
        milestone_id: str,
        dag_status: str,
        eval_status: str,
        eval_res=None,
        error_msg: Optional[str] = None,
        attempt: int = 0,
    ):
        """Update the evaluation summary file (local only, NOT visible to agent).

        This maintains a JSON summary of all evaluation results for debugging and analysis.
        For retries, results are stored with keys like "M001-retry1", "M001-retry2".

        Args:
            milestone_id: The milestone being evaluated
            dag_status: DAG dimension status - "unlocked", "completed", "failed", "error"
            eval_status: Evaluation dimension status - "passed", "failed", "error"
            eval_res: Optional evaluation result object
            error_msg: Optional error message
            attempt: Attempt number (0=first, 1=retry1, etc.)
        """
        with self._summary_lock:
            summary = self._load_summary_or_init()
            summary_file = self._summary_file_path()

            # Note: We rebuild statistics from scratch at the end of this method,
            # so we don't need to track incremental changes here.

            # Determine result key based on attempt
            result_key = milestone_id if attempt == 0 else f"{milestone_id}-retry{attempt}"
            result_dir = self.trial_root / "evaluation" / result_key

            # Build result entry with dual-dimension status
            result_entry: dict[str, Any] = {
                "dag_status": dag_status,
                "eval_status": eval_status,
                "timestamp": time.ctime(),
                "result_dir": str(result_dir),
                "attempt": attempt,
            }

            # Add tag hash for resume deduplication (only for first attempt)
            if attempt == 0 and self._container_exists():
                tag_name = f"agent-impl-{milestone_id}"
                tag_hash = self._get_tag_hash(tag_name)
                if tag_hash:
                    result_entry["tag_hash"] = tag_hash

            if error_msg:
                result_entry["error"] = error_msg

            if eval_res:
                result_entry["repo_config_binding_mode"] = (
                    eval_res.repo_config_binding_mode
                )
                result_entry["repo_config_sha256"] = eval_res.repo_config_sha256
                result_entry["runtime_policy_binding_mode"] = (
                    eval_res.runtime_policy_binding_mode
                )
                result_entry["runtime_policy_sha256"] = (
                    eval_res.runtime_policy_sha256
                )
                result_entry["runtime_policy_mode"] = eval_res.runtime_policy_mode
                p2p_failed = len(eval_res.pass_to_pass_failure) if eval_res.pass_to_pass_failure else 0
                result_entry["test_summary"] = {
                    "total": eval_res.total_tests,
                    "passed": eval_res.passed_tests,
                    "failed": eval_res.failed_tests,
                    "error": eval_res.error_tests,
                    "skipped": eval_res.skipped_tests,
                    "fail_to_pass_required": eval_res.fail_to_pass_required,
                    "fail_to_pass_achieved": eval_res.fail_to_pass_achieved,
                    "none_to_pass_required": eval_res.none_to_pass_required,
                    "none_to_pass_achieved": eval_res.none_to_pass_achieved,
                    "pass_to_pass_required": eval_res.pass_to_pass_required,
                    "pass_to_pass_achieved": eval_res.pass_to_pass_success_count,
                    "pass_to_pass_failed": p2p_failed,
                    "pass_to_pass_missing": eval_res.pass_to_pass_missing,
                }

            summary["results"][result_key] = result_entry

            # Clear pending evaluation entry (if any) now that we have a recorded outcome
            # (attempt==0 uses key "mid#0", retries use "mid#N")
            try:
                summary["resume_state"]["pending_evaluations"].pop(f"{milestone_id}#{attempt}", None)
            except Exception:
                # Never let resume_state maintenance break summary updates
                pass

            # Build detailed milestone status lists using dual-dimension fields
            # eval_status: "passed", "failed", "error"
            # dag_status: "unlocked", "completed", "failed", "error"
            evaluated_passed = sorted([m for m, r in summary["results"].items() if r.get("eval_status") == "passed"])
            evaluated_failed = sorted([m for m, r in summary["results"].items() if r.get("eval_status") == "failed"])
            evaluated_error = sorted([m for m, r in summary["results"].items() if r.get("eval_status") == "error"])
            # Track early-unlocked milestones (dag_status == "unlocked" means early-unlock mode was used)
            early_unlocked = sorted([m for m, r in summary["results"].items() if r.get("dag_status") == "unlocked"])
            evaluated_set = set(evaluated_passed) | set(evaluated_failed) | set(evaluated_error)

            # Calculate pending milestones status from DAG
            pending_milestones = self.dag.all_milestones - evaluated_set

            # Get runnable (available) milestones
            available = sorted([m for m in self.dag.get_next_runnable() if m not in evaluated_set])

            # Get skipped milestones (blocked by strong dependency failure)
            skipped = sorted(list(self.dag.skipped_milestones))

            # Get submitted milestones (awaiting evaluation)
            submitted = sorted(list(self.dag.submitted_milestones))

            # Get blocked milestones (dependencies not yet met, not skipped, not submitted)
            blocked = sorted(
                [
                    m
                    for m in pending_milestones
                    if m not in available
                    and m not in self.dag.skipped_milestones
                    and m not in self.dag.submitted_milestones
                ]
            )

            # Update statistics with counts
            summary["statistics"] = {
                "passed": len(evaluated_passed),
                "failed": len(evaluated_failed),
                "error": len(evaluated_error),
                "available": len(available),
                "submitted": len(submitted),
                "blocked": len(blocked),
                "skipped": len(skipped),
                "early_unlocked": len(early_unlocked),  # How many were early-unlocked
            }

            # Update milestone ID lists for quick reference
            summary["milestone_status"] = {
                "passed": evaluated_passed,
                "failed": evaluated_failed,
                "error": evaluated_error,
                "available": available,
                "submitted": submitted,
                "blocked": blocked,
                "skipped": skipped,
                "early_unlocked": early_unlocked,  # Track early-unlocked separately
            }

            # Legacy fields for backward compatibility
            summary["completed"] = evaluated_passed
            summary["failed"] = evaluated_failed
            summary["errors"] = evaluated_error

            # Save summary
            self._refresh_resume_state(summary)
            self._write_json_atomic(summary_file, summary)

        logger.info(f"Evaluation summary updated: {summary_file}")

        # Generate filtered evaluation result for this milestone
        if eval_res and attempt == 0:  # Only for first attempt, not retries
            eval_result_path = result_dir / "evaluation_result.json"
            if eval_result_path.exists():
                from harness.e2e.evaluator import generate_filtered_evaluation

                filtered_path = generate_filtered_evaluation(eval_result_path, self.workspace_root, milestone_id)
                if filtered_path:
                    logger.info(f"Generated filtered result for {milestone_id}: {filtered_path}")

        # Generate summary_filtered.json
        self._generate_filtered_summary(summary_file)

    def _generate_filtered_summary(self, summary_file: Path):
        """Generate summary_filtered.json from individual filtered evaluation results.

        Reads each milestone's evaluation_result_filtered.json (if exists) and
        aggregates into a filtered summary with recalculated statistics.

        Args:
            summary_file: Path to the original summary.json
        """
        import json

        if not summary_file.exists():
            return

        with open(summary_file, "r") as f:
            summary = json.load(f)

        # Create filtered summary as a copy
        import copy

        filtered_summary = copy.deepcopy(summary)
        filtered_summary["filtered"] = True

        eval_root = summary_file.parent
        results = filtered_summary.get("results", {})

        # Track which milestones have filtered results
        has_any_filtered = False

        for result_key, result_entry in results.items():
            # Skip retries (e.g., "M001-retry1")
            if "-retry" in result_key:
                continue

            milestone_id = result_key
            result_dir = eval_root / milestone_id
            filtered_result_path = result_dir / "evaluation_result_filtered.json"

            if filtered_result_path.exists():
                has_any_filtered = True
                try:
                    with open(filtered_result_path, "r") as f:
                        filtered_eval = json.load(f)

                    # Update result entry with filtered data
                    if "test_summary" in filtered_eval:
                        result_entry["test_summary"] = filtered_eval["test_summary"]

                    # Update eval_status based on filtered resolved
                    if filtered_eval.get("resolved"):
                        result_entry["eval_status"] = "passed"
                    else:
                        # Keep original status if it was error
                        if result_entry.get("eval_status") != "error":
                            result_entry["eval_status"] = "failed"

                    # Add filter_stats if present
                    if "filter_stats" in filtered_eval:
                        result_entry["filter_stats"] = filtered_eval["filter_stats"]

                except Exception as e:
                    logger.warning(f"Failed to load filtered result for {milestone_id}: {e}")

        # Recalculate statistics based on filtered results
        evaluated_passed = sorted([m for m, r in results.items() if r.get("eval_status") == "passed"])
        evaluated_failed = sorted([m for m, r in results.items() if r.get("eval_status") == "failed"])
        evaluated_error = sorted([m for m, r in results.items() if r.get("eval_status") == "error"])
        early_unlocked = sorted([m for m, r in results.items() if r.get("dag_status") == "unlocked"])

        filtered_summary["statistics"] = {
            "passed": len(evaluated_passed),
            "failed": len(evaluated_failed),
            "error": len(evaluated_error),
            "available": summary.get("statistics", {}).get("available", 0),
            "submitted": summary.get("statistics", {}).get("submitted", 0),
            "blocked": summary.get("statistics", {}).get("blocked", 0),
            "skipped": summary.get("statistics", {}).get("skipped", 0),
            "early_unlocked": len(early_unlocked),
        }

        filtered_summary["milestone_status"] = {
            "passed": evaluated_passed,
            "failed": evaluated_failed,
            "error": evaluated_error,
            "available": summary.get("milestone_status", {}).get("available", []),
            "submitted": summary.get("milestone_status", {}).get("submitted", []),
            "blocked": summary.get("milestone_status", {}).get("blocked", []),
            "skipped": summary.get("milestone_status", {}).get("skipped", []),
            "early_unlocked": early_unlocked,
        }

        # Legacy fields
        filtered_summary["completed"] = evaluated_passed
        filtered_summary["failed"] = evaluated_failed
        filtered_summary["errors"] = evaluated_error

        # Save filtered summary
        filtered_summary_file = summary_file.parent / "summary_filtered.json"
        with open(filtered_summary_file, "w") as f:
            json.dump(filtered_summary, f, indent=2)

        if has_any_filtered:
            logger.info(f"Generated filtered summary: {filtered_summary_file}")

    def _update_task_queue_file(self, output_dir: Path):
        """Update the task queue file (silent mode - no status/feedback info).

        This generates a simple task queue showing only available tasks.
        Agent sees no information about completed/failed tasks or test results.
        Also copies SRS files for available tasks to e2e_workspace/srs/{milestone_id}_SRS.md
        """
        runnable = self.dag.get_next_runnable()

        # Copy SRS files for available tasks
        self._copy_srs_for_tasks(runnable)

        queue_content = "# Task Queue\n\n"
        queue_content += "The following tasks are available for implementation.\n"
        queue_content += "Read the corresponding SRS.md for requirements.\n\n"

        queue_content += "## Available Tasks\n"
        if runnable:
            for m in runnable:
                # Point to container path where SRS is copied (flat structure with _SRS.md suffix)
                container_srs_path = f"/e2e_workspace/srs/{m}_SRS.md"
                queue_content += f"- {m}: See SRS at {container_srs_path}\n"
        else:
            queue_content += "(No tasks currently available)\n"

        queue_content += "\n(New tasks will appear here as they become available)\n"

        queue_file = output_dir / "TASK_QUEUE.md"
        with open(queue_file, "w") as f:
            f.write(queue_content)

        subprocess.run(
            ["docker", "cp", str(queue_file), f"{self.container_name}:/e2e_workspace/TASK_QUEUE.md"], check=True
        )
        # Protect TASK_QUEUE.md from agent tampering: root-owned, read-only
        subprocess.run(
            ["docker", "exec", self.container_name, "chown", "root:root", "/e2e_workspace/TASK_QUEUE.md"],
            check=True,
        )
        subprocess.run(
            ["docker", "exec", self.container_name, "chmod", "444", "/e2e_workspace/TASK_QUEUE.md"],
            check=True,
        )

    def _copy_srs_for_tasks(self, milestone_ids: list):
        """Copy SRS files for given milestones to e2e_workspace/srs/

        This makes SRS accessible inside the container at /e2e_workspace/srs/{mid}_SRS.md
        Completed task SRS files are preserved (not removed).
        """
        import tempfile

        # Ensure srs directory exists in container
        subprocess.run(["docker", "exec", self.container_name, "mkdir", "-p", "/e2e_workspace/srs"], check=True)

        # Get current SRS files in container (now flat structure with {mid}_SRS.md naming)
        result = subprocess.run(
            ["docker", "exec", self.container_name, "ls", "/e2e_workspace/srs"], capture_output=True, text=True
        )
        existing_files = (
            set(result.stdout.strip().split()) if result.returncode == 0 and result.stdout.strip() else set()
        )

        # NOTE: We no longer remove SRS for completed tasks - they are preserved for reference

        if not milestone_ids:
            return

        for mid in milestone_ids:
            target_filename = f"{mid}_SRS.md"

            # Skip if already exists in container
            if target_filename in existing_files:
                continue

            # Source: srs_root/{mid}/SRS.md
            src_srs_file = self.srs_root / mid / "SRS.md"
            if not src_srs_file.exists():
                logger.warning(f"SRS.md not found: {src_srs_file}")
                continue

            # Copy directly to container using a temp file (avoid creating local srs directory)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
                tmp.write(src_srs_file.read_text(encoding="utf-8"))
                tmp_path = tmp.name

            try:
                # Copy to container with new filename
                subprocess.run(
                    ["docker", "cp", tmp_path, f"{self.container_name}:/e2e_workspace/srs/{target_filename}"],
                    check=True,
                )
                # Fix permissions: make readable by fakeroot user (UID 1000)
                subprocess.run(
                    ["docker", "exec", self.container_name, "chmod", "644", f"/e2e_workspace/srs/{target_filename}"],
                    check=True,
                )
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.container_name,
                        "chown",
                        "1000:1000",
                        f"/e2e_workspace/srs/{target_filename}",
                    ],
                    check=True,
                )
                logger.info(f"Copied SRS for {mid} to container as {target_filename}")
            finally:
                import os

                os.unlink(tmp_path)
