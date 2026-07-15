#!/usr/bin/env python3
"""Re-capture immutable trial snapshots from preserved agent submission tags.

This is a migration/forensics tool: it never overwrites the trial's original
evaluation artifacts. It rebuilds tar + integrity sidecar pairs in a separate
output directory using the current capture semantics and the exact baseline
commit persisted in evaluation/summary.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from harness.e2e.container_setup import inspect_docker_image_id
from harness.e2e.orchestrator import E2EOrchestrator
from harness.e2e.repo_config_binding import load_trial_repo_config_binding
from harness.e2e.runtime_policy_binding import (
    TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING,
    load_trial_runtime_policy_binding,
)
from harness.utils.snapshot import (
    ROOT_BUILD_FILES,
    expand_atomic_manifest_overlay,
    get_snapshot_paths,
)
from harness.utils.src_filter import SrcFileFilter


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def _build_capture(
    trial_root: Path,
    container: str,
    expected_agent_image_id: str | None,
) -> E2EOrchestrator:
    metadata = _load_json(trial_root / "trial_metadata.json")
    summary = _load_json(trial_root / "evaluation" / "summary.json")
    baseline = (summary.get("resume_state") or {}).get("snapshot_baseline_commit")
    if not isinstance(baseline, str) or not baseline.strip():
        raise RuntimeError("Trial summary has no persisted snapshot_baseline_commit")

    recorded_image_id = metadata.get("agent_image_id")
    if recorded_image_id is not None and not isinstance(recorded_image_id, str):
        raise RuntimeError("trial_metadata.agent_image_id must be a string")
    if recorded_image_id and expected_agent_image_id and recorded_image_id != expected_agent_image_id:
        raise RuntimeError(
            "Explicit agent image ID conflicts with trial metadata: "
            f"metadata={recorded_image_id}, explicit={expected_agent_image_id}"
        )
    expected_image_id = recorded_image_id or expected_agent_image_id
    if not expected_image_id:
        raise RuntimeError(
            "Legacy trial has no persisted agent_image_id; pass the exact "
            "--expected-agent-image-id recorded for the original container"
        )
    actual_image_id = inspect_docker_image_id(container, container=True)
    if actual_image_id != expected_image_id:
        raise RuntimeError(
            "Recapture container image does not match the trial-pinned agent image: "
            f"expected={expected_image_id}, actual={actual_image_id}"
        )

    capture = object.__new__(E2EOrchestrator)
    capture.container_name = container
    capture.repo_src_dirs = list(metadata.get("repo_src_dirs") or [])
    capture.src_filter = SrcFileFilter(
        src_dirs=capture.repo_src_dirs,
        test_dirs=list(metadata.get("test_dirs") or []),
        exclude_patterns=list(metadata.get("exclude_patterns") or []),
        generated_patterns=list(metadata.get("generated_patterns") or []),
        modifiable_test_patterns=list(
            metadata.get("modifiable_test_patterns") or []
        ),
    )
    capture._snapshot_baseline_commit = baseline.strip()
    capture._recapture_summary_results = summary.get("results") or {}
    capture.repo_config_binding = load_trial_repo_config_binding(
        trial_root,
        metadata,
        expected_repo_name=metadata.get("repo_name"),
    )
    metadata_schema = metadata.get("trial_metadata_schema_version", 1)
    if (
        "runtime_policy_binding" in metadata
        or (
            isinstance(metadata_schema, int)
            and not isinstance(metadata_schema, bool)
            and metadata_schema
            >= TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING
        )
    ):
        capture.runtime_policy_binding = load_trial_runtime_policy_binding(
            trial_root,
            metadata,
            expected_repo_name=metadata.get("repo_name"),
        )
    else:
        capture.runtime_policy_binding = None
    return capture


def _capture_one(
    capture: E2EOrchestrator,
    milestone_id: str,
    output_root: Path,
) -> Path:
    agent_tag = f"agent-impl-{milestone_id}"
    tag = capture._docker_exec_git(
        "rev-parse", "--verify", f"{agent_tag}^{{commit}}"
    )
    if tag.returncode != 0:
        detail = (tag.stderr or tag.stdout or "missing tag").strip()
        raise RuntimeError(f"Cannot resolve {agent_tag}: {detail}")
    tag_commit = tag.stdout.strip()
    expected_result = getattr(capture, "_recapture_summary_results", {}).get(
        milestone_id
    )
    expected_commit = (
        expected_result.get("tag_hash") if isinstance(expected_result, dict) else None
    )
    if not isinstance(expected_commit, str) or not expected_commit.strip():
        raise RuntimeError(
            f"Trial summary has no submitted tag_hash for {milestone_id}; "
            "refusing an unbound recapture"
        )
    if tag_commit != expected_commit.strip():
        raise RuntimeError(
            f"Preserved {agent_tag} no longer matches trial summary: "
            f"tag={tag_commit}, summary={expected_commit.strip()}"
        )

    result_dir = output_root / milestone_id
    result_dir.mkdir(parents=True, exist_ok=False)
    snapshot = result_dir / "source_snapshot.tar"

    existing_roots = capture._get_existing_root_files_in_git(
        tag_commit, ROOT_BUILD_FILES
    )
    overlay = expand_atomic_manifest_overlay(
        capture._get_build_manifest_overlay_in_git(tag_commit),
        capture._get_existing_build_manifests_in_git(tag_commit),
        capture.repo_src_dirs,
    )
    manifests = set(overlay.upserts)
    existing_dirs = capture._get_existing_src_dirs_in_git(
        tag_commit, capture.repo_src_dirs
    )
    if capture.repo_src_dirs and not existing_dirs:
        raise RuntimeError(f"No configured source roots exist at {agent_tag}")
    snapshot_paths = get_snapshot_paths(
        capture.repo_src_dirs,
        existing_root_files=existing_roots,
        existing_src_dirs=existing_dirs,
        extra_build_manifests=manifests,
    )
    if not snapshot_paths:
        raise RuntimeError(f"Snapshot path discovery is empty for {agent_tag}")

    command = [
        "docker", "exec", "--user", "fakeroot",
        "-e", "HOME=/home/fakeroot", "-w", "/testbed",
        capture.container_name,
        "git", "archive", "--format=tar", tag_commit,
        *snapshot_paths,
    ]
    with snapshot.open("wb") as stream:
        archived = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE)
    if archived.returncode != 0:
        snapshot.unlink(missing_ok=True)
        detail = archived.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git archive failed for {agent_tag}: {detail}")

    capture._filter_tar_archive(snapshot, extra_build_manifests=manifests)
    capture._check_snapshot_capture_integrity(
        agent_tag,
        snapshot,
        snapshot_paths,
        overlay,
        tag_commit,
    )
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-root", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--milestone", action="append", default=[])
    parser.add_argument(
        "--expected-agent-image-id",
        help=(
            "Required for legacy trials without trial_metadata.agent_image_id; "
            "must be the original container's full 64-hex Docker image ID"
        ),
    )
    args = parser.parse_args()

    trial_root = args.trial_root.resolve()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise RuntimeError(f"Output root already exists: {output_root}")
    output_root.mkdir(parents=True)

    capture = _build_capture(
        trial_root,
        args.container,
        args.expected_agent_image_id,
    )
    if args.milestone:
        milestones = args.milestone
    else:
        milestones = sorted(
            path.name
            for path in (trial_root / "evaluation").iterdir()
            if path.is_dir() and (path / "source_snapshot.tar").is_file()
        )
    if not milestones:
        raise RuntimeError("No milestone snapshots found")

    for milestone_id in milestones:
        snapshot = _capture_one(capture, milestone_id, output_root)
        print(f"{milestone_id}\t{snapshot}")
    print(f"Re-captured {len(milestones)} snapshot(s) under {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
