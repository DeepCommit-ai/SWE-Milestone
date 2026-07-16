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
import fcntl
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Any, Tuple, Optional, Set, FrozenSet
from dataclasses import dataclass, field

import yaml

from harness.e2e.image_version import local_ref, resolve_image
from harness.e2e.quarantine import load_quarantine_config
from harness.e2e.repo_config_binding import (
    RepoConfigBindingError,
    RepoConfigIdentity,
)
from harness.e2e.runtime_policy_binding import (
    RUNTIME_POLICY_MODES,
    RuntimePolicyBindingError,
    RuntimePolicyIdentity,
    load_bound_runtime_policy,
)
from harness.e2e.residue_prune import (
    DEFAULT_PRUNE_EXTENSIONS,
    FAIL_CLOSED_SKIP_REASONS,
    ResiduePruneSafetyError,
    assert_prune_set_safe,
    capture_excluded_from_config,
    check_snapshot_integrity,
    compute_prune_set,
    normalize_extensions,
    normalize_keep_list,
    normalize_tar_members,
    resolve_prune_enablement,
)
from harness.utils.rust_test_filter import (
    get_rust_files_from_tar,
    process_rust_files_in_container,
)
from harness.utils.src_filter import SrcFileFilter
from harness.utils.snapshot import (
    GO_MANIFEST_BASENAMES,
    GO_MANIFEST_PROJECTION_SCHEMA_VERSION,
    ManifestOverlay,
    SNAPSHOT_METADATA_SCHEMA_VERSION,
    find_build_manifests,
    is_build_manifest,
    is_go_build_manifest,
    is_go_manifest_in_scope,
    normalize_snapshot_path,
    snapshot_sha256,
)
from harness.utils.test_id_normalizer import TestIdNormalizer
from harness.test_runner.core.milestone_attempt import run_single_state_tests
from harness.test_runner.core.test_executor import (
    detect_infrastructure_failure,
    extract_first_fatal_error,
)
from harness.test_runner.core.types import MilestoneTestConfig
from harness.test_runner.core.report_parser import convert_to_summary

logger = logging.getLogger(__name__)

ZERO_TESTS_WITH_REQUIRED_TESTS = "zero-tests-with-required-tests"
BUILD_FAILURE_WITH_ZERO_TESTS = "build-failure-with-zero-tests"
OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION = 4
OFFLINE_CACHE_LABEL_PREFIX = "org.evoclaw.evaluation-closure"
GO_EVALUATOR_PATH = (
    "/usr/local/go/bin:/go/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)
INTERNAL_EVALUATION_NETWORK = "evoclaw-eval-internal-v1"


def _validated_overlay_paths(value: object, *, field: str) -> List[str]:
    """Return Dockerfile-safe absolute evaluator overlay paths.

    Quarantine policy is trusted benchmark data, but it is still parsed from
    YAML and later interpolated into a Dockerfile.  Reject ambiguous paths
    instead of turning malformed policy into a host-side build primitive.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result: List[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.startswith("/"):
            raise ValueError(f"{field} path must be absolute: {raw!r}")
        path = PurePosixPath(raw)
        if any(part in ("", ".", "..") for part in path.parts[1:]):
            raise ValueError(f"unsafe closure cache path: {raw!r}")
        if not re.fullmatch(r"/[A-Za-z0-9._/@+\-]+(?:/[A-Za-z0-9._@+\-]+)*", raw):
            raise ValueError(f"unsupported closure cache path: {raw!r}")
        if raw not in result:
            result.append(raw)
    return result


def _validated_cache_paths(value: object) -> List[str]:
    """Compatibility wrapper for vetted package-manager cache paths."""
    return _validated_overlay_paths(value, field="closure.cache_paths")


def _configured_go_toolchain_version(quarantine_config: Optional[dict]) -> str:
    """Return the exact Go toolchain promised by the offline closure policy."""
    closure = quarantine_config.get("closure") if isinstance(quarantine_config, dict) else None
    toolchain = closure.get("toolchain") if isinstance(closure, dict) else None
    raw = toolchain.get("go") if isinstance(toolchain, dict) else None
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("closure.toolchain.go must be a string")
    version = raw.strip().removeprefix("go")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise ValueError(f"invalid closure.toolchain.go version: {raw!r}")
    return version


def _parse_go_version(output: str) -> str:
    """Extract the numeric version token from canonical ``go version`` output."""
    match = re.search(r"(?:^|\s)go(\d+\.\d+(?:\.\d+)?)(?:\s|$)", output or "")
    return match.group(1) if match else ""


def _render_offline_cache_overlay_dockerfile(
    milestone_image: str,
    closure_image: str,
    cache_paths: List[str],
    replace_paths: Optional[List[str]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> str:
    """Build a milestone image with the agent's vetted runtime overlaid.

    Every overlaid path is replaced exactly. Merging a milestone-local cache
    with the agent's captured closure would let evaluator-only dependency bytes
    repair a graph that no model could build in its own container.
    """
    replace = list(replace_paths or [])
    lines = [f"FROM {closure_image} AS closure", f"FROM {milestone_image}"]
    for key, value in sorted((labels or {}).items()):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", key):
            raise ValueError(f"unsafe Docker label key: {key!r}")
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
            raise ValueError(f"unsafe Docker label value for {key}: {value!r}")
        lines.append(f"LABEL {key}={value}")
    replaced: Set[str] = set()
    for path in [*cache_paths, *replace]:
        if path in replaced:
            continue
        replaced.add(path)
        lines.append(f"RUN rm -rf {path}")
    copied: Set[str] = set()
    for path in [*cache_paths, *replace]:
        if path in copied:
            continue
        copied.add(path)
        lines.append(f"COPY --from=closure {path} {path}")
    return "\n".join(lines) + "\n"


def _docker_image_id(image: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "image not found").strip()
        raise RuntimeError(f"Cannot inspect Docker image {image}: {detail}")
    return result.stdout.strip().removeprefix("sha256:")


def _immutable_image_ref(image_id: str) -> str:
    """Return a Docker reference that cannot be retargeted by a mutable tag."""
    normalized = str(image_id or "").removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError(f"invalid Docker image ID: {image_id!r}")
    return f"sha256:{normalized}"


def _bind_local_image_alias(image_id: str, role: str) -> str:
    """Bind a full local image ID to a deterministic Dockerfile-safe name.

    Docker accepts ``sha256:<config-id>`` for ``docker run`` but BuildKit treats
    that spelling in ``FROM`` as a remote repository named ``sha256``.  A local
    alias whose tag contains the full ID is therefore required.  It is created
    immediately before the build under the derived-image lock and verified
    against the source ID; ``--pull=false`` keeps resolution local.
    """
    normalized = _immutable_image_ref(image_id).removeprefix("sha256:")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", role):
        raise ValueError(f"invalid immutable image alias role: {role!r}")
    alias = f"evoclaw-immutable/{role}:sha256-{normalized}"
    tagged = subprocess.run(
        ["docker", "image", "tag", f"sha256:{normalized}", alias],
        capture_output=True,
        text=True,
    )
    if tagged.returncode != 0:
        detail = (tagged.stderr or tagged.stdout or "docker tag failed").strip()
        raise RuntimeError(f"Cannot bind local image alias {alias}: {detail}")
    actual = _docker_image_id(alias)
    if actual != normalized:
        raise RuntimeError(
            f"Local image alias {alias} resolved to {actual}, expected {normalized}"
        )
    return alias


def _docker_image_labels(image: str) -> Dict[str, str]:
    """Read image labels without trusting Docker's Go-template quoting."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "image not found").strip()
        raise RuntimeError(f"Cannot inspect Docker image {image}: {detail}")
    try:
        inspected = json.loads(result.stdout)
        labels = ((inspected[0].get("Config") or {}).get("Labels") or {})
    except (json.JSONDecodeError, IndexError, AttributeError) as exc:
        raise RuntimeError(f"Malformed Docker inspect output for {image}") from exc
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in labels.items()
    ):
        raise RuntimeError(f"Malformed Docker labels for {image}")
    return labels


def ensure_internal_evaluation_network() -> str:
    """Return a bridge with an interface but no external route.

    ``--network none`` removes ``eth0`` entirely and breaks otherwise-stable
    tests that inspect a private IP or bind a non-loopback listener.  A Docker
    internal bridge preserves baseline interface semantics while still denying
    Internet/host routing.  Existing networks are never trusted by name alone.
    """
    name = INTERNAL_EVALUATION_NETWORK
    lock_path = Path("/tmp") / f"{name}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inspected = subprocess.run(
            ["docker", "network", "inspect", name],
            capture_output=True,
            text=True,
        )
        if inspected.returncode != 0:
            created = subprocess.run(
                [
                    "docker", "network", "create", "--driver", "bridge",
                    "--internal",
                    "--label", "org.evoclaw.evaluation-network.schema=1",
                    name,
                ],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                detail = (created.stderr or created.stdout or "create failed").strip()
                raise RuntimeError(
                    f"Cannot create internal evaluator network {name}: {detail}"
                )
            inspected = subprocess.run(
                ["docker", "network", "inspect", name],
                capture_output=True,
                text=True,
            )
        try:
            data = json.loads(inspected.stdout)[0]
            internal = data.get("Internal") is True
            driver = data.get("Driver")
            label = ((data.get("Labels") or {}).get(
                "org.evoclaw.evaluation-network.schema"
            ))
        except (json.JSONDecodeError, IndexError, AttributeError) as exc:
            raise RuntimeError(
                f"Cannot inspect internal evaluator network {name}"
            ) from exc
        if inspected.returncode != 0 or not internal or driver != "bridge" or label != "1":
            raise RuntimeError(
                f"Evaluator network {name} is not the expected internal bridge "
                f"(internal={internal}, driver={driver!r}, schema={label!r})"
            )
    return name


def ensure_offline_evaluation_image(
    *,
    repo_name: str,
    milestone_id: str,
    milestone_image: str,
    quarantine_config: Optional[dict],
    expected_closure_image_id: str = "",
) -> Tuple[str, str, str, str]:
    """Overlay the base-offline union cache onto a milestone evaluator image.

    Agent containers already run from ``base-offline``.  Evaluator containers
    historically ran from milestone images built before that union existed, so
    an agent could compile a dependency that the evaluator could only obtain by
    going back online.  This local, content-addressed derived image gives both
    sides the same vetted bytes without rebuilding or retagging every milestone
    image.  It is rebuilt automatically whenever either source image changes.

    Returns ``(effective_image, milestone_image_id, closure_image_id,
    effective_image_id)``. ``effective_image`` is always an immutable digest
    reference whenever an overlay is required.
    """
    closure = quarantine_config.get("closure") if isinstance(quarantine_config, dict) else None
    cache_paths = _validated_cache_paths(
        closure.get("cache_paths") if isinstance(closure, dict) else None
    )
    replace_paths = _validated_overlay_paths(
        closure.get("evaluator_replace_paths")
        if isinstance(closure, dict)
        else None,
        field="closure.evaluator_replace_paths",
    )
    expected_go_toolchain = _configured_go_toolchain_version(quarantine_config)
    # ``closure.toolchain.go`` is an executable-runtime contract, not merely a
    # post-start assertion.  Agent containers run directly from base-offline,
    # but evaluator containers start from heterogeneous milestone images.  The
    # closure image therefore has to clean-replace the canonical Go tree even
    # when a policy author did not redundantly list it under
    # ``evaluator_replace_paths``.  Otherwise only the module cache is copied
    # and the evaluator can silently retain an older milestone toolchain.
    if expected_go_toolchain and "/usr/local/go" not in replace_paths:
        replace_paths.append("/usr/local/go")
    milestone_id_hash = _docker_image_id(milestone_image)
    if not cache_paths and not replace_paths:
        return (
            _immutable_image_ref(milestone_id_hash),
            milestone_id_hash,
            "",
            milestone_id_hash,
        )

    closure_image = resolve_image(local_ref(repo_name, "base-offline"))
    if expected_closure_image_id:
        closure_id_hash = _immutable_image_ref(expected_closure_image_id).removeprefix(
            "sha256:"
        )
        # The exact image captured with the submission must still exist locally.
        # Inspecting the digest also prevents a current base-offline tag from
        # silently substituting different bytes during replay.
        actual_by_digest = _docker_image_id(_immutable_image_ref(closure_id_hash))
        if actual_by_digest != closure_id_hash:
            raise RuntimeError(
                "Captured agent closure image ID resolves to different bytes: "
                f"expected {closure_id_hash}, got {actual_by_digest}"
            )
    else:
        closure_id_hash = _docker_image_id(closure_image)
    # Source image IDs are necessary but not sufficient cache keys: changing
    # the policy's cache paths with the same two images must not silently reuse
    # an older derived image. Keep the overlay recipe itself content-addressed.
    overlay_policy_hash = hashlib.sha256(
        json.dumps(
            {
                "schema_version": OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION,
                "cache_paths": cache_paths,
                "replace_paths": replace_paths,
                "expected_go_toolchain": expected_go_toolchain,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    derived = local_ref(
        repo_name,
        f"{milestone_id}-eval-closure",
        f"{milestone_id_hash[:12]}-{closure_id_hash[:12]}-{overlay_policy_hash[:12]}",
    )
    expected_labels = {
        f"{OFFLINE_CACHE_LABEL_PREFIX}.schema": str(
            OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION
        ),
        f"{OFFLINE_CACHE_LABEL_PREFIX}.milestone-image-id": milestone_id_hash,
        f"{OFFLINE_CACHE_LABEL_PREFIX}.closure-image-id": closure_id_hash,
        f"{OFFLINE_CACHE_LABEL_PREFIX}.policy-sha256": overlay_policy_hash,
    }
    lock_name = hashlib.sha256(derived.encode()).hexdigest()[:24]
    lock_path = Path("/tmp") / f"evoclaw-eval-image-{lock_name}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        exists = subprocess.run(
            ["docker", "image", "inspect", derived],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if exists.returncode == 0:
            actual_labels = _docker_image_labels(derived)
            mismatched = {
                key: (expected, actual_labels.get(key))
                for key, expected in expected_labels.items()
                if actual_labels.get(key) != expected
            }
            if mismatched:
                raise RuntimeError(
                    "Refusing stale/colliding evaluator closure image "
                    f"{derived}: parent/policy labels mismatch: {mismatched}"
                )
        else:
            milestone_alias = _bind_local_image_alias(
                milestone_id_hash, "milestone-parent"
            )
            closure_alias = _bind_local_image_alias(
                closure_id_hash, "closure-parent"
            )
            dockerfile = _render_offline_cache_overlay_dockerfile(
                milestone_alias,
                closure_alias,
                cache_paths,
                replace_paths,
                expected_labels,
            )
            built = subprocess.run(
                [
                    "docker", "build", "--pull=false", "--network=none",
                    "--tag", derived, "-",
                ],
                input=dockerfile,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if built.returncode != 0:
                detail = "\n".join(
                    part for part in (built.stdout, built.stderr) if part
                )[-6000:]
                raise RuntimeError(
                    "Failed to overlay the vetted offline cache onto evaluator "
                    f"image {milestone_image}:\n{detail}"
                )
        # A tag is only a cache/index. Run the exact image built or validated
        # under the lock so a later retag cannot alter this evaluation.
        effective_id = _docker_image_id(derived)
        final_labels = _docker_image_labels(_immutable_image_ref(effective_id))
        if any(final_labels.get(key) != value for key, value in expected_labels.items()):
            raise RuntimeError(
                f"Evaluator closure image {effective_id} lost its provenance labels"
            )
    return (
        _immutable_image_ref(effective_id),
        milestone_id_hash,
        closure_id_hash,
        effective_id,
    )


def baseline_required_test_counts(baseline: Dict[str, Any]) -> Dict[str, int]:
    """Return required F2P/N2P/P2P counts from a baseline classification."""
    classification = (
        baseline.get("stable_classification")
        or baseline.get("classification")
        or baseline
    )
    if not isinstance(classification, dict):
        return {"fail_to_pass": 0, "none_to_pass": 0, "pass_to_pass": 0}

    def count(category: str) -> int:
        value = classification.get(category)
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        return 0

    counts = {
        "fail_to_pass": count("fail_to_pass"),
        "none_to_pass": count("none_to_pass"),
        "pass_to_pass": count("pass_to_pass"),
    }

    # Legacy classifications may expose required new tests only here.
    if counts["none_to_pass"] == 0:
        new_tests = classification.get("new_tests") or baseline.get("new_tests") or []
        counts["none_to_pass"] = sum(
            1
            for item in new_tests
            if isinstance(item, dict) and item.get("end_outcome") == "passed"
        )
    return counts


def baseline_has_required_tests(baseline: Dict[str, Any]) -> bool:
    """Return whether a classification requires any F2P, N2P, or P2P test."""
    return any(baseline_required_test_counts(baseline).values())


def load_repo_config(repo_name: str, workspace_root: Optional[Path] = None) -> dict:
    """Load repository-specific config from config/{repo_name}.yaml.

    Searches for the config file in the following order:
    1. {data_root}/config/{repo_name}.yaml  (workspace_root's parent)
    2. {project_root}/config/{repo_name}.yaml  (legacy fallback)

    Args:
        repo_name: Repository name (e.g., 'microsoft_markitdown_v0.1.1_v0.1.3')
        workspace_root: Path to the workspace root (e.g., .../SWE-Milestone-data/repo_name)

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


def load_bound_repo_config(
    repo_name: str,
    config_path: Path,
    expected_sha256: str,
) -> dict:
    """Load one explicitly pinned repo config without a live-file fallback."""
    identity = RepoConfigIdentity(repo_name=repo_name, sha256=expected_sha256)
    path = Path(config_path)
    if path.is_symlink():
        raise RepoConfigBindingError(
            f"frozen repo config must not be a symlink: {path}"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepoConfigBindingError(
            f"cannot read frozen repo config at {path}: {exc}"
        ) from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != identity.sha256:
        raise RepoConfigBindingError(
            f"frozen repo config digest mismatch at {path}: "
            f"expected {identity.sha256}, got {actual}"
        )
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RepoConfigBindingError(
            f"invalid frozen repo config YAML at {path}: {exc}"
        ) from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RepoConfigBindingError(
            f"frozen repo config at {path} must contain a YAML mapping"
        )
    return value


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


def normalize_scoring_nodeid(
    nodeid: str,
    framework: Optional[str],
    go_module: Optional[str] = None,
) -> str:
    """Normalize a test ID without discarding identity-bearing prefixes.

    Maven and Gradle Surefire IDs use ``module::class::method``.  The module
    prefix is part of the test identity: different reactor modules can contain
    the same class and method names.  Other frameworks retain the historical
    Ginkgo/path normalization behavior.
    """
    if framework in ("maven", "gradle"):
        return normalize_java_hashcode(nodeid)
    return normalize_ginkgo_nodeid(nodeid, go_module)


def _java_moduleless_nodeid(nodeid: str) -> str:
    """Return the class/method portion of a Java ID for guarded fallback.

    Some legacy baseline or runtime reports do not carry a module prefix.  We
    may bridge that representation gap only when it identifies exactly one
    module-aware runtime test; callers must reject ambiguous matches.
    """
    normalized = normalize_java_hashcode(nodeid)
    parts = normalized.split("::")
    return "::".join(parts[1:]) if len(parts) >= 3 else normalized


def _java_nodeid_has_module(nodeid: str) -> bool:
    """Whether a Surefire-style ID explicitly carries a module prefix."""
    return len(normalize_java_hashcode(nodeid).split("::")) >= 3


def _aggregate_test_outcomes(outcomes: List[str]) -> str:
    """Conservatively combine repeated observations of one logical test."""
    if not outcomes:
        return "unknown"
    if "error" in outcomes:
        return "error"
    if "failed" in outcomes:
        return "failed"
    if all(outcome == "passed" for outcome in outcomes):
        return "passed"
    return "skipped"


def _build_scoring_test_outcomes(
    summary_payload: Dict[str, Any],
    *,
    framework: Optional[str],
    go_module: Optional[str] = None,
    normalizer: Optional[TestIdNormalizer] = None,
) -> Tuple[
    Dict[str, str],
    Dict[str, List[Tuple[str, str]]],
    Dict[str, List[Tuple[str, str]]],
]:
    """Build exact, fuzz-normalized, and guarded Java fallback indexes."""
    exact: Dict[str, str] = {}
    normalized_groups: Dict[str, List[Tuple[str, str]]] = {}
    java_moduleless_groups: Dict[str, List[Tuple[str, str]]] = {}
    summary_results = summary_payload.get("results", {})
    if not isinstance(summary_results, dict):
        return exact, normalized_groups, java_moduleless_groups

    def add_outcome(nodeid: str, outcome: str) -> None:
        canonical = normalize_scoring_nodeid(nodeid, framework, go_module)
        if canonical in exact:
            exact[canonical] = _aggregate_test_outcomes([exact[canonical], outcome])
        else:
            exact[canonical] = outcome

        if framework in ("maven", "gradle"):
            moduleless = _java_moduleless_nodeid(canonical)
            java_moduleless_groups.setdefault(moduleless, []).append((canonical, outcome))

        if normalizer:
            fuzz_normalized = normalizer.normalize(nodeid)
            normalized_groups.setdefault(fuzz_normalized, []).append((nodeid, outcome))

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

    return exact, normalized_groups, java_moduleless_groups


def _lookup_scoring_outcome(
    test_id: str,
    *,
    framework: Optional[str],
    outcomes: Dict[str, str],
    normalized_groups: Dict[str, List[Tuple[str, str]]],
    java_moduleless_groups: Dict[str, List[Tuple[str, str]]],
    normalizer: Optional[TestIdNormalizer] = None,
) -> str:
    """Resolve an outcome, allowing only unambiguous identity fallbacks."""
    canonical = normalize_scoring_nodeid(test_id, framework)
    if canonical in outcomes:
        return outcomes[canonical]

    if framework in ("maven", "gradle"):
        matches = java_moduleless_groups.get(_java_moduleless_nodeid(canonical), [])
        matched_ids = {matched_id for matched_id, _ in matches}
        if len(matched_ids) == 1:
            runtime_id = next(iter(matched_ids))
            # Bridge only a missing-prefix representation gap. If both IDs
            # explicitly name different modules, they are different tests even
            # when class/method text happens to match.
            if _java_nodeid_has_module(canonical) != _java_nodeid_has_module(runtime_id):
                return _aggregate_test_outcomes([outcome for _, outcome in matches])
        # More than one module owns this class/method.  Returning unknown is
        # fail-closed: never credit module A with module B's result.
        if matches:
            return "unknown"

    if normalizer:
        matches = normalized_groups.get(normalizer.normalize(test_id), [])
        if matches:
            return _aggregate_test_outcomes([outcome for _, outcome in matches])

    return "unknown"


def _find_free_port() -> int:
    """Ask the OS for a free TCP port. Host-network evaluations get a unique
    webServer port per run via SWE_MILESTONE_EVAL_PORT (fixed ports would make
    concurrent evals mutually exclusive and collide with foreign services)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_test_framework(
    repo_config: dict,
    workspace_root: Path,
    milestone_id: str,
    baseline_ids: Optional[List[str]] = None,
) -> Optional[str]:
    """Resolve the test framework for TestIdNormalizer, robustly.

    `repo_config["test_framework"]` (from config/<repo>.yaml) is authoritative
    but that file lives beside the *canonical* data root; a derived/de-pinned
    workspace-root may not have it, in which case the raw value is None and Go
    random-subtest normalization silently no-ops (the 2026-07-12 go-zero
    incident: N2P required 17 -> 222). So:

    1. explicit config value wins;
    2. else infer from the milestone's own test_config command text (always
       shipped with the milestone, unlike the repo config);
    3. else, if the baseline clearly contains go_test random subtests
       (`Parent/<rand>` that the go_test normalizer would collapse) yet we did
       NOT resolve go_test, fail loudly instead of scoring wrong.
    """
    explicit = repo_config.get("test_framework")
    if explicit:
        return explicit

    inferred = _infer_framework_from_test_config(workspace_root, milestone_id)

    if inferred != "go_test" and baseline_ids:
        probe = TestIdNormalizer(framework="go_test")
        collapsible = sum(1 for t in baseline_ids if probe.normalize(t) != t)
        if collapsible >= 5:
            raise ValueError(
                f"{milestone_id}: baseline has {collapsible} go_test random-subtest "
                f"IDs but test_framework resolved to {inferred!r} — normalization "
                f"would silently no-op and scores would crater (cf. go-zero "
                f"17->222 incident). Ensure config/<repo>.yaml is reachable from "
                f"workspace-root, or set framework in the milestone test_config."
            )
    return inferred


def _infer_framework_from_test_config(workspace_root: Path, milestone_id: str) -> Optional[str]:
    """Infer framework from the milestone test_config command text. Returns
    None (not a default) when nothing matches, so the caller's fail-loud guard
    can distinguish 'inferred something' from 'could not tell'."""
    config_path = workspace_root / "dockerfiles" / milestone_id / "test_config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path) as f:
            modes = json.load(f)
    except Exception:
        return None
    if not isinstance(modes, list):
        return None
    for mode in modes:
        if isinstance(mode, dict) and mode.get("framework"):
            return mode["framework"]
    joined = "\n".join(
        str(m.get("test_cmd", "")) for m in modes if isinstance(m, dict)
    ).lower()
    # Order matters: match the specific tool before the generic 'test' word.
    if "cargo test" in joined:
        return "cargo"
    if "ginkgo" in joined:
        return "ginkgo"
    if "go test" in joined:
        return "go_test"
    if "mvn " in joined or "mvnw" in joined:
        return "maven"
    if "gradle" in joined:
        return "gradle"
    if "vitest" in joined:
        return "vitest"
    if "jest" in joined:
        return "jest"
    if "pytest" in joined or "python -m pytest" in joined:
        return "pytest"
    return None


def _milestone_requires_docker_socket(workspace_root: Path, milestone_id: str) -> bool:
    """True when the milestone's test_config declares requires_docker_socket
    (testcontainers e2e tests). Same flag the classification runner consumes
    (run_milestone_tests) — the e2e evaluator historically ignored it, which
    is the root cause of the F-2a incident class."""
    config_path = workspace_root / "dockerfiles" / milestone_id / "test_config.json"
    if not config_path.exists():
        return False
    try:
        config = MilestoneTestConfig.from_file(config_path, include_original=False)
        return config.requires_docker_socket_any()
    except Exception:
        return False


def _scan_file_for_infrastructure_failure(
    path: Path, chunk_bytes: int = 8_000_000, overlap: int = 4096
) -> Optional[str]:
    """Chunked full-file scan for infra-failure signatures (F-2a). Reports can
    be tens of MB on one JSON line; the overlap catches boundary-straddling
    matches without loading unbounded files into memory."""
    tail = ""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_bytes)
                if not chunk:
                    return None
                text = tail + chunk.decode("utf-8", errors="replace")
                sig = detect_infrastructure_failure(text)
                if sig:
                    return sig
                tail = text[-overlap:]
    except OSError:
        return None


class InfrastructureFailureError(RuntimeError):
    """The evaluation environment (docker/testcontainers/...) broke the test
    run — the recorded failures are not the agent's (F-2a).

    Raised only AFTER the flagged evaluation_result.json is persisted, so the
    orchestrator's transient-retry loop re-runs the evaluation; exhausted
    retries leave the flagged JSON + evaluation_error.log behind and the
    result stays scoring_untrusted."""


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

    # Fail-loud eval metadata (docs/residue-prune-spec.md, phases 1a/1b)
    base_tag: str = ""  # full tag the eval tree was based on, e.g. milestone-M1-end
    fallback_triggered: bool = False  # END base failed -> graded on START base
    end_compile_error: str = ""  # first fatal context from the failed END-base build
    start_compile_error: str = ""  # first fatal context from the failed START-base build
    build_failure_fail_closed: bool = False
    partial_test_universe: bool = False
    build_failure_diagnostics: List[str] = field(default_factory=list)
    residue_prune_enabled: bool = False
    pruned_files_count: int = 0
    pruned_files: List[str] = field(default_factory=list)
    keep_list_hits: List[str] = field(default_factory=list)
    snapshot_integrity_ok: Optional[bool] = None  # None = check not run
    snapshot_missing_count: int = 0
    residue_prune_skipped_reason: str = ""  # "", ls-tree-failed, snapshot-integrity-failed, safety-abort, tar-unreadable, config-invalid
    manifest_evaluator_base: str = ""
    manifest_evaluator_head: str = ""
    manifest_base_reason: str = ""
    manifest_merged_count: int = 0
    manifest_agent_exact_count: int = 0
    manifest_agent_added_count: int = 0
    manifest_evaluator_missing_count: int = 0
    manifest_conflict_files_count: int = 0
    manifest_conflict_hunks_count: int = 0
    manifest_agent_authoritative_paths: List[str] = field(default_factory=list)
    post_snapshot_script: str = ""
    post_snapshot_script_sha256: str = ""
    post_snapshot_script_applied: bool = False
    gt_test_graft_suffix: str = ""
    gt_test_graft_removed_count: int = 0
    gt_test_graft_restored_count: int = 0
    offline_cache_overlay_image: str = ""
    offline_cache_milestone_image_id: str = ""
    offline_cache_closure_image_id: str = ""
    offline_cache_effective_image_id: str = ""
    repo_config_binding_mode: str = "legacy-unbound"
    repo_config_sha256: str = ""
    runtime_policy_binding_mode: str = "legacy-live"
    runtime_policy_sha256: str = ""
    runtime_policy_mode: str = ""
    snapshot_agent_image_id: str = ""
    snapshot_agent_tag_commit: str = ""
    go_toolchain_expected: str = ""
    go_toolchain_actual: str = ""
    go_toolchain_executable: str = ""
    go_toolchain_goroot: str = ""
    go_module_closure_enabled: bool = False
    go_module_closure_applied: bool = False
    go_module_production_compile_checked: bool = False
    go_module_production_compile_error: str = ""
    go_module_test_graph_contract_error: str = ""
    go_module_test_graph_added_modules: List[str] = field(default_factory=list)
    go_partial_package_filter_applied: bool = False
    go_partial_package_filter_excluded: List[str] = field(default_factory=list)
    go_partial_package_filter_included: int = 0
    go_manifest_projection_complete: bool = False
    go_manifest_projection_removed: List[str] = field(default_factory=list)
    go_test_local_proxy_used: bool = False
    go_module_test_mod_changed: bool = False
    go_module_sum_changed: bool = False
    go_module_manifest_sha256_before: str = ""
    go_module_manifest_sha256_after: str = ""
    go_test_graph_sha256_before: str = ""
    go_test_graph_sha256_after: str = ""
    go_module_closure_error: str = ""
    # F-2a: first matched infrastructure-failure signature ("" = none detected).
    # Set when test output shows the environment (docker/testcontainers/...)
    # broke the run — these failures are not the agent's.
    infrastructure_failure: str = ""
    # Hard validity gate: zero executed tests cannot be graded when the
    # milestone classification contains required tests.
    infra_invalid_reason: str = ""
    # A deterministic build failure is a graded outcome (score 0), not an
    # infrastructure-invalid cell.  Keep the reason explicit in raw JSON so
    # every downstream collector preserves it in the denominator.
    scored_failure_reason: str = ""

    def __post_init__(self) -> None:
        """Classify zero-test results at the result boundary."""
        self.classify_zero_test_result()

    def _has_build_failure_evidence(self) -> bool:
        return bool(
            self.start_compile_error
            or self.end_compile_error
            or self.build_failure_diagnostics
            or self.go_module_production_compile_error
            or self.go_module_test_graph_contract_error
        )

    def classify_zero_test_result(self) -> None:
        """Keep deterministic build failures graded while rejecting unknown runs."""
        has_required_tests = any((
            self.fail_to_pass_required > 0,
            self.none_to_pass_required > 0,
            self.pass_to_pass_required > 0,
        ))
        if self.total_tests == 0 and has_required_tests:
            if self._has_build_failure_evidence() and not self.infrastructure_failure:
                self.scored_failure_reason = BUILD_FAILURE_WITH_ZERO_TESTS
                self.infra_invalid_reason = ""
            else:
                if self.scored_failure_reason == BUILD_FAILURE_WITH_ZERO_TESTS:
                    self.scored_failure_reason = ""
                self.infra_invalid_reason = ZERO_TESTS_WITH_REQUIRED_TESTS
        if self.infra_invalid_reason:
            self.resolved = False
        if self.scored_failure_reason:
            self.resolved = False

    @property
    def scoring_untrusted(self) -> bool:
        """True when this result must never be scored as-is: residue prune was
        requested but did not complete (additive overlay may have resurrected
        the GT solution), or an infrastructure failure poisoned the test run
        (F-2a). Any resolution recompute (orchestrator/run_milestone) MUST AND
        this in so it cannot flip resolved back to True (codex F1)."""
        if self.infrastructure_failure or self.infra_invalid_reason:
            return True
        return self.residue_prune_skipped_reason in FAIL_CLOSED_SKIP_REASONS

    @property
    def resolution_locked_false(self) -> bool:
        """Whether an outer threshold recompute must preserve ``resolved=False``.

        Compatibility mode may score reports from packages that did run, but
        those observations cannot prove a complete solution when the submitted
        production graph does not compile or evaluator-owned tests would need
        to change an existing submitted MVS selection.
        """
        return bool(
            self.scoring_untrusted
            or self.go_module_production_compile_error
            or self.go_module_test_graph_contract_error
        )

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
        result["base_tag"] = self.base_tag
        result["fallback_triggered"] = self.fallback_triggered
        result["end_compile_error"] = self.end_compile_error
        result["start_compile_error"] = self.start_compile_error
        result["build_failure_policy"] = {
            "fail_closed": self.build_failure_fail_closed,
            "partial_test_universe": self.partial_test_universe,
            "diagnostics": self.build_failure_diagnostics,
        }
        result["residue_prune"] = {
            "enabled": self.residue_prune_enabled,
            "pruned_files_count": self.pruned_files_count,
            "pruned_files": self.pruned_files,
            "keep_list_hits": self.keep_list_hits,
            "skipped_reason": self.residue_prune_skipped_reason,
        }
        result["snapshot_integrity"] = {
            "ok": self.snapshot_integrity_ok,
            "missing_count": self.snapshot_missing_count,
        }
        result["manifest_overlay"] = {
            "evaluator_base": self.manifest_evaluator_base,
            "evaluator_head": self.manifest_evaluator_head,
            "base_reason": self.manifest_base_reason,
            "merged_count": self.manifest_merged_count,
            "agent_exact_count": self.manifest_agent_exact_count,
            "agent_added_count": self.manifest_agent_added_count,
            "evaluator_missing_count": self.manifest_evaluator_missing_count,
            "conflict_files_count": self.manifest_conflict_files_count,
            "conflict_hunks_count": self.manifest_conflict_hunks_count,
            "conflict_policy": "evaluator-wins",
            "agent_authoritative_paths": self.manifest_agent_authoritative_paths,
        }
        result["evaluation_environment"] = {
            "post_snapshot_script": self.post_snapshot_script,
            "post_snapshot_script_sha256": self.post_snapshot_script_sha256,
            "post_snapshot_script_applied": self.post_snapshot_script_applied,
            "gt_test_graft_suffix": self.gt_test_graft_suffix,
            "gt_test_graft_removed_count": self.gt_test_graft_removed_count,
            "gt_test_graft_restored_count": self.gt_test_graft_restored_count,
            "offline_cache_overlay_image": self.offline_cache_overlay_image,
            "offline_cache_milestone_image_id": self.offline_cache_milestone_image_id,
            "offline_cache_closure_image_id": self.offline_cache_closure_image_id,
            "offline_cache_effective_image_id": self.offline_cache_effective_image_id,
            "repo_config_binding_mode": self.repo_config_binding_mode,
            "repo_config_sha256": self.repo_config_sha256,
            "runtime_policy_binding_mode": self.runtime_policy_binding_mode,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "runtime_policy_mode": self.runtime_policy_mode,
            "snapshot_agent_image_id": self.snapshot_agent_image_id,
            "snapshot_agent_tag_commit": self.snapshot_agent_tag_commit,
            "go_toolchain_expected": self.go_toolchain_expected,
            "go_toolchain_actual": self.go_toolchain_actual,
            "go_toolchain_executable": self.go_toolchain_executable,
            "go_toolchain_goroot": self.go_toolchain_goroot,
            "go_module_closure": {
                "enabled": self.go_module_closure_enabled,
                "applied": self.go_module_closure_applied,
                "production_compile_checked": self.go_module_production_compile_checked,
                "production_compile_error": self.go_module_production_compile_error,
                "test_graph_contract_error": self.go_module_test_graph_contract_error,
                "test_graph_added_modules": self.go_module_test_graph_added_modules,
                "partial_package_filter_applied": self.go_partial_package_filter_applied,
                "partial_package_filter_excluded": self.go_partial_package_filter_excluded,
                "partial_package_filter_included": self.go_partial_package_filter_included,
                "exact_manifest_projection": self.go_manifest_projection_complete,
                "projected_absent_removed": self.go_manifest_projection_removed,
                "local_cache_proxy_used": self.go_test_local_proxy_used,
                "test_overlay_changed": self.go_module_test_mod_changed,
                "sum_changed": self.go_module_sum_changed,
                "manifest_sha256_before": self.go_module_manifest_sha256_before,
                "manifest_sha256_after": self.go_module_manifest_sha256_after,
                "test_graph_sha256_before": self.go_test_graph_sha256_before,
                "test_graph_sha256_after": self.go_test_graph_sha256_after,
                "error": self.go_module_closure_error,
            },
        }
        result["infrastructure_failure"] = self.infrastructure_failure
        result["scored_failure_reason"] = self.scored_failure_reason
        result["infra_invalid"] = bool(self.infra_invalid_reason)
        result["infra_invalid_reason"] = self.infra_invalid_reason
        if self.infra_invalid_reason:
            result["eval_status"] = "infra-invalid"
        elif self.scored_failure_reason:
            result["eval_status"] = "failed"
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
            (
                f"Milestone Status: 🚫 INFRA-INVALID ({self.infra_invalid_reason})"
                if self.infra_invalid_reason
                else (
                    f"Milestone Status: ❌ NOT RESOLVED ({self.scored_failure_reason})"
                    if self.scored_failure_reason
                    else f"Milestone Status: {'✅ RESOLVED' if self.resolved else '❌ NOT RESOLVED'}"
                )
            ),
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

    def sync_pass_to_pass_status() -> None:
        """Keep the detailed P2P counters aligned with test_summary."""
        p2p_status = tests_status.get("PASS_TO_PASS")
        if not isinstance(p2p_status, dict):
            return
        if "success_count" in p2p_status:
            p2p_status["success_count"] = test_summary.get(
                "pass_to_pass_achieved", 0
            )
        if "missing" in p2p_status:
            p2p_status["missing"] = test_summary.get("pass_to_pass_missing", 0)

    # A zero-test result represents an evaluation/setup failure.  Filtering
    # must never turn the empty equalities (0 == 0) into a resolved result, nor
    # subtract the static invalid-test counts from zero required tests.
    if test_summary.get("total", 0) == 0:
        for key in (
            "fail_to_pass_required",
            "fail_to_pass_achieved",
            "none_to_pass_required",
            "none_to_pass_achieved",
            "pass_to_pass_required",
            "pass_to_pass_achieved",
            "pass_to_pass_failed",
            "pass_to_pass_missing",
        ):
            if isinstance(test_summary.get(key), (int, float)):
                test_summary[key] = max(0, test_summary[key])

        sync_pass_to_pass_status()
        result["resolved"] = False
        result["filtered"] = True
        result["filter_stats"] = {
            "fail_to_pass_filtered": 0,
            "none_to_pass_filtered": 0,
            "pass_to_pass_filtered": 0,
            "pass_to_pass_missing_filtered": 0,
            "invalid_f2p_count": len(invalid_f2p),
            "invalid_n2p_count": len(invalid_n2p),
            "invalid_p2p_count": len(invalid_p2p),
        }
        return result

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
        test_summary["fail_to_pass_required"] = max(
            0, test_summary["fail_to_pass_required"] - f2p_total_filtered
        )
    if "fail_to_pass_achieved" in test_summary:
        test_summary["fail_to_pass_achieved"] = len(tests_status.get("FAIL_TO_PASS", {}).get("success", []))

    if "none_to_pass_required" in test_summary:
        test_summary["none_to_pass_required"] = max(
            0, test_summary["none_to_pass_required"] - n2p_total_filtered
        )
    if "none_to_pass_achieved" in test_summary:
        test_summary["none_to_pass_achieved"] = len(tests_status.get("NONE_TO_PASS", {}).get("success", []))

    # For pass_to_pass: reduce required, missing, and failure by invalid test counts
    if "pass_to_pass_required" in test_summary:
        original_p2p_required = max(0, test_summary["pass_to_pass_required"])
        invalid_p2p_removed = min(len(invalid_p2p), original_p2p_required)
        test_summary["pass_to_pass_required"] = original_p2p_required - invalid_p2p_removed
        p2p_filtered_missing = min(p2p_filtered_missing, invalid_p2p_removed)
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

    # Keep both serialized views consistent.  Downstream consumers inspect
    # either tests_status or test_summary, so updating only one creates
    # replay-dependent results.
    sync_pass_to_pass_status()

    # Recalculate resolved status
    f2p_required = test_summary.get("fail_to_pass_required", 0)
    f2p_achieved = test_summary.get("fail_to_pass_achieved", 0)
    n2p_required = test_summary.get("none_to_pass_required", 0)
    n2p_achieved = test_summary.get("none_to_pass_achieved", 0)
    p2p_failure_count = len(tests_status.get("PASS_TO_PASS", {}).get("failure", []))
    p2p_missing = test_summary.get("pass_to_pass_missing", 0)

    result["resolved"] = (
        test_summary.get("total", 0) > 0
        and f2p_achieved == f2p_required
        and p2p_failure_count == 0
        and p2p_missing == 0
        and n2p_achieved == n2p_required
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
        build_failure_fail_closed: bool = False,
        repo_config_path: Optional[Path] = None,
        repo_config_sha256: Optional[str] = None,
        runtime_policy_path: Optional[Path] = None,
        runtime_policy_sha256: Optional[str] = None,
        runtime_policy_mode: Optional[str] = None,
    ):
        self.workspace_root = workspace_root
        self.milestone_id = milestone_id
        self.patch_file = patch_file
        self.baseline_classification = baseline_classification
        self.filter_src_only = filter_src_only
        self.agent_attempt = agent_attempt
        self.keep_container = keep_container
        if not isinstance(build_failure_fail_closed, bool):
            raise ValueError("build_failure_fail_closed must be a boolean")
        self.build_failure_fail_closed = build_failure_fail_closed

        # Extract repo name from workspace_root path
        # SWE-Milestone path structure: .../SWE-Milestone-data/navidrome_navidrome_v0.57.0_v0.58.0
        # AgentBench path structure: .../harness_workspace/repo_name/test_name
        # Detect which structure by checking if workspace_root itself has metadata.json
        if (workspace_root / "metadata.json").exists():
            # SWE-Milestone: workspace_root IS the repo directory
            self.repo_name = workspace_root.name
            self.test_name = None
        else:
            # AgentBench: workspace_root is repo/test_name
            self.repo_name = workspace_root.parent.name
            self.test_name = workspace_root.name

        # New trials bind the exact repo YAML once.  Older/direct invocations
        # remain explicitly legacy-unbound for compatibility and are labelled
        # as such in the result; a half-specified binding never falls back.
        if (repo_config_path is None) != (repo_config_sha256 is None):
            raise RepoConfigBindingError(
                "repo_config_path and repo_config_sha256 must be provided together"
            )
        if repo_config_path is not None and repo_config_sha256 is not None:
            self.repo_config = load_bound_repo_config(
                self.repo_name,
                repo_config_path,
                repo_config_sha256,
            )
            self.repo_config_binding_mode = "trial-pinned"
            self.repo_config_sha256 = repo_config_sha256
        else:
            self.repo_config = load_repo_config(
                self.repo_name, workspace_root=workspace_root
            )
            self.repo_config_binding_mode = "legacy-unbound"
            self.repo_config_sha256 = ""
            logger.warning(
                "Repository config is legacy-unbound; use --repo-config plus "
                "--repo-config-sha256 for reproducible/promotion-grade evaluation"
            )
        self._project_root = Path(__file__).resolve().parents[2]
        runtime_binding_values = (
            runtime_policy_path,
            runtime_policy_sha256,
            runtime_policy_mode,
        )
        if any(value is not None for value in runtime_binding_values) and not all(
            value is not None for value in runtime_binding_values
        ):
            raise RuntimePolicyBindingError(
                "runtime_policy_path, runtime_policy_sha256, and "
                "runtime_policy_mode must be provided together"
            )
        if all(value is not None for value in runtime_binding_values):
            runtime_binding = load_bound_runtime_policy(
                self.repo_name,
                runtime_policy_path,
                runtime_policy_sha256,
                runtime_policy_mode,
            )
            self.quarantine_config = runtime_binding.effective_policy
            self.runtime_policy_binding_mode = "trial-pinned"
            self.runtime_policy_sha256 = runtime_binding.sha256
            self.runtime_policy_mode = runtime_binding.mode
        else:
            self.quarantine_config = load_quarantine_config(
                self.repo_name,
                self._project_root,
            )
            self.runtime_policy_binding_mode = "legacy-live"
            self.runtime_policy_sha256 = ""
            self.runtime_policy_mode = ""
            logger.warning(
                "Runtime/quarantine policy is legacy-live; pinned promotion "
                "requires an explicit frozen policy path, SHA256, and mode"
            )

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
                # Residue prune (docs/residue-prune-spec.md): metadata carries the
                # per-range facts — keep-list, optional extension pin, optional
                # enablement override; the mechanism lives here. DEFAULT-OFF:
                # preserve the historical additive overlay unless a dataset
                # explicitly opts into deletion inference.
                _has_partition = bool(metadata.get("repo_src_dirs") and metadata.get("test_dirs"))
                prune_requested, _enable_reason = resolve_prune_enablement(
                    metadata.get("residue_prune"), _has_partition
                )
                if _enable_reason == "legacy-no-partition":
                    print(
                        "ℹ️  residue prune: metadata lacks repo_src_dirs/test_dirs — "
                        "additive overlay retained (legacy dataset, pruning undefined)"
                    )
                self.prune_keep_list = normalize_keep_list(metadata.get("prune_keep_list", []))
                # F3/F6: per-range language scope. Metadata may pin prune_extensions
                # (phase 1 navidrome -> [".go"] so its ui/src TS is never pruned);
                # absent -> the multi-language default; empty list -> prune nothing.
                pe = normalize_extensions(metadata.get("prune_extensions"))
                self.prune_extensions = DEFAULT_PRUNE_EXTENSIONS if pe is None else pe
                if metadata.get("repo_src_dirs") and metadata.get("test_dirs"):
                    self._prune_filter: Optional[SrcFileFilter] = SrcFileFilter(
                        src_dirs=metadata["repo_src_dirs"],
                        test_dirs=metadata["test_dirs"],
                        exclude_patterns=metadata.get("exclude_patterns", []),
                        generated_patterns=metadata.get("generated_patterns", []),
                        modifiable_test_patterns=metadata.get("modifiable_test_patterns", []),
                    )
                else:
                    self._prune_filter = None
        else:
            self.test_dir = "test/"
            self.test_workdir = "/testbed"
            self.test_timeout = 50
            self.docker_cpus = 16
            prune_requested = False
            self.prune_keep_list = frozenset()
            self.prune_extensions = DEFAULT_PRUNE_EXTENSIONS
            self._prune_filter = None

        # A/B override for re-evaluation experiments: SWE_MILESTONE_RESIDUE_PRUNE=1/0.
        # M4: unparseable values raise rather than silently disabling.
        env_prune = os.environ.get("SWE_MILESTONE_RESIDUE_PRUNE")
        if env_prune is not None:
            val = env_prune.strip().lower()
            if val in ("1", "true", "yes", "on"):
                prune_requested = True
            elif val in ("0", "false", "no", "off"):
                prune_requested = False
            else:
                raise ValueError(f"SWE_MILESTONE_RESIDUE_PRUNE: unparseable value {env_prune!r}")

        # F3 (codex): pruning must NOT silently downgrade to disabled when it was
        # requested but its config is unusable — that fails OPEN (partial snapshot
        # graded against the additive GT tree). Record config-invalid so the
        # cell is scoring-untrusted (fail-closed).
        self.residue_prune_enabled = prune_requested and self._prune_filter is not None
        self._prune_config_invalid = prune_requested and self._prune_filter is None
        if self._prune_config_invalid:
            print("⚠️  residue_prune requested but metadata lacks repo_src_dirs/test_dirs — cell marked scoring-untrusted")

        # Fail-loud eval metadata threaded into EvaluationResult (spec phase 1a)
        self._eval_meta: Dict[str, Any] = {
            "base_tag": "",
            "fallback_triggered": False,
            "end_compile_error": "",
            "start_compile_error": "",
            "build_failure_fail_closed": self.build_failure_fail_closed,
            "partial_test_universe": False,
            "build_failure_diagnostics": [],
            "residue_prune_enabled": self.residue_prune_enabled,
            "pruned_files_count": 0,
            "pruned_files": [],
            "keep_list_hits": [],
            "snapshot_integrity_ok": None,
            "snapshot_missing_count": 0,
            "residue_prune_skipped_reason": "config-invalid" if self._prune_config_invalid else "",
            "manifest_upserts_count": 0,
            "manifest_deletes_count": 0,
            "manifest_merged_count": 0,
            "manifest_agent_exact_count": 0,
            "manifest_agent_added_count": 0,
            "manifest_evaluator_missing_count": 0,
            "manifest_conflict_files_count": 0,
            "manifest_conflict_hunks_count": 0,
            "manifest_agent_authoritative_paths": [],
            "manifest_evaluator_base": "",
            "manifest_evaluator_head": "",
            "manifest_base_reason": "",
            "post_snapshot_script": "",
            "post_snapshot_script_sha256": "",
            "post_snapshot_script_applied": False,
            "gt_test_graft_suffix": "",
            "gt_test_graft_removed_count": 0,
            "gt_test_graft_restored_count": 0,
            "offline_cache_overlay_image": "",
            "offline_cache_milestone_image_id": "",
            "offline_cache_closure_image_id": "",
            "offline_cache_effective_image_id": "",
            "repo_config_binding_mode": self.repo_config_binding_mode,
            "repo_config_sha256": self.repo_config_sha256,
            "runtime_policy_binding_mode": self.runtime_policy_binding_mode,
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "runtime_policy_mode": self.runtime_policy_mode,
            "snapshot_agent_image_id": "",
            "snapshot_agent_tag_commit": "",
            "go_toolchain_expected": "",
            "go_toolchain_actual": "",
            "go_toolchain_executable": "",
            "go_toolchain_goroot": "",
            "go_module_closure_enabled": False,
            "go_module_closure_applied": False,
            "go_module_production_compile_checked": False,
            "go_module_production_compile_error": "",
            "go_module_test_graph_contract_error": "",
            "go_module_test_graph_added_modules": [],
            "go_partial_package_filter_applied": False,
            "go_partial_package_filter_excluded": [],
            "go_partial_package_filter_included": 0,
            "go_manifest_projection_complete": False,
            "go_manifest_projection_removed": [],
            "go_test_local_proxy_used": False,
            "go_module_production_mod_changed": False,
            "go_module_test_mod_changed": False,
            "go_module_sum_changed": False,
            "go_module_manifest_sha256_before": "",
            "go_module_manifest_sha256_after": "",
            "go_test_graph_sha256_before": "",
            "go_test_graph_sha256_after": "",
            "go_module_closure_error": "",
        }
        self._snapshot_metadata: Optional[Dict[str, Any]] = None
        self._manifest_overlay: Optional[ManifestOverlay] = None
        self._go_manifest_inventory: Optional[FrozenSet[str]] = None
        self._go_exec_env: Dict[str, str] = {}
        self._go_test_import_owners: Dict[str, Set[str]] = {}
        self._baseline_required_test_counts = {
            "fail_to_pass": 0,
            "none_to_pass": 0,
            "pass_to_pass": 0,
        }
        # Ensure test_dir ends with / for consistent path handling
        if not self.test_dir.endswith("/"):
            self.test_dir = self.test_dir + "/"
        print(f"📋 Test directory: {self.test_dir}")
        print(f"📋 Test workdir: {self.test_workdir}")
        print(f"📋 Test timeout: {self.test_timeout}s" if self.test_timeout else "📋 Test timeout: disabled")
        print(
            "📋 Build failure policy: "
            + ("fail-closed" if self.build_failure_fail_closed else "score completed package/module reports")
        )
        print(f"📋 Docker CPUs: {self.docker_cpus}")

        # Docker image name (single naming authority: image_version.py):
        #   SWE-Milestone: swe-milestone/{repo_full}__{milestone_id}:{tag}
        #               e.g. swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__milestone_006:v1.0
        #   AgentBench (legacy, images only exist under the OLD local scheme —
        #   deliberately NOT migrated, see docs/versioning.md):
        #               {repo}/{test_name}/{milestone_id}:{tag}
        # Note: Docker image names must be lowercase (OCI spec requirement)
        # Benchmark data version is pinned via SWE_MILESTONE_IMAGE_TAG (default in
        # image_version.py); resolve_image falls back to :latest WITH a warning
        # when the default pin is absent locally (never when set explicitly).
        if self.test_name:
            image_base = f"{self.repo_name.lower()}/{self.test_name.lower()}/{milestone_id.lower()}"
        else:
            image_base = local_ref(self.repo_name, milestone_id)
        self.docker_image = resolve_image(image_base)

        # F-2a: testcontainers milestones need the host Docker socket inside
        # the eval container — consume the same test_config flag as the
        # classification runner instead of relying on ambient host luck.
        self.needs_docker_socket = _milestone_requires_docker_socket(self.workspace_root, milestone_id)
        if self.needs_docker_socket:
            print("🐳 requires_docker_socket: mounting host Docker socket (testcontainers parity)")

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

    def _verify_evaluator_go_toolchain(self) -> None:
        """Fail closed unless the running evaluator has the closure's exact Go.

        The agent runs in ``base-offline`` while tests run in a milestone-derived
        image. Merely rendering a COPY instruction is not proof of parity: an
        omitted replacement policy or stale derived tag can otherwise compile
        the same submission under a different Go release. Probe the live
        container and persist both sides of the assertion in the result.
        """
        expected = _configured_go_toolchain_version(self.quarantine_config)
        self._eval_meta["go_toolchain_expected"] = expected
        if not expected:
            return
        probe = subprocess.run(
            [
                "docker", "exec", self.container_name, "sh", "-c",
                "set -e; printf 'executable=%s\\n' \"$(command -v go)\"; "
                "go version; printf 'goroot=%s\\n' \"$(go env GOROOT)\"; "
                "printf 'golang_version=%s\\n' \"${GOLANG_VERSION:-}\"",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = "\n".join(part for part in (probe.stdout, probe.stderr) if part).strip()
        actual = _parse_go_version(output)
        executable_match = re.search(r"^executable=(.+)$", output, re.MULTILINE)
        goroot_match = re.search(r"^goroot=(.+)$", output, re.MULTILINE)
        executable = executable_match.group(1).strip() if executable_match else ""
        goroot = goroot_match.group(1).strip() if goroot_match else ""
        self._eval_meta["go_toolchain_actual"] = actual
        self._eval_meta["go_toolchain_executable"] = executable
        self._eval_meta["go_toolchain_goroot"] = goroot
        if probe.returncode != 0 or not actual or not executable or not goroot:
            raise RuntimeError(
                "Cannot verify evaluator Go toolchain "
                f"(expected go{expected}): {output or 'go version produced no output'}"
            )
        if executable != "/usr/local/go/bin/go" or goroot != "/usr/local/go":
            raise RuntimeError(
                "Evaluator Go toolchain path mismatch: "
                f"command -v go={executable!r}, GOROOT={goroot!r}"
            )
        if actual != expected:
            raise RuntimeError(
                "Evaluator/agent Go toolchain mismatch: "
                f"expected go{expected} from base-offline, found go{actual}"
            )
        if f"golang_version={expected}" not in output.splitlines():
            raise RuntimeError(
                "Evaluator Go metadata mismatch: "
                f"expected GOLANG_VERSION={expected}, probe was:\n{output}"
            )

    def _verify_evaluator_cache_policy(self) -> None:
        """Re-run answer-exclusion and canonical-proxy checks in the live eval."""
        if not (
            isinstance(self.quarantine_config, dict)
            and self.quarantine_config.get("go_offline")
        ):
            return
        proxy_path = self._go_local_cache_proxy().removeprefix("file://")
        probe = subprocess.run(
            [
                "docker", "exec", self.container_name, "sh", "-c",
                "test -d \"$1\" && test -r \"$1\" && "
                "test -n \"$(find \"$1\" -type f -print -quit)\"",
                "evoclaw-cache-probe", proxy_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout or "missing/empty proxy").strip()
            raise RuntimeError(f"Evaluator canonical Go proxy is unusable: {detail}")
        raw_globs = self.quarantine_config.get("cache_forbid_globs") or []
        if not isinstance(raw_globs, list) or not all(
            isinstance(pattern, str) and pattern.startswith("/")
            for pattern in raw_globs
        ):
            raise RuntimeError("cache_forbid_globs must be a list of absolute patterns")
        for pattern in raw_globs:
            audit = subprocess.run(
                [
                    "docker", "exec", self.container_name, "bash", "-c",
                    "compgen -G \"$1\" | head -5", "evoclaw-cache-audit", pattern,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if audit.stdout.strip():
                raise RuntimeError(
                    "Evaluator cache contains forbidden answer material matching "
                    f"{pattern}:\n{audit.stdout.strip()}"
                )

    def start_container(self) -> None:
        """Start Docker container (image already at baseline commit)."""

        snapshot_agent_image_id = ""
        if self._go_module_closure_requested() and self.patch_file.suffix == ".tar":
            metadata, _ = self._load_and_validate_snapshot_metadata()
            if self._go_module_closure_enabled():
                snapshot_agent_image_id = metadata["agent_base_image_id"]

        effective_image, milestone_image_id, closure_image_id, effective_image_id = (
            ensure_offline_evaluation_image(
                repo_name=self.repo_name,
                milestone_id=self.milestone_id,
                milestone_image=self.docker_image,
                quarantine_config=self.quarantine_config,
                expected_closure_image_id=snapshot_agent_image_id,
            )
        )
        if closure_image_id:
            print(
                "📦 Evaluator offline cache parity: "
                f"milestone={milestone_image_id[:12]}…, "
                f"closure={closure_image_id[:12]}…"
            )
            self._eval_meta["offline_cache_overlay_image"] = effective_image
        self._eval_meta["offline_cache_milestone_image_id"] = milestone_image_id
        self._eval_meta["offline_cache_closure_image_id"] = closure_image_id
        self._eval_meta["offline_cache_effective_image_id"] = effective_image_id
        self.docker_image = effective_image

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
            "--pull=never",  # hermetic eval: image must already be local (docs/versioning.md)
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
        ]
        go_offline = bool(
            isinstance(self.quarantine_config, dict)
            and self.quarantine_config.get("go_offline")
        )
        if go_offline:
            # Test/evaluation containers have no model endpoint to preserve, so
            # unlike agent quarantine they use an internal-only bridge. It
            # preserves a private eth0 for network-sensitive tests while Docker
            # supplies no external route. The Go env remains a second layer.
            if not self.needs_docker_socket:
                cmd += ["--network", ensure_internal_evaluation_network()]
            cmd += [
                "-e", "GOPROXY=file:///go/pkg/mod/cache/download",
                "-e", "GONOPROXY=none",
                "-e", "GOSUMDB=off",
                "-e", "GOTOOLCHAIN=local",
                "-e", "GOMODCACHE=/tmp/evoclaw-gomodcache",
                "-e", "GOCACHE=/tmp/evoclaw-go-build",
                "-e", "GOROOT=/usr/local/go",
                "-e", f"PATH={GO_EVALUATOR_PATH}",
            ]
            expected_go = _configured_go_toolchain_version(self.quarantine_config)
            if expected_go:
                cmd += ["-e", f"GOLANG_VERSION={expected_go}"]
        if self.needs_docker_socket:
            # Parity with classification-time runs (run_milestone_tests):
            # socket so testcontainers can spawn containers, host network so
            # tests reach them, Ryuk disabled to avoid reaper connect issues.
            # Each run also gets a unique webServer port (consumed by the
            # milestone's apply_patches.sh hook) so host-network evals can run
            # concurrently and survive foreign services squatting on 8080.
            eval_port = _find_free_port()
            print(f"🐳 SWE_MILESTONE_EVAL_PORT={eval_port}")
            cmd += [
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "--network",
                "host",
                "-e",
                "TESTCONTAINERS_RYUK_DISABLED=true",
                "-e",
                f"SWE_MILESTONE_EVAL_PORT={eval_port}",
            ]
        cmd += [
            self.docker_image,
            "tail",
            "-f",
            "/dev/null",
        ]

        subprocess.run(cmd, capture_output=True, text=True, check=True)
        try:
            self._verify_evaluator_go_toolchain()
            self._verify_evaluator_cache_policy()
        except Exception:
            # A parity failure makes this container unusable and must never
            # leave a misleading live evaluator behind.
            subprocess.run(
                ["docker", "rm", "-f", self.container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            raise
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
        # Some legacy milestone images replace /usr/bin/git with a checkout
        # simulator. Evaluator tree construction must use the real binary or
        # the reported tag and actual HEAD diverge silently.
        git_prefix = 'git_bin=git; if test -x /usr/bin/git.real; then git_bin=/usr/bin/git.real; fi; '
        if clean:
            checkout_script = (
                git_prefix
                + f"cd /testbed && "
                + f'"$git_bin" checkout {tag_name} --force && '
                + '"$git_bin" clean -fd'  # -f: force, -d: include directories
            )
        else:
            checkout_script = git_prefix + f'cd /testbed && "$git_bin" checkout {tag_name} --force'

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
        # Go closure mode already compiled production packages against the
        # exact submitted graph before constructing the evaluator-private test
        # modfile. Reusing the generic command here would inject that private
        # graph and could hide an agent dependency/API failure.
        eval_meta = getattr(self, "_eval_meta", {})
        if eval_meta.get("go_module_production_compile_checked"):
            print("🔨 Checking compilation with exact submitted Go module graph...")
            submitted_error = eval_meta.get(
                "go_module_production_compile_error", ""
            )
            if submitted_error:
                return False, submitted_error
            print("✅ Submitted Go production compile gate passed")
            return True, ""

        # Get build_command from repo config
        build_command = self.repo_config.get("build_command")
        if not build_command:
            # No build command configured, skip compilation check
            return True, ""

        print(f"🔨 Checking compilation with: {build_command[:60]}...")
        env_args = [
            item
            for key, value in getattr(self, "_go_exec_env", {}).items()
            for item in ("-e", f"{key}={value}")
        ]
        compile_cmd = [
            "docker", "exec", *env_args, self.container_name,
            "bash", "-c", f"{build_command} 2>&1",
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

            # Anchor diagnostics at a framework-aware fatal signature. Looking
            # for the substring "error" mistakes Go -v package progress such
            # as "errors"/"oserror" for the failure and hides the real cause.
            fatal = extract_first_fatal_error(output)
            if fatal:
                error_summary = fatal
            else:
                error_summary = "\n".join(output.splitlines()[-15:])[-1500:]
            return False, f"Compilation failed (exit {result.returncode}):\n{error_summary}"

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

    def _git_ls_tree(self, tag_name: str) -> Optional[Set[str]]:
        """List every file of a tag's tree inside the container (repo-relative).

        Runs as the eval container's default user (root) — the same way
        _checkout_to_tag / git clean already run on this tree — NOT the
        capture-side fakeroot user (the eval container is a fresh root
        container from the milestone image; forcing fakeroot hits dubious
        ownership and returns None, silently disabling pruning). quotePath=false
        + -z keep non-ASCII paths byte-accurate against the tar member names
        (review L1); safe.directory guards against any ownership quirk.
        """
        script = r"""
git_bin=git
if test -x /usr/bin/git.real; then git_bin=/usr/bin/git.real; fi
"$git_bin" -C /testbed -c core.quotePath=false -c safe.directory=/testbed \
    ls-tree -r -z --name-only "$1"
"""
        cmd = [
            "docker", "exec", self.container_name, "bash", "-c", script,
            "evoclaw-ls-tree", tag_name,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        return {p for p in result.stdout.split("\0") if p}

    def _load_and_validate_snapshot_metadata(self) -> Tuple[Dict[str, Any], ManifestOverlay]:
        """Load snapshot metadata and fail closed on incomplete provenance."""
        if self._snapshot_metadata is not None and self._manifest_overlay is not None:
            return self._snapshot_metadata, self._manifest_overlay

        sidecar = self.patch_file.parent / (self.patch_file.stem + ".integrity.json")
        if not sidecar.exists():
            raise RuntimeError(
                f"Snapshot metadata sidecar is missing: {sidecar}; recapture required"
            )
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Snapshot metadata sidecar is unreadable: {sidecar}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Snapshot metadata sidecar must contain a JSON object")
        if data.get("schema_version") != SNAPSHOT_METADATA_SCHEMA_VERSION:
            raise RuntimeError(
                "Unsupported snapshot metadata schema_version: "
                f"{data.get('schema_version')!r}; recapture required"
            )
        if data.get("ok") is not True:
            raise RuntimeError("Snapshot metadata records a failed capture integrity gate")

        repo_binding_declared = "repo_config_binding" in data
        raw_repo_binding = data.get("repo_config_binding")
        if (
            self.repo_config_binding_mode == "trial-pinned"
            and (not repo_binding_declared or raw_repo_binding is None)
        ):
            raise RuntimeError(
                "Snapshot repo config binding is missing; recapture with the "
                "trial-pinned repository config"
            )
        if repo_binding_declared and raw_repo_binding is None:
            raise RuntimeError("Invalid snapshot repo config binding: expected an object")
        if raw_repo_binding is not None:
            try:
                repo_identity = RepoConfigIdentity.from_mapping(raw_repo_binding)
            except RepoConfigBindingError as exc:
                raise RuntimeError(
                    f"Invalid snapshot repo config binding: {exc}"
                ) from exc
            if repo_identity.repo_name != self.repo_name:
                raise RuntimeError(
                    "Snapshot repo config binding repo mismatch: "
                    f"expected {self.repo_name!r}, got {repo_identity.repo_name!r}"
                )
            if self.repo_config_binding_mode != "trial-pinned":
                raise RuntimeError(
                    "Snapshot requires a trial-pinned repo config; pass the "
                    "frozen config path and SHA256"
                )
            if repo_identity.sha256 != self.repo_config_sha256:
                raise RuntimeError(
                    "Snapshot repo config binding digest mismatch: "
                    f"snapshot={repo_identity.sha256}, "
                    f"evaluator={self.repo_config_sha256}"
                )

        runtime_binding_declared = "runtime_policy_binding" in data
        raw_runtime_binding = data.get("runtime_policy_binding")
        if (
            self.runtime_policy_binding_mode == "trial-pinned"
            and (not runtime_binding_declared or raw_runtime_binding is None)
        ):
            raise RuntimeError(
                "Snapshot runtime policy binding is missing; recapture with "
                "the trial-pinned runtime policy"
            )
        if runtime_binding_declared and raw_runtime_binding is None:
            raise RuntimeError(
                "Invalid snapshot runtime policy binding: expected an object"
            )
        if raw_runtime_binding is not None:
            try:
                runtime_identity = RuntimePolicyIdentity.from_mapping(
                    raw_runtime_binding
                )
            except RuntimePolicyBindingError as exc:
                raise RuntimeError(
                    f"Invalid snapshot runtime policy binding: {exc}"
                ) from exc
            if runtime_identity.repo_name != self.repo_name:
                raise RuntimeError(
                    "Snapshot runtime policy repo mismatch: "
                    f"expected {self.repo_name!r}, got "
                    f"{runtime_identity.repo_name!r}"
                )
            if self.runtime_policy_binding_mode != "trial-pinned":
                raise RuntimeError(
                    "Snapshot requires a trial-pinned runtime policy; pass the "
                    "frozen policy path, SHA256, and mode"
                )
            if runtime_identity.sha256 != self.runtime_policy_sha256:
                raise RuntimeError(
                    "Snapshot runtime policy digest mismatch: "
                    f"snapshot={runtime_identity.sha256}, "
                    f"evaluator={self.runtime_policy_sha256}"
                )
            if runtime_identity.mode != self.runtime_policy_mode:
                raise RuntimeError(
                    "Snapshot runtime policy mode mismatch: "
                    f"snapshot={runtime_identity.mode}, "
                    f"evaluator={self.runtime_policy_mode}"
                )

        tag = data.get("tag")
        allowed_tags = {
            f"agent-impl-{self.milestone_id}",
            f"agent-workdir-{self.milestone_id}",
        }
        if not isinstance(tag, str) or tag not in allowed_tags:
            raise RuntimeError(
                f"Snapshot metadata tag {tag!r} does not match milestone {self.milestone_id}"
            )
        expected_hash = data.get("snapshot_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise RuntimeError("Snapshot metadata snapshot_sha256 is missing or invalid")
        actual_hash = snapshot_sha256(self.patch_file)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Snapshot metadata hash mismatch: expected {expected_hash}, got {actual_hash}"
            )

        # Go replay uses the exact base-offline bytes handed to the agent.
        raw_go_projection = data.get("go_manifest_projection")
        go_requested = self._go_module_closure_requested()
        agent_image_id = data.get("agent_base_image_id")
        agent_tag_commit = data.get("agent_tag_commit")
        capture_filter = data.get("capture_filter")
        missing_exact_go: List[str] = []
        if go_requested:
            if agent_image_id is None:
                missing_exact_go.append("agent_base_image_id")
            elif not isinstance(agent_image_id, str) or not re.fullmatch(
                r"[0-9a-f]{64}", agent_image_id
            ):
                raise RuntimeError("Snapshot metadata has an invalid agent_base_image_id")
            if agent_tag_commit is None:
                missing_exact_go.append("agent_tag_commit")
            elif not isinstance(agent_tag_commit, str) or not re.fullmatch(
                r"[0-9a-f]{40,64}", agent_tag_commit
            ):
                raise RuntimeError("Snapshot metadata has an invalid agent_tag_commit")
            if raw_go_projection is None:
                missing_exact_go.append("go_manifest_projection")
            if not isinstance(capture_filter, dict):
                missing_exact_go.append("capture_filter")
            if missing_exact_go:
                raise RuntimeError(
                    "Snapshot lacks exact Go replay provenance: "
                    + ", ".join(missing_exact_go)
                    + "; recapture required"
                )
            self._eval_meta["snapshot_agent_image_id"] = agent_image_id
            self._eval_meta["snapshot_agent_tag_commit"] = agent_tag_commit

        try:
            overlay = ManifestOverlay.from_metadata(data.get("manifest_overlay"))
        except ValueError as exc:
            raise RuntimeError(f"Invalid manifest overlay metadata: {exc}") from exc
        if not re.fullmatch(r"[0-9a-f]{40,64}", overlay.baseline_commit):
            raise RuntimeError(
                f"Invalid manifest overlay baseline commit: {overlay.baseline_commit!r}"
            )

        if raw_go_projection is None:
            go_inventory: FrozenSet[str] = frozenset()
        else:
            if not isinstance(raw_go_projection, dict):
                raise RuntimeError("go_manifest_projection must be an object")
            if (
                raw_go_projection.get("schema_version")
                != GO_MANIFEST_PROJECTION_SCHEMA_VERSION
            ):
                raise RuntimeError(
                    "Unsupported go_manifest_projection schema_version: "
                    f"{raw_go_projection.get('schema_version')!r}"
                )
            raw_present = raw_go_projection.get("present")
            if not isinstance(raw_present, list) or not all(
                isinstance(path, str) for path in raw_present
            ):
                raise RuntimeError("go_manifest_projection.present must be a list of strings")
            try:
                normalized_present = [normalize_snapshot_path(path) for path in raw_present]
            except ValueError as exc:
                raise RuntimeError(f"Invalid Go manifest projection path: {exc}") from exc
            if len(set(normalized_present)) != len(normalized_present):
                raise RuntimeError("go_manifest_projection.present contains duplicate paths")
            if not all(is_go_build_manifest(path) for path in normalized_present):
                raise RuntimeError(
                    "go_manifest_projection.present contains a non-Go manifest path"
                )
            go_inventory = frozenset(normalized_present)
            go_upserts = frozenset(
                path for path in overlay.upserts if is_go_build_manifest(path)
            )
            if go_inventory != go_upserts:
                raise RuntimeError(
                    "Go manifest projection does not match snapshot upserts "
                    f"(projection={sorted(go_inventory)}, upserts={sorted(go_upserts)})"
                )

        try:
            with tarfile.open(self.patch_file) as archive:
                regular_files = {
                    member.name.removeprefix("./").rstrip("/")
                    for member in archive.getmembers()
                    if member.isfile()
                }
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(f"Snapshot tar is unreadable: {self.patch_file}: {exc}") from exc

        tar_manifests = find_build_manifests(regular_files)
        if tar_manifests != set(overlay.upserts):
            unexpected = sorted(tar_manifests - set(overlay.upserts))
            missing = sorted(set(overlay.upserts) - tar_manifests)
            raise RuntimeError(
                "Snapshot manifest inventory does not match sidecar "
                f"(unexpected={unexpected[:10]}, missing={missing[:10]})"
            )
        deleted_present = set(overlay.deletes) & regular_files
        if deleted_present:
            raise RuntimeError(
                f"Snapshot contains manifest tombstone path(s): {sorted(deleted_present)}"
            )

        self._snapshot_metadata = data
        self._manifest_overlay = overlay
        self._go_manifest_inventory = go_inventory
        self._eval_meta["go_manifest_projection_complete"] = raw_go_projection is not None
        self._eval_meta["manifest_upserts_count"] = len(overlay.upserts)
        self._eval_meta["manifest_deletes_count"] = len(overlay.deletes)
        return data, overlay

    def _load_capture_excluded(self, start_files: Set[str]) -> Optional[FrozenSet[str]]:
        """Rebuild the capture-time exclusion witness from the tar's sidecar.

        The sidecar records the capture-time FILTER CONFIG (codex F4); we rebuild
        that exact filter and apply it to the START tree, so the witness covers
        even GT tests the agent deleted (absent from the tag tree the old
        file-list witness was derived from) and survives eval-side test_dirs
        drift. Falls back to a legacy `filtered_out` list if present. None => no
        sidecar (legacy tar) => no witness (drift unprotected; acceptable only
        when metadata has not drifted since capture).
        """
        data, _ = self._load_and_validate_snapshot_metadata()
        cfg = data.get("capture_filter")
        if cfg is not None:
            return capture_excluded_from_config(cfg, start_files)
        filtered = data.get("filtered_out")  # legacy sidecar
        if filtered is not None:
            return frozenset(filtered)
        return None

    def _load_capture_build_manifests(self) -> Set[str]:
        """Load the exact manifest exception set recorded at capture time.

        This keeps eval-side snapshot integrity aligned with capture: recursive
        Maven POMs are expected only when they were changed by the agent, not
        merely because they exist in the milestone START tree.
        """
        _, overlay = self._load_and_validate_snapshot_metadata()
        return set(overlay.upserts)

    def _apply_manifest_deletions(self) -> Tuple[bool, str]:
        """Apply manifest tombstones after END and environment patches."""
        _, overlay = self._load_and_validate_snapshot_metadata()
        if not overlay.deletes:
            return True, ""
        command = [
            "docker",
            "exec",
            "-i",
            self.container_name,
            "xargs",
            "-0",
            "rm",
            "-f",
            "--",
        ]
        payload = "".join(f"/testbed/{path}\0" for path in sorted(overlay.deletes))
        result = subprocess.run(command, input=payload, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown rm error").strip()
            return False, f"Failed to apply manifest tombstones: {detail}"
        print(f"🪦 Applied {len(overlay.deletes)} build-manifest tombstone(s)")
        return True, ""

    def _apply_exact_go_manifest_projection(self) -> Tuple[bool, str]:
        """Make both presence and absence of submitted Go manifests exact.

        A delta/tombstone overlay cannot describe a manifest that exists only
        in milestone END. The capture side therefore records the complete
        present-set for the source roots it owns. Remove any non-test Go
        manifest in that scope that is absent from the set, while leaving GT
        testdata modules under evaluator authority.
        """
        data, _ = self._load_and_validate_snapshot_metadata()
        if not self._go_module_closure_enabled():
            return True, ""
        inventory = getattr(self, "_go_manifest_inventory", None)
        if inventory is None:
            return False, "Exact Go manifest inventory was not loaded"
        raw_filter = data.get("capture_filter")
        if not isinstance(raw_filter, dict):
            return False, "Exact Go manifest projection requires capture_filter metadata"
        try:
            capture_filter = SrcFileFilter(
                src_dirs=raw_filter.get("src_dirs", []),
                test_dirs=raw_filter.get("test_dirs", []),
                exclude_patterns=raw_filter.get("exclude_patterns", []),
                generated_patterns=raw_filter.get("generated_patterns", []),
                modifiable_test_patterns=raw_filter.get(
                    "modifiable_test_patterns", []
                ),
            )
        except Exception as exc:
            return False, f"Cannot rebuild Go manifest capture scope: {exc}"

        command = [
            "docker", "exec", self.container_name,
            "find", "/testbed",
            "-path", "/testbed/.git", "-prune", "-o",
            "-type", "f", "(",
            "-name", "go.mod", "-o",
            "-name", "go.sum", "-o",
            "-name", "go.work", "-o",
            "-name", "go.work.sum", ")", "-print0",
        ]
        discovered = subprocess.run(command, capture_output=True, text=True)
        if discovered.returncode != 0:
            detail = (discovered.stderr or discovered.stdout or "find failed").strip()
            return False, f"Cannot enumerate evaluator Go manifests: {detail}"

        def protected(path: str) -> bool:
            return capture_filter.is_test_file(path) or capture_filter.is_excluded(path)

        current: Set[str] = set()
        for raw in discovered.stdout.split("\0"):
            if not raw:
                continue
            prefix = "/testbed/"
            if not raw.startswith(prefix):
                return False, f"Unexpected Go manifest path outside /testbed: {raw}"
            try:
                path = normalize_snapshot_path(raw[len(prefix):])
            except ValueError as exc:
                return False, f"Unsafe evaluator Go manifest path: {exc}"
            if (
                is_go_manifest_in_scope(path, capture_filter.src_dirs)
                and not protected(path)
            ):
                current.add(path)

        invalid_inventory = {
            path
            for path in inventory
            if not is_go_manifest_in_scope(path, capture_filter.src_dirs)
            or protected(path)
        }
        if invalid_inventory:
            return False, (
                "Go manifest inventory escapes its capture source scope: "
                f"{sorted(invalid_inventory)}"
            )
        missing = set(inventory) - current
        if missing:
            return False, (
                "Submitted Go manifest(s) disappeared during environment setup: "
                f"{sorted(missing)}"
            )
        remove = sorted(current - set(inventory))
        if remove:
            removed = subprocess.run(
                [
                    "docker", "exec", "-i", self.container_name,
                    "xargs", "-0", "rm", "-f", "--",
                ],
                input="".join(f"/testbed/{path}\0" for path in remove),
                capture_output=True,
                text=True,
            )
            if removed.returncode != 0:
                detail = (removed.stderr or removed.stdout or "rm failed").strip()
                return False, f"Cannot project absent Go manifests: {detail}"
        self._eval_meta["go_manifest_projection_removed"] = remove
        print(
            "🧭 Exact Go manifest projection: "
            f"present={len(inventory)}, removed-as-absent={len(remove)}"
        )
        return True, ""

    def _restore_agent_manifest_upserts(self) -> Tuple[bool, str]:
        """Restore exact agent manifests after legacy environment hooks run.

        Some milestone images implement ``apply_patches.sh`` by copying a
        prepared POM over the working tree.  Running the hook after the
        three-way merge therefore erased the agent side of that merge (M018
        root POM, M025 dependency BOM).  Keep ``snapshot.tar`` until the hook
        completes, restore only the sidecar-authorized manifest upserts, and
        then perform the merge once.  Source and test files are never
        re-extracted here.
        """
        _, overlay = self._load_and_validate_snapshot_metadata()
        upserts = sorted(overlay.upserts)
        if not upserts:
            command = [
                "docker", "exec", self.container_name,
                "rm", "-f", "/testbed/snapshot.tar",
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown rm error").strip()
                return False, f"Failed to remove snapshot archive: {detail}"
            return True, ""

        command = [
            "docker", "exec", "-i", self.container_name,
            "bash", "-c",
            "cd /testbed && "
            "tar --extract --file=snapshot.tar --null --files-from=- && "
            "rm -f snapshot.tar",
        ]
        payload = "".join(f"{path}\0" for path in upserts)
        result = subprocess.run(command, input=payload, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown tar error").strip()
            return False, f"Failed to restore agent manifest upserts: {detail}"
        return True, ""

    def _manifest_agent_authoritative_paths(
        self, overlay: ManifestOverlay
    ) -> FrozenSet[str]:
        """Return narrowly configured manifest paths that must remain exact.

        Some legacy environment hooks replace an entire manifest with a
        prepared copy instead of applying a small environment-only delta.  A
        three-way merge cannot reliably separate that wholesale replacement
        from the agent's dependency-management changes.  The repository config
        may therefore name exact manifest paths for a specific milestone.  The
        exception is intentionally narrow and recorded in the evaluation
        result.  A milestone-scoped entry is conditional: it applies only when
        that exact path is an upsert in the bound snapshot.  Trials for the same
        milestone may legitimately submit different manifest sets, so an
        untouched path is a no-op rather than a configuration error.
        """
        configured = self.repo_config.get("evaluation_manifest_agent_authoritative", {})
        if configured is None:
            return frozenset()
        if not isinstance(configured, dict):
            raise ValueError("must be a milestone-to-path-list mapping")
        raw_paths = configured.get(self.milestone_id, [])
        if not isinstance(raw_paths, list) or not all(
            isinstance(path, str) for path in raw_paths
        ):
            raise ValueError(f"{self.milestone_id} must map to a list of strings")

        normalized: Set[str] = set()
        for raw_path in raw_paths:
            path = normalize_snapshot_path(raw_path)
            if not is_build_manifest(path):
                raise ValueError(f"contains a non-manifest path: {path}")
            if path in normalized:
                raise ValueError(f"contains a duplicate path: {path}")
            if path in overlay.upserts:
                normalized.add(path)

        # Go's checksum/workspace companions are derived from the selected
        # module graph.  Resolving textual conflicts independently can create a
        # mixed state that neither the agent nor evaluator ever built.  When
        # the repository opts into the general Go closure, retain every exact
        # agent Go manifest upsert as one side of that state; the offline
        # reconciliation phase below then adds only dependencies induced by the
        # authoritative evaluator tests.
        if self._go_module_closure_enabled():
            normalized.update(
                path
                for path in overlay.upserts
                if PurePosixPath(path).name in GO_MANIFEST_BASENAMES
            )
        return frozenset(normalized)

    def _go_module_closure_requested(self) -> bool:
        value = self.repo_config.get("evaluation_go_module_closure", False)
        if not isinstance(value, bool):
            raise ValueError("evaluation_go_module_closure must be a boolean")
        return value

    def _go_module_closure_enabled(self) -> bool:
        """Whether exact closure is configured for this repository."""
        return self._go_module_closure_requested()

    def _merge_manifest_upserts(self, base_suffix: str = "end") -> Tuple[bool, str]:
        """Merge agent manifest edits onto the prepared evaluator manifest tree.

        Snapshot manifests are exact agent files.  A milestone image may also
        carry evaluator-owned preprocessing in the same file (for example
        dependency-management fixes committed on top of the raw END tag).
        Blind extraction replaces that preprocessing with the agent file.

        The merge base must be the *raw milestone state immediately below the
        evaluator preparation commit*, not the trial-wide snapshot BASE.  The
        latter may be absent from a milestone image and, more importantly,
        includes the milestone's ground-truth manifest delta in the evaluator
        side of the merge.  That both leaks GT changes and creates conflicts
        unrelated to environment preparation.

        For every manifest changed by both the agent and evaluator preparation,
        perform a real three-way content merge:

          current = exact agent snapshot
          base    = raw evaluator START/END state
          other   = prepared evaluator START/END state

        If evaluator preparation did not touch a manifest, the exact agent file
        remains authoritative.  Agent-added manifests also remain exact agent
        files.  Explicit agent deletions are applied later as tombstones.  An
        ambiguous preparation base or merge-tool error fails closed.  When the
        agent and the environment delta edit the same hunk, the prepared
        evaluator hunk wins: evaluator preprocessing is part of the test
        environment, while non-conflicting agent hunks are retained.  The same
        policy applies to a file-level add/add conflict, where no common file
        exists for a meaningful three-way merge: retain the prepared evaluator
        manifest and record the file as a conflict.
        """
        _, overlay = self._load_and_validate_snapshot_metadata()
        upserts = sorted(overlay.upserts)
        try:
            agent_authoritative = self._manifest_agent_authoritative_paths(overlay)
        except ValueError as exc:
            return False, f"Invalid evaluation_manifest_agent_authoritative config: {exc}"
        self._eval_meta["manifest_merged_count"] = 0
        self._eval_meta["manifest_agent_exact_count"] = 0
        self._eval_meta["manifest_agent_added_count"] = 0
        self._eval_meta["manifest_evaluator_missing_count"] = 0
        self._eval_meta["manifest_conflict_files_count"] = 0
        self._eval_meta["manifest_conflict_hunks_count"] = 0
        self._eval_meta["manifest_agent_authoritative_paths"] = sorted(agent_authoritative)
        if not upserts:
            return True, ""

        # Prepared milestone images in the supported datasets commit their
        # environment patch directly on top of a synthetic "Start/End state for
        # <milestone>" commit and force the milestone tag to that prepared
        # commit.  Resolve that parent once.  An unprepared tag already points
        # at the raw state, so its environment delta is empty (base == HEAD).
        resolve_script = r"""
set -e
milestone=$1
suffix=$2
git_bin=git
if test -x /usr/bin/git.real; then git_bin=/usr/bin/git.real; fi
git_cmd=("$git_bin" -C /testbed -c safe.directory=/testbed)

prepared_head=$("${git_cmd[@]}" rev-parse HEAD)
prepared_subject=$("${git_cmd[@]}" log -1 --format=%s HEAD)
parent=$("${git_cmd[@]}" rev-parse HEAD^ 2>/dev/null || true)
parent_subject=''
if [ -n "$parent" ]; then
    parent_subject=$("${git_cmd[@]}" log -1 --format=%s "$parent")
fi

case "$suffix" in
    end) raw_prefix="End state for $milestone" ;;
    start) raw_prefix="Start state for $milestone" ;;
    *) printf 'invalid evaluator state suffix: %s\n' "$suffix" >&2; exit 42 ;;
esac

if [[ "$prepared_subject" == "$raw_prefix"* ]]; then
    evaluator_base=$prepared_head
    reason=raw-tag
elif [ -n "$parent" ] && [[ "$parent_subject" == "$raw_prefix"* ]]; then
    evaluator_base=$parent
    reason=prepared-parent
elif [ -n "$parent" ] && {
    # Dataset preparation commits may carry a version in the marker, e.g.
    # ``[ENV-PATCH-v0.91] ...``.  Match the stable marker prefix rather than
    # only the historical unversioned spelling ``[ENV-PATCH]``.
    [[ "$prepared_subject" == \[ENV-PATCH* ]] ||
    [[ "$prepared_subject" == "Apply compilation patches"* ]];
}; then
    evaluator_base=$parent
    reason=prepared-subject
else
    printf 'cannot identify raw %s state below prepared HEAD %s (%s)\n' \
        "$suffix" "$prepared_head" "$prepared_subject" >&2
    exit 43
fi

printf '%s\t%s\t%s\n' "$prepared_head" "$evaluator_base" "$reason"
"""
        resolution = subprocess.run(
            [
                "docker",
                "exec",
                self.container_name,
                "bash",
                "-c",
                resolve_script,
                "evoclaw-resolve-manifest-base",
                self.milestone_id,
                base_suffix,
            ],
            capture_output=True,
            text=True,
        )
        if resolution.returncode != 0:
            detail = (resolution.stderr or resolution.stdout or "unknown resolver error").strip()
            return False, (
                "Cannot determine evaluator manifest preparation baseline: "
                f"{detail}"
            )
        fields = resolution.stdout.strip().split("\t")
        if len(fields) != 3 or not all(fields):
            return False, (
                "Cannot determine evaluator manifest preparation baseline: "
                f"unexpected resolver output {resolution.stdout!r}"
            )
        prepared_head, evaluator_base, base_reason = fields
        self._eval_meta["manifest_evaluator_base"] = evaluator_base
        self._eval_meta["manifest_evaluator_head"] = prepared_head
        self._eval_meta["manifest_base_reason"] = base_reason

        merge_script = r"""
set -e
evaluator_base=$1
prepared_head=$2
path=$3
git_bin=git
if test -x /usr/bin/git.real; then git_bin=/usr/bin/git.real; fi
git_cmd=("$git_bin" -C /testbed -c safe.directory=/testbed)

# If neither side of the evaluator preparation contains the path, it is an
# agent addition and the extracted file is already authoritative.
if ! "${git_cmd[@]}" cat-file -e "$evaluator_base:$path" 2>/dev/null; then
    if "${git_cmd[@]}" cat-file -e "$prepared_head:$path" 2>/dev/null; then
        if [ ! -f "/testbed/$path" ]; then
            printf 'agent manifest missing after extraction: %s\n' "$path" >&2
            exit 44
        fi
        "${git_cmd[@]}" show "$prepared_head:$path" > "/testbed/$path"
        printf 'evaluator-added-conflict\n'
        exit 0
    fi
    printf 'agent-added\n'
    exit 0
fi

# Preserve current source-authority behavior if evaluator preparation removed a
# manifest. Explicit agent deletion is handled separately by the sidecar.
if ! "${git_cmd[@]}" cat-file -e "$prepared_head:$path" 2>/dev/null; then
    printf 'evaluator-missing\n'
    exit 0
fi

if [ ! -f "/testbed/$path" ]; then
    printf 'agent manifest missing after extraction: %s\n' "$path" >&2
    exit 44
fi

# No evaluator-owned delta for this path: retain the exact agent snapshot and
# do not mix in ground-truth milestone changes from the prepared END tree.
base_blob=$("${git_cmd[@]}" rev-parse "$evaluator_base:$path")
prepared_blob=$("${git_cmd[@]}" rev-parse "$prepared_head:$path")
if [ "$base_blob" = "$prepared_blob" ]; then
    printf 'agent-exact\n'
    exit 0
fi

tmp=$(mktemp -d /tmp/evoclaw-manifest-merge.XXXXXX)
trap 'rm -rf "$tmp"' EXIT
"${git_cmd[@]}" show "$evaluator_base:$path" > "$tmp/base"
"${git_cmd[@]}" show "$prepared_head:$path" > "$tmp/evaluator"

set +e
git merge-file -p \
    -L agent-snapshot -L raw-milestone -L evaluator-prepared \
    "/testbed/$path" "$tmp/base" "$tmp/evaluator" > "$tmp/merged"
rc=$?
set -e
if [ "$rc" -gt 0 ] && [ "$rc" -le 127 ]; then
    # The evaluator side contains only the prepared-environment delta.  Resolve
    # overlapping hunks in its favor, while merge-file retains every
    # non-conflicting agent hunk.  Keep the original conflict count for audit.
    set +e
    git merge-file -p --theirs \
        -L agent-snapshot -L raw-milestone -L evaluator-prepared \
        "/testbed/$path" "$tmp/base" "$tmp/evaluator" > "$tmp/merged"
    resolved_rc=$?
    set -e
    if [ "$resolved_rc" -ne 0 ]; then
        printf 'three-way manifest conflict resolution failed for %s (git merge-file rc=%s)\n' \
            "$path" "$resolved_rc" >&2
        exit "$resolved_rc"
    fi
    cat "$tmp/merged" > "/testbed/$path"
    printf 'merged-evaluator-conflict:%s\n' "$rc"
    exit 0
elif [ "$rc" -ne 0 ]; then
    printf 'three-way manifest merge failed for %s (git merge-file rc=%s)\n' "$path" "$rc" >&2
    exit "$rc"
fi

# Redirecting into the existing regular file preserves the snapshot mode.
cat "$tmp/merged" > "/testbed/$path"
printf 'merged\n'
"""

        states = {
            "merged": 0,
            "merged-evaluator-conflict": 0,
            "evaluator-added-conflict": 0,
            "agent-exact": 0,
            "agent-added": 0,
            "evaluator-missing": 0,
            "agent-authoritative": 0,
        }
        conflict_hunks = 0
        for path in upserts:
            if path in agent_authoritative:
                states["agent-authoritative"] += 1
                continue
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_name,
                    "bash",
                    "-c",
                    merge_script,
                    "evoclaw-manifest-merge",
                    evaluator_base,
                    prepared_head,
                    path,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown merge error").strip()
                return False, f"Failed to merge build manifest {path}: {detail}"
            state = result.stdout.strip()
            if state.startswith("merged-evaluator-conflict:"):
                _, _, raw_count = state.partition(":")
                try:
                    conflict_hunks += int(raw_count)
                except ValueError:
                    return False, (
                        f"Failed to merge build manifest {path}: "
                        f"invalid conflict count {raw_count!r}"
                    )
                state = "merged-evaluator-conflict"
            if state not in states:
                return False, (
                    f"Failed to merge build manifest {path}: unexpected merge state {state!r}"
                )
            states[state] += 1

        self._eval_meta["manifest_merged_count"] = states["merged"]
        self._eval_meta["manifest_agent_exact_count"] = states["agent-exact"]
        self._eval_meta["manifest_agent_added_count"] = states["agent-added"]
        self._eval_meta["manifest_evaluator_missing_count"] = states["evaluator-missing"]
        self._eval_meta["manifest_conflict_files_count"] = (
            states["merged-evaluator-conflict"] + states["evaluator-added-conflict"]
        )
        self._eval_meta["manifest_conflict_hunks_count"] = conflict_hunks
        print(
            "🧩 Environment-only manifest overlay: "
            f"base={evaluator_base[:12]} ({base_reason}), "
            f"merged={states['merged']}, "
            f"conflicts={self._eval_meta['manifest_conflict_files_count']} files/"
            f"{conflict_hunks} hunks, add-add={states['evaluator-added-conflict']} "
            f"(evaluator-wins), agent-exact={states['agent-exact']}, "
            f"agent-added={states['agent-added']}, evaluator-missing={states['evaluator-missing']}, "
            f"agent-authoritative={states['agent-authoritative']}"
        )
        return True, ""

    def _maybe_prune_residue(self, base_suffix: str) -> Tuple[bool, str]:
        """Delete residue source files after tar extraction (spec §2, phase 1b).

        Source files are agent-authoritative: a snapshot-absent source file that
        existed at START was deleted by the agent and must not be resurrected by
        the base checkout. Tests, modifiable tests, generated files, non-code
        assets and keep-list entries are never deleted (V3b assertion enforces
        this at runtime).

        FAIL-CLOSED (review F1): a failed snapshot-integrity gate does NOT
        silently fall back to additive scoring (that would let a near-empty tar
        resurrect the whole GT solution). It records the failure, skips the
        deletions, and the caller forces resolved=False for the cell.
        """
        meta = self._eval_meta
        meta["residue_prune_enabled"] = self.residue_prune_enabled
        # Preserve a config-invalid verdict set at init (prune requested but
        # unusable config) — it must survive to keep the cell scoring-untrusted.
        if not self._prune_config_invalid:
            meta["residue_prune_skipped_reason"] = ""
        # L3: reset per-pass counters up front so an early return never leaves
        # stale counts from a previous (END) pass in a START-fallback report.
        meta["pruned_files_count"] = 0
        meta["pruned_files"] = []
        meta["keep_list_hits"] = []
        if not self.residue_prune_enabled:
            return True, ""

        base_tag = f"milestone-{self.milestone_id}-{base_suffix}"
        start_tag = f"milestone-{self.milestone_id}-start"

        try:
            with tarfile.open(self.patch_file) as tf:
                tar_files = normalize_tar_members(tf.getnames())
        except (tarfile.TarError, OSError) as e:
            meta["residue_prune_skipped_reason"] = "tar-unreadable"
            return False, f"residue prune: cannot list snapshot tar: {e}"

        base_files = self._git_ls_tree(base_tag)
        start_files = base_files if base_suffix == "start" else self._git_ls_tree(start_tag)
        if base_files is None or start_files is None:
            # Cannot list the tree -> cannot compute what the agent deleted.
            # HARD ERROR: never fall through to additive scoring (that is the
            # fail-open hole). The eval fails for this cell instead.
            meta["residue_prune_skipped_reason"] = "ls-tree-failed"
            return False, "residue prune: cannot list git trees in container (ls-tree-failed)"

        capture_excluded = self._load_capture_excluded(start_files)

        # Snapshot integrity is DIAGNOSTIC ONLY — there is no safety gate. By
        # design, tar-absence always means the agent deleted the file, so a
        # near-empty tar prunes the base source and scores an honest low, rather
        # than being "protected" by a gate a malicious agent could trigger. We
        # still record the missing count so a genuine capture loss (deepseek
        # style) is visible for investigation / re-capture.
        integrity = check_snapshot_integrity(
            start_files,
            tar_files,
            self._prune_filter,
            extra_build_manifests=self._load_capture_build_manifests(),
        )
        meta["snapshot_integrity_ok"] = integrity.ok
        meta["snapshot_missing_count"] = integrity.missing_count
        if not integrity.ok:
            print(
                f"ℹ️  snapshot integrity: {integrity.missing_count}/{integrity.expected_count} expected "
                f"files missing from tar — recorded; pruning proceeds (if this is capture loss, re-capture)"
            )

        # START provenance guard is active in phase 1b: GT-added files survive.
        prune_set = compute_prune_set(
            base_files,
            tar_files,
            start_files,
            self._prune_filter,
            self.prune_keep_list,
            extensions=self.prune_extensions,
            capture_excluded=capture_excluded,
        )
        meta["keep_list_hits"] = sorted((base_files - tar_files) & set(self.prune_keep_list))
        meta["pruned_files_count"] = 0
        meta["pruned_files"] = []
        if not prune_set:
            print(f"✂️  residue prune: nothing to prune on {base_tag}")
            return True, ""

        try:
            assert_prune_set_safe(
                prune_set,
                self._prune_filter,
                self.prune_keep_list,
                extensions=self.prune_extensions,
                capture_excluded=capture_excluded,
            )
        except ResiduePruneSafetyError as e:
            # Fail loud: a protected class in the prune set means filter/config drift.
            meta["residue_prune_skipped_reason"] = "safety-abort"
            return False, f"residue prune safety abort (V3b): {e}"

        # L6: xargs -0 (null-separated) is portable and safe for any path.
        rm_cmd = ["docker", "exec", "-i", self.container_name, "xargs", "-0", "rm", "-f", "--"]
        rm_input = "".join(f"/testbed/{p}\0" for p in prune_set)
        result = subprocess.run(rm_cmd, input=rm_input, capture_output=True, text=True)
        if result.returncode != 0:
            return False, f"residue prune: rm failed: {result.stderr}"

        meta["pruned_files_count"] = len(prune_set)
        meta["pruned_files"] = prune_set
        print(f"✂️  residue prune: removed {len(prune_set)} residue source file(s) (base={base_tag})")
        return True, ""

    def _graft_ground_truth_tests(self, gt_tag_suffix: str) -> Tuple[bool, str]:
        """Replace the test tree with the exact prepared GT tag's test tree.

        This is used when source is evaluated on the START base after END fails
        compilation.  The source base and the test authority are deliberately
        independent: START provides a compilable product baseline, while END
        still supplies the milestone's authoritative new/regression tests.
        Go manifests inside protected test/testdata paths are part of the GT
        fixture tree and are grafted with it. Product manifests remain under
        the submitted-tree projection and are never grafted here.
        """
        if self._prune_filter is None:
            return False, "GT test graft requires repo_src_dirs/test_dirs metadata"

        start_tag = f"milestone-{self.milestone_id}-start"
        gt_tag = f"milestone-{self.milestone_id}-{gt_tag_suffix}"
        start_files = self._git_ls_tree(start_tag)
        gt_files = self._git_ls_tree(gt_tag)
        if start_files is None or gt_files is None:
            return False, f"GT test graft cannot list {start_tag} and {gt_tag}"

        def select_tests(paths: Set[str]) -> Set[str]:
            return {
                path
                for path in paths
                if self._prune_filter.is_test_file(path)
                and not self._prune_filter.is_modifiable_test_file(path)
                and (
                    not is_build_manifest(path)
                    or is_go_build_manifest(path)
                )
            }

        start_tests = select_tests(start_files)
        gt_tests = select_tests(gt_files)
        remove_tests = sorted(start_tests | gt_tests)

        if remove_tests:
            result = subprocess.run(
                ["docker", "exec", "-i", self.container_name, "xargs", "-0", "rm", "-f", "--"],
                input="".join(f"/testbed/{path}\0" for path in remove_tests),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return False, f"GT test graft removal failed: {result.stderr}"

        # Avoid command-line length limits while retaining exact argv paths.
        ordered_gt_tests = sorted(gt_tests)
        for index in range(0, len(ordered_gt_tests), 200):
            chunk = ordered_gt_tests[index : index + 200]
            restore_script = r"""
git_bin=git
if test -x /usr/bin/git.real; then git_bin=/usr/bin/git.real; fi
tag=$1
shift
"$git_bin" -C /testbed checkout "$tag" -- "$@"
"""
            result = subprocess.run(
                [
                    "docker", "exec", self.container_name, "bash", "-c",
                    restore_script, "evoclaw-graft-tests", gt_tag, *chunk,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return False, f"GT test graft restore failed for {gt_tag}: {result.stderr}"

        self._eval_meta["gt_test_graft_suffix"] = gt_tag_suffix
        self._eval_meta["gt_test_graft_removed_count"] = len(remove_tests)
        self._eval_meta["gt_test_graft_restored_count"] = len(ordered_gt_tests)
        print(
            f"🧪 GT test graft: restored {len(ordered_gt_tests)} {gt_tag_suffix.upper()} "
            f"test file(s), removed {len(remove_tests)} START/END residue file(s)"
        )
        return True, ""

    def _go_module_dirs(self) -> List[str]:
        configured = self.repo_config.get("evaluation_go_module_dirs", ["."])
        if not isinstance(configured, list) or not configured:
            raise ValueError("evaluation_go_module_dirs must be a non-empty list")
        result: List[str] = []
        for raw in configured:
            if not isinstance(raw, str):
                raise ValueError("evaluation_go_module_dirs entries must be strings")
            normalized = raw.strip().removeprefix("./") or "."
            path = PurePosixPath(normalized)
            if normalized != "." and (
                path.is_absolute() or any(part in ("", ".", "..") for part in path.parts)
            ):
                raise ValueError(f"unsafe Go module directory: {raw!r}")
            if not re.fullmatch(r"\.|[A-Za-z0-9._@+\-/]+", normalized):
                raise ValueError(f"unsupported Go module directory: {raw!r}")
            if normalized not in result:
                result.append(normalized)
        if len(result) != 1:
            raise ValueError(
                "evaluation Go modfile isolation currently requires exactly one "
                "test module directory"
            )
        return result

    def _validate_immutable_go_test_config(self) -> Tuple[bool, str]:
        """Reject test commands that can mutate submitted dependency state."""
        config_path = (
            self.workspace_root / "dockerfiles" / self.milestone_id / "test_config.json"
        )
        if not config_path.exists():
            return True, ""
        try:
            modes = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"Go test config is unreadable: {config_path}: {exc}"
        if not isinstance(modes, list):
            return False, f"Go test config must be a list: {config_path}"
        forbidden = re.compile(
            r"(?:\bgo\s+(?:get\b|mod\s+(?:tidy|edit|download|vendor)\b)|"
            r"(?:^|\s)-mod(?:=|\s+)mod(?:\s|$)|"
            r"(?:^|\s)-modfile(?:=|\s+)|"
            r"(?:^|\s)(?:env\s+)?GOFLAGS\s*=)"
        )
        for index, mode in enumerate(modes):
            if not isinstance(mode, dict):
                continue
            command = str(mode.get("test_cmd", ""))
            match = forbidden.search(command)
            if match:
                return False, (
                    f"Go test config mode {index} mutates dependency state "
                    f"({match.group(0).strip()!r}); evaluator manifests are immutable"
                )
        return True, ""

    def _hash_go_manifest_state(self, module_dirs: List[str]) -> Tuple[bool, str]:
        script = r"""
set -e
cd /testbed
for module_dir in "$@"; do
    for name in go.mod go.sum go.work go.work.sum; do
        path=$module_dir/$name
        if test -f "$path"; then
            printf 'present\t%s\t' "$path"
            sha256sum "$path" | awk '{print $1}'
        else
            printf 'absent\t%s\n' "$path"
        fi
    done
done | sha256sum | awk '{print $1}'
"""
        result = subprocess.run(
            [
                "docker", "exec", self.container_name, "bash", "-c", script,
                "evoclaw-go-manifest-hash", *module_dirs,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "hash command failed").strip()
            return False, detail
        digest = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False, f"unexpected Go manifest hash output: {result.stdout!r}"
        return True, digest

    def _hash_go_test_graph(self) -> Tuple[bool, str]:
        """Hash the evaluator-private modfile pair, including absence state."""
        script = r"""
set -e
for path in /tmp/evoclaw-evaluation.mod /tmp/evoclaw-evaluation.sum; do
    if test -f "$path"; then
        printf 'present\t%s\t' "$path"
        sha256sum "$path" | awk '{print $1}'
    else
        printf 'absent\t%s\n' "$path"
    fi
done | sha256sum | awk '{print $1}'
"""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-c", script],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "hash command failed").strip()
            return False, detail
        digest = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False, f"unexpected evaluator Go graph hash output: {result.stdout!r}"
        return True, digest

    @staticmethod
    def _parse_go_json_stream(output: str) -> List[Dict[str, Any]]:
        """Parse concatenated JSON objects emitted by ``go list -json``."""
        decoder = json.JSONDecoder()
        index = 0
        objects: List[Dict[str, Any]] = []
        while index < len(output):
            while index < len(output) and output[index].isspace():
                index += 1
            if index >= len(output):
                break
            value, index = decoder.raw_decode(output, index)
            if not isinstance(value, dict):
                raise ValueError("go list JSON stream contains a non-object value")
            objects.append(value)
        return objects

    def _discover_go_test_imports(
        self,
        *,
        workdir: str,
        env: Dict[str, str],
        modfile: str,
    ) -> Tuple[bool, Set[str], Set[str], str]:
        """Discover test imports without allowing Go to select new modules.

        Only modules referenced by tests may be pre-seeded from the current END
        graph. Seeding every END-only module can introduce an unrelated module
        whose requirements upgrade the submitted graph and cause a false MVS
        contract conflict.

        A readonly ``go list -test`` is not sufficient here: Go may reject an
        otherwise compilable historical go.mod as needing tidy before it emits
        package JSON. Run a tiny standard-library-only parser with modules
        disabled instead. It reads imports but cannot resolve or select any
        dependency, so a union proxy's future versions cannot influence
        discovery.
        """
        scanner = r'''
package main

import (
	"encoding/json"
	"go/build"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

type record struct {
	Import string
	Directory string
}

func main() {
	if len(os.Args) != 2 { panic("expected module root") }
	root, absErr := filepath.Abs(os.Args[1])
	if absErr != nil { panic(absErr) }
	records := map[record]bool{}
	err := filepath.WalkDir(os.Args[1], func(path string, entry fs.DirEntry, err error) error {
		if err != nil { return err }
		if entry.IsDir() {
			name := entry.Name()
			if path != os.Args[1] && (name == ".git" || name == "vendor" || name == "testdata" || strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_")) {
				return filepath.SkipDir
			}
			// `go test ./...` stops at nested module boundaries.  Walking into one
			// here would make an unrelated module's tests look like dependencies of
			// the configured module and could falsely invalidate its exact graph.
			if path != os.Args[1] {
				if _, statErr := os.Stat(filepath.Join(path, "go.mod")); statErr == nil {
					return filepath.SkipDir
				} else if !os.IsNotExist(statErr) {
					return statErr
				}
			}
			return nil
		}
		if !strings.HasSuffix(entry.Name(), "_test.go") { return nil }
		// Match the same buildable file set as an untagged `go test` on this
		// container.  Parsing every _test.go would include evaluator fixtures
		// guarded by `//go:build ignore` (and files for another GOOS/GOARCH), then
		// falsely widen the private module graph with dependencies Go never uses.
		matched, matchErr := build.Default.MatchFile(filepath.Dir(path), entry.Name())
		if matchErr != nil { return matchErr }
		if !matched { return nil }
		file, parseErr := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if parseErr != nil { return parseErr }
		relative, relErr := filepath.Rel(root, filepath.Dir(path))
		if relErr != nil { return relErr }
		directory := "."
		if relative != "." { directory = "./" + filepath.ToSlash(relative) }
		for _, spec := range file.Imports {
			value, unquoteErr := strconv.Unquote(spec.Path.Value)
			if unquoteErr != nil { return unquoteErr }
			records[record{Import: value, Directory: directory}] = true
		}
		return nil
	})
	if err != nil { panic(err) }
	ordered := make([]record, 0, len(records))
	for value := range records { ordered = append(ordered, value) }
	sort.Slice(ordered, func(i, j int) bool {
		if ordered[i].Import != ordered[j].Import { return ordered[i].Import < ordered[j].Import }
		return ordered[i].Directory < ordered[j].Directory
	})
	encoder := json.NewEncoder(os.Stdout)
	for _, value := range ordered {
		if encodeErr := encoder.Encode(value); encodeErr != nil { panic(encodeErr) }
	}
}
'''
        command = (
            "set -eu; helper=/tmp/evoclaw-go-test-imports.go; "
            "trap 'rm -f \"$helper\"' EXIT; "
            "cat >\"$helper\" <<'EVOCLAW_GO_IMPORT_SCANNER'\n"
            + scanner
            + "\nEVOCLAW_GO_IMPORT_SCANNER\n"
            + "GO111MODULE=off go run \"$helper\" \"$PWD\""
        )
        result = self._go_exec(
            command,
            workdir=workdir,
            env=env,
        )
        combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            return False, set(), set(), combined[-4000:]
        imports: Set[str] = set()
        owners: Dict[str, Set[str]] = {}
        try:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                import_path = record.get("Import") if isinstance(record, dict) else None
                directory = record.get("Directory") if isinstance(record, dict) else None
                if not isinstance(import_path, str) or not import_path:
                    raise ValueError("missing import path")
                if not isinstance(directory, str) or not re.fullmatch(
                    r"\.|\./[A-Za-z0-9._@+\-/]+", directory
                ):
                    raise ValueError(f"unsafe package directory: {directory!r}")
                imports.add(import_path)
                owners.setdefault(import_path, set()).add(directory)
        except (json.JSONDecodeError, ValueError) as exc:
            return False, set(), set(), f"Malformed Go test import scanner output: {exc}"
        self._go_test_import_owners = owners
        external = {
            import_path
            for import_path in imports
            if "." in import_path.partition("/")[0]
        }
        return True, imports, external, ""

    def _verify_go_evaluation_state(self, stage: str) -> Tuple[bool, str]:
        """Fail closed if compile/tests changed real or evaluator Go state."""
        if not self._eval_meta.get("go_module_closure_enabled"):
            return True, ""
        try:
            module_dirs = self._go_module_dirs()
        except ValueError as exc:
            return False, f"Invalid Go evaluation config during {stage}: {exc}"
        real_ok, real_hash = self._hash_go_manifest_state(module_dirs)
        if not real_ok:
            return False, f"Cannot hash submitted Go manifests after {stage}: {real_hash}"
        self._eval_meta["go_module_manifest_sha256_after"] = real_hash
        expected_real = self._eval_meta.get("go_module_manifest_sha256_before", "")
        if real_hash != expected_real:
            error = (
                f"{stage} mutated submitted Go manifests "
                f"({expected_real} -> {real_hash})"
            )
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        graph_ok, graph_hash = self._hash_go_test_graph()
        if not graph_ok:
            return False, f"Cannot hash evaluator Go graph after {stage}: {graph_hash}"
        self._eval_meta["go_test_graph_sha256_after"] = graph_hash
        expected_graph = self._eval_meta.get("go_test_graph_sha256_before", "")
        if graph_hash != expected_graph:
            error = (
                f"{stage} mutated evaluator-private Go test graph "
                f"({expected_graph} -> {graph_hash})"
            )
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        return True, ""

    @staticmethod
    def _go_offline_cache_miss(output: str) -> bool:
        # Only this token means the Go command had a valid package lookup but
        # was unable to query the sealed cache as a proxy. Invalid versions and
        # unsupported toolchain directives are submitted-manifest errors, not
        # evaluator infrastructure failures.
        return "module lookup disabled by GOPROXY=off" in output

    def _go_local_cache_proxy(self) -> str:
        """Return the vetted download cache as a read-only local Go proxy."""
        closure = (
            self.quarantine_config.get("closure")
            if isinstance(self.quarantine_config, dict)
            else None
        )
        cache_paths = _validated_cache_paths(
            closure.get("cache_paths") if isinstance(closure, dict) else None
        )
        candidates = [path for path in cache_paths if path.endswith("/cache/download")]
        if len(candidates) != 1:
            raise ValueError(
                "Go evaluation requires exactly one vetted */cache/download path"
            )
        return f"file://{candidates[0]}"

    @staticmethod
    def _compare_go_semver(left: str, right: str) -> int:
        """Compare canonical Go module semantic versions without a proxy query."""
        pattern = re.compile(
            r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
            r"(?:\+[0-9A-Za-z.-]+)?$"
        )

        def parse(value: str) -> Tuple[Tuple[int, int, int], Optional[List[str]]]:
            match = pattern.fullmatch(value)
            if not match:
                raise ValueError(f"non-canonical Go module version: {value!r}")
            core = tuple(int(match.group(index)) for index in (1, 2, 3))
            prerelease = match.group(4)
            return core, prerelease.split(".") if prerelease is not None else None

        left_core, left_pre = parse(left)
        right_core, right_pre = parse(right)
        if left_core != right_core:
            return 1 if left_core > right_core else -1
        if left_pre is None or right_pre is None:
            if left_pre is right_pre:
                return 0
            return 1 if left_pre is None else -1
        for left_item, right_item in zip(left_pre, right_pre):
            if left_item == right_item:
                continue
            left_numeric = left_item.isdigit()
            right_numeric = right_item.isdigit()
            if left_numeric and right_numeric:
                return 1 if int(left_item) > int(right_item) else -1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return 1 if left_item > right_item else -1
        if len(left_pre) == len(right_pre):
            return 0
        return 1 if len(left_pre) > len(right_pre) else -1

    @classmethod
    def _parse_go_module_graph(
        cls,
        output: str,
        mod_json: Dict[str, Any],
    ) -> Dict[str, Tuple[str, str, str]]:
        """Compute the MVS-selected build list from ``go mod graph`` output.

        ``go list -m all`` unnecessarily fetches ``.info`` metadata that is not
        needed to compile a version already pinned by go.mod/go.sum.  Historical
        offline closures therefore may compile perfectly yet lack that metadata.
        ``go mod graph`` consumes only the module files needed by MVS; taking the
        maximum canonical version per path reproduces the selected build list
        without widening the agent environment.
        """
        module = mod_json.get("Module")
        main_path = module.get("Path") if isinstance(module, dict) else None
        if not isinstance(main_path, str) or not main_path:
            raise ValueError("go.mod JSON has no module path")

        selected: Dict[str, str] = {}

        def observe(token: str) -> None:
            if token == main_path:
                return
            path, separator, version = token.rpartition("@")
            if not separator or not path or not version:
                raise ValueError(f"malformed Go module graph token: {token!r}")
            # Go 1.21 represents language/toolchain requirements as graph nodes;
            # their exact values are audited separately as go.mod directives.
            if path in {"go", "toolchain"}:
                return
            current = selected.get(path)
            if current is None or cls._compare_go_semver(version, current) > 0:
                selected[path] = version

        for line in (output or "").splitlines():
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(f"malformed go mod graph row: {line!r}")
            observe(parts[0])
            observe(parts[1])

        replacements = mod_json.get("Replace") or []
        if not isinstance(replacements, list):
            raise ValueError("go.mod Replace field is not a list")

        graph: Dict[str, Tuple[str, str, str]] = {main_path: ("", "", "")}
        for path, version in selected.items():
            replacement: Optional[Dict[str, Any]] = None
            for candidate in replacements:
                if not isinstance(candidate, dict):
                    continue
                old = candidate.get("Old")
                if not isinstance(old, dict) or old.get("Path") != path:
                    continue
                old_version = old.get("Version") or ""
                if old_version in ("", version):
                    if replacement is None or old_version == version:
                        replacement = candidate
            replace_path = ""
            replace_version = ""
            if replacement is not None:
                new = replacement.get("New")
                if not isinstance(new, dict) or not isinstance(new.get("Path"), str):
                    raise ValueError(f"malformed replacement for {path}")
                replace_path = new["Path"]
                replace_version = str(new.get("Version") or "")
            graph[path] = (version, replace_path, replace_version)
        return graph

    def _read_go_module_graph(
        self,
        *,
        workdir: str,
        env: Dict[str, str],
        modfile: str = "",
    ) -> Tuple[bool, Dict[str, Tuple[str, str, str]], str]:
        modfile_arg = f" -modfile={modfile}" if modfile else ""
        result = self._go_exec(
            f"go mod graph{modfile_arg}",
            workdir=workdir,
            env=env,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            return False, {}, output[-4000:]
        edit = self._go_exec(
            f"go mod edit -json{' ' + modfile if modfile else ''}",
            workdir=workdir,
            env=env,
        )
        if edit.returncode != 0:
            detail = "\n".join(part for part in (edit.stdout, edit.stderr) if part)
            return False, {}, detail[-4000:]
        try:
            mod_json = json.loads(edit.stdout)
            if not isinstance(mod_json, dict):
                raise ValueError("go mod edit JSON is not an object")
            return True, self._parse_go_module_graph(result.stdout, mod_json), ""
        except (json.JSONDecodeError, ValueError) as exc:
            return False, {}, str(exc)

    def _read_go_mod_semantics(
        self,
        *,
        workdir: str,
        env: Dict[str, str],
        modfile: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        modfile_arg = f" {modfile}" if modfile else ""
        result = self._go_exec(
            f"go mod edit -json{modfile_arg}", workdir=workdir, env=env
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            return False, {}, output[-4000:]
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return False, {}, f"malformed go mod edit JSON: {exc}"
        if not isinstance(raw, dict):
            return False, {}, "go mod edit JSON is not an object"
        # Require additions are the one intentional private-test delta. Every
        # other semantic directive must remain byte-semantically identical.
        module = raw.get("Module")
        module_path = module.get("Path") if isinstance(module, dict) else None
        witness = {
            "ModulePath": module_path,
            "Go": raw.get("Go"),
            "Toolchain": raw.get("Toolchain"),
            "Replace": raw.get("Replace") or [],
            "Exclude": raw.get("Exclude") or [],
            "Retract": raw.get("Retract") or [],
        }
        return True, witness, ""

    def _prepare_end_go_graph(
        self,
        *,
        module_dir: str,
        workdir: str,
        env: Dict[str, str],
    ) -> Tuple[bool, Dict[str, Tuple[str, str, str]], Set[str], str]:
        """Materialize the current milestone END graph as the GT dependency pin."""
        tag = f"milestone-{self.milestone_id}-end"
        script = r'''
set -eu
git_bin=git
if test -x /usr/bin/git.real; then git_bin=/usr/bin/git.real; fi
"$git_bin" -C /testbed show "$1:$2" > /tmp/evoclaw-end.mod
if "$git_bin" -C /testbed cat-file -e "$1:$3" 2>/dev/null; then
  "$git_bin" -C /testbed show "$1:$3" > /tmp/evoclaw-end.sum
else
  : > /tmp/evoclaw-end.sum
fi
'''
        prepared = subprocess.run(
            [
                "docker", "exec", self.container_name, "bash", "-c", script,
                "evoclaw-end-go-graph", tag,
                "go.mod" if module_dir == "." else f"{module_dir}/go.mod",
                "go.sum" if module_dir == "." else f"{module_dir}/go.sum",
            ],
            capture_output=True,
            text=True,
        )
        if prepared.returncode != 0:
            detail = (prepared.stderr or prepared.stdout or "git show failed").strip()
            return False, {}, set(), f"Cannot materialize {tag} Go graph: {detail}"
        ok, graph, error = self._read_go_module_graph(
            workdir=workdir,
            env=env,
            modfile="/tmp/evoclaw-end.mod",
        )
        if not ok:
            return False, {}, set(), f"Milestone END Go graph is not reproducible: {error}"
        sums = subprocess.run(
            [
                "docker", "exec", self.container_name, "sh", "-c",
                "sed '/^[[:space:]]*$/d' /tmp/evoclaw-end.sum",
            ],
            capture_output=True,
            text=True,
        )
        if sums.returncode != 0:
            detail = (sums.stderr or sums.stdout or "cannot read go.sum").strip()
            return False, {}, set(), f"Cannot read milestone END go.sum: {detail}"
        return True, graph, set(sums.stdout.splitlines()), ""

    def _configure_partial_go_test_package_filter(
        self,
        *,
        workdir: str,
        base_env: Dict[str, str],
        unsafe_test_imports: Set[str],
    ) -> Tuple[bool, str]:
        """Run every safe package when private tests need an invalid graph.

        A single evaluator-owned test import can make a broad ``go test ./...``
        stop during package loading, discarding valid reports from unrelated
        packages.  Compatibility mode must not repair the submitted module
        graph, so enumerate production packages with that exact graph and omit
        only directories whose tests own the unsafe imports.
        """
        owners = getattr(self, "_go_test_import_owners", {})
        excluded_dirs = sorted({
            directory
            for import_path in unsafe_test_imports
            for directory in owners.get(import_path, set())
        })
        if not excluded_dirs:
            return False, (
                "cannot map incompatible evaluator test import(s) to package "
                f"directories: {sorted(unsafe_test_imports)}"
            )

        listed = self._go_exec(
            "go list -mod=readonly -f '{{.ImportPath}}\\t{{.Dir}}' ./...",
            workdir=workdir,
            env=base_env,
        )
        output = "\n".join(part for part in (listed.stdout, listed.stderr) if part)
        if listed.returncode != 0:
            return False, f"cannot enumerate submitted production packages: {output[-4000:]}"

        root = PurePosixPath(workdir)
        excluded = set(excluded_dirs)
        included_packages: List[str] = []
        observed_excluded: Set[str] = set()
        try:
            for line in listed.stdout.splitlines():
                if not line.strip():
                    continue
                import_path, separator, absolute_dir = line.partition("\t")
                if not separator:
                    raise ValueError(f"malformed go list row: {line!r}")
                if not re.fullmatch(r"[A-Za-z0-9._~+@/\-]+", import_path):
                    raise ValueError(f"unsafe Go package import path: {import_path!r}")
                directory = PurePosixPath(absolute_dir)
                relative = directory.relative_to(root)
                relative_text = "." if str(relative) == "." else f"./{relative}"
                if relative_text in excluded:
                    observed_excluded.add(relative_text)
                else:
                    included_packages.append(import_path)
        except ValueError as exc:
            return False, f"cannot parse submitted package inventory: {exc}"

        missing_dirs = excluded - observed_excluded
        if missing_dirs:
            return False, (
                "unsafe evaluator test package directory is absent from the "
                f"submitted production inventory: {sorted(missing_dirs)}"
            )
        if not included_packages:
            return False, "all submitted production packages require the incompatible test graph"

        package_file = "/tmp/evoclaw-safe-test-packages"
        written = subprocess.run(
            [
                "docker", "exec", "-i", self.container_name, "sh", "-c",
                f"umask 077; cat > {package_file}; chmod 0444 {package_file}",
            ],
            input="\n".join(included_packages) + "\n",
            capture_output=True,
            text=True,
        )
        if written.returncode != 0:
            detail = (written.stderr or written.stdout or "write failed").strip()
            return False, f"cannot persist safe Go package inventory: {detail}"

        self._eval_meta["go_partial_package_filter_applied"] = True
        self._eval_meta["go_partial_package_filter_excluded"] = sorted(observed_excluded)
        self._eval_meta["go_partial_package_filter_included"] = len(included_packages)
        self._go_exec_env["EVOCLAW_GO_TEST_PACKAGE_FILE"] = package_file
        return True, ""

    def _use_submitted_go_graph_after_test_contract_error(
        self,
        *,
        base_env: Dict[str, str],
        workdir: str,
        error: str,
        unsafe_test_imports: Set[str],
    ) -> Tuple[bool, str]:
        """Keep compatibility partial scoring without using a repaired graph."""
        self._eval_meta["go_module_test_graph_contract_error"] = error
        self._eval_meta["go_module_closure_error"] = error
        self._eval_meta["partial_test_universe"] = True
        self._eval_meta.setdefault("build_failure_diagnostics", []).append(error)
        if getattr(self, "build_failure_fail_closed", False):
            return False, error
        discarded = subprocess.run(
            [
                "docker", "exec", self.container_name, "rm", "-f", "--",
                "/tmp/evoclaw-evaluation.mod",
                "/tmp/evoclaw-evaluation.sum",
            ],
            capture_output=True,
            text=True,
        )
        if discarded.returncode != 0:
            detail = (discarded.stderr or discarded.stdout or "rm failed").strip()
            return False, f"Cannot discard incompatible evaluator Go graph: {detail}"
        graph_ok, absent_hash = self._hash_go_test_graph()
        if not graph_ok:
            return False, f"Cannot verify discarded evaluator Go graph: {absent_hash}"
        self._eval_meta["go_test_graph_sha256_before"] = absent_hash
        self._eval_meta["go_test_graph_sha256_after"] = absent_hash
        self._go_exec_env = {
            **base_env,
            "GOFLAGS": "-buildvcs=false -mod=readonly",
        }
        filter_ok, filter_error = self._configure_partial_go_test_package_filter(
            workdir=workdir,
            base_env=base_env,
            unsafe_test_imports=unsafe_test_imports,
        )
        if not filter_ok:
            diagnostic = (
                "Exact submitted graph retained, but safe package narrowing was "
                f"unavailable: {filter_error}"
            )
            self._eval_meta.setdefault("build_failure_diagnostics", []).append(
                diagnostic
            )
            # Compatibility scoring is safe only when the broad package
            # pattern is replaced by an audited subset.  Falling through here
            # would run ``./...`` on a graph we already know cannot represent
            # some evaluator-owned tests and could silently score a different
            # package universe.
            return False, diagnostic
        print(
            "⚠️  GT-test module graph is incompatible with the submitted graph; "
            "running safe packages on the exact submitted graph for partial-report scoring"
        )
        return True, ""

    def _validate_go_module_topology(
        self,
        module_dirs: List[str],
    ) -> Tuple[bool, str]:
        """Reject workspace/multi-module layouts the one-modfile runner cannot bind."""
        inventory = getattr(self, "_go_manifest_inventory", None)
        if inventory is None:
            return False, "Exact Go manifest inventory is unavailable"
        workspace_files = {
            path
            for path in inventory
            if PurePosixPath(path).name in {"go.work", "go.work.sum"}
        }
        if workspace_files:
            return False, (
                "Go workspace evaluation is unsupported by isolated -modfile mode; "
                f"configure a workspace-specific evaluator: {sorted(workspace_files)}"
            )

        def manifest_root(path: str) -> str:
            parent = str(PurePosixPath(path).parent)
            return "." if parent == "." else parent

        roots = {manifest_root(path) for path in inventory}
        configured = set(module_dirs)
        extra = roots - configured
        if extra:
            return False, (
                "Multiple scoped Go module roots require independent evaluator "
                f"modfiles; unsupported roots: {sorted(extra)}"
            )
        missing_mod = {
            module_dir
            for module_dir in configured
            if ("go.mod" if module_dir == "." else f"{module_dir}/go.mod")
            not in inventory
        }
        if missing_mod:
            return False, (
                "Submitted tree is missing configured Go module manifest(s): "
                f"{sorted(missing_mod)}"
            )
        return True, ""

    def _go_exec(
        self,
        command: str,
        *,
        workdir: str,
        env: Dict[str, str],
        timeout: int = 600,
    ) -> subprocess.CompletedProcess:
        env_args = [
            item
            for key, value in env.items()
            for item in ("-e", f"{key}={value}")
        ]
        return subprocess.run(
            [
                "docker", "exec", "--workdir", workdir, *env_args,
                self.container_name, "bash", "-c", command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _run_go_module_closure(self) -> Tuple[bool, str]:
        """Validate agent manifests and create an isolated GT-test modfile.

        Submitted manifests stay byte-for-byte immutable. Production packages
        must resolve offline with ``-mod=readonly``; an agent omission is never
        repaired. Only after that gate passes may a /tmp modfile gain
        dependencies induced by evaluator-owned tests. Compile/test commands
        then consume that temporary graph read-only.
        """
        try:
            enabled = self._go_module_closure_enabled()
            module_dirs = self._go_module_dirs() if enabled else []
        except ValueError as exc:
            return False, f"Invalid Go evaluation config: {exc}"
        self._eval_meta["go_module_closure_enabled"] = enabled
        if not enabled:
            return True, ""
        # This method is rerun after END -> START fallback. Never carry an END
        # graph/verdict/provenance into the START attempt.
        previous_contract_error = self._eval_meta.get(
            "go_module_test_graph_contract_error", ""
        )
        if previous_contract_error:
            self._eval_meta["build_failure_diagnostics"] = [
                item
                for item in self._eval_meta.get("build_failure_diagnostics", [])
                if item != previous_contract_error
            ]
        self._go_exec_env = {}
        self._eval_meta["go_module_closure_applied"] = False
        self._eval_meta["go_module_production_compile_checked"] = False
        self._eval_meta["go_module_production_compile_error"] = ""
        self._eval_meta["go_module_test_graph_contract_error"] = ""
        self._eval_meta["go_module_test_graph_added_modules"] = []
        self._eval_meta["go_partial_package_filter_applied"] = False
        self._eval_meta["go_partial_package_filter_excluded"] = []
        self._eval_meta["go_partial_package_filter_included"] = 0
        self._eval_meta["go_module_test_mod_changed"] = False
        self._eval_meta["go_module_sum_changed"] = False
        self._eval_meta["go_module_closure_error"] = ""
        self._eval_meta["go_test_graph_sha256_before"] = ""
        self._eval_meta["go_test_graph_sha256_after"] = ""
        self._eval_meta["partial_test_universe"] = False
        self._go_test_import_owners = {}

        config_ok, config_error = self._validate_immutable_go_test_config()
        if not config_ok:
            self._eval_meta["go_module_closure_error"] = config_error
            return False, config_error

        topology_ok, topology_error = self._validate_go_module_topology(module_dirs)
        if not topology_ok:
            self._eval_meta["go_module_closure_error"] = topology_error
            return False, topology_error

        hash_ok, before_hash = self._hash_go_manifest_state(module_dirs)
        if not hash_ok:
            return False, f"Cannot hash submitted Go manifests: {before_hash}"
        self._eval_meta["go_module_manifest_sha256_before"] = before_hash

        module_dir = module_dirs[0]
        workdir = "/testbed" if module_dir == "." else f"/testbed/{module_dir}"
        base_env = {
            "GOPROXY": self._go_local_cache_proxy(),
            "GONOPROXY": "none",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
            "GOMODCACHE": "/tmp/evoclaw-gomodcache",
            "GOCACHE": "/tmp/evoclaw-go-build",
        }
        self._eval_meta["go_test_local_proxy_used"] = True
        production = self._go_exec(
            "go list -mod=readonly -deps ./...",
            workdir=workdir,
            env=base_env,
        )
        production_output = "\n".join(
            part for part in (production.stdout, production.stderr) if part
        )
        if production.returncode != 0:
            error = (
                "Submitted Go production graph failed under the same sealed "
                "cache and -mod=readonly policy available to the agent; the "
                "evaluator will not repair it:\n"
                f"{production_output[-4000:]}"
            )
            self._eval_meta["go_module_closure_error"] = error
            return False, error

        # ``go list -deps`` resolves imports but does not type-check production
        # packages. The evaluator-private test graph constructed below may add
        # or upgrade modules, so compiling only with that graph can silently
        # repair an invalid submitted graph. Compile once against the exact
        # submitted manifests before creating any private modfile. A type error
        # is deferred to _check_compilation so END -> START fallback and the
        # explicitly requested partial-report compatibility mode still work;
        # manifest mutation is always a hard environment failure.
        try:
            production_compile = self._go_exec(
                "go build -buildvcs=false -mod=readonly ./...",
                workdir=workdir,
                env=base_env,
            )
            production_compile_output = "\n".join(
                part
                for part in (production_compile.stdout, production_compile.stderr)
                if part
            )
            if production_compile.returncode != 0:
                fatal = extract_first_fatal_error(production_compile_output)
                detail = fatal or "\n".join(
                    production_compile_output.splitlines()[-15:]
                )[-1500:]
                self._eval_meta["go_module_production_compile_error"] = (
                    "Submitted Go production graph failed type-check under "
                    f"-mod=readonly (exit {production_compile.returncode}):\n{detail}"
                )
        except subprocess.TimeoutExpired:
            self._eval_meta["go_module_production_compile_error"] = (
                "Submitted Go production graph compilation timed out after 10 minutes"
            )
        self._eval_meta["go_module_production_compile_checked"] = True

        compile_hash_ok, compile_hash = self._hash_go_manifest_state(module_dirs)
        if not compile_hash_ok:
            return False, (
                "Cannot hash submitted Go manifests after production compile: "
                f"{compile_hash}"
            )
        self._eval_meta["go_module_manifest_sha256_after"] = compile_hash
        if compile_hash != before_hash:
            error = (
                "Submitted Go production compile mutated manifests "
                f"({before_hash} -> {compile_hash})"
            )
            self._eval_meta["go_module_closure_error"] = error
            return False, error

        submitted_graph_ok, submitted_graph, submitted_graph_error = (
            self._read_go_module_graph(workdir=workdir, env=base_env)
        )
        if not submitted_graph_ok:
            error = (
                "Cannot record exact submitted Go module graph under readonly policy: "
                f"{submitted_graph_error}"
            )
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        semantics_ok, submitted_semantics, semantics_error = (
            self._read_go_mod_semantics(workdir=workdir, env=base_env)
        )
        if not semantics_ok:
            error = f"Cannot record submitted go.mod semantics: {semantics_error}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        end_ok, end_graph, end_sums, end_error = self._prepare_end_go_graph(
            module_dir=module_dir,
            workdir=workdir,
            env=base_env,
        )
        if not end_ok:
            self._eval_meta["go_module_closure_error"] = end_error
            return False, end_error
        submitted_sums_result = subprocess.run(
            [
                "docker", "exec", "--workdir", workdir, self.container_name,
                "sh", "-c", "test ! -f go.sum || sed '/^[[:space:]]*$/d' go.sum",
            ],
            capture_output=True,
            text=True,
        )
        if submitted_sums_result.returncode != 0:
            detail = (
                submitted_sums_result.stderr
                or submitted_sums_result.stdout
                or "cannot read go.sum"
            ).strip()
            return False, f"Cannot record submitted go.sum: {detail}"
        submitted_sums = set(submitted_sums_result.stdout.splitlines())

        prepared = subprocess.run(
            [
                "docker", "exec", self.container_name, "bash", "-c",
                "set -e; cd \"$1\"; cp go.mod /tmp/evoclaw-evaluation.mod; "
                "if test -f go.sum; then cp go.sum /tmp/evoclaw-evaluation.sum; "
                "else : > /tmp/evoclaw-evaluation.sum; fi",
                "evoclaw-go-modfile", workdir,
            ],
            capture_output=True,
            text=True,
        )
        if prepared.returncode != 0:
            detail = (prepared.stderr or prepared.stdout or "copy failed").strip()
            return False, f"Cannot create evaluator Go modfile: {detail}"

        temp_mod = "/tmp/evoclaw-evaluation.mod"
        # Discover imports of tests that are actually present. Do not seed every
        # END-only module: an unrelated END production dependency can itself
        # require a newer version of an existing submitted module and create a
        # false MVS conflict.
        imports_ok, test_imports, missing_imports, imports_error = (
            self._discover_go_test_imports(
                workdir=workdir,
                env=base_env,
                modfile=temp_mod,
            )
        )
        if not imports_ok:
            error = f"Cannot discover evaluator-owned Go test imports: {imports_error}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error

        seed_modules: Dict[str, str] = {}
        seed_test_imports: Set[str] = set()
        unpinned_missing: Set[str] = set()
        for import_path in sorted(test_imports | missing_imports):
            matches = [
                module_path
                for module_path in end_graph
                if import_path == module_path or import_path.startswith(module_path + "/")
            ]
            if not matches:
                if import_path in missing_imports:
                    unpinned_missing.add(import_path)
                continue
            module_path = max(matches, key=len)
            if module_path in submitted_graph:
                continue
            version, replace_path, _replace_version = end_graph[module_path]
            if not version or replace_path:
                unpinned_missing.add(import_path)
                continue
            seed_modules[module_path] = version
            seed_test_imports.add(import_path)
        if unpinned_missing:
            error = (
                "Evaluator-owned Go tests import package(s) absent from the "
                "submitted graph without an exact usable milestone-END module pin: "
                f"{sorted(unpinned_missing)}"
            )
            return self._use_submitted_go_graph_after_test_contract_error(
                base_env=base_env,
                workdir=workdir,
                error=error,
                unsafe_test_imports=unpinned_missing,
            )

        seed_args = [
            f"-require={path}@{version}"
            for path, version in sorted(seed_modules.items())
        ]
        if seed_args:
            seeded = subprocess.run(
                [
                    "docker", "exec", "--workdir", workdir,
                    *[item for pair in (
                        ("-e", f"{key}={value}") for key, value in base_env.items()
                    ) for item in pair],
                    self.container_name, "go", "mod", "edit",
                    *seed_args, temp_mod,
                ],
                capture_output=True,
                text=True,
            )
            if seeded.returncode != 0:
                detail = (seeded.stderr or seeded.stdout or "go mod edit failed").strip()
                error = f"Cannot seed milestone-pinned GT-test modules: {detail}"
                self._eval_meta["go_module_closure_error"] = error
                return False, error

            # Detect MVS upgrades before loading test packages.  Loading can
            # require an intermediate module zip that is in neither the agent
            # graph nor the milestone-END graph.  More importantly, once an
            # exact END-pinned test dependency upgrades an existing submitted
            # selection, compatibility mode must keep the submitted graph and
            # omit only the owning test packages rather than repairing it.
            preflight_ok, preflight_graph, preflight_error = (
                self._read_go_module_graph(
                    workdir=workdir,
                    env=base_env,
                    modfile=temp_mod,
                )
            )
            if not preflight_ok:
                error = f"Cannot preflight milestone-pinned GT-test modules: {preflight_error}"
                self._eval_meta["go_module_closure_error"] = error
                return False, error
            preflight_changed = {
                path: {
                    "submitted": submitted_graph[path],
                    "private": preflight_graph.get(path),
                }
                for path in submitted_graph
                if preflight_graph.get(path) != submitted_graph[path]
            }
            preflight_added = sorted(set(preflight_graph) - set(submitted_graph))
            preflight_invalid_added = {
                path: {
                    "private": preflight_graph[path],
                    "milestone_end": end_graph.get(path),
                }
                for path in preflight_added
                if end_graph.get(path) != preflight_graph[path]
            }
            self._eval_meta["go_module_test_graph_added_modules"] = preflight_added
            if preflight_changed or preflight_invalid_added:
                detail = {
                    "changed_existing": preflight_changed,
                    "invalid_added": preflight_invalid_added,
                }
                error = (
                    "GT-test module seed would change an existing submitted MVS "
                    "selection or use a dependency not pinned by this milestone "
                    f"END graph: {json.dumps(detail, sort_keys=True)}"
                )
                return self._use_submitted_go_graph_after_test_contract_error(
                    base_env=base_env,
                    workdir=workdir,
                    error=error,
                    unsafe_test_imports=seed_test_imports,
                )
        closure = self._go_exec(
            f"go list -mod=mod -modfile={temp_mod} -test -deps ./...",
            workdir=workdir,
            env=base_env,
        )
        closure_output = "\n".join(
            part for part in (closure.stdout, closure.stderr) if part
        )
        if closure.returncode != 0 and self._go_offline_cache_miss(closure_output):
            # GOPROXY=off deliberately forbids even a lookup in an already
            # populated cache. Retry against the exact content-addressed union
            # cache as a local file proxy; Docker networking remains disabled.
            try:
                local_proxy = self._go_local_cache_proxy()
            except ValueError as exc:
                error = f"Invalid evaluator Go cache policy: {exc}"
                self._eval_meta["go_module_closure_error"] = error
                return False, error
            closure = self._go_exec(
                f"go list -mod=mod -modfile={temp_mod} -test -deps ./...",
                workdir=workdir,
                env={**base_env, "GOPROXY": local_proxy},
            )
            closure_output = "\n".join(
                part for part in (closure.stdout, closure.stderr) if part
            )
            self._eval_meta["go_test_local_proxy_used"] = True
        if closure.returncode != 0:
            kind = (
                "Vetted evaluator cache cannot satisfy the GT-test module graph"
                if self._eval_meta["go_test_local_proxy_used"]
                else "Evaluator could not construct the GT-test Go module graph"
            )
            error = f"{kind}:\n{closure_output[-4000:]}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error

        # The writable pass above is allowed to extend only the evaluator's
        # private modfile.  Re-resolve that exact graph read-only so later test
        # commands cannot be the first place a malformed or incomplete temp
        # graph is discovered.
        verified = self._go_exec(
            f"go list -mod=readonly -modfile={temp_mod} -test -deps ./...",
            workdir=workdir,
            env=base_env,
        )
        verified_output = "\n".join(
            part for part in (verified.stdout, verified.stderr) if part
        )
        if verified.returncode != 0:
            kind = (
                "Evaluator offline Go cache is incomplete after GT-test closure"
                if self._go_offline_cache_miss(verified_output)
                else "Evaluator GT-test Go module graph failed readonly verification"
            )
            error = f"{kind}:\n{verified_output[-4000:]}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error

        hash_ok, after_hash = self._hash_go_manifest_state(module_dirs)
        if not hash_ok:
            return False, f"Cannot re-hash submitted Go manifests: {after_hash}"
        self._eval_meta["go_module_manifest_sha256_after"] = after_hash
        if after_hash != before_hash:
            error = (
                "Go evaluation closure mutated submitted manifests "
                f"({before_hash} -> {after_hash})"
            )
            self._eval_meta["go_module_closure_error"] = error
            return False, error

        private_semantics_ok, private_semantics, private_semantics_error = (
            self._read_go_mod_semantics(
                workdir=workdir,
                env=base_env,
                modfile=temp_mod,
            )
        )
        if not private_semantics_ok:
            error = f"Cannot audit evaluator-private go.mod semantics: {private_semantics_error}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        if private_semantics != submitted_semantics:
            error = (
                "GT-test graph changed submitted go.mod contract directives "
                "(module/go/toolchain/replace/exclude/retract); evaluator will "
                "not grade with the repaired graph"
            )
            return self._use_submitted_go_graph_after_test_contract_error(
                base_env=base_env,
                workdir=workdir,
                error=error,
                unsafe_test_imports=seed_test_imports or missing_imports,
            )

        private_graph_ok, private_graph, private_graph_error = (
            self._read_go_module_graph(
                workdir=workdir,
                env=base_env,
                modfile=temp_mod,
            )
        )
        if not private_graph_ok:
            error = f"Cannot audit evaluator-private Go module graph: {private_graph_error}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        changed_existing = {
            path: {
                "submitted": submitted_graph[path],
                "private": private_graph.get(path),
            }
            for path in submitted_graph
            if private_graph.get(path) != submitted_graph[path]
        }
        added = sorted(set(private_graph) - set(submitted_graph))
        invalid_added = {
            path: {
                "private": private_graph[path],
                "milestone_end": end_graph.get(path),
            }
            for path in added
            if end_graph.get(path) != private_graph[path]
        }
        self._eval_meta["go_module_test_graph_added_modules"] = added

        private_sums_result = subprocess.run(
            [
                "docker", "exec", self.container_name, "sh", "-c",
                "sed '/^[[:space:]]*$/d' /tmp/evoclaw-evaluation.sum",
            ],
            capture_output=True,
            text=True,
        )
        if private_sums_result.returncode != 0:
            detail = (
                private_sums_result.stderr
                or private_sums_result.stdout
                or "cannot read private go.sum"
            ).strip()
            return False, f"Cannot audit evaluator-private go.sum: {detail}"
        private_sums = set(private_sums_result.stdout.splitlines())
        unpinned_sums = sorted((private_sums - submitted_sums) - end_sums)
        if changed_existing or invalid_added or unpinned_sums:
            detail = {
                "changed_existing": changed_existing,
                "invalid_added": invalid_added,
                "unpinned_sum_sample": unpinned_sums[:10],
            }
            error = (
                "GT-test module closure would change an existing submitted MVS "
                "selection or use a dependency not pinned by this milestone END "
                f"graph: {json.dumps(detail, sort_keys=True)}"
            )
            return self._use_submitted_go_graph_after_test_contract_error(
                base_env=base_env,
                workdir=workdir,
                error=error,
                unsafe_test_imports=seed_test_imports or missing_imports,
            )

        def differs(submitted: str, temporary: str) -> bool:
            delta = subprocess.run(
                [
                    "docker", "exec", "--workdir", workdir,
                    self.container_name, "bash", "-c",
                    # A repository is allowed to omit go.sum. Treat an absent
                    # submitted sum and an empty private sum as equivalent;
                    # otherwise report that evaluator tests extended it.
                    "if test -f \"$1\"; then cmp -s \"$1\" \"$2\"; "
                    "elif test ! -s \"$2\"; then exit 0; else exit 1; fi",
                    "evoclaw-go-modfile-diff", submitted, temporary,
                ],
                capture_output=True,
                text=True,
            )
            if delta.returncode not in (0, 1):
                detail = (delta.stderr or delta.stdout or "cmp failed").strip()
                raise RuntimeError(detail)
            return delta.returncode == 1

        try:
            mod_changed = differs("go.mod", "/tmp/evoclaw-evaluation.mod")
            sum_changed = differs("go.sum", "/tmp/evoclaw-evaluation.sum")
        except RuntimeError as exc:
            error = f"Cannot audit evaluator Go modfile delta: {exc}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        changed = mod_changed or sum_changed
        self._eval_meta["go_module_test_mod_changed"] = mod_changed
        self._eval_meta["go_module_sum_changed"] = sum_changed
        graph_ok, graph_hash = self._hash_go_test_graph()
        if not graph_ok:
            error = f"Cannot hash evaluator-private Go test graph: {graph_hash}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        protected = subprocess.run(
            [
                "docker", "exec", self.container_name,
                "chmod", "0444",
                "/tmp/evoclaw-evaluation.mod",
                "/tmp/evoclaw-evaluation.sum",
            ],
            capture_output=True,
            text=True,
        )
        if protected.returncode != 0:
            detail = (protected.stderr or protected.stdout or "chmod failed").strip()
            error = f"Cannot make evaluator Go test graph read-only: {detail}"
            self._eval_meta["go_module_closure_error"] = error
            return False, error
        self._eval_meta["go_test_graph_sha256_before"] = graph_hash
        self._eval_meta["go_test_graph_sha256_after"] = graph_hash
        self._eval_meta["go_module_closure_applied"] = True
        self._go_exec_env = {
            **base_env,
            "GOFLAGS": f"-buildvcs=false -mod=readonly -modfile={temp_mod}",
        }
        print(
            "🐹 Go module gate: agent production manifests are readonly-valid; "
            f"GT test overlay={'changed' if changed else 'unchanged'}"
        )
        return True, ""

    def _run_post_snapshot_script(self) -> Tuple[bool, str]:
        """Run a host-owned, config-pinned evaluation closure script.

        The script lives outside the agent snapshot and is copied read-only from
        the benchmark workspace.  Unsafe/missing paths and non-zero exits fail
        closed; its identity is persisted in evaluation_result.json.
        """
        configured = self.repo_config.get("evaluation_post_snapshot_script")
        if configured is None:
            return True, ""
        if not isinstance(configured, str) or not configured.strip():
            return False, "evaluation_post_snapshot_script must be a non-empty relative path"

        relative = Path(configured)
        workspace = self.workspace_root.resolve()
        script = (workspace / relative).resolve()
        try:
            script.relative_to(workspace)
        except ValueError:
            return False, "evaluation_post_snapshot_script escapes workspace_root"
        if relative.is_absolute() or not script.is_file():
            return False, f"evaluation_post_snapshot_script is missing or unsafe: {configured}"

        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        self._eval_meta["post_snapshot_script"] = configured
        self._eval_meta["post_snapshot_script_sha256"] = digest
        destination = "/tmp/evaluation-post-snapshot.sh"
        copied = subprocess.run(
            ["docker", "cp", str(script), f"{self.container_name}:{destination}"],
            capture_output=True,
            text=True,
        )
        if copied.returncode != 0:
            return False, f"failed to copy evaluation post-snapshot script: {copied.stderr}"
        applied = subprocess.run(
            ["docker", "exec", self.container_name, "bash", destination],
            capture_output=True,
            text=True,
        )
        if applied.returncode != 0:
            output = "\n".join(part for part in (applied.stdout, applied.stderr) if part)
            return False, f"evaluation post-snapshot script failed (exit {applied.returncode}):\n{output}"
        self._eval_meta["post_snapshot_script_applied"] = True
        print(f"🔧 Applied evaluation closure script {configured} (sha256={digest[:12]}…)")
        return True, ""

    def _apply_tar_to_container(
        self,
        base_suffix: str = "end",
        gt_test_suffix: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Extract tar archive to container and process Rust test regions.

        Args:
            base_suffix: Prepared tag used as the product/manifest base.
            gt_test_suffix: Prepared tag supplying authoritative tests. Defaults
                to ``base_suffix``; START compilation fallback passes ``end``.

        Returns:
            Tuple of (success, error_message)
        """
        gt_test_suffix = gt_test_suffix or base_suffix

        # Validate the sidecar before mutating the evaluator container. Missing,
        # stale, or malformed manifest semantics are an infrastructure error, not
        # a reason to fall back to an additive overlay.
        try:
            self._load_and_validate_snapshot_metadata()
        except RuntimeError as exc:
            return False, str(exc)

        # Copy tar to container
        copy_cmd = ["docker", "cp", str(self.patch_file), f"{self.container_name}:/testbed/snapshot.tar"]
        result = subprocess.run(copy_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False, f"Failed to copy tar to container: {result.stderr}"

        # Extract tar archive (overwrites existing files). Keep the archive
        # until legacy apply_patches.sh hooks finish: those hooks may replace a
        # POM wholesale, after which we restore only authorized manifest
        # upserts and perform the three-way merge once.
        extract_cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-c",
            "cd /testbed && tar -xf snapshot.tar",
        ]
        result = subprocess.run(extract_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            return False, f"Failed to extract tar archive:\n{result.stderr}\n{result.stdout}"

        print(f"Successfully extracted tar archive: {self.patch_file.name}")

        # Residue prune (docs/residue-prune-spec.md): enforce source-file
        # authority before env patches re-create excluded files.
        prune_ok, prune_error = self._maybe_prune_residue(base_suffix=base_suffix)
        if not prune_ok:
            return False, prune_error

        # Run apply_patches.sh if it exists in the container (to reapply compilation fixes)
        patch_check_cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-c",
            "if test -x /usr/local/bin/apply_patches.sh; then /usr/local/bin/apply_patches.sh; fi",
        ]
        patch_result = subprocess.run(patch_check_cmd, capture_output=True, text=True)
        if patch_result.returncode != 0:
            output = "\n".join(part for part in (patch_result.stdout, patch_result.stderr) if part)
            return False, f"milestone environment patch hook failed (exit {patch_result.returncode}):\n{output}"
        if "patches applied" in patch_result.stdout.lower():
            print("🔧 Re-applied compilation patches after tar extraction")

        # Legacy hooks sometimes copy prepared POMs wholesale. Restore the
        # exact agent manifest files from the still-bound snapshot, then merge
        # evaluator-owned preprocessing onto them. Doing this before the hook
        # made the hook silently erase the merge.
        restore_ok, restore_error = self._restore_agent_manifest_upserts()
        if not restore_ok:
            return False, restore_error
        merge_ok, merge_error = self._merge_manifest_upserts(base_suffix=base_suffix)
        if not merge_ok:
            return False, merge_error

        # END (including evaluator-owned compilation patches) is the base of the
        # three-way overlay. A manifest the agent deleted must stay deleted even
        # if apply_patches.sh happens to recreate it.
        delete_ok, delete_error = self._apply_manifest_deletions()
        if not delete_ok:
            return False, delete_error

        projection_ok, projection_error = self._apply_exact_go_manifest_projection()
        if not projection_ok:
            return False, projection_error

        if gt_test_suffix != base_suffix:
            graft_ok, graft_error = self._graft_ground_truth_tests(gt_test_suffix)
            if not graft_ok:
                return False, graft_error

        closure_ok, closure_error = self._run_post_snapshot_script()
        if not closure_ok:
            return False, closure_error

        # Dependency state is sealed only after every evaluator-owned setup
        # hook has run. Compile/tests below receive the resulting private graph
        # read-only and are hash-audited before and after execution.
        go_ok, go_error = self._run_go_module_closure()
        if not go_ok:
            return False, go_error

        # For Rust projects: replace agent's inline tests with GT tests
        try:
            rust_files = get_rust_files_from_tar(self.patch_file)
        except Exception as exc:
            return False, f"Rust test filtering failed closed: {exc}"
        if rust_files:
            print(
                f"🦀 Processing {len(rust_files)} Rust files for test region replacement (using {gt_test_suffix} tag)..."
            )
            filter_result = process_rust_files_in_container(
                container_name=self.container_name,
                milestone_id=self.milestone_id,
                rust_files=rust_files,
                gt_tag_suffix=gt_test_suffix,
            )
            if filter_result["processed"] > 0 or filter_result["total_agent_tests_removed"] > 0:
                print(f"   Processed: {filter_result['processed']} files")
                print(f"   Agent test regions removed: {filter_result['total_agent_tests_removed']}")
                print(f"   GT test regions appended: {filter_result['total_gt_tests_appended']}")
            if filter_result["failed"] > 0:
                failures = [
                    f"{detail['file']}: {detail['reason']}"
                    for detail in filter_result["details"]
                    if not detail["success"] and not detail["skipped"]
                ]
                return False, (
                    "Rust test filtering failed closed for "
                    f"{filter_result['failed']} file(s): " + "; ".join(failures)
                )

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
            # L7: END checkout failed -> the working tree is whatever the image
            # ships (START); prune/graft must use the START base, not END.
            print(f"⚠️  Failed to checkout to END tag: {error}")
            print("   Falling back to current state (START tag)")
            self._eval_meta["base_tag"] = f"milestone-{self.milestone_id}-start"
            self._eval_meta["fallback_triggered"] = True
            return self._apply_tar_to_container(base_suffix="start", gt_test_suffix="end")

        self._eval_meta["base_tag"] = f"milestone-{self.milestone_id}-end"
        return self._apply_tar_to_container(base_suffix="end", gt_test_suffix="end")

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
        self._eval_meta["end_compile_error"] = ""
        self._eval_meta["start_compile_error"] = ""
        end_compile_error = ""
        success, error = self._checkout_to_tag("end")
        if not success:
            print(f"⚠️  Failed to checkout to END tag: {error}")
            print("   Falling back to current state (START tag)")
        else:
            # Apply agent's code on top of END tag, using END tag GT tests
            success, error = self._apply_tar_to_container(base_suffix="end", gt_test_suffix="end")
            if not success:
                return False, error

            # Check compilation
            compile_ok, compile_error = self._check_compilation()
            if compile_ok:
                print("✅ Code compiles successfully on END tag base")
                self._eval_meta["base_tag"] = f"milestone-{self.milestone_id}-end"
                return True, ""
            else:
                end_compile_error = compile_error
                self._eval_meta["end_compile_error"] = compile_error
                print(f"⚠️  Compilation failed on END tag base:")
                # Print first few lines of error
                for line in compile_error.split("\n")[:5]:
                    print(f"   {line}")

        # Step 2: Fallback to START tag
        print("\n📦 Fallback: Using START tag (baseline) as base")
        self._eval_meta["base_tag"] = f"milestone-{self.milestone_id}-start"
        self._eval_meta["fallback_triggered"] = True
        success, error = self._checkout_to_tag("start")
        if not success:
            return False, f"Failed to checkout to START tag: {error}"

        # Re-apply agent source on START while retaining the milestone's END
        # tests. Falling back to START tests would silently erase N2P coverage.
        success, error = self._apply_tar_to_container(base_suffix="start", gt_test_suffix="end")
        if not success:
            return False, error

        # Check compilation on START tag
        compile_ok, compile_error = self._check_compilation()
        if compile_ok:
            print("✅ Code compiles successfully on START tag base")
        else:
            print(f"⚠️  Compilation also failed on START tag base (agent code has errors)")
            self._eval_meta["start_compile_error"] = compile_error
            details = ["Agent code failed compilation on START tag base.", compile_error]
            if end_compile_error:
                details.extend(["Agent code also failed compilation on END tag base.", end_compile_error])
            diagnostic = "\n".join(part for part in details if part)
            self._eval_meta["build_failure_diagnostics"].append(diagnostic)
            if self._eval_meta.get("go_module_production_compile_error"):
                self._eval_meta["partial_test_universe"] = True

            if self.build_failure_fail_closed:
                # Strict mode: continuing can produce a tiny syntactically
                # valid report from only the packages/modules that compiled.
                return False, diagnostic

            print(
                "⚠️  Compatibility policy enabled: continuing to the test runner; "
                "completed package/module reports will be parsed and scored"
            )

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
            def __init__(self, container_name: str, exec_env: Dict[str, str]):
                self.container_name = container_name
                self.exec_env = exec_env

            def run(
                self,
                script: str,
                timeout: Optional[int] = None,
                extra_volumes: Optional[Dict[str, str]] = None,
            ) -> Tuple[int, str, str]:
                # extra_volumes is ignored: the container is started with /output mounted.
                package_file = self.exec_env.get("EVOCLAW_GO_TEST_PACKAGE_FILE", "")
                if package_file:
                    script, replacements = re.subn(
                        r"(?<!\S)\./\.\.\.(?!\S)",
                        '$(cat "$EVOCLAW_GO_TEST_PACKAGE_FILE")',
                        script,
                    )
                    if replacements == 0:
                        return (
                            2,
                            "",
                            "Safe Go package filtering was requested, but the test "
                            "command contains no standalone ./... pattern",
                        )
                env_args = [
                    item
                    for key, value in self.exec_env.items()
                    for item in ("-e", f"{key}={value}")
                ]
                cmd = [
                    "docker", "exec", *env_args, self.container_name,
                    "bash", "-c", script,
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                    return result.returncode, result.stdout, result.stderr
                except subprocess.TimeoutExpired:
                    return -1, "", f"Command timed out after {timeout} seconds"

        runner = _ExecRunner(self.container_name, self._go_exec_env)
        build_failure_diagnostics: List[str] = []
        pre_ok, pre_error = self._verify_go_evaluation_state(
            "compilation pre-test gate"
        )
        if not pre_ok:
            raise RuntimeError(pre_error)
        try:
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
                build_failure_fail_closed=self.build_failure_fail_closed,
                build_failure_diagnostics=build_failure_diagnostics,
            )
        finally:
            # Preserve deterministic compiler/setup evidence even when the
            # report parser finds zero runnable tests and raises.  The failure
            # path uses this evidence to emit a scored 0 instead of an
            # infrastructure-invalid result.
            if build_failure_diagnostics:
                self._eval_meta["partial_test_universe"] = True
                self._eval_meta["build_failure_diagnostics"].extend(
                    build_failure_diagnostics
                )
            post_ok, post_error = self._verify_go_evaluation_state(
                "test execution"
            )
            if not post_ok:
                raise RuntimeError(post_error)

        # Optional human-readable summary (useful for debugging)
        convert_to_summary(merged_path, self.output_dir / "eval_summary.json")
        with open(merged_path) as f:
            return json.load(f)

    def _scan_infrastructure_failure(self) -> str:
        """Scan raw eval output files (merged report + per-mode tee logs) for
        known infrastructure-failure signatures (F-2a)."""
        for path in sorted(self.output_dir.glob("eval*")):
            if not path.is_file():
                continue
            sig = _scan_file_for_infrastructure_failure(path)
            if sig:
                return sig
        return ""

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
        # Determine test framework for test ID normalization (Go fuzz/
        # parameterized tests with random subtest IDs). Robust: explicit config
        # wins, else infer from the milestone test_config, else fail loud if the
        # baseline clearly needs go_test normalization but we couldn't resolve it
        # (guards the 2026-07-12 config-missing incident).
        _bc = (
            baseline_classification.get("stable_classification")
            or baseline_classification.get("classification")
            or baseline_classification
        )
        _baseline_ids: Optional[List[str]] = None
        if isinstance(_bc, dict):
            _baseline_ids = []
            for _cat in ("none_to_pass", "fail_to_pass", "pass_to_pass"):
                for _t in _bc.get(_cat, []) or []:
                    _baseline_ids.append(_t if isinstance(_t, str) else _t.get("test_id", ""))
        test_framework = _resolve_test_framework(
            self.repo_config, self.workspace_root, self.milestone_id, _baseline_ids
        )
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
        test_outcomes, normalized_groups, java_moduleless_groups = _build_scoring_test_outcomes(
            summary_payload,
            framework=test_framework,
            normalizer=test_id_normalizer,
        )

        def lookup_outcome(test_id: str) -> str:
            return _lookup_scoring_outcome(
                test_id,
                framework=test_framework,
                outcomes=test_outcomes,
                normalized_groups=normalized_groups,
                java_moduleless_groups=java_moduleless_groups,
                normalizer=test_id_normalizer,
            )

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
            current_outcome = lookup_outcome(test_id)
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
            current_outcome = lookup_outcome(test_id)
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
            current_outcome = lookup_outcome(test_id)
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

        # Compatibility mode may still score completed packages, but a
        # submission that failed the exact production compile gate can never
        # be declared fully resolved merely because the private GT-test graph
        # happened to make every observed test pass.
        if (
            self._eval_meta.get("go_module_production_compile_error")
            or self._eval_meta.get("go_module_test_graph_contract_error")
        ):
            resolved = False

        # F1 fail-closed: when residue prune was ENABLED but its MECHANISM could
        # not run (config invalid, ls-tree failed, tar unreadable), the additive
        # overlay may still hold the GT solution. Such a cell must never count as
        # resolved. This is NOT the removed integrity gate — there is no
        # "snapshot looks suspicious -> skip+protect" path; a near-empty tar
        # prunes and scores honestly. Belt-and-suspenders: ls-tree/tar failures
        # already return an error before reaching here.
        skip_reason = self._eval_meta.get("residue_prune_skipped_reason", "")
        if skip_reason in FAIL_CLOSED_SKIP_REASONS and resolved:
            print(f"🚫 resolved forced False: residue prune mechanism failed ({skip_reason}, fail-closed)")
            resolved = False

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
            base_tag=self._eval_meta["base_tag"],
            fallback_triggered=self._eval_meta["fallback_triggered"],
            end_compile_error=self._eval_meta["end_compile_error"],
            start_compile_error=self._eval_meta["start_compile_error"],
            build_failure_fail_closed=self._eval_meta["build_failure_fail_closed"],
            partial_test_universe=self._eval_meta["partial_test_universe"],
            build_failure_diagnostics=self._eval_meta["build_failure_diagnostics"],
            residue_prune_enabled=self._eval_meta["residue_prune_enabled"],
            pruned_files_count=self._eval_meta["pruned_files_count"],
            pruned_files=self._eval_meta["pruned_files"],
            keep_list_hits=self._eval_meta["keep_list_hits"],
            snapshot_integrity_ok=self._eval_meta["snapshot_integrity_ok"],
            snapshot_missing_count=self._eval_meta["snapshot_missing_count"],
            residue_prune_skipped_reason=self._eval_meta["residue_prune_skipped_reason"],
            manifest_evaluator_base=self._eval_meta["manifest_evaluator_base"],
            manifest_evaluator_head=self._eval_meta["manifest_evaluator_head"],
            manifest_base_reason=self._eval_meta["manifest_base_reason"],
            manifest_merged_count=self._eval_meta["manifest_merged_count"],
            manifest_agent_exact_count=self._eval_meta["manifest_agent_exact_count"],
            manifest_agent_added_count=self._eval_meta["manifest_agent_added_count"],
            manifest_evaluator_missing_count=self._eval_meta["manifest_evaluator_missing_count"],
            manifest_conflict_files_count=self._eval_meta["manifest_conflict_files_count"],
            manifest_conflict_hunks_count=self._eval_meta["manifest_conflict_hunks_count"],
            manifest_agent_authoritative_paths=self._eval_meta[
                "manifest_agent_authoritative_paths"
            ],
            post_snapshot_script=self._eval_meta["post_snapshot_script"],
            post_snapshot_script_sha256=self._eval_meta["post_snapshot_script_sha256"],
            post_snapshot_script_applied=self._eval_meta["post_snapshot_script_applied"],
            gt_test_graft_suffix=self._eval_meta["gt_test_graft_suffix"],
            gt_test_graft_removed_count=self._eval_meta["gt_test_graft_removed_count"],
            gt_test_graft_restored_count=self._eval_meta["gt_test_graft_restored_count"],
            offline_cache_overlay_image=self._eval_meta["offline_cache_overlay_image"],
            offline_cache_milestone_image_id=self._eval_meta[
                "offline_cache_milestone_image_id"
            ],
            offline_cache_closure_image_id=self._eval_meta[
                "offline_cache_closure_image_id"
            ],
            offline_cache_effective_image_id=self._eval_meta[
                "offline_cache_effective_image_id"
            ],
            repo_config_binding_mode=self._eval_meta.get(
                "repo_config_binding_mode", "legacy-unbound"
            ),
            repo_config_sha256=self._eval_meta.get("repo_config_sha256", ""),
            runtime_policy_binding_mode=self._eval_meta.get(
                "runtime_policy_binding_mode", "legacy-live"
            ),
            runtime_policy_sha256=self._eval_meta.get(
                "runtime_policy_sha256", ""
            ),
            runtime_policy_mode=self._eval_meta.get("runtime_policy_mode", ""),
            snapshot_agent_image_id=self._eval_meta["snapshot_agent_image_id"],
            snapshot_agent_tag_commit=self._eval_meta["snapshot_agent_tag_commit"],
            go_toolchain_expected=self._eval_meta["go_toolchain_expected"],
            go_toolchain_actual=self._eval_meta["go_toolchain_actual"],
            go_toolchain_executable=self._eval_meta["go_toolchain_executable"],
            go_toolchain_goroot=self._eval_meta["go_toolchain_goroot"],
            go_module_closure_enabled=self._eval_meta["go_module_closure_enabled"],
            go_module_closure_applied=self._eval_meta["go_module_closure_applied"],
            go_module_production_compile_checked=self._eval_meta[
                "go_module_production_compile_checked"
            ],
            go_module_production_compile_error=self._eval_meta[
                "go_module_production_compile_error"
            ],
            go_module_test_graph_contract_error=self._eval_meta[
                "go_module_test_graph_contract_error"
            ],
            go_module_test_graph_added_modules=self._eval_meta[
                "go_module_test_graph_added_modules"
            ],
            go_partial_package_filter_applied=self._eval_meta[
                "go_partial_package_filter_applied"
            ],
            go_partial_package_filter_excluded=self._eval_meta[
                "go_partial_package_filter_excluded"
            ],
            go_partial_package_filter_included=self._eval_meta[
                "go_partial_package_filter_included"
            ],
            go_manifest_projection_complete=self._eval_meta[
                "go_manifest_projection_complete"
            ],
            go_manifest_projection_removed=self._eval_meta[
                "go_manifest_projection_removed"
            ],
            go_test_local_proxy_used=self._eval_meta["go_test_local_proxy_used"],
            go_module_test_mod_changed=self._eval_meta["go_module_test_mod_changed"],
            go_module_sum_changed=self._eval_meta["go_module_sum_changed"],
            go_module_manifest_sha256_before=self._eval_meta[
                "go_module_manifest_sha256_before"
            ],
            go_module_manifest_sha256_after=self._eval_meta[
                "go_module_manifest_sha256_after"
            ],
            go_test_graph_sha256_before=self._eval_meta[
                "go_test_graph_sha256_before"
            ],
            go_test_graph_sha256_after=self._eval_meta[
                "go_test_graph_sha256_after"
            ],
            go_module_closure_error=self._eval_meta["go_module_closure_error"],
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
            self._baseline_required_test_counts = baseline_required_test_counts(baseline)

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

            # 6. F-2a: scan raw eval output for known infrastructure-failure
            # signatures; a hit locks scoring_untrusted (fail-closed) and the
            # orchestrator turns it into a retryable error.
            infra_sig = self._scan_infrastructure_failure()
            if infra_sig:
                evaluation.infrastructure_failure = infra_sig
                evaluation.classify_zero_test_result()
                print(f"🚫 Infrastructure failure detected in test output: {infra_sig}")
            if evaluation.infra_invalid_reason:
                print("🚫 Infra-invalid: zero tests executed with required tests")

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
    parser.add_argument(
        "--repo-config",
        type=Path,
        help="Trial-frozen repo_config.yaml (must be paired with --repo-config-sha256)",
    )
    parser.add_argument(
        "--repo-config-sha256",
        help="Expected SHA256 of --repo-config",
    )
    parser.add_argument(
        "--runtime-policy",
        type=Path,
        help="Trial-frozen runtime_policy.yaml (requires SHA256 and mode)",
    )
    parser.add_argument(
        "--runtime-policy-sha256",
        help="Expected SHA256 of --runtime-policy",
    )
    parser.add_argument(
        "--runtime-policy-mode",
        choices=sorted(RUNTIME_POLICY_MODES),
        help="Frozen runtime policy mode",
    )
    build_failure_policy = parser.add_mutually_exclusive_group()
    build_failure_policy.add_argument(
        "--allow-partial-build-reports",
        dest="build_failure_fail_closed",
        action="store_false",
        help=(
            "When compilation/build setup fails but the test command completed, "
            "parse and score reports from packages/modules that did run. Runner "
            "timeouts and nonzero outer exits remain fail-closed."
        ),
    )
    build_failure_policy.add_argument(
        "--fail-closed-build-reports",
        dest="build_failure_fail_closed",
        action="store_true",
        help="Strict opt-in: reject partial reports after build/setup failures.",
    )
    parser.set_defaults(build_failure_fail_closed=False)

    args = parser.parse_args()

    # Validate required arguments
    required = ["workspace_root", "milestone_id", "patch_file", "baseline_classification"]
    missing = [arg for arg in required if not getattr(args, arg, None)]
    if missing:
        parser.error(f"Missing required arguments: {', '.join('--' + arg.replace('_', '-') for arg in missing)}")
    if (args.repo_config is None) != (args.repo_config_sha256 is None):
        parser.error("--repo-config and --repo-config-sha256 must be provided together")
    runtime_policy_args = (
        args.runtime_policy,
        args.runtime_policy_sha256,
        args.runtime_policy_mode,
    )
    if any(value is not None for value in runtime_policy_args) and not all(
        value is not None for value in runtime_policy_args
    ):
        parser.error(
            "--runtime-policy, --runtime-policy-sha256, and "
            "--runtime-policy-mode must be provided together"
        )

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
        build_failure_fail_closed=args.build_failure_fail_closed,
        repo_config_path=args.repo_config,
        repo_config_sha256=args.repo_config_sha256,
        runtime_policy_path=args.runtime_policy,
        runtime_policy_sha256=args.runtime_policy_sha256,
        runtime_policy_mode=args.runtime_policy_mode,
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
            fail_to_pass_required=evaluator._baseline_required_test_counts["fail_to_pass"],
            fail_to_pass_achieved=0,
            pass_to_pass_required=evaluator._baseline_required_test_counts["pass_to_pass"],
            none_to_pass_required=evaluator._baseline_required_test_counts["none_to_pass"],
            none_to_pass_achieved=0,
        )

        # Fail-loud fields must survive the failure path too (phase 1a): the
        # evaluator instance already holds the real prune/base facts for this
        # run — a defaulted residue_prune block on a failed cell reads as
        # "pruning never ran", which is exactly the silence 1a exists to end.
        meta = evaluator._eval_meta
        result.base_tag = meta["base_tag"]
        result.fallback_triggered = meta["fallback_triggered"]
        result.end_compile_error = meta.get("end_compile_error", "")
        result.start_compile_error = meta.get("start_compile_error", "")
        result.build_failure_fail_closed = meta.get("build_failure_fail_closed", False)
        result.partial_test_universe = meta.get("partial_test_universe", False)
        result.build_failure_diagnostics = meta.get("build_failure_diagnostics", [])
        result.residue_prune_enabled = meta["residue_prune_enabled"]
        result.pruned_files_count = meta["pruned_files_count"]
        result.pruned_files = meta["pruned_files"]
        result.keep_list_hits = meta["keep_list_hits"]
        result.snapshot_integrity_ok = meta["snapshot_integrity_ok"]
        result.snapshot_missing_count = meta["snapshot_missing_count"]
        result.residue_prune_skipped_reason = meta["residue_prune_skipped_reason"]
        result.manifest_evaluator_base = meta.get("manifest_evaluator_base", "")
        result.manifest_evaluator_head = meta.get("manifest_evaluator_head", "")
        result.manifest_base_reason = meta.get("manifest_base_reason", "")
        result.manifest_merged_count = meta.get("manifest_merged_count", 0)
        result.manifest_agent_exact_count = meta.get("manifest_agent_exact_count", 0)
        result.manifest_agent_added_count = meta.get("manifest_agent_added_count", 0)
        result.manifest_evaluator_missing_count = meta.get("manifest_evaluator_missing_count", 0)
        result.manifest_conflict_files_count = meta.get("manifest_conflict_files_count", 0)
        result.manifest_conflict_hunks_count = meta.get("manifest_conflict_hunks_count", 0)
        result.manifest_agent_authoritative_paths = meta.get(
            "manifest_agent_authoritative_paths", []
        )
        result.post_snapshot_script = meta.get("post_snapshot_script", "")
        result.post_snapshot_script_sha256 = meta.get("post_snapshot_script_sha256", "")
        result.post_snapshot_script_applied = meta.get("post_snapshot_script_applied", False)
        result.gt_test_graft_suffix = meta.get("gt_test_graft_suffix", "")
        result.gt_test_graft_removed_count = meta.get("gt_test_graft_removed_count", 0)
        result.gt_test_graft_restored_count = meta.get("gt_test_graft_restored_count", 0)
        result.offline_cache_overlay_image = meta.get("offline_cache_overlay_image", "")
        result.offline_cache_milestone_image_id = meta.get(
            "offline_cache_milestone_image_id", ""
        )
        result.offline_cache_closure_image_id = meta.get(
            "offline_cache_closure_image_id", ""
        )
        result.offline_cache_effective_image_id = meta.get(
            "offline_cache_effective_image_id", ""
        )
        result.repo_config_binding_mode = meta.get(
            "repo_config_binding_mode", "legacy-unbound"
        )
        result.repo_config_sha256 = meta.get("repo_config_sha256", "")
        result.runtime_policy_binding_mode = meta.get(
            "runtime_policy_binding_mode", "legacy-live"
        )
        result.runtime_policy_sha256 = meta.get("runtime_policy_sha256", "")
        result.runtime_policy_mode = meta.get("runtime_policy_mode", "")
        result.snapshot_agent_image_id = meta.get("snapshot_agent_image_id", "")
        result.snapshot_agent_tag_commit = meta.get("snapshot_agent_tag_commit", "")
        result.go_toolchain_expected = meta.get("go_toolchain_expected", "")
        result.go_toolchain_actual = meta.get("go_toolchain_actual", "")
        result.go_toolchain_executable = meta.get("go_toolchain_executable", "")
        result.go_toolchain_goroot = meta.get("go_toolchain_goroot", "")
        result.go_module_closure_enabled = meta.get("go_module_closure_enabled", False)
        result.go_module_closure_applied = meta.get("go_module_closure_applied", False)
        result.go_module_production_compile_checked = meta.get(
            "go_module_production_compile_checked", False
        )
        result.go_module_production_compile_error = meta.get(
            "go_module_production_compile_error", ""
        )
        result.go_module_test_graph_contract_error = meta.get(
            "go_module_test_graph_contract_error", ""
        )
        result.go_module_test_graph_added_modules = meta.get(
            "go_module_test_graph_added_modules", []
        )
        result.go_partial_package_filter_applied = meta.get(
            "go_partial_package_filter_applied", False
        )
        result.go_partial_package_filter_excluded = meta.get(
            "go_partial_package_filter_excluded", []
        )
        result.go_partial_package_filter_included = meta.get(
            "go_partial_package_filter_included", 0
        )
        result.go_manifest_projection_complete = meta.get(
            "go_manifest_projection_complete", False
        )
        result.go_manifest_projection_removed = meta.get(
            "go_manifest_projection_removed", []
        )
        result.go_test_local_proxy_used = meta.get("go_test_local_proxy_used", False)
        result.go_module_test_mod_changed = meta.get("go_module_test_mod_changed", False)
        result.go_module_sum_changed = meta.get("go_module_sum_changed", False)
        result.go_module_manifest_sha256_before = meta.get(
            "go_module_manifest_sha256_before", ""
        )
        result.go_module_manifest_sha256_after = meta.get(
            "go_module_manifest_sha256_after", ""
        )
        result.go_test_graph_sha256_before = meta.get(
            "go_test_graph_sha256_before", ""
        )
        result.go_test_graph_sha256_after = meta.get(
            "go_test_graph_sha256_after", ""
        )
        result.go_module_closure_error = meta.get("go_module_closure_error", "")
        # Fields used to classify zero-test failures are populated after the
        # dataclass constructor on this exception path, so refresh the verdict
        # before serializing the raw result.
        result.classify_zero_test_result()
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
