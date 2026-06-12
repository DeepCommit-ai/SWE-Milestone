#!/usr/bin/env python3
"""
Evaluation system for milestone patch validation.

This module evaluates agent-generated patches by:
1. Loading the patch file (tar archive or diff)
2. Applying it to a Docker container
3. Running tests in the container
4. Comparing results against baseline classification
5. Determining if the milestone passes

Usage:
    python harness/e2e/evaluator.py \\
        --workspace-root DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 \\
        --milestone-id M001 \\
        --agent-name claude_sonnet_4_5_max \\
        --patch-file path/to/snapshot.tar \\
        --baseline-classification path/to/baseline_classification.json
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional, Set
from dataclasses import dataclass

import yaml

from harness.e2e.image_version import resolve_image
from harness.utils.rust_test_filter import (
    get_rust_files_from_tar,
    process_rust_files_in_container,
)
from harness.utils.test_id_normalizer import TestIdNormalizer
from harness.test_runner.core.milestone_attempt import run_single_state_tests
from harness.test_runner.core.report_parser import convert_to_summary

logger = logging.getLogger(__name__)


def load_repo_config(repo_name: str, workspace_root: Optional[Path] = None) -> dict:
    """Load repository-specific config from config/{repo_name}.yaml.

    Searches for the config file in the following order:
    1. {data_root}/config/{repo_name}.yaml  (workspace_root's parent)
    2. {project_root}/config/{repo_name}.yaml  (legacy fallback)

    Args:
        repo_name: Repository name (e.g., 'microsoft_markitdown_v0.1.1_v0.1.3')
        workspace_root: Path to the workspace root (e.g., .../EvoClaw-data/repo_name)

    Returns:
        Dictionary with config values, or empty dict if not found
    """
    search_paths = []
    if workspace_root:
        search_paths.append(workspace_root.parent / "config" / f"{repo_name}.yaml")
    # Legacy fallback: project_root/config/
    project_root = Path(__file__).parent.parent.parent
    search_paths.append(project_root / "config" / f"{repo_name}.yaml")

    for config_path in search_paths:
        if config_path.exists():
            try:
                with open(config_path, "r") as f:
                    config = yaml.safe_load(f)
                    logger.info(f"Loaded repo config from {config_path}")
                    return config if config else {}
            except Exception as e:
                logger.warning(f"Failed to load repo config from {config_path}: {e}")
    return {}


def normalize_java_hashcode(nodeid: str) -> str:
    """
    Normalize Java object hashcodes in test nodeids.

    Java parameterized tests often include object.toString() in test names,
    which contains memory addresses (hashcodes) like @5faeeb56. These change
    between JVM runs, causing test ID mismatches.

    Examples:
        Input:  "...RestProtocolTest::bean argument test [body: Book@5faeeb56]"
        Output: "...RestProtocolTest::bean argument test [body: Book@<HASH>]"

    Args:
        nodeid: The test nodeid to normalize

    Returns:
        Nodeid with Java hashcodes replaced by placeholder
    """
    if not nodeid:
        return nodeid
    # Pattern matches Java object hashCode: @followed by 6-8 hex chars
    # Examples: @5faeeb56, @7dbae40, @1727e03a
    return re.sub(r"@[a-f0-9]{6,8}", "@<HASH>", nodeid)


def normalize_ginkgo_nodeid(nodeid: str, go_module: Optional[str] = None) -> str:
    """
    Normalize test nodeid to a canonical format for comparison.

    This handles multiple inconsistencies:
    1. Ginkgo (Go) test path format differences:
       - Baseline: "github.com/navidrome/navidrome/adapters/taglib::Extractor > test"
       - Runtime:  "/testbed::Extractor / test"
    2. Java parameterized test hashcode differences:
       - Baseline: "...RestProtocolTest::test [body: Book@5faeeb56]"
       - Runtime:  "...RestProtocolTest::test [body: Book@62f11ebb]"

    Args:
        nodeid: The test nodeid to normalize
        go_module: Optional Go module name (unused, kept for API compatibility)

    Returns:
        Normalized test name string
    """
    if not nodeid:
        return nodeid

    # First, normalize Java hashcodes (applies to all test types)
    nodeid = normalize_java_hashcode(nodeid)

    # Extract the test name portion (after ::)
    if "::" in nodeid:
        test_name = nodeid.split("::", 1)[1]
    else:
        test_name = nodeid

    # Normalize separator: " > " -> " / "
    test_name = test_name.replace(" > ", " / ")

    return test_name


def build_nodeid_map(test_ids: List[str], go_module: Optional[str] = None) -> Dict[str, str]:
    """
    Build a map from normalized nodeid to original nodeid.

    Args:
        test_ids: List of test IDs (in any format)
        go_module: Optional Go module name for normalization

    Returns:
        Dict mapping normalized nodeid -> original nodeid
    """
    result = {}
    for test_id in test_ids:
        normalized = normalize_ginkgo_nodeid(test_id, go_module)
        result[normalized] = test_id
    return result


@dataclass
class EvaluationResult:
    """Result of patch evaluation."""

    # Metadata
    milestone_id: str

    # Patch status
    patch_is_None: bool
    patch_exists: bool
    patch_successfully_applied: bool

    # Evaluation result
    resolved: bool

    # Structured test results (stable tests only)
    fail_to_pass_success: List[str]
    fail_to_pass_failure: List[str]
    pass_to_pass_success_count: int  # Count only, avoid huge list
    pass_to_pass_failure: List[str]
    pass_to_pass_missing: int  # Tests not found in results (skipped modules or ID mismatch)
    none_to_pass_success: List[str]  # New tests that passed
    none_to_pass_failure: List[str]  # New tests that failed

    # Test statistics
    total_tests: int
    passed_tests: int
    failed_tests: int
    error_tests: int
    skipped_tests: int
    fail_to_pass_required: int
    fail_to_pass_achieved: int
    pass_to_pass_required: int  # Total count of pass_to_pass tests from baseline
    none_to_pass_required: int
    none_to_pass_achieved: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert to structured dictionary matching reference format."""
        result = {
            "milestone_id": self.milestone_id,
            "patch_is_None": self.patch_is_None,
            "patch_exists": self.patch_exists,
            "patch_successfully_applied": self.patch_successfully_applied,
            "resolved": self.resolved,
            "tests_status": {
                "FAIL_TO_PASS": {"success": self.fail_to_pass_success, "failure": self.fail_to_pass_failure},
                "NONE_TO_PASS": {"success": self.none_to_pass_success, "failure": self.none_to_pass_failure},
                "PASS_TO_PASS": {
                    "success_count": self.pass_to_pass_success_count,
                    "failure": self.pass_to_pass_failure,
                    "missing": self.pass_to_pass_missing,
                },
            },
            "test_summary": {
                "total": self.total_tests,
                "passed": self.passed_tests,
                "failed": self.failed_tests,
                "error": self.error_tests,
                "skipped": self.skipped_tests,
                "fail_to_pass_required": self.fail_to_pass_required,
                "fail_to_pass_achieved": self.fail_to_pass_achieved,
                "none_to_pass_required": self.none_to_pass_required,
                "none_to_pass_achieved": self.none_to_pass_achieved,
                "pass_to_pass_required": self.pass_to_pass_required,
                "pass_to_pass_achieved": self.pass_to_pass_success_count,
                "pass_to_pass_failed": len(self.pass_to_pass_failure),
                "pass_to_pass_missing": self.pass_to_pass_missing,
            },
        }
        return result

    def summary(self) -> str:
        """Generate human-readable summary."""
        lines = [
            f"{'='*60}",
            f"Milestone Evaluation: {self.milestone_id}",
            f"{'='*60}",
            f"",
            f"Patch Status:",
            f"  Exists: {'✅' if self.patch_exists else '❌'}",
            f"  Applied: {'✅' if self.patch_successfully_applied else '❌'}",
            f"",
            f"Milestone Status: {'✅ RESOLVED' if self.resolved else '❌ NOT RESOLVED'}",
            f"",
            f"Fail-to-Pass Tests:",
            f"  Required: {self.fail_to_pass_required}",
            f"  Achieved: {self.fail_to_pass_achieved}",
            f"",
        ]

        if self.fail_to_pass_success:
            lines.append(f"FAIL_TO_PASS Success ({len(self.fail_to_pass_success)}):")
            for test in self.fail_to_pass_success:
                lines.append(f"  ✅ {test}")
            lines.append("")

        if self.fail_to_pass_failure:
            lines.append(f"FAIL_TO_PASS Failure ({len(self.fail_to_pass_failure)}):")
            for test in self.fail_to_pass_failure:
                lines.append(f"  ❌ {test}")
            lines.append("")

        if self.pass_to_pass_failure:
            lines.append(f"PASS_TO_PASS Failure (Regressions) ({len(self.pass_to_pass_failure)}):")
            for test in self.pass_to_pass_failure:
                lines.append(f"  ⚠️  {test}")
            lines.append("")

        if self.pass_to_pass_missing > 0:
            lines.append(f"PASS_TO_PASS Missing ({self.pass_to_pass_missing}):")
            lines.append(
                f"  ⚠️  {self.pass_to_pass_missing} tests not found in results (skipped modules or ID mismatch)"
            )
            lines.append("")

        if self.none_to_pass_required > 0:
            lines.extend(
                [
                    f"None-to-Pass Tests (New Tests):",
                    f"  Required: {self.none_to_pass_required}",
                    f"  Achieved: {self.none_to_pass_achieved}",
                    f"",
                ]
            )

            if self.none_to_pass_success:
                lines.append(f"NONE_TO_PASS Success ({len(self.none_to_pass_success)}):")
                for test in self.none_to_pass_success:
                    lines.append(f"  ✅ {test}")
                lines.append("")

            if self.none_to_pass_failure:
                lines.append(f"NONE_TO_PASS Failure ({len(self.none_to_pass_failure)}):")
                for test in self.none_to_pass_failure:
                    lines.append(f"  ❌ {test}")
                lines.append("")

        lines.extend(
            [
                f"Test Summary:",
                f"  Total:   {self.total_tests}",
                f"  Passed:  {self.passed_tests}",
                f"  Failed:  {self.failed_tests}",
                f"  Error:   {self.error_tests}",
                f"  Skipped: {self.skipped_tests}",
                f"",
                f"PASS_TO_PASS Success: {self.pass_to_pass_success_count} (not listed)",
            ]
        )

        lines.append(f"{'='*60}")

        return "\n".join(lines)


# =============================================================================
# Filtered Evaluation Result Generation
# =============================================================================


def load_filter_list(workspace_root: Path, milestone_id: str) -> Optional[Dict[str, List[str]]]:
    """Load filter_list.json for a milestone if it exists.

    Args:
        workspace_root: Path to workspace root
        milestone_id: Milestone ID (e.g., "M001")

    Returns:
        Dictionary with invalid test lists, or None if file doesn't exist
    """
    filter_path = workspace_root / "test_results" / milestone_id / f"{milestone_id}_filter_list.json"
    if not filter_path.exists():
        logger.debug(f"No filter_list.json found at {filter_path}")
        return None

    try:
        with open(filter_path) as f:
            filter_list = json.load(f)
        logger.info(f"Loaded filter_list.json from {filter_path}")
        return filter_list
    except Exception as e:
        logger.warning(f"Failed to load filter_list.json: {e}")
        return None


def filter_evaluation_result(
    eval_dict: Dict[str, Any],
    filter_list: Dict[str, List[str]],
    ran_test_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Filter evaluation result dict, excluding invalid tests.

    Recalculates statistics and resolved status after filtering.

    Args:
        eval_dict: Original evaluation result dict (from EvaluationResult.to_dict())
        filter_list: Dictionary with invalid_fail_to_pass, invalid_none_to_pass,
                     invalid_pass_to_pass lists
        ran_test_ids: Optional set of test IDs that actually ran in the evaluation.
                      Used to correctly adjust pass_to_pass_missing when filtering P2P tests.

    Returns:
        Filtered evaluation result dict with recalculated metrics
    """
    import copy

    result = copy.deepcopy(eval_dict)

    # Helper to extract test_id from filter list items (handles both string and dict formats)
    def extract_test_ids(items: list) -> set:
        result_set = set()
        for item in items:
            if isinstance(item, dict):
                # New format: {"test_id": "...", "reason": "..."}
                if "test_id" in item:
                    result_set.add(item["test_id"])
            elif isinstance(item, str):
                # Old format: just the test_id string
                result_set.add(item)
        return result_set

    # Get invalid test sets
    invalid_f2p = extract_test_ids(filter_list.get("invalid_fail_to_pass", []))
    invalid_n2p = extract_test_ids(filter_list.get("invalid_none_to_pass", []))
    invalid_p2p = extract_test_ids(filter_list.get("invalid_pass_to_pass", []))

    tests_status = result.get("tests_status", {})
    test_summary = result.get("test_summary", {})

    # Track how many tests were filtered from each category
    f2p_filtered_success = 0
    f2p_filtered_failure = 0
    n2p_filtered_success = 0
    n2p_filtered_failure = 0
    p2p_filtered_failure = 0

    # Combine invalid_f2p and invalid_n2p into one set for robust filtering
    # This way, even if data is placed in the wrong field, it still works correctly
    invalid_f2p_n2p = invalid_f2p | invalid_n2p

    # Filter FAIL_TO_PASS
    if "FAIL_TO_PASS" in tests_status:
        f2p = tests_status["FAIL_TO_PASS"]
        original_success = f2p.get("success", [])
        original_failure = f2p.get("failure", [])

        filtered_success = [t for t in original_success if t not in invalid_f2p_n2p]
        filtered_failure = [t for t in original_failure if t not in invalid_f2p_n2p]

        f2p_filtered_success = len(original_success) - len(filtered_success)
        f2p_filtered_failure = len(original_failure) - len(filtered_failure)

        f2p["success"] = filtered_success
        f2p["failure"] = filtered_failure

    # Filter NONE_TO_PASS
    if "NONE_TO_PASS" in tests_status:
        n2p = tests_status["NONE_TO_PASS"]
        original_success = n2p.get("success", [])
        original_failure = n2p.get("failure", [])

        filtered_success = [t for t in original_success if t not in invalid_f2p_n2p]
        filtered_failure = [t for t in original_failure if t not in invalid_f2p_n2p]

        n2p_filtered_success = len(original_success) - len(filtered_success)
        n2p_filtered_failure = len(original_failure) - len(filtered_failure)

        n2p["success"] = filtered_success
        n2p["failure"] = filtered_failure

    # Filter PASS_TO_PASS
    p2p_filtered_missing = 0
    if "PASS_TO_PASS" in tests_status:
        p2p = tests_status["PASS_TO_PASS"]
        original_failure = p2p.get("failure", [])
        failure_set = set(original_failure)

        filtered_failure = [t for t in original_failure if t not in invalid_p2p]
        p2p_filtered_failure = len(original_failure) - len(filtered_failure)

        p2p["failure"] = filtered_failure

        # Determine how many invalid P2P tests were "missing" (didn't run at all)
        # vs "success" (ran and passed). This is needed to correctly adjust
        # pass_to_pass_missing.
        if ran_test_ids is not None:
            for tid in invalid_p2p:
                if tid not in failure_set and tid not in ran_test_ids:
                    p2p_filtered_missing += 1

    # Recalculate test_summary
    # Both F2P and N2P are filtered by combined invalid_f2p_n2p set
    f2p_total_filtered = f2p_filtered_success + f2p_filtered_failure
    n2p_total_filtered = n2p_filtered_success + n2p_filtered_failure

    if "fail_to_pass_required" in test_summary:
        test_summary["fail_to_pass_required"] -= f2p_total_filtered
    if "fail_to_pass_achieved" in test_summary:
        test_summary["fail_to_pass_achieved"] = len(tests_status.get("FAIL_TO_PASS", {}).get("success", []))

    if "none_to_pass_required" in test_summary:
        test_summary["none_to_pass_required"] -= n2p_total_filtered
    if "none_to_pass_achieved" in test_summary:
        test_summary["none_to_pass_achieved"] = len(tests_status.get("NONE_TO_PASS", {}).get("success", []))

    # For pass_to_pass: reduce required, missing, and failure by invalid test counts
    if "pass_to_pass_required" in test_summary:
        test_summary["pass_to_pass_required"] -= len(invalid_p2p)
    if "pass_to_pass_failed" in test_summary:
        test_summary["pass_to_pass_failed"] = len(tests_status.get("PASS_TO_PASS", {}).get("failure", []))
    if "pass_to_pass_missing" in test_summary:
        # Reduce missing by the number of invalid tests that were missing (didn't run)
        test_summary["pass_to_pass_missing"] = max(0, test_summary["pass_to_pass_missing"] - p2p_filtered_missing)
    if "pass_to_pass_achieved" in test_summary:
        # Achieved = required - failed - missing
        p2p_required = test_summary.get("pass_to_pass_required", 0)
        p2p_failed = test_summary.get("pass_to_pass_failed", 0)
        p2p_missing = test_summary.get("pass_to_pass_missing", 0)
        test_summary["pass_to_pass_achieved"] = max(0, p2p_required - p2p_failed - p2p_missing)

    # Also update success_count in tests_status if present
    if "PASS_TO_PASS" in tests_status and "success_count" in tests_status["PASS_TO_PASS"]:
        tests_status["PASS_TO_PASS"]["success_count"] = test_summary.get("pass_to_pass_achieved", 0)

    # Recalculate resolved status
    f2p_required = test_summary.get("fail_to_pass_required", 0)
    f2p_achieved = test_summary.get("fail_to_pass_achieved", 0)
    n2p_required = test_summary.get("none_to_pass_required", 0)
    n2p_achieved = test_summary.get("none_to_pass_achieved", 0)
    p2p_failure_count = len(tests_status.get("PASS_TO_PASS", {}).get("failure", []))
    p2p_missing = test_summary.get("pass_to_pass_missing", 0)

    result["resolved"] = (
        f2p_achieved == f2p_required and p2p_failure_count == 0 and p2p_missing == 0 and n2p_achieved == n2p_required
    )

    # Add metadata about filtering
    result["filtered"] = True
    result["filter_stats"] = {
        # Tests filtered from each category (using combined invalid_f2p + invalid_n2p)
        "fail_to_pass_filtered": f2p_total_filtered,
        "none_to_pass_filtered": n2p_total_filtered,
        "pass_to_pass_filtered": p2p_filtered_failure,
        "pass_to_pass_missing_filtered": p2p_filtered_missing,
        # Count of invalid tests from filter_list.json
        "invalid_f2p_count": len(invalid_f2p),
        "invalid_n2p_count": len(invalid_n2p),
        "invalid_p2p_count": len(invalid_p2p),
    }

    return result


def generate_filtered_evaluation(
    eval_result_path: Path,
    workspace_root: Path,
    milestone_id: str,
) -> Optional[Path]:
    """Generate evaluation_result_filtered.json from evaluation_result.json.

    Args:
        eval_result_path: Path to evaluation_result.json
        workspace_root: Path to workspace root
        milestone_id: Milestone ID

    Returns:
        Path to filtered result file, or None if no filter_list exists
    """
    filter_list = load_filter_list(workspace_root, milestone_id)
    if filter_list is None:
        return None

    # Check if any filtering is needed
    has_invalid = any(
        filter_list.get(key) for key in ["invalid_fail_to_pass", "invalid_none_to_pass", "invalid_pass_to_pass"]
    )
    if not has_invalid:
        logger.debug(f"filter_list.json exists but has no invalid tests for {milestone_id}")
        return None

    try:
        with open(eval_result_path) as f:
            eval_dict = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load evaluation_result.json: {e}")
        return None

    # Load ran test IDs from eval.json artifacts to correctly handle P2P missing counts.
    # Without this, filtering P2P tests that were "missing" (didn't run) would not
    # reduce pass_to_pass_missing, causing pass_to_pass_achieved to be undercounted.
    ran_test_ids: Optional[Set[str]] = None
    artifacts_dir = eval_result_path.parent / "artifacts"
    if artifacts_dir.exists():
        ran_test_ids = set()
        for eval_json_path in artifacts_dir.glob("*/eval.json"):
            try:
                with open(eval_json_path) as f:
                    eval_data = json.load(f)
                for test in eval_data.get("tests", []):
                    if "nodeid" in test:
                        ran_test_ids.add(test["nodeid"])
            except Exception as e:
                logger.warning(f"Failed to load {eval_json_path}: {e}")

    filtered_dict = filter_evaluation_result(eval_dict, filter_list, ran_test_ids=ran_test_ids)

    filtered_path = eval_result_path.parent / "evaluation_result_filtered.json"
    with open(filtered_path, "w") as f:
        json.dump(filtered_dict, f, indent=2)

    logger.info(f"Generated filtered evaluation result: {filtered_path}")
    return filtered_path


class PatchEvaluator:
    """Evaluates patches by applying them to Docker containers and running tests."""

    def __init__(
        self,
        workspace_root: Path,
        milestone_id: str,
        patch_file: Path,
        baseline_classification: Path,
        filter_src_only: bool = True,
        output_dir: Optional[Path] = None,
        agent_attempt: int = 0,
        keep_container: bool = False,
    ):
        self.workspace_root = workspace_root
        self.milestone_id = milestone_id
        self.patch_file = patch_file
        self.baseline_classification = baseline_classification
        self.filter_src_only = filter_src_only
        self.agent_attempt = agent_attempt
        self.keep_container = keep_container

        # Extract repo name from workspace_root path
        # EvoClaw path structure: .../EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0
        # AgentBench path structure: .../harness_workspace/repo_name/test_name
        # Detect which structure by checking if workspace_root itself has metadata.json
        if (workspace_root / "metadata.json").exists():
            # EvoClaw: workspace_root IS the repo directory
            self.repo_name = workspace_root.name
            self.test_name = None
        else:
            # AgentBench: workspace_root is repo/test_name
            self.repo_name = workspace_root.parent.name
            self.test_name = workspace_root.name

        # Load repo configuration
        self.repo_config = load_repo_config(self.repo_name, workspace_root=workspace_root)

        # Load test_dir and test_workdir from metadata.json if available
        metadata_path = workspace_root / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as f:
                metadata = json.load(f)
                self.test_dir = metadata.get("test_dir", "test/")
                # test_workdir is the directory to cd into before running tests
                # defaults to /testbed for backwards compatibility
                self.test_workdir = metadata.get("test_workdir", "/testbed")
                # test_timeout: timeout per test in seconds (0 or None to disable)
                # supports legacy key "pytest_timeout" for backwards compatibility
                self.test_timeout = metadata.get("test_timeout") or metadata.get("pytest_timeout", 50)
                # docker_cpus: number of CPUs for Docker container (default: 16)
                self.docker_cpus = metadata.get("docker_cpus", 16)
        else:
            self.test_dir = "test/"
            self.test_workdir = "/testbed"
            self.test_timeout = 50
            self.docker_cpus = 16
        # Ensure test_dir ends with / for consistent path handling
        if not self.test_dir.endswith("/"):
            self.test_dir = self.test_dir + "/"
        print(f"📋 Test directory: {self.test_dir}")
        print(f"📋 Test workdir: {self.test_workdir}")
        print(f"📋 Test timeout: {self.test_timeout}s" if self.test_timeout else "📋 Test timeout: disabled")
        print(f"📋 Docker CPUs: {self.docker_cpus}")

        # Docker image name: {repo_name_lower}/{milestone_id_lower}:{image_tag}
        # EvoClaw example: navidrome_navidrome_v0.57.0_v0.58.0/milestone_006:v0.9
        # AgentBench example: urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/m001:v0.9
        # Note: Docker image names must be lowercase (OCI spec requirement)
        # Benchmark data version is pinned via EVOCLAW_IMAGE_TAG (default: v0.9);
        # resolve_image falls back to :latest WITH a warning when the default
        # pin is absent locally (never when the tag was set explicitly).
        if self.test_name:
            image_base = f"{self.repo_name.lower()}/{self.test_name.lower()}/{milestone_id.lower()}"
        else:
            image_base = f"{self.repo_name.lower()}/{milestone_id.lower()}"
        self.docker_image = resolve_image(image_base)

        # Docker container name: {repo_base}-{milestone_id}-{pid}-eval[-retry{N}]
        # Extract repo base name (e.g., "urllib3_urllib3_2.0.6_2.3.0" -> "urllib3")
        # Include process ID to avoid conflicts when running multiple evaluations in parallel
        # For retries, append -retry{N} suffix to avoid container name conflicts
        repo_base = self.repo_name.split("_")[0]
        pid = os.getpid()
        if agent_attempt == 0:
            self.container_name = f"{repo_base}-{milestone_id.lower()}-{pid}-eval"
        else:
            self.container_name = f"{repo_base}-{milestone_id.lower()}-{pid}-eval-retry{agent_attempt}"

        # Host-side directory mounted to /output in the container for test artifacts.
        # Include PID to avoid collisions when running in parallel.
        if output_dir:
            self.output_dir = output_dir / "artifacts" / str(pid)
        else:
            # Fallback for CLI usage
            self.output_dir = self.workspace_root / "evaluation" / self.milestone_id / "artifacts" / str(pid)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def load_baseline_classification(self) -> Dict[str, Any]:
        """Load baseline test classification."""
        with open(self.baseline_classification) as f:
            return json.load(f)

    def start_container(self) -> None:
        """Start Docker container (image already at baseline commit)."""

        # Stop and remove existing container if it exists
        subprocess.run(
            ["docker", "stop", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "rm", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Start new container
        # Note: No memory limit (--memory) to allow tests to use host memory freely.
        # This avoids OOM issues for memory-intensive tests (e.g., transformer models).
        # Note: --ulimit nofile=65535:65535 increases file descriptor limit to avoid
        # "Too many open files" errors when running many tests in parallel.
        # Note: --init properly reaps zombie child processes (e.g., plugin processes)
        cmd = [
            "docker",
            "run",
            "-d",
            "--init",
            "--name",
            self.container_name,
            "--cpus",
            str(self.docker_cpus),
            "--ulimit",
            "nofile=65535:65535",
            "-v",
            f"{str(self.output_dir.resolve())}:/output",
            self.docker_image,
            "tail",
            "-f",
            "/dev/null",
        ]

        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"Started container: {self.container_name} (image: {self.docker_image}, cpus: {self.docker_cpus})")

    def _checkout_to_tag(self, tag_suffix: str, clean: bool = True) -> Tuple[bool, str]:
        """Checkout to a specific milestone tag in the container.

        Args:
            tag_suffix: 'start' or 'end' to checkout to milestone-{milestone_id}-start/end
            clean: If True, also run git clean to remove untracked files (prevents data pollution)

        Returns:
            Tuple of (success, error_message)
        """
        tag_name = f"milestone-{self.milestone_id}-{tag_suffix}"

        # Build checkout command
        # --force: overwrite modified tracked files
        # git clean -fd: remove untracked files and directories (prevents data pollution)
        if clean:
            checkout_script = (
                f"cd /testbed && "
                f"git checkout {tag_name} --force && "
                f"git clean -fd"  # -f: force, -d: include directories
            )
        else:
            checkout_script = f"cd /testbed && git checkout {tag_name} --force"

        checkout_cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-c",
            checkout_script,
        ]
        result = subprocess.run(checkout_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, f"Failed to checkout to {tag_name}: {result.stderr}"

        clean_msg = " (cleaned untracked files)" if clean else ""
        print(f"📍 Checked out to tag: {tag_name}{clean_msg}")
        return True, ""

    def _check_compilation(self) -> Tuple[bool, str]:
        """Check if the code compiles successfully.

        Uses build_command from repo config if available.
        For projects without build_command, this is a no-op (always returns True).

        Returns:
            Tuple of (success, error_message)
        """
        # Get build_command from repo config
        build_command = self.repo_config.get("build_command")
        if not build_command:
            # No build command configured, skip compilation check
            return True, ""

        print(f"🔨 Checking compilation with: {build_command[:60]}...")
        compile_cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-c",
            f"{build_command} 2>&1",
        ]
        try:
            result = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return False, "Compilation check timed out after 10 minutes"

        if result.returncode != 0:
            output = result.stdout + result.stderr

            # Check if this is just npm warnings (not actual errors)
            # npm warnings should not cause compilation failure
            if self._is_npm_warning_only(output):
                print("✅ Compilation check passed (npm warnings only, no actual errors)")
                return True, ""

            # Extract key error messages
            error_lines = [line for line in output.split("\n") if "error" in line.lower()][:10]
            error_summary = "\n".join(error_lines) if error_lines else output[-500:]
            return False, f"Compilation failed:\n{error_summary}"

        print("✅ Compilation check passed")
        return True, ""

    def _is_npm_warning_only(self, output: str) -> bool:
        """Check if build output contains only npm warnings without real errors.

        npm can exit with non-zero code for warnings (like peer dependency warnings)
        but the build might still succeed. This method checks if the output contains
        only warnings and not actual errors.

        Args:
            output: Combined stdout/stderr from build command

        Returns:
            True if output contains only warnings (no real errors)
        """
        lines = output.strip().split("\n")

        # Patterns that indicate real errors (not just warnings)
        error_patterns = [
            "npm ERR!",  # npm actual error
            "npm error",  # npm 7+ error format
            ": error:",  # Go/TypeScript compiler error
            "FAILED:",  # General failure
            "Error:",  # General error (but not "error" in warning text)
            "fatal error",  # Fatal errors
            "compilation failed",  # Explicit compilation failure
            "build failed",  # Explicit build failure
            "cannot find module",  # Module not found
            "syntax error",  # Syntax errors
            "undefined:",  # Go undefined errors
        ]

        # Patterns that are just warnings (ok to ignore)
        warning_patterns = [
            "npm WARN",  # npm warning
            "npm warn",  # npm 7+ warning format
            "warning:",  # General warnings
            "WARN ",  # General warn prefix
        ]

        has_real_error = False
        has_warning = False

        for line in lines:
            line_lower = line.lower()

            # Check for real errors
            for pattern in error_patterns:
                if pattern.lower() in line_lower:
                    # Make sure it's not part of a warning message
                    is_warning_line = any(wp.lower() in line_lower for wp in warning_patterns)
                    if not is_warning_line:
                        has_real_error = True
                        break

            # Check for warnings
            for pattern in warning_patterns:
                if pattern.lower() in line_lower:
                    has_warning = True
                    break

            if has_real_error:
                break

        # Only treat as warning-only if we found warnings but no real errors
        return has_warning and not has_real_error

    def _apply_tar_to_container(self, gt_tag_suffix: str = "end") -> Tuple[bool, str]:
        """Extract tar archive to container and process Rust test regions.

        Args:
            gt_tag_suffix: Tag suffix for GT tests ("end" or "start")

        Returns:
            Tuple of (success, error_message)
        """
        # Copy tar to container
        copy_cmd = ["docker", "cp", str(self.patch_file), f"{self.container_name}:/testbed/snapshot.tar"]
        result = subprocess.run(copy_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, f"Failed to copy tar to container: {result.stderr}"

        # Extract tar archive (overwrites existing files)
        extract_cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-c",
            "cd /testbed && tar -xf snapshot.tar && rm snapshot.tar",
        ]
        result = subprocess.run(extract_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            return False, f"Failed to extract tar archive:\n{result.stderr}\n{result.stdout}"

        print(f"Successfully extracted tar archive: {self.patch_file.name}")

        # Run apply_patches.sh if it exists in the container (to reapply compilation fixes)
        patch_check_cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-c",
            "test -x /usr/local/bin/apply_patches.sh && /usr/local/bin/apply_patches.sh || true",
        ]
        patch_result = subprocess.run(patch_check_cmd, capture_output=True, text=True)
        if "patches applied" in patch_result.stdout.lower():
            print("🔧 Re-applied compilation patches after tar extraction")

        # For Rust projects: replace agent's inline tests with GT tests
        rust_files = get_rust_files_from_tar(self.patch_file)
        if rust_files:
            print(
                f"🦀 Processing {len(rust_files)} Rust files for test region replacement (using {gt_tag_suffix} tag)..."
            )
            filter_result = process_rust_files_in_container(
                container_name=self.container_name,
                milestone_id=self.milestone_id,
                rust_files=rust_files,
                gt_tag_suffix=gt_tag_suffix,
            )
            if filter_result["processed"] > 0 or filter_result["total_agent_tests_removed"] > 0:
                print(f"   Processed: {filter_result['processed']} files")
                print(f"   Agent test regions removed: {filter_result['total_agent_tests_removed']}")
                print(f"   GT test regions appended: {filter_result['total_gt_tests_appended']}")
            if filter_result["failed"] > 0:
                print(f"   ⚠️  Failed: {filter_result['failed']} files")
                for detail in filter_result["details"]:
                    if not detail["success"] and not detail["skipped"]:
                        print(f"      - {detail['file']}: {detail['reason']}")

        return True, ""

    def _apply_tar_simple(self) -> Tuple[bool, str]:
        """Apply tar archive on END tag (for projects without build_command).

        Returns:
            Tuple of (success, error_message)
        """
        # Checkout to END tag first
        print("\n📦 Using END tag as base")
        success, error = self._checkout_to_tag("end")
        if not success:
            print(f"⚠️  Failed to checkout to END tag: {error}")
            print("   Falling back to current state (START tag)")

        return self._apply_tar_to_container(gt_tag_suffix="end")

    def _apply_tar_with_fallback(self) -> Tuple[bool, str]:
        """Apply tar archive with END tag first, fallback to START tag on compile error.

        Strategy:
        1. Checkout to END tag (complete implementation)
        2. Apply agent's code
        3. Check compilation
        4. If fails, checkout to START tag and re-apply

        Returns:
            Tuple of (success, error_message)
        """
        # Step 1: Try with END tag first
        print("\n📦 Strategy: Try END tag first (complete implementation as base)")
        success, error = self._checkout_to_tag("end")
        if not success:
            print(f"⚠️  Failed to checkout to END tag: {error}")
            print("   Falling back to current state (START tag)")
        else:
            # Apply agent's code on top of END tag, using END tag GT tests
            success, error = self._apply_tar_to_container(gt_tag_suffix="end")
            if not success:
                return False, error

            # Check compilation
            compile_ok, compile_error = self._check_compilation()
            if compile_ok:
                print("✅ Code compiles successfully on END tag base")
                return True, ""
            else:
                print(f"⚠️  Compilation failed on END tag base:")
                # Print first few lines of error
                for line in compile_error.split("\n")[:5]:
                    print(f"   {line}")

        # Step 2: Fallback to START tag
        print("\n📦 Fallback: Using START tag (baseline) as base")
        success, error = self._checkout_to_tag("start")
        if not success:
            return False, f"Failed to checkout to START tag: {error}"

        # Re-apply agent's code on top of START tag, using START tag GT tests
        success, error = self._apply_tar_to_container(gt_tag_suffix="start")
        if not success:
            return False, error

        # Check compilation on START tag
        compile_ok, compile_error = self._check_compilation()
        if compile_ok:
            print("✅ Code compiles successfully on START tag base")
        else:
            print(f"⚠️  Compilation also failed on START tag base (agent code has errors)")
            # Still continue - tests will fail but we want to report the results

        return True, ""

    def apply_patch(self, filter_src_only: bool = True) -> Tuple[bool, str]:
        """
        Apply patch to Docker container.

        For Rust projects with test_config.json:
        1. First tries applying patch on top of END tag (complete implementation)
        2. If compilation fails, falls back to START tag (baseline)

        Supports two formats:
        - tar archive: extracted directly to /testbed (used by E2E orchestrator)
        - diff/patch: applied via git apply

        Args:
            filter_src_only: If True, only apply changes to src/ directory, excluding test files

        Returns:
            Tuple of (success, error_message)
        """
        if not self.patch_file.exists():
            return False, f"Patch file not found: {self.patch_file}"

        # Detect file type by extension
        is_tar = self.patch_file.suffix == ".tar" or self.patch_file.name.endswith(".tar")

        if is_tar:
            # For projects with build_command, try END tag first, then fall back to START tag
            if self.repo_config.get("build_command"):
                return self._apply_tar_with_fallback()
            else:
                return self._apply_tar_simple()

        # Handle diff/patch file (no fallback logic for now)
        copy_cmd = ["docker", "cp", str(self.patch_file), f"{self.container_name}:/testbed/patch.diff"]
        result = subprocess.run(copy_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, f"Failed to copy patch to container: {result.stderr}"

        # Filter patch if needed
        if filter_src_only:
            filter_cmd = [
                "docker",
                "exec",
                self.container_name,
                "bash",
                "-c",
                """cd /testbed && \
                   git apply --verbose patch.diff --include='src/*' --exclude='test/*' --exclude='tests/*' || \
                   (filterdiff -i 'src/*' -x 'test/*' -x 'tests/*' patch.diff > patch_filtered.diff && \
                    git apply --verbose patch_filtered.diff)""",
            ]
            result = subprocess.run(filter_cmd, capture_output=True, text=True)

            if result.returncode != 0:
                return False, f"Failed to apply filtered patch:\n{result.stderr}\n{result.stdout}"

            print(f"Successfully applied filtered patch (src/ only): {self.patch_file.name}")
        else:
            # Apply patch without filtering
            apply_cmd = [
                "docker",
                "exec",
                self.container_name,
                "bash",
                "-c",
                "cd /testbed && git apply --verbose patch.diff",
            ]
            result = subprocess.run(apply_cmd, capture_output=True, text=True)

            if result.returncode != 0:
                return False, f"Failed to apply patch:\n{result.stderr}"

            print(f"Successfully applied patch: {self.patch_file.name}")

        return True, ""

    def _apply_struct_field_fixes(self) -> None:
        """Apply struct field compatibility fixes before running tests.

        When agent code doesn't have certain struct fields, but GT test code
        references them, we need to remove those references to avoid E0560
        (no such field) compilation errors.

        Configuration is read from repo config's `struct_field_fixes` section.
        """
        fixes = self.repo_config.get("struct_field_fixes", [])
        if not fixes:
            return

        for fix in fixes:
            check_file = fix.get("check_file")
            check_pattern = fix.get("check_pattern")
            fix_file = fix.get("fix_file")
            remove_pattern = fix.get("remove_line_pattern")

            if not all([check_file, check_pattern, fix_file, remove_pattern]):
                logger.warning(f"Incomplete struct_field_fix config: {fix}")
                continue

            # Check if the pattern exists in the source file
            check_cmd = [
                "docker",
                "exec",
                self.container_name,
                "bash",
                "-c",
                f"grep -q '{check_pattern}' /testbed/{check_file}",
            ]
            result = subprocess.run(check_cmd, capture_output=True, text=True)

            if result.returncode == 0:
                # Pattern found - field exists, no fix needed
                logger.debug(f"Field pattern '{check_pattern}' found in {check_file}, no fix needed")
                continue

            # Pattern not found - field doesn't exist, apply fix
            # Count how many lines will be removed
            # Note: grep -c exits 0 only when matches found, exits 1 when no matches
            count_cmd = [
                "docker",
                "exec",
                self.container_name,
                "bash",
                "-c",
                f"grep -c '{remove_pattern}' /testbed/{fix_file}",
            ]
            count_result = subprocess.run(count_cmd, capture_output=True, text=True)

            # Only proceed if grep found matches (exit code 0)
            if count_result.returncode != 0:
                logger.debug(f"No '{remove_pattern}' found in {fix_file}, skipping fix")
                continue

            count = count_result.stdout.strip()

            # Apply the fix - remove lines matching the pattern
            fix_cmd = [
                "docker",
                "exec",
                self.container_name,
                "bash",
                "-c",
                f"sed -i '/{remove_pattern}/d' /testbed/{fix_file}",
            ]
            fix_result = subprocess.run(fix_cmd, capture_output=True, text=True)

            if fix_result.returncode == 0:
                print(f"🔧 Applied struct field fix: removed {count} '{remove_pattern}' lines from {fix_file}")
            else:
                logger.warning(f"Failed to apply struct field fix to {fix_file}: {fix_result.stderr}")

    def run_tests(self) -> Dict[str, Any]:
        """
        Run tests in Docker container.

        Returns:
            Test results in standardized format (from test_runner report_parser):
            {
                "tests": [{"nodeid": str, "outcome": str}, ...],
                "summary": {"total": int, "passed": int, "failed": int, "error": int, "skipped": int},
                ...
            }
        """
        # Apply struct field compatibility fixes before running tests
        self._apply_struct_field_fixes()

        class _ExecRunner:
            def __init__(self, container_name: str):
                self.container_name = container_name

            def run(
                self,
                script: str,
                timeout: Optional[int] = None,
                extra_volumes: Optional[Dict[str, str]] = None,
            ) -> Tuple[int, str, str]:
                # extra_volumes is ignored: the container is started with /output mounted.
                cmd = ["docker", "exec", self.container_name, "bash", "-c", script]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                    return result.returncode, result.stdout, result.stderr
                except subprocess.TimeoutExpired:
                    return -1, "", f"Command timed out after {timeout} seconds"

        runner = _ExecRunner(self.container_name)
        merged_path = run_single_state_tests(
            runner,  # type: ignore[arg-type] - duck-typed runner adapter
            workspace_root=self.workspace_root,
            milestone_id=self.milestone_id,
            output_dir=self.output_dir,
            workers=self.docker_cpus,
            timeout=self.test_timeout,
            workdir=self.test_workdir,
            test_dir=self.test_dir,
            verbose=False,
            output_prefix="eval",
        )

        # Optional human-readable summary (useful for debugging)
        convert_to_summary(merged_path, self.output_dir / "eval_summary.json")
        with open(merged_path) as f:
            return json.load(f)

    def compare_results(
        self,
        baseline_classification: Dict[str, Any],
        test_results: Dict[str, Any],
        patch_exists: bool,
        patch_applied: bool,
    ) -> EvaluationResult:
        """
        Compare test results against baseline classification.

        Uses stable_classification (excluding flaky tests) if available for resolved judgment.
        Flaky tests are evaluated separately and reported for reference.

        Args:
            baseline_classification: Baseline test classification
            test_results: Standardized report dict or eval_summary.json content
            patch_exists: Whether patch file exists
            patch_applied: Whether patch was successfully applied

        Returns:
            EvaluationResult with detailed comparison
        """
        # Determine test framework from repo config for test ID normalization
        # This handles Go fuzz/parameterized tests with random subtest IDs
        test_framework = self.repo_config.get("test_framework")
        test_id_normalizer = TestIdNormalizer(framework=test_framework, enable_normalization=True)

        def load_summary_payload(results: Dict[str, Any]) -> Dict[str, Any]:
            """Prefer eval_summary.json when available; fall back to provided results."""
            if isinstance(results.get("results"), dict) and isinstance(results.get("summary"), dict):
                return results

            summary_path = self.output_dir / "eval_summary.json"
            if summary_path.exists():
                try:
                    with open(summary_path) as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning(f"Failed to load eval summary from {summary_path}: {e}")

            return results

        def build_test_outcomes(
            summary_payload: Dict[str, Any],
            go_module: Optional[str] = None,
            normalizer: Optional[TestIdNormalizer] = None,
        ) -> Tuple[Dict[str, str], Dict[str, List[Tuple[str, str]]]]:
            """Build a normalized nodeid -> outcome map from summary `results`.

            All nodeids are normalized using both normalize_ginkgo_nodeid (for Ginkgo/Java)
            and TestIdNormalizer (for Go fuzz/parameterized tests) to handle format
            differences between baseline and runtime test IDs.

            Returns:
                Tuple of:
                - outcomes: normalized_id -> outcome (for exact match lookups)
                - normalized_groups: normalized_id -> [(original_id, outcome), ...] (for fuzz test matching)
            """
            outcomes: Dict[str, str] = {}
            normalized_groups: Dict[str, List[Tuple[str, str]]] = {}
            summary_results = summary_payload.get("results", {})
            if not isinstance(summary_results, dict):
                return outcomes, normalized_groups

            def add_outcome(nodeid: str, outcome: str) -> None:
                # First apply Ginkgo/Java normalization
                ginkgo_normalized = normalize_ginkgo_nodeid(nodeid, go_module)
                outcomes[ginkgo_normalized] = outcome

                # Also apply TestIdNormalizer for fuzz test matching
                if normalizer:
                    fuzz_normalized = normalizer.normalize(nodeid)
                    if fuzz_normalized not in normalized_groups:
                        normalized_groups[fuzz_normalized] = []
                    normalized_groups[fuzz_normalized].append((nodeid, outcome))

            for item in summary_results.get("failed", []):
                if isinstance(item, dict) and item.get("nodeid"):
                    add_outcome(item["nodeid"], "failed")

            for item in summary_results.get("error", []):
                if isinstance(item, dict) and item.get("nodeid"):
                    add_outcome(item["nodeid"], "error")

            for item in summary_results.get("xpassed", []):
                if isinstance(item, dict) and item.get("nodeid"):
                    add_outcome(item["nodeid"], "failed")

            for item in summary_results.get("xfailed", []):
                if isinstance(item, dict) and item.get("nodeid"):
                    add_outcome(item["nodeid"], "passed")

            for group in summary_results.get("skipped", []):
                if not isinstance(group, dict):
                    continue
                for test_id in group.get("tests", []) or []:
                    add_outcome(test_id, "skipped")

            for test_id in summary_results.get("passed", []) or []:
                add_outcome(test_id, "passed")

            return outcomes, normalized_groups

        def extract_test_ids(items: List[Any]) -> List[str]:
            ids: List[str] = []
            for item in items:
                if isinstance(item, dict):
                    test_id = item.get("test_id") or item.get("nodeid")
                    if test_id:
                        ids.append(test_id)
                else:
                    ids.append(item)
            return ids

        def dedupe_by_normalization(test_ids: List[str], normalizer: Optional[TestIdNormalizer] = None) -> List[str]:
            """Deduplicate test IDs by their normalized form.

            For fuzz/parameterized tests with random IDs (e.g., TestMap/JBzrWpYM,
            TestMap/bYuXm9Hl), this groups them into a single logical test (TestMap).

            Args:
                test_ids: List of original test IDs
                normalizer: TestIdNormalizer for fuzz test normalization

            Returns:
                List of unique normalized test IDs
            """
            if not normalizer:
                return test_ids

            seen: Set[str] = set()
            result: List[str] = []
            for test_id in test_ids:
                if not test_id:
                    continue
                normalized = normalizer.normalize(test_id)
                if normalized not in seen:
                    seen.add(normalized)
                    result.append(normalized)  # Use normalized ID
            return result

        def lookup_outcome(
            test_id: str,
            outcomes: Dict[str, str],
            normalized_groups: Dict[str, List[Tuple[str, str]]],
            normalizer: Optional[TestIdNormalizer] = None,
        ) -> str:
            """Look up test outcome with fallback to normalized matching.

            First tries exact match (after Ginkgo normalization), then falls back to
            TestIdNormalizer-based matching for fuzz/parameterized tests.

            Args:
                test_id: The baseline test ID to look up
                outcomes: Ginkgo-normalized nodeid -> outcome map
                normalized_groups: Fuzz-normalized nodeid -> [(original_id, outcome), ...] map
                normalizer: TestIdNormalizer for fuzz test matching

            Returns:
                Test outcome: "passed", "failed", "error", "skipped", or "unknown"
            """
            # First try exact match with Ginkgo normalization
            ginkgo_normalized = normalize_ginkgo_nodeid(test_id)
            if ginkgo_normalized in outcomes:
                return outcomes[ginkgo_normalized]

            # Fall back to TestIdNormalizer-based matching for fuzz tests
            if normalizer:
                fuzz_normalized = normalizer.normalize(test_id)
                if fuzz_normalized in normalized_groups:
                    matches = normalized_groups[fuzz_normalized]
                    outcomes_list = [outcome for _, outcome in matches]
                    # Priority 1: Any failed/error → failed
                    if any(o in ("failed", "error") for o in outcomes_list):
                        return "failed"
                    # Priority 2: All passed → passed
                    if all(o == "passed" for o in outcomes_list):
                        return "passed"
                    # Priority 3: Other cases (pass+skip or all skip) → skipped
                    return "skipped"

            return "unknown"

        # Use stable_classification if available, otherwise fall back to classification
        if "stable_classification" in baseline_classification:
            classification_to_use = baseline_classification["stable_classification"]
            print("📊 Using stable_classification for resolved judgment (excluding flaky tests)")
        elif "classification" in baseline_classification:
            classification_to_use = baseline_classification["classification"]
            print("⚠️  No stable_classification found, using full classification")
        else:
            classification_to_use = baseline_classification

        summary_payload = load_summary_payload(test_results)
        summary = summary_payload.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}

        # Build test outcomes with normalized nodeids
        # Test results are already in /testbed format, so no go_module needed
        # Pass normalizer for fuzz test matching
        test_outcomes, normalized_groups = build_test_outcomes(summary_payload, normalizer=test_id_normalizer)

        # Extract and deduplicate test IDs by normalization
        # This handles fuzz/parameterized tests with random IDs (e.g., TestMap/xxx)
        # by grouping them into single logical tests
        fail_to_pass_ids_raw = extract_test_ids(classification_to_use.get("fail_to_pass", []))
        pass_to_pass_ids_raw = extract_test_ids(classification_to_use.get("pass_to_pass", []))
        none_to_pass_ids_raw = extract_test_ids(classification_to_use.get("none_to_pass", []))
        if not none_to_pass_ids_raw:
            none_to_pass_ids_raw = [
                t.get("test_id")
                for t in baseline_classification.get("new_tests", [])
                if isinstance(t, dict) and t.get("end_outcome") == "passed"
            ]

        # Deduplicate by normalization (e.g., 9 TestMap/* → 1 TestMap)
        fail_to_pass_ids = dedupe_by_normalization(fail_to_pass_ids_raw, test_id_normalizer)
        pass_to_pass_ids = dedupe_by_normalization(pass_to_pass_ids_raw, test_id_normalizer)
        none_to_pass_ids = dedupe_by_normalization(none_to_pass_ids_raw, test_id_normalizer)

        # Log deduplication stats if any reduction occurred
        if len(fail_to_pass_ids) < len(fail_to_pass_ids_raw):
            print(f"📊 F2P deduped: {len(fail_to_pass_ids_raw)} → {len(fail_to_pass_ids)} tests")
        if len(pass_to_pass_ids) < len(pass_to_pass_ids_raw):
            print(f"📊 P2P deduped: {len(pass_to_pass_ids_raw)} → {len(pass_to_pass_ids)} tests")
        if len(none_to_pass_ids) < len(none_to_pass_ids_raw):
            print(f"📊 N2P deduped: {len(none_to_pass_ids_raw)} → {len(none_to_pass_ids)} tests")

        # Check which fail_to_pass tests now pass (stable tests only)
        # Use lookup_outcome for normalized matching (handles fuzz tests)
        fail_to_pass_success = []
        fail_to_pass_failure = []
        for test_id in fail_to_pass_ids:
            current_outcome = lookup_outcome(test_id, test_outcomes, normalized_groups, test_id_normalizer)
            if current_outcome == "passed":
                fail_to_pass_success.append(test_id)
            else:
                fail_to_pass_failure.append(test_id)

        # Check for pass_to_pass failures (regressions) - stable tests only
        # Use lookup_outcome for normalized matching (handles fuzz tests)
        pass_to_pass_failure = []
        pass_to_pass_success_count = 0
        pass_to_pass_missing = 0
        for test_id in pass_to_pass_ids:
            current_outcome = lookup_outcome(test_id, test_outcomes, normalized_groups, test_id_normalizer)
            if current_outcome in ("failed", "error"):
                pass_to_pass_failure.append(test_id)
            elif current_outcome == "passed":
                pass_to_pass_success_count += 1
            else:
                # Test not found in results (skipped module or ID mismatch)
                pass_to_pass_missing += 1

        # Check which none_to_pass tests now pass (stable tests only)
        # Use lookup_outcome for normalized matching (handles fuzz tests)
        # Must be done BEFORE resolved calculation
        none_to_pass_success = []
        none_to_pass_failure = []
        for test_id in none_to_pass_ids:
            if not test_id:
                continue
            current_outcome = lookup_outcome(test_id, test_outcomes, normalized_groups, test_id_normalizer)
            if current_outcome == "passed":
                none_to_pass_success.append(test_id)
            else:
                none_to_pass_failure.append(test_id)

        if none_to_pass_ids:
            print(
                f"📋 New tests (NONE_TO_PASS): {len(none_to_pass_success)} passed, {len(none_to_pass_failure)} failed"
            )

        # Determine if milestone resolved (based on stable tests only)
        # All three test categories must pass:
        # - FAIL_TO_PASS: All required tests must now pass
        # - PASS_TO_PASS: No regressions AND all must run (tests that passed before must still pass)
        # - NONE_TO_PASS: All new tests must pass
        total_tests = summary.get("total", 0)
        has_required_tests = len(fail_to_pass_ids) > 0 or len(pass_to_pass_ids) > 0 or len(none_to_pass_ids) > 0
        if total_tests == 0 and has_required_tests:
            resolved = False  # Tests didn't run but should have
        else:
            resolved = (
                len(fail_to_pass_success) == len(fail_to_pass_ids)  # All F2P tests pass
                and len(pass_to_pass_failure) == 0  # No regressions
                and pass_to_pass_missing == 0  # All P2P tests must run
                and len(none_to_pass_success) == len(none_to_pass_ids)  # All N2P tests pass
            )

        if pass_to_pass_missing > 0:
            print(f"⚠️  PASS_TO_PASS Missing: {pass_to_pass_missing} tests not found (skipped modules or ID mismatch)")

        return EvaluationResult(
            milestone_id=self.milestone_id,
            patch_is_None=(self.patch_file is None),
            patch_exists=patch_exists,
            patch_successfully_applied=patch_applied,
            resolved=resolved,
            fail_to_pass_success=fail_to_pass_success,
            fail_to_pass_failure=fail_to_pass_failure,
            pass_to_pass_success_count=pass_to_pass_success_count,
            pass_to_pass_failure=pass_to_pass_failure,
            pass_to_pass_missing=pass_to_pass_missing,
            none_to_pass_success=none_to_pass_success,
            none_to_pass_failure=none_to_pass_failure,
            total_tests=summary.get("total", 0),
            passed_tests=summary.get("passed", 0),
            failed_tests=summary.get("failed", 0),
            error_tests=summary.get("error", 0),
            skipped_tests=summary.get("skipped", 0),
            fail_to_pass_required=len(fail_to_pass_ids),
            fail_to_pass_achieved=len(fail_to_pass_success),
            pass_to_pass_required=len(pass_to_pass_ids),
            none_to_pass_required=len(none_to_pass_ids),
            none_to_pass_achieved=len(none_to_pass_success),
        )

    def cleanup(self) -> None:
        """Stop and remove Docker container."""
        subprocess.run(
            ["docker", "stop", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "rm", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Cleaned up container: {self.container_name}")

    def evaluate(self) -> EvaluationResult:
        """
        Run full evaluation workflow.

        Returns:
            EvaluationResult with pass/fail determination
        """
        try:
            # 1. Load baseline classification
            print(f"Loading baseline classification: {self.baseline_classification}")
            baseline = self.load_baseline_classification()

            # 2. Start container at baseline commit
            print("Starting Docker container...")
            self.start_container()

            # 3. Apply patch
            if self.filter_src_only:
                print(f"Applying patch (src/ only): {self.patch_file}")
            else:
                print(f"Applying patch: {self.patch_file}")
            success, error = self.apply_patch(filter_src_only=self.filter_src_only)
            if not success:
                raise RuntimeError(f"Patch application failed: {error}")

            # 4. Run tests once (no retry logic in E2E evaluator)
            print("Running tests...")
            test_results = self.run_tests()

            # 5. Compare results
            print("Comparing results against baseline...")
            evaluation = self.compare_results(baseline, test_results, patch_exists=True, patch_applied=True)

            return evaluation

        finally:
            if self.keep_container:
                print(f"📦 Container kept: {self.container_name}")
            else:
                self.cleanup()


def main():
    """Main entry point for evaluation CLI."""
    parser = argparse.ArgumentParser(
        description="Evaluate agent-generated patches for milestones",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python harness/e2e/evaluator.py \\
      --workspace-root DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 \\
      --milestone-id M001 \\
      --patch-file path/to/snapshot.tar \\
      --baseline-classification path/to/classification.json
        """,
    )

    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Path to test workspace directory (e.g., DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2)",
    )
    parser.add_argument("--milestone-id", type=str, help="Milestone ID (e.g., M001)")
    parser.add_argument("--patch-file", type=Path, help="Path to patch file to evaluate")
    parser.add_argument("--baseline-classification", type=Path, help="Path to baseline classification JSON")
    parser.add_argument("--output", type=Path, help="Output file for evaluation results (JSON)")
    parser.add_argument(
        "--no-filter-src",
        action="store_true",
        help="Disable filtering to apply all changes (default: only apply src/ changes)",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Keep Docker container after evaluation (for debugging/inspection)",
    )

    args = parser.parse_args()

    # Validate required arguments
    required = ["workspace_root", "milestone_id", "patch_file", "baseline_classification"]
    missing = [arg for arg in required if not getattr(args, arg, None)]
    if missing:
        parser.error(f"Missing required arguments: {', '.join('--' + arg.replace('_', '-') for arg in missing)}")

    # Generate default output path if not specified
    if not args.output:
        output_dir = args.workspace_root / "evaluation" / args.milestone_id / "results"
        args.output = output_dir / "evaluation_result.json"
    else:
        # Use the parent directory of --output as the output_dir for artifacts
        output_dir = args.output.parent

    # Create evaluator
    evaluator = PatchEvaluator(
        workspace_root=args.workspace_root,
        milestone_id=args.milestone_id,
        patch_file=args.patch_file,
        baseline_classification=args.baseline_classification,
        filter_src_only=not args.no_filter_src,
        output_dir=output_dir,
        keep_container=getattr(args, "keep_container", False),
    )

    # Run evaluation
    try:
        result = evaluator.evaluate()

        # Print summary
        print("\n" + result.summary())

        # Save results to file (always save, use default path if not specified)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(result.to_dict(), f, indent=2)
        print(f"\nResults saved to: {args.output}")

        # Generate filtered evaluation if filter_list exists
        filtered_path = generate_filtered_evaluation(args.output, args.workspace_root, args.milestone_id)
        if filtered_path:
            print(f"Filtered results saved to: {filtered_path}")

        # Exit with appropriate code
        sys.exit(0 if result.resolved else 1)

    except Exception as e:
        print(f"\n❌ Evaluation failed: {e}", file=sys.stderr)

        # Create a failed evaluation result instead of just exiting
        result = EvaluationResult(
            milestone_id=args.milestone_id,
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

        # Save failed result to file
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result_dict = result.to_dict()
        result_dict["error_message"] = str(e)
        with open(args.output, "w") as f:
            json.dump(result_dict, f, indent=2)
        print(f"\nFailed results saved to: {args.output}")

        sys.exit(2)


if __name__ == "__main__":
    main()
