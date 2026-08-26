from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.e2e import run_milestone
from harness.e2e.runtime_policy_binding import (
    EMPTY_RUNTIME_POLICY_BYTES,
    RUNTIME_POLICY_MODE_ABSENT,
    RUNTIME_POLICY_MODE_PROTECTED,
    RUNTIME_POLICY_MODE_UNPROTECTED,
    ResolvedRuntimePolicy,
    RuntimePolicyBindingError,
    freeze_runtime_policy,
)


REPO = "owner_repo_v1_v2"


class _Noop:
    def __init__(self, *_args, **_kwargs):
        pass


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "data" / REPO
    workspace.mkdir(parents=True)
    (workspace / "metadata.json").write_text(
        json.dumps(
            {
                "repo_src_dirs": ["."],
                "test_dirs": ["tests/"],
                "exclude_patterns": [],
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _binding(output: Path, mode: str):
    if mode == RUNTIME_POLICY_MODE_ABSENT:
        resolved = ResolvedRuntimePolicy(
            repo_name=REPO,
            raw_bytes=EMPTY_RUNTIME_POLICY_BYTES,
            policy={},
            mode=mode,
            source_path=None,
        )
    else:
        raw = b"ecosystem: [none]\n"
        resolved = ResolvedRuntimePolicy(
            repo_name=REPO,
            raw_bytes=raw,
            policy={"ecosystem": ["none"]},
            mode=mode,
            source_path=output.parent / "source-policy.yaml",
        )
    return freeze_runtime_policy(output.parent, resolved)


def _patch_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_milestone, "ContainerSetup", _Noop)
    monkeypatch.setattr(run_milestone, "AgentRunner", _Noop)
    monkeypatch.setattr(run_milestone, "_activate_runtime_policy", lambda _binding: None)


def test_explicit_image_precedes_protected_policy_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    output = workspace / "mstone_trial" / "trial_001" / "M001"
    binding = _binding(output, RUNTIME_POLICY_MODE_PROTECTED)
    _patch_runners(monkeypatch)
    monkeypatch.setattr(
        run_milestone,
        "image_for_runtime_policy",
        lambda _policy: (_ for _ in ()).throw(AssertionError("must not select default")),
    )

    runner = run_milestone.MilestoneRunner(
        workspace_root=workspace,
        milestone_id="M001",
        srs_path=workspace / "SRS.md",
        output_dir=output,
        image_name="explicit:image",
        runtime_policy_binding=binding,
    )

    assert runner.image_name == "explicit:image"


def test_protected_policy_defaults_to_offline_closure_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    output = workspace / "mstone_trial" / "trial_001" / "M001"
    binding = _binding(output, RUNTIME_POLICY_MODE_PROTECTED)
    _patch_runners(monkeypatch)
    monkeypatch.setattr(
        run_milestone,
        "image_for_runtime_policy",
        lambda policy: f"offline:{policy.sha256[:8]}",
    )

    runner = run_milestone.MilestoneRunner(
        workspace_root=workspace,
        milestone_id="M001",
        srs_path=workspace / "SRS.md",
        output_dir=output,
        runtime_policy_binding=binding,
    )

    assert runner.image_name == f"offline:{binding.sha256[:8]}"


def test_explicit_unprotected_cannot_conflict_with_supplied_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    output = workspace / "mstone_trial" / "trial_001" / "M001"
    binding = _binding(output, RUNTIME_POLICY_MODE_PROTECTED)
    _patch_runners(monkeypatch)

    with pytest.raises(RuntimePolicyBindingError, match="conflicts"):
        run_milestone.MilestoneRunner(
            workspace_root=workspace,
            milestone_id="M001",
            srs_path=workspace / "SRS.md",
            output_dir=output,
            runtime_policy_binding=binding,
            unprotected=True,
        )


def test_invalid_protected_policy_is_rejected_before_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    output = workspace / "mstone_trial" / "trial_001" / "M001"
    raw = b"ecosystem: [go]\n"
    invalid = ResolvedRuntimePolicy(
        repo_name=REPO,
        raw_bytes=raw,
        policy={"ecosystem": ["go"]},
        mode=RUNTIME_POLICY_MODE_PROTECTED,
        source_path=tmp_path / "invalid-policy.yaml",
    )
    _patch_runners(monkeypatch)
    monkeypatch.setattr(
        run_milestone,
        "resolve_runtime_policy",
        lambda *_args, **_kwargs: invalid,
    )

    with pytest.raises(
        RuntimePolicyBindingError,
        match="failed quarantine coverage validation",
    ):
        run_milestone.MilestoneRunner(
            workspace_root=workspace,
            milestone_id="M001",
            srs_path=workspace / "SRS.md",
            output_dir=output,
        )

    assert not (output.parent / "runtime_policy.yaml").exists()


@pytest.mark.parametrize(
    "mode", [RUNTIME_POLICY_MODE_ABSENT, RUNTIME_POLICY_MODE_UNPROTECTED]
)
def test_disabled_policy_modes_keep_historical_milestone_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    workspace = _workspace(tmp_path)
    output = workspace / "mstone_trial" / "trial_001" / "M001"
    binding = _binding(output, mode)
    _patch_runners(monkeypatch)
    seen = []
    monkeypatch.setattr(
        run_milestone,
        "resolve_image",
        lambda base: seen.append(base) or "historical:image",
    )
    monkeypatch.setattr(
        run_milestone,
        "image_for_runtime_policy",
        lambda _policy: (_ for _ in ()).throw(AssertionError("offline image selected")),
    )

    runner = run_milestone.MilestoneRunner(
        workspace_root=workspace,
        milestone_id="M001",
        srs_path=workspace / "SRS.md",
        output_dir=output,
        runtime_policy_binding=binding,
    )

    assert runner.image_name == "historical:image"
    assert seen == [f"swe-milestone/{REPO.lower()}__m001"]
