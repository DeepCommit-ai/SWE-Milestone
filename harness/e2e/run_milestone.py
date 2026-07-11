#!/usr/bin/env python3
"""Single milestone code generation with direct evaluation.

This module provides a streamlined pipeline for running a single milestone
code generation task with Claude, followed by evaluation against baseline tests.

Key Features:
- Truncates git history to prevent agent from seeing future commits
- Uses prompt templates instead of hardcoded strings
- Extracts source snapshot using git archive
- Directly evaluates using PatchEvaluator

Usage:
    python harness/e2e/run_milestone.py \
        --workspace-root DATA/harness_workspace/repo/version \
        --milestone-id M001 \
        --srs-path DATA/.../srs/v1/M001/SRS.md
"""

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import List, Optional

import yaml

from harness.e2e.container_setup import ContainerSetup
from harness.e2e.image_version import resolve_image
from harness.e2e.agent_runner import AgentRunner
from harness.e2e.evaluator import PatchEvaluator, EvaluationResult
from harness.e2e.test_masking import mask_tests_by_names
from harness.e2e.log_parser import get_parser, TrialStats
from harness.e2e.residue_prune import (
    capture_filter_config as _capture_filter_config,
    check_snapshot_integrity,
    classify_capture_loss,
    normalize_tar_members,
    parse_status_porcelain_z,
)
from harness.utils.src_filter import SrcFileFilter
from harness.utils.snapshot import ROOT_BUILD_FILES, get_snapshot_paths

logger = logging.getLogger("e2e.run_milestone")

PROMPT_DIR = Path(__file__).parent / "prompt"


def get_next_trial_name(base_name: str, result_dir: Path) -> str:
    """Generate next trial name with auto-incrementing suffix.

    Scans existing directories in result_dir and finds the next available
    number for the given base_name.

    If base_name already ends with a numeric suffix (e.g., "complete_run_001"),
    it is returned as-is without auto-incrementing.

    Args:
        base_name: Base name prefix (e.g., "milestone_run") or
                   fixed name with suffix (e.g., "complete_run_001")
        result_dir: Directory containing existing trial directories

    Returns:
        Next trial name (e.g., "milestone_run_001") or the original
        base_name if it already has a numeric suffix
    """
    # If base_name already ends with _NNN (numeric suffix), use it directly
    if re.match(r".*_\d{3}$", base_name):
        return base_name

    if not result_dir.exists():
        return f"{base_name}_001"

    # Find all existing trial directories
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)$")
    max_num = 0

    for item in result_dir.iterdir():
        if item.is_dir():
            match = pattern.match(item.name)
            if match:
                num = int(match.group(1))
                max_num = max(max_num, num)

    return f"{base_name}_{max_num + 1:03d}"


def load_workspace_metadata(workspace_root: Path) -> dict:
    """Load metadata.json from workspace root.

    Args:
        workspace_root: Path to workspace root directory

    Returns:
        Dictionary with metadata values

    Raises:
        FileNotFoundError: If metadata.json doesn't exist
        KeyError: If required fields are missing
    """
    logger = logging.getLogger(__name__)
    metadata_path = workspace_root / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata.json not found at {metadata_path}")

    with open(metadata_path) as f:
        metadata = json.load(f)

    # Strict validation - no fallback
    required = ["repo_src_dirs", "test_dirs"]
    missing = [k for k in required if k not in metadata]
    if missing:
        raise KeyError(f"Missing required fields in metadata.json: {missing}")

    # Fallback to config YAML for optional patterns if not in metadata
    # workspace_root: DATA/harness_workspace/navidrome_navidrome_v0.57.0_v0.58.0/baseline_004_v4
    # config_path:    config/navidrome_navidrome_v0.57.0_v0.58.0.yaml
    if "generated_patterns" not in metadata or "modifiable_test_patterns" not in metadata:
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


class MilestoneRunner:
    """Single milestone code generation + evaluation pipeline.

    This class orchestrates the complete workflow:
    1. Start Docker container from milestone image
    2. Truncate git history (prevent cheating)
    3. Run Claude agent with SRS prompt
    4. Verify submission tag exists
    5. Extract source snapshot using git archive
    6. Run PatchEvaluator for test evaluation
    """

    def __init__(
        self,
        workspace_root: Path,
        milestone_id: str,
        srs_path: Path,
        output_dir: Path,
        image_name: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929",
        timeout_ms: int = 1800_000,
        agent_name: str = "claude-code",
        keep_container: bool = False,
        force: bool = False,
        main_branch: str = "main",
        prompt_version: str = "milestone_v1",
        test_masking: bool = True,
        reasoning_effort: Optional[str] = None,
        trial_name: Optional[str] = None,
        max_retries: int = 5,
    ):
        """Initialize MilestoneRunner.

        Args:
            workspace_root: Harness workspace root (e.g., DATA/.../test_multi_stage_V2)
            milestone_id: Milestone ID (e.g., M001)
            srs_path: Path to SRS document
            output_dir: Output directory for logs and results
            image_name: Docker image (default: auto-derived from workspace)
            model: Claude model to use
            timeout_ms: Agent timeout in milliseconds
            agent_name: Git user name for agent commits
            keep_container: Keep container after completion
            force: Force recreate container
            main_branch: Name of main branch
            prompt_version: Prompt template version
            test_masking: Enable test masking (default: True)
            trial_name: Trial name for unique container naming
            max_retries: Max agent execution retry attempts (default: 5)
        """
        self.workspace_root = Path(workspace_root)
        self.milestone_id = milestone_id
        self.srs_path = Path(srs_path)
        self.output_dir = Path(output_dir)
        self.model = model
        self.timeout_ms = timeout_ms
        self.agent_name = agent_name
        self.keep_container = keep_container
        self.force = force
        self.main_branch = main_branch
        self.prompt_version = prompt_version
        self.test_masking = test_masking
        self.reasoning_effort = reasoning_effort
        self.trial_name = trial_name
        self.max_retries = max_retries

        # Fallback flag for workdir snapshot extraction
        self._use_workdir_fallback = False

        # Load metadata - strict validation, no fallback
        metadata = load_workspace_metadata(workspace_root)
        self.repo_src_dirs = metadata["repo_src_dirs"]
        self.test_dirs = metadata["test_dirs"]
        self.exclude_patterns = metadata.get("exclude_patterns", [])
        self.generated_patterns = metadata.get("generated_patterns", [])
        self.modifiable_test_patterns = metadata.get("modifiable_test_patterns", [])

        # Auto-derive image name if not provided
        if image_name:
            self.image_name = image_name
        else:
            # Path structure: .../harness_workspace/repo_name/test_version
            # Benchmark data version is pinned via SWE_MILESTONE_IMAGE_TAG (default:
            # v0.9); falls back to :latest with a warning when the default pin
            # is absent locally (never when the tag was set explicitly).
            repo_name = self.workspace_root.parent.name.lower()
            test_version = self.workspace_root.name.lower()
            self.image_name = resolve_image(f"{repo_name}/{test_version}/{milestone_id.lower()}")

        # Container name (includes trial_name for uniqueness across trials)
        if self.trial_name:
            self.container_name = f"{milestone_id.lower()}-{self.trial_name.lower()}-runner"
        else:
            self.container_name = f"{milestone_id.lower()}-milestone-runner"

        # Initialize ContainerSetup
        self.container_setup = ContainerSetup(
            container_name=self.container_name,
            image_name=self.image_name,
            workdir="/testbed",
            agent_name=self.agent_name,
            agent_framework_name=self.agent_name,
            repo_name=self.workspace_root.parent.name,  # authoritative repo id (F2)
        )

        # Log directory
        self.log_dir = self.output_dir / "log"

        # Initialize AgentRunner
        self.agent_runner = AgentRunner(
            container_name=self.container_name,
            workdir="/testbed",
            model=self.model,
            timeout_ms=self.timeout_ms,
            log_dir=self.log_dir,
            agent_name=self.agent_name,
            reasoning_effort=self.reasoning_effort,
        )

        # Initialize SrcFileFilter for tar filtering
        self.src_filter = SrcFileFilter(
            src_dirs=self.repo_src_dirs,
            test_dirs=self.test_dirs,
            exclude_patterns=self.exclude_patterns,
            generated_patterns=self.generated_patterns,
            modifiable_test_patterns=self.modifiable_test_patterns,
        )

    def run(self) -> EvaluationResult:
        """Execute complete pipeline.

        Returns:
            EvaluationResult with pass/fail determination

        Raises:
            RuntimeError: If any phase fails
        """
        try:
            # Setup directories
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._setup_logging()

            # Phase 1: Start container
            logger.info("=" * 60)
            logger.info("Phase 1: Starting container")
            logger.info("=" * 60)
            logger.info(f"Image: {self.image_name}")
            logger.info(f"Container: {self.container_name}")
            self.container_setup.start_container(force=self.force)

            # Phase 2: Truncate git history
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 2: Truncating git history")
            logger.info("=" * 60)

            # Record base commit BEFORE truncation (branch-out point of start_version from main)
            # Use merge-base instead of rev-parse because the tag may be on a release branch
            # that gets removed during truncation. merge-base finds the point on main where
            # the release branch diverged, which will survive truncation.
            start_version = self._get_start_version()
            if start_version:
                base_commit_result = subprocess.run(
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
                        "-c",
                        "safe.directory=/testbed",
                        "merge-base",
                        start_version,
                        self.main_branch,
                    ],
                    capture_output=True,
                    text=True,
                )
                self._base_commit = base_commit_result.stdout.strip() if base_commit_result.returncode == 0 else None
                if self._base_commit:
                    logger.info(
                        f"Base commit for squashing: {self._base_commit[:12]} (merge-base of {start_version} and {self.main_branch})"
                    )
                else:
                    logger.warning(f"Could not find tag {start_version}, will use HEAD after truncation")
                    if base_commit_result.stderr:
                        logger.warning(f"Git error: {base_commit_result.stderr.strip()}")
            else:
                self._base_commit = None
                logger.warning("Could not determine start_version, will use HEAD after truncation")

            self.container_setup.truncate_git_history(self.main_branch)

            # Apply whitelist-based network lockdown (blocks code hosting, removes sudo)
            self.container_setup.lock_network()

            # Fallback: if no base commit from tag, use HEAD after truncation
            if not self._base_commit:
                base_commit_result = subprocess.run(
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
                        "rev-parse",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                )
                self._base_commit = base_commit_result.stdout.strip() if base_commit_result.returncode == 0 else None
                if self._base_commit:
                    logger.info(f"Fallback base commit: {self._base_commit[:12]}")

            # Phase 2.5: Mask tests (if enabled)
            if self.test_masking:
                logger.info("")
                logger.info("=" * 60)
                logger.info("Phase 2.5: Masking tests")
                logger.info("=" * 60)
                self._apply_test_masking()

            # Phase 2.6: Strip ENV-PATCH comments (always run)
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 2.6: Stripping ENV-PATCH comments")
            logger.info("=" * 60)
            strip_result = self._strip_env_patch_comments()
            if strip_result["stripped_files"] > 0:
                logger.info(
                    f"Stripped ENV-PATCH from {strip_result['stripped_files']} files, "
                    f"{strip_result['total_lines_removed']} lines removed"
                )
            else:
                logger.info("No ENV-PATCH comments found")

            # Phase 2.7: Squash commits to clean git history
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 2.7: Squashing commits to clean git history")
            logger.info("=" * 60)
            squash_result = self._squash_to_base()
            if squash_result["success"]:
                logger.info(f"Squashed {squash_result['commits_squashed']} commits into one")
            else:
                logger.warning(f"Squash failed: {squash_result.get('error', 'unknown error')}")

            # Phase 3: Run agent
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 3: Running agent")
            logger.info("=" * 60)
            prompt = self._load_prompt()

            # Retry configuration
            max_retries = self.max_retries
            retry_delay = 60  # seconds
            last_error = None
            success = False
            session_id = None

            for attempt in range(1, max_retries + 1):
                if attempt > 1:
                    logger.info(f"Agent execution attempt {attempt}/{max_retries}")
                    # Add separator in log files for clarity
                    if self.log_dir:
                        separator = f"\n{'='*60}\n=== RETRY ATTEMPT {attempt}/{max_retries} ===\n{'='*60}\n"
                        for log_file in ["agent_stdout.txt", "agent_stderr.txt"]:
                            log_path = self.log_dir / log_file
                            if log_path.exists():
                                with open(log_path, "a", encoding="utf-8") as f:
                                    f.write(separator)

                try:
                    success, session_id = self.agent_runner.run(prompt)

                    if success:
                        if attempt > 1:
                            logger.info(f"Agent completed successfully on attempt {attempt}")
                        break
                    else:
                        # Read last lines of stderr to get error details
                        last_error = self._get_last_stderr_lines(20)
                        logger.warning(f"Attempt {attempt}/{max_retries} failed. Last stderr:")
                        for line in last_error.split("\n")[-10:]:
                            if line.strip():
                                logger.warning(f"  {line}")

                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"Attempt {attempt}/{max_retries} failed with exception: {last_error}")

                # Wait before retry (unless it's the last attempt)
                if attempt < max_retries:
                    wait_seconds = retry_delay
                    if self.agent_runner._last_model_unavailable:
                        hint = self.agent_runner._last_model_hint or (
                            f"Repeated 500 errors observed for model '{self.model}'. "
                            "This may be transient; if persistent, try a different model alias."
                        )
                        logger.error(
                            "❗ Repeated 500 errors observed; possible model/backend compatibility issue (inferred). %s",
                            hint,
                        )
                        last_error = hint
                        break
                    if self.agent_runner._last_invalid_session:
                        logger.warning(
                            "⚠️ Invalid session identifier detected during retry flow; next attempt will start fresh session."
                        )
                    if self.agent_runner._last_auth_error:
                        logger.warning("🔑 Auth error detected - attempting credential refresh from host...")
                        if self.agent_runner.refresh_container_credentials():
                            logger.info("🔑 Credentials refreshed successfully - retrying immediately")
                            wait_seconds = 0
                        else:
                            logger.error("🔑 Credential refresh failed")
                    elif self.agent_runner._last_rate_limit:
                        reset_secs = self.agent_runner._rate_limit_reset_seconds or 3600
                        reset_mins = reset_secs / 60
                        logger.warning(f"⏳ Rate limit detected - cooldown {reset_mins:.0f}m")
                        if self.agent_runner.refresh_container_credentials():
                            logger.info("🔑 Credentials refreshed (rate limit wait still required)")
                        wait_seconds = reset_secs

                    if wait_seconds > 0:
                        logger.info(f"Waiting {wait_seconds}s before retry...")
                        time.sleep(wait_seconds)

            if not success:
                logger.warning(f"Agent execution failed after {max_retries} attempts. Last error: {last_error}")
                logger.warning("Proceeding with workdir snapshot extraction and evaluation...")
                self._use_workdir_fallback = True

            # Phase 4: Verify tag exists (only if agent completed)
            if success:
                logger.info("")
                logger.info("=" * 60)
                logger.info("Phase 4: Verifying submission tag")
                logger.info("=" * 60)
                if not self._verify_tag_exists():
                    logger.warning("Tag not found, waiting 60s before sending reminder...")
                    time.sleep(60)
                    self.agent_runner.resume_session(session_id, self._commit_reminder())
                    if not self._verify_tag_exists():
                        logger.warning(
                            f"Agent did not create submission tag: agent-impl-{self.milestone_id}. "
                            "Falling back to workdir snapshot extraction."
                        )
                        self._use_workdir_fallback = True
                    else:
                        logger.info(f"Tag verified after reminder: agent-impl-{self.milestone_id}")
                else:
                    logger.info(f"Tag verified: agent-impl-{self.milestone_id}")
            else:
                logger.info("")
                logger.info("=" * 60)
                logger.info("Phase 4: Skipped (agent did not complete successfully)")
                logger.info("=" * 60)

            # Phase 5: Extract snapshot
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 5: Extracting source snapshot")
            logger.info("=" * 60)
            if self._use_workdir_fallback:
                logger.warning("Using FALLBACK: extracting from working directory (not git tag)")
                snapshot_path = self._extract_snapshot_from_workdir()
            else:
                snapshot_path = self._extract_snapshot()
            logger.info(f"Snapshot saved to: {snapshot_path}")

            # Phase 5.5: Extract and parse agent logs
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 5.5: Extracting agent logs")
            logger.info("=" * 60)
            stats = self._extract_and_parse_logs()
            if stats:
                stats_path = self.output_dir / "agent_stats.json"
                stats.to_json(stats_path)
                logger.info(f"Agent stats saved to: {stats_path}")

            # Phase 6: Run evaluation
            logger.info("")
            logger.info("=" * 60)
            logger.info("Phase 6: Running evaluation")
            logger.info("=" * 60)
            result = self._run_evaluation(snapshot_path)

            # Extract agent trace
            self.agent_runner.extract_trace(self.log_dir)

            logger.info("")
            logger.info("=" * 60)
            logger.info(f"Evaluation complete: {'PASSED' if result.resolved else 'FAILED'}")
            logger.info("=" * 60)

            return result

        finally:
            if not self.keep_container:
                self.container_setup.cleanup()

    def _setup_logging(self) -> None:
        """Setup file logging."""
        # Save to trial root (same level as agent_stats.json)
        log_file = self.output_dir / "milestone_runner.log"
        handler = logging.FileHandler(log_file)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.info(f"Logging to: {log_file}")

    def _get_start_version(self) -> Optional[str]:
        """Extract start version from workspace path.

        Parses workspace_root.parent.name which has format:
        {owner}_{repo}_{start_version}_{end_version}

        Examples:
        - burntsushi_ripgrep_14.1.1_15.0.0 -> 14.1.1
        - navidrome_navidrome_v0.57.0_v0.58.0 -> v0.57.0

        Returns:
            Start version string or None if cannot be parsed
        """
        # workspace_root.parent.name is like: burntsushi_ripgrep_14.1.1_15.0.0
        repo_version_name = self.workspace_root.parent.name

        # Pattern: {owner}_{repo}_{start}_{end}
        # Versions can be like: 14.1.1, v0.57.0, 2.0.6, etc.
        # Strategy: split by '_' and look for version-like patterns from the end
        parts = repo_version_name.split("_")

        # Need at least 4 parts: owner, repo, start, end
        if len(parts) < 4:
            logger.warning(f"Cannot parse start_version from: {repo_version_name}")
            return None

        # Version pattern: starts with 'v' or digit, contains dots
        # Also handles patterns like "dubbo-3.3.3" (name-version format)
        def is_version(s: str) -> bool:
            if not s:
                return False
            # Direct version: 14.1.1, v0.57.0
            if s[0].isdigit() or s[0] == "v":
                return "." in s
            # Name-version format: dubbo-3.3.3
            if "-" in s:
                version_part = s.split("-")[-1]
                return version_part and version_part[0].isdigit() and "." in version_part
            return False

        # Find versions from the end
        # Last part should be end_version, second to last should be start_version
        end_version = parts[-1]
        start_version = parts[-2]

        if is_version(start_version) and is_version(end_version):
            logger.info(f"Parsed start_version: {start_version} (from {repo_version_name})")
            return start_version

        # Fallback: try to find version patterns anywhere
        versions = [p for p in parts if is_version(p)]
        if len(versions) >= 2:
            start_version = versions[0]
            logger.info(f"Parsed start_version (fallback): {start_version}")
            return start_version

        logger.warning(f"Cannot parse start_version from: {repo_version_name}")
        return None

    def _get_last_stderr_lines(self, n: int = 20) -> str:
        """Get the last n lines from agent_stderr.txt.

        Args:
            n: Number of lines to return

        Returns:
            Last n lines of stderr, or empty string if file doesn't exist
        """
        stderr_path = self.log_dir / "agent_stderr.txt"
        if not stderr_path.exists():
            return ""
        try:
            with open(stderr_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                return "".join(lines[-n:])
        except Exception:
            return ""

    def _load_prompt(self) -> str:
        """Load and fill prompt template.

        Returns:
            Filled prompt string

        Raises:
            FileNotFoundError: If template or SRS file not found
        """
        template_file = PROMPT_DIR / f"{self.prompt_version}.md"
        if not template_file.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_file}")

        template = template_file.read_text(encoding="utf-8")
        srs_content = self.srs_path.read_text(encoding="utf-8")

        # Replace template variables
        prompt = template.replace("{srs_content}", srs_content)
        prompt = prompt.replace("{milestone_id}", self.milestone_id)

        logger.info(f"Loaded prompt template: {self.prompt_version}")
        logger.info(f"SRS file: {self.srs_path}")
        logger.info(f"Prompt length: {len(prompt)} chars")

        return prompt

    def _commit_reminder(self) -> str:
        """Generate commit reminder message.

        Returns:
            Reminder message for agent
        """
        return f"""You have not created the submission tag yet.

Please commit your changes and create the tag:
```bash
git add .
git commit -m "Implement {self.milestone_id}"
git tag agent-impl-{self.milestone_id}
```

**IMPORTANT**: The `git tag agent-impl-{self.milestone_id}` command signals task completion.
"""

    def _verify_tag_exists(self) -> bool:
        """Check if submission tag exists.

        Returns:
            True if tag exists
        """
        tag_name = f"agent-impl-{self.milestone_id}"
        result = self.container_setup.docker_exec_git("tag", "-l", tag_name)
        return tag_name in result.stdout

    def _apply_test_masking(self) -> dict:
        """Apply test masking to prevent information leakage.

        Reads fail_to_pass and none_to_pass from baseline classification,
        then masks corresponding test files and inline tests.

        Supports two classification formats:
        1. Flat: {"fail_to_pass": [...], "new_tests": [...]}
        2. Nested: {"stable_classification": {"fail_to_pass": [...], "none_to_pass": [...]}}

        Returns:
            Summary dict from masking operation
        """
        # Load baseline classification
        baseline_json = self._resolve_baseline_classification()
        with open(baseline_json) as f:
            baseline = json.load(f)

        # Handle nested stable_classification format
        if "stable_classification" in baseline:
            classification = baseline["stable_classification"]
        else:
            classification = baseline

        # Combine fail_to_pass and none_to_pass (new tests)
        test_names = []
        if "fail_to_pass" in classification:
            test_names.extend(classification["fail_to_pass"])
        if "none_to_pass" in classification:
            test_names.extend(classification["none_to_pass"])
        # Fallback: also check new_tests (may be list of strings or list of dicts)
        if "new_tests" in classification and not test_names:
            new_tests = classification["new_tests"]
            if new_tests and isinstance(new_tests[0], dict):
                test_names.extend(t.get("test_id", "") for t in new_tests if t.get("test_id"))
            elif new_tests and isinstance(new_tests[0], str):
                test_names.extend(new_tests)

        if not test_names:
            logger.warning("No fail_to_pass or none_to_pass found in baseline classification")
            return {"skipped": True, "reason": "no tests to mask"}

        logger.info(f"Masking {len(test_names)} tests from baseline classification")
        result = mask_tests_by_names(
            container_name=self.container_name,
            test_names=test_names,
            src_filter=self.src_filter,
            workdir="/testbed",
        )
        logger.info(
            f"Targeted masking: {result['masked_test_files']} test files, "
            f"{result['masked_src_files']} source files with inline tests removed"
        )
        return result

    def _strip_env_patch_comments(self) -> dict:
        """Strip all [ENV-PATCH] commented lines from source files.

        These comments are added during image building to make START state compile,
        but they leak test implementation hints to agents.

        Returns:
            Summary dict with stripped file count
        """
        # Find and strip ENV-PATCH comments from all .rs files in src directories
        result = {"stripped_files": 0, "total_lines_removed": 0}

        for src_dir in self.repo_src_dirs:
            # Use sed to remove lines containing [ENV-PATCH]
            cmd = [
                "docker",
                "exec",
                "--user",
                "root",
                "-w",
                "/testbed",
                self.container_name,
                "bash",
                "-c",
                f"find {src_dir} -name '*.rs' -type f -exec grep -l 'ENV-PATCH' {{}} \\; 2>/dev/null | head -100",
            ]
            find_result = subprocess.run(cmd, capture_output=True, text=True)

            if find_result.returncode == 0 and find_result.stdout.strip():
                files = find_result.stdout.strip().split("\n")
                for file_path in files:
                    if not file_path:
                        continue
                    # Count lines before removal
                    count_cmd = [
                        "docker",
                        "exec",
                        "--user",
                        "root",
                        "-w",
                        "/testbed",
                        self.container_name,
                        "grep",
                        "-c",
                        "ENV-PATCH",
                        file_path,
                    ]
                    count_result = subprocess.run(count_cmd, capture_output=True, text=True)
                    lines_count = int(count_result.stdout.strip()) if count_result.returncode == 0 else 0

                    # Remove lines containing ENV-PATCH
                    sed_cmd = [
                        "docker",
                        "exec",
                        "--user",
                        "root",
                        "-w",
                        "/testbed",
                        self.container_name,
                        "sed",
                        "-i",
                        "/\\[ENV-PATCH\\]/d",
                        file_path,
                    ]
                    sed_result = subprocess.run(sed_cmd, capture_output=True, text=True)
                    if sed_result.returncode == 0:
                        result["stripped_files"] += 1
                        result["total_lines_removed"] += lines_count
                        logger.info(f"Stripped {lines_count} ENV-PATCH lines from {file_path}")

        if result["stripped_files"] > 0:
            # Commit the changes so agent sees clean state
            commit_cmd = [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                "-w",
                "/testbed",
                self.container_name,
                "bash",
                "-c",
                "git add -A && git commit --allow-empty -m '[HARNESS] Strip ENV-PATCH comments' 2>/dev/null || true",
            ]
            subprocess.run(commit_cmd, capture_output=True)

        return result

    def _squash_to_base(self) -> dict:
        """Squash all commits from BASE to HEAD into a single commit.

        This prevents agent from accessing ENV-PATCH via git history.
        Uses self._base_commit which is recorded after Phase 2 truncation.

        Returns:
            Summary dict with success status and commits squashed count
        """
        result = {"success": False, "commits_squashed": 0, "error": None}

        # Check if base commit was recorded
        if not hasattr(self, "_base_commit") or not self._base_commit:
            result["error"] = "Base commit not recorded"
            return result

        base_commit = self._base_commit

        # First, fix permissions as root (some files may have been created by root)
        logger.info("Fixing /testbed permissions before squash...")
        fix_perm_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            "/testbed",
            self.container_name,
            "bash",
            "-c",
            "chown -R fakeroot:fakeroot /testbed && chmod -R u+rw /testbed",
        ]
        fix_result = subprocess.run(fix_perm_cmd, capture_output=True, text=True)
        if fix_result.returncode != 0:
            logger.warning(f"Permission fix may have failed: {fix_result.stderr}")

        squash_script = f"""
set -e
cd /testbed

# Configure git identity for commits
git config user.email "harness@test.local"
git config user.name "Test Harness"

# Get BASE commit
BASE_COMMIT="{base_commit}"

# Count commits to squash
COMMITS_TO_SQUASH=$(git rev-list --count $BASE_COMMIT..HEAD)
echo "commits_to_squash=$COMMITS_TO_SQUASH"

if [ "$COMMITS_TO_SQUASH" -eq 0 ]; then
    echo "No commits to squash"
    exit 0
fi

# Commit any staged/unstaged changes first
git add -A
git diff --cached --quiet || git commit -m "[HARNESS] Pre-squash changes"

# Soft reset to BASE (keeps working directory)
git reset --soft $BASE_COMMIT

# Create squashed commit
git commit -m "Initial state for milestone"

# Clean up unreachable objects
git reflog expire --expire=now --all
git gc --prune=now

# Verify
echo "new_head=$(git rev-parse --short HEAD)"
echo "parent=$(git rev-parse --short HEAD~1 2>/dev/null || echo 'none')"
"""

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
            "/bin/bash",
            "-c",
            squash_script,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True)

        if proc.returncode == 0:
            result["success"] = True
            for line in proc.stdout.strip().split("\n"):
                if line.startswith("commits_to_squash="):
                    result["commits_squashed"] = int(line.split("=")[1])
                elif line.startswith("new_head="):
                    logger.info(f"New HEAD: {line.split('=')[1]}")
                elif line.startswith("parent="):
                    logger.info(f"Parent (BASE): {line.split('=')[1]}")
        else:
            result["error"] = proc.stderr
            logger.error(f"Squash failed: {proc.stderr}")

        return result

    def _get_existing_src_dirs(self, tag_name: str) -> list[str]:
        """Get list of source directories that actually exist in the repo at the given tag.

        Args:
            tag_name: Git tag to check

        Returns:
            List of existing directory paths
        """
        existing = []
        for src_dir in self.repo_src_dirs:
            # Use git ls-tree to check if directory exists at the tag
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
                    "-d",
                    tag_name,
                    src_dir.rstrip("/"),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                existing.append(src_dir)
            else:
                logger.debug(f"Skipping non-existent directory: {src_dir}")

        if len(existing) < len(self.repo_src_dirs):
            skipped = set(self.repo_src_dirs) - set(existing)
            logger.warning(f"Skipped {len(skipped)} non-existent directories: {sorted(skipped)}")

        return existing

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
            return set()

        # Parse output: each line is a file that exists
        existing = set()
        for line in result.stdout.strip().split("\n"):
            if line:
                existing.add(line)
        return existing

    def _extract_snapshot(self) -> Path:
        """Extract source snapshot using git archive.

        Returns:
            Path to snapshot tar file

        Raises:
            RuntimeError: If extraction fails
        """
        tag_name = f"agent-impl-{self.milestone_id}"
        # Save to evaluation/ directory
        eval_dir = self.output_dir / "evaluation"
        eval_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = eval_dir / "source_snapshot.tar"

        # Filter repo_src_dirs to only include directories that exist in the repo
        existing_dirs = self._get_existing_src_dirs(tag_name)
        if not existing_dirs:
            raise RuntimeError(f"No source directories found in repository at {tag_name}")

        # Include root build files (Cargo.toml, go.mod, etc.) to preserve agent's dependency config
        # Batch check which root files exist (single git ls-tree call)
        existing_root_files = self._get_existing_root_files_in_git(tag_name, ROOT_BUILD_FILES)

        snapshot_paths = get_snapshot_paths(existing_dirs, existing_root_files=existing_root_files)

        # Build git archive command
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
            "archive",
            "--format=tar",
            tag_name,
        ] + snapshot_paths

        logger.info(f"Extracting from tag: {tag_name}")
        logger.info(f"Snapshot paths: {snapshot_paths} (src dirs filtered from {len(self.repo_src_dirs)} configured)")

        with open(snapshot_path, "wb") as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
            if result.returncode != 0:
                raise RuntimeError(f"git archive failed: {result.stderr.decode()}")

        # Filter tar to remove test/excluded files
        filtered_count = self._filter_tar_archive(snapshot_path)
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} test/excluded files")

        # Snapshot capture loss check (residue-prune spec §11.4-H): pipeline
        # loss + uncommitted work + out-of-scope commits. Diagnostics only —
        # sidecar JSON + warning, never breaks extraction. The START tag does
        # not exist in the agent container (tags stripped at setup, anti-leak);
        # single-milestone runs have no previous submission either, so the
        # out-of-scope channel usually records diff_base=null here.
        try:
            res = self.container_setup.docker_exec_git(
                "-c", "core.quotePath=false", "ls-tree", "-r", "-z", "--name-only", tag_name
            )
            if res.returncode == 0:
                tag_files = {p for p in res.stdout.split("\0") if p}
                with tarfile.open(snapshot_path) as tf:
                    tar_files = normalize_tar_members(tf.getnames())
                report = check_snapshot_integrity(tag_files, tar_files, self.src_filter)

                # Uncommitted work at capture time: absent from BOTH the tag
                # tree and the tar, invisible to the ls-tree channel above.
                uncommitted_in: List[str] = []
                uncommitted_out: List[str] = []
                status = self.container_setup.docker_exec_git(
                    "-c", "core.quotePath=false", "status", "--porcelain", "-z"
                )
                if status.returncode == 0:
                    dirty = parse_status_porcelain_z(status.stdout)
                    uncommitted_in, uncommitted_out = classify_capture_loss(
                        dirty, self.src_filter, snapshot_paths
                    )

                # Committed outside the capture scope since the previous
                # submission (--diff-filter=d: deletions are not lost work).
                committed_out: List[str] = []
                diff_base: Optional[str] = None
                tags_res = self.container_setup.docker_exec_git(
                    "tag", "-l", "agent-impl-*", "--sort=creatordate"
                )
                if tags_res.returncode == 0:
                    tags = [t for t in tags_res.stdout.splitlines() if t.strip()]
                    if tag_name in tags and tags.index(tag_name) > 0:
                        diff_base = tags[tags.index(tag_name) - 1]
                if diff_base:
                    diff = self.container_setup.docker_exec_git(
                        "-c", "core.quotePath=false", "diff", "--name-only", "-z",
                        "--diff-filter=d", diff_base, tag_name,
                    )
                    if diff.returncode == 0:
                        changed = [p for p in diff.stdout.split("\0") if p]
                        _, committed_out = classify_capture_loss(
                            changed, self.src_filter, snapshot_paths
                        )

                sidecar = snapshot_path.parent / (snapshot_path.stem + ".integrity.json")
                sidecar.write_text(
                    json.dumps(
                        {
                            "tag": tag_name,
                            "ok": report.ok,
                            "expected_count": report.expected_count,
                            "missing_count": report.missing_count,
                            "missing_sample": report.missing_sample,
                            "capture_filter": _capture_filter_config(self.src_filter),
                            "uncommitted_lost_count": len(uncommitted_in),
                            "uncommitted_lost_sample": uncommitted_in[:20],
                            "uncommitted_outside_count": len(uncommitted_out),
                            "uncommitted_outside_sample": uncommitted_out[:20],
                            "committed_outside_count": len(committed_out),
                            "committed_outside_sample": committed_out[:20],
                            "outside_diff_base": diff_base,
                        },
                        indent=2,
                    )
                )
                if not report.ok:
                    logger.warning(
                        f"⚠️  snapshot capture integrity: {report.missing_count}/{report.expected_count} "
                        f"includable files of {tag_name} missing from tar"
                    )
                if uncommitted_in or uncommitted_out:
                    logger.warning(
                        f"⚠️  snapshot capture: {len(uncommitted_in) + len(uncommitted_out)} uncommitted "
                        f"path(s) at capture of {tag_name} — not in the snapshot "
                        f"(in-scope: {uncommitted_in[:3]}, out-of-scope: {uncommitted_out[:3]})"
                    )
                if committed_out:
                    logger.warning(
                        f"⚠️  snapshot capture: {len(committed_out)} path(s) committed since {diff_base} "
                        f"lie outside the capture scope and will never reach the snapshot: {committed_out[:3]}"
                    )
        except Exception as e:  # diagnostics must never break the pipeline
            logger.warning(f"snapshot capture integrity check failed: {e}")

        return snapshot_path

    def _filter_tar_archive(self, tar_path: Path) -> int:
        """Filter tar archive to remove test files (but keep generated code).

        Uses should_include_in_snapshot() which includes:
        - Regular source files (not test, not excluded)
        - Generated code files (e.g., .pb.go) even if in exclude_patterns

        Args:
            tar_path: Path to tar file to filter (modified in place)

        Returns:
            Number of files filtered out
        """
        # Skip filtering if no patterns defined
        if not self.src_filter.test_dirs and not self.src_filter.exclude_patterns:
            return 0

        filtered_count = 0
        non_regular: List[str] = []
        temp_path = tar_path.with_suffix(".filtered.tar")

        with tarfile.open(tar_path, "r") as src:
            with tarfile.open(temp_path, "w") as dst:
                for member in src.getmembers():
                    # Keep directory entries as-is.
                    if member.isdir():
                        dst.addfile(member)
                        continue

                    # F2 (codex): reject non-regular members (symlink/hardlink/
                    # device) — a symlink named like a GT test would slip through
                    # the path filter and replace the GT test at eval time.
                    if not member.isfile():
                        filtered_count += 1
                        non_regular.append(member.name)
                        continue

                    # Check if file should be included in snapshot
                    # This includes src files AND generated code files
                    if self.src_filter.should_include_in_snapshot(member.name):
                        fileobj = src.extractfile(member)
                        if fileobj:
                            dst.addfile(member, fileobj)
                    else:
                        filtered_count += 1
                        logger.debug(f"Filtered: {member.name}")

        # Replace original with filtered
        temp_path.replace(tar_path)
        if non_regular:
            # Never silent: a range legitimately relying on symlinks would
            # otherwise lose them without a trace (§11 decision 2026-07-11).
            logger.warning(
                f"⚠️  Dropped {len(non_regular)} non-regular tar member(s) "
                f"(symlink/hardlink/device) from snapshot: {non_regular[:5]}"
            )
        return filtered_count

    def _get_existing_workdir_dirs(self) -> list[str]:
        """Get list of source directories that actually exist in the container workdir.

        Returns:
            List of existing directory paths
        """
        existing = []
        for src_dir in self.repo_src_dirs:
            dir_path = src_dir.rstrip("/")
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
                    "test",
                    "-d",
                    dir_path,
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                existing.append(src_dir)
        return existing

    def _get_existing_root_files_in_workdir(self, files: list[str]) -> set[str]:
        """Check which files exist in the container working directory (batch check).

        Args:
            files: List of file paths to check (relative to /testbed)

        Returns:
            Set of files that exist
        """
        if not files:
            return set()

        # Check each file and echo if it exists (semicolon-separated commands)
        check_script = "; ".join(f'[ -f "{f}" ] && echo "{f}"' for f in files)
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
                "sh",
                "-c",
                check_script,
            ],
            capture_output=True,
            text=True,
        )

        existing = set()
        for line in result.stdout.strip().split("\n"):
            if line:
                existing.add(line)
        return existing

    def _extract_snapshot_from_workdir(self) -> Path:
        """Extract source snapshot directly from container working directory.

        This is the fallback method when the agent did not create a submission tag.
        It captures the current state of source directories regardless of git status.

        Returns:
            Path to snapshot tar file

        Raises:
            RuntimeError: If extraction fails or no source directories exist
        """
        eval_dir = self.output_dir / "evaluation"
        eval_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = eval_dir / "source_snapshot.tar"

        existing_dirs = self._get_existing_workdir_dirs()
        if not existing_dirs:
            raise RuntimeError("No source directories found in container working directory")

        # Include root build files (Cargo.toml, go.mod, etc.) to preserve agent's dependency config
        # Batch check which root files exist (single docker exec call)
        existing_root_files = self._get_existing_root_files_in_workdir(ROOT_BUILD_FILES)

        snapshot_paths = get_snapshot_paths(existing_dirs, existing_root_files=existing_root_files)

        logger.info("Extracting from working directory (fallback mode)")
        logger.info(f"Snapshot paths: {snapshot_paths}")

        # Use tar to directly archive src directories + root build files from the working directory
        # --ignore-failed-read: silently skip files that don't exist (e.g., Cargo.toml in non-Rust projects)
        paths_arg = " ".join(snapshot_paths)
        tar_cmd = f"tar -cf - --ignore-failed-read {paths_arg} 2>/dev/null"

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
            "sh",
            "-c",
            tar_cmd,
        ]

        with open(snapshot_path, "wb") as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
            if result.returncode != 0:
                raise RuntimeError(f"tar archive failed: {result.stderr.decode()}")

        # Reuse existing filter logic to remove test/excluded files
        filtered_count = self._filter_tar_archive(snapshot_path)
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} test/excluded files")

        return snapshot_path

    def _run_evaluation(self, snapshot_path: Path) -> EvaluationResult:
        """Run PatchEvaluator directly.

        Args:
            snapshot_path: Path to source snapshot tar file

        Returns:
            EvaluationResult with test results

        Raises:
            FileNotFoundError: If baseline classification not found
        """
        baseline_json = self._resolve_baseline_classification()

        # Use evaluation/ directory for all evaluation outputs
        eval_dir = self.output_dir / "evaluation"
        eval_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Baseline classification: {baseline_json}")
        logger.info(f"Evaluation output: {eval_dir}")

        evaluator = PatchEvaluator(
            workspace_root=self.workspace_root,
            milestone_id=self.milestone_id,
            patch_file=snapshot_path,
            baseline_classification=baseline_json,
            output_dir=eval_dir,
        )

        error_message = None
        try:
            result = evaluator.evaluate()
        except Exception as e:
            # Create a failed evaluation result instead of raising
            logger.error(f"Evaluation failed: {e}")
            error_message = str(e)
            result = EvaluationResult(
                milestone_id=self.milestone_id,
                patch_is_None=False,
                patch_exists=True,
                patch_successfully_applied=False,
                resolved=False,
                fail_to_pass_success=[],
                fail_to_pass_failure=[],
                pass_to_pass_success_count=0,
                pass_to_pass_failure=[],
                pass_to_pass_missing=0,
                none_to_pass_success=[],
                none_to_pass_failure=[],
                total_tests=0,
                passed_tests=0,
                failed_tests=0,
                error_tests=0,
                skipped_tests=0,
                fail_to_pass_required=0,
                fail_to_pass_achieved=0,
                pass_to_pass_required=0,
                none_to_pass_required=0,
                none_to_pass_achieved=0,
            )

        # Save result (even if evaluation failed)
        result_path = eval_dir / "evaluation_result.json"
        result_dict = result.to_dict()
        if error_message:
            result_dict["error_message"] = error_message

        # Record snapshot extraction method in result
        if self._use_workdir_fallback:
            result_dict["snapshot_extraction_method"] = "workdir_fallback"
            result_dict["agent_created_tag"] = False
        else:
            result_dict["snapshot_extraction_method"] = "git_tag"
            result_dict["agent_created_tag"] = True

        with open(result_path, "w") as f:
            json.dump(result_dict, f, indent=2)
        logger.info(f"Result saved to: {result_path}")

        # Generate filtered evaluation result (if filter_list exists)
        from harness.e2e.evaluator import generate_filtered_evaluation

        filtered_path = generate_filtered_evaluation(result_path, self.workspace_root, self.milestone_id)
        if filtered_path:
            logger.info(f"Filtered result saved to: {filtered_path}")

        return result

    def _extract_and_parse_logs(self) -> Optional[TrialStats]:
        """Extract and parse agent logs.

        Returns:
            TrialStats object or None if parsing fails
        """
        try:
            parser = get_parser(self.agent_name)

            # 1. Extract JSONL logs from container (to log/{agent_name}/)
            logs_dir = parser.extract_raw_logs(self.container_name, self.log_dir)

            # 2. Parse tool calls
            tool_calls = parser.parse_tool_calls(logs_dir)

            # 3. Update tool calls with result information
            parser.parse_tool_results(logs_dir, tool_calls)

            # 4. Parse agent_stdout.txt statistics (pass logs_dir for raw log parsing)
            stdout_file = self.log_dir / "agent_stdout.txt"
            stdout_stats = parser.parse_stdout_stats(stdout_file, logs_dir)

            # 5. Parse framework-native finest-grained usage units (message/turn)
            native_usage_units = parser.parse_native_usage_units(logs_dir, stdout_file)

            # 6. Get milestone times from git tags
            milestone_times = parser.get_milestone_times(self.container_name)

            # 7. Compute complete trial statistics
            trial_name = self.output_dir.name
            session_history_path = self.log_dir / "session_history.jsonl"
            stats = parser.compute_trial_stats(
                trial_name=trial_name,
                model=self.model,
                tool_calls=tool_calls,
                stdout_stats=stdout_stats,
                milestone_times=milestone_times,
                reasoning_effort=self.reasoning_effort,
                session_history_path=session_history_path,
                native_usage_units=native_usage_units,
                trial_dir=self.output_dir,
            )

            logger.info(
                f"Agent stats: {stats.total_tool_calls} tool calls, "
                f"{stats.total_turns} turns, ${stats.total_cost_usd:.2f}"
            )

            return stats

        except Exception as e:
            logger.warning(f"Failed to extract/parse agent logs: {e}")
            return None

    def _resolve_baseline_classification(self) -> Path:
        """Find baseline classification JSON.

        Searches in common locations for the baseline classification file.

        Returns:
            Path to baseline classification JSON

        Raises:
            FileNotFoundError: If not found in any location
        """
        # Check common locations
        candidates = [
            self.workspace_root / "test_results" / self.milestone_id / f"{self.milestone_id}_classification.json",
            self.workspace_root / "test_data" / self.milestone_id / f"{self.milestone_id}_classification.json",
            self.workspace_root / "classification" / f"{self.milestone_id}_classification.json",
        ]

        for path in candidates:
            if path.exists():
                return path

        raise FileNotFoundError(
            f"Baseline classification not found for {self.milestone_id}. "
            f"Searched in: {[str(p) for p in candidates]}"
        )


def setup_logging() -> None:
    """Setup console logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main():
    """Main entry point for CLI."""
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Run single milestone code generation with direct evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
    python harness/e2e/run_milestone.py \\
        --workspace-root DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 \\
        --milestone-id M001 \\
        --srs-path DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/srs/v1/M001/SRS.md

Output Structure:
    {workspace}/mstone_trial/{trial_name}/{milestone_id}/
        log/
            agent_prompt.txt
            agent_stdout.txt
            agent_stderr.txt
            session_id.txt
            milestone_runner.log
        source_snapshot.tar
        artifacts/
            {pid}/
        evaluation_result.json
        """,
    )

    # Required arguments
    parser.add_argument(
        "--workspace-root",
        type=Path,
        required=True,
        help="Harness workspace root (e.g., DATA/.../test_multi_stage_V2)",
    )
    parser.add_argument(
        "--milestone-id",
        required=True,
        help="Milestone ID (e.g., M001)",
    )
    parser.add_argument(
        "--srs-path",
        type=Path,
        required=True,
        help="Path to SRS document",
    )

    # Optional arguments
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: workspace/mstone_trial/{trial}/{mid})",
    )
    parser.add_argument(
        "--image",
        help="Docker image (default: {repo}/{version}/{mid}:$SWE_MILESTONE_IMAGE_TAG, tag default v0.9)",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        help="Model to use",
    )
    parser.add_argument(
        "--agent",
        default="claude-code",
        choices=["claude-code", "codex", "gemini-cli", "openhands"],
        help="Agent framework to use (default: claude-code)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Agent timeout in seconds (default: 1800 = 30 min)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Max agent execution retry attempts (default: 5)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort level (default: per-agent, codex=xhigh, claude-code=high)",
    )
    parser.add_argument(
        "--trial-name",
        default="milestone_run",
        help="Trial name base (default: milestone_run)",
    )
    parser.add_argument(
        "--prompt-version",
        default="milestone_v1",
        help="Prompt template version (default: milestone_v1)",
    )
    parser.add_argument(
        "--main-branch",
        default="main",
        help="Main branch name (default: main)",
    )
    parser.add_argument(
        "--no-test-masking",
        action="store_true",
        help="Disable test masking (default: enabled)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep container after completion",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force mode: recreate container and overwrite existing output directory (skip trial name auto-increment)",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.workspace_root.exists():
        parser.error(f"Workspace root not found: {args.workspace_root}")
    if not args.srs_path.exists():
        parser.error(f"SRS file not found: {args.srs_path}")

    # Auto-generate output_dir if not specified
    if args.output_dir is None:
        trial_dir = args.workspace_root / "mstone_trial"
        trial_dir.mkdir(parents=True, exist_ok=True)

        if args.force:
            # Force mode: use trial name directly without auto-increment
            # Add _001 suffix if not already present
            if re.match(r".*_\d{3}$", args.trial_name):
                trial_name = args.trial_name
            else:
                trial_name = f"{args.trial_name}_001"
        else:
            # Normal mode: auto-increment trial name
            trial_name = get_next_trial_name(args.trial_name, trial_dir)

        args.output_dir = trial_dir / trial_name / args.milestone_id

        # Force mode: clean up existing milestone directory
        if args.force and args.output_dir.exists():
            logger.warning(f"Force mode: removing existing directory {args.output_dir}")
            shutil.rmtree(args.output_dir)

        logger.info(f"Output directory: {args.output_dir}")
    else:
        # User specified --output-dir, derive trial_name from path or use default
        # Expected structure: .../mstone_trial/{trial_name}/{milestone_id}/
        if args.output_dir.name == args.milestone_id:
            # Path ends with milestone_id: .../trial_name/milestone_id
            trial_name = args.output_dir.parent.name
        else:
            # Custom path structure, use the base trial_name arg
            trial_name = args.trial_name

        # Force mode: clean up existing output directory
        if args.force and args.output_dir.exists():
            logger.warning(f"Force mode: removing existing directory {args.output_dir}")
            shutil.rmtree(args.output_dir)

    # Create and run MilestoneRunner
    runner = MilestoneRunner(
        workspace_root=args.workspace_root,
        milestone_id=args.milestone_id,
        srs_path=args.srs_path,
        output_dir=args.output_dir,
        image_name=args.image,
        model=args.model,
        timeout_ms=args.timeout * 1000,
        agent_name=args.agent,
        keep_container=args.keep,
        force=args.force,
        main_branch=args.main_branch,
        prompt_version=args.prompt_version,
        test_masking=not args.no_test_masking,
        reasoning_effort=args.reasoning_effort,
        trial_name=trial_name,
        max_retries=args.max_retries,
    )

    try:
        result = runner.run()
        print("\n" + result.summary())
        sys.exit(0 if result.resolved else 1)
    except Exception as e:
        logger.exception("Milestone run failed")
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
