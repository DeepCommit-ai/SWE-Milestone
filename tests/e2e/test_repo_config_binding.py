from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harness.e2e.evaluator import load_bound_repo_config
from harness.e2e.repo_config_binding import (
    EMPTY_REPO_CONFIG_BYTES,
    RepoConfigBindingError,
    RepoConfigIdentity,
    ResolvedRepoConfig,
    freeze_repo_config,
    load_trial_repo_config_binding,
    resolve_repo_config,
)
from harness.e2e import run_milestone
from harness.e2e.run_e2e import _resolve_trial_relative_path, load_workspace_metadata


REPO = "example_owner_repo_v1_v2"


def _write_config(root: Path, text: str, *, repo: str = REPO) -> Path:
    path = root / "config" / f"{repo}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _resolved(raw: bytes = b"evaluation_go_module_closure: true\n") -> ResolvedRepoConfig:
    return ResolvedRepoConfig(
        repo_name=REPO,
        raw_bytes=raw,
        config={"evaluation_go_module_closure": True},
        source_path=Path("/source/config.yaml"),
    )


def _metadata(binding, trial_root: Path, *, schema: int = 2) -> dict:
    return {
        "trial_metadata_schema_version": schema,
        "repo_name": REPO,
        "repo_config_binding": binding.to_metadata(trial_root),
    }


def test_resolve_prefers_data_root_and_preserves_exact_bytes(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / REPO
    workspace.mkdir(parents=True)
    project = tmp_path / "project"
    data_path = _write_config(data_root, "evaluation_go_module_closure: true\n")
    _write_config(project, "evaluation_go_module_closure: false\n")

    resolved = resolve_repo_config(REPO, workspace, project_root=project)

    assert resolved.source_path == data_path.resolve()
    assert resolved.raw_bytes == b"evaluation_go_module_closure: true\n"
    assert resolved.config == {"evaluation_go_module_closure": True}
    assert resolved.sha256 == hashlib.sha256(resolved.raw_bytes).hexdigest()


def test_resolve_uses_project_fallback_and_explicit_empty_when_absent(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / REPO
    workspace.mkdir(parents=True)
    project = tmp_path / "project"
    project_path = _write_config(project, "test_framework: go\n")

    fallback = resolve_repo_config(REPO, workspace, project_root=project)
    assert fallback.source_path == project_path.resolve()
    assert fallback.config == {"test_framework": "go"}

    project_path.unlink()
    absent = resolve_repo_config(REPO, workspace, project_root=project)
    assert absent.source_path is None
    assert absent.raw_bytes == EMPTY_REPO_CONFIG_BYTES
    assert absent.config == {}


def test_residue_repo_config_is_capture_authority(tmp_path: Path) -> None:
    workspace = tmp_path / REPO
    workspace.mkdir()
    (workspace / "metadata.json").write_text(
        json.dumps(
            {
                "repo_src_dirs": ["live-src"],
                "test_dirs": ["live-tests/**"],
                "exclude_patterns": ["live-excluded/**"],
                "generated_patterns": ["live-generated/**"],
            }
        )
    )
    repo_config = {
        "repo_src_dirs": ["dubbo-rpc"],
        "test_dirs": ["**/src/test/**"],
        "exclude": ["**/target/**"],
        "generated_patterns": ["**/generated/**"],
        "residue_prune": True,
        "prune_extensions": [".java"],
        "prune_keep_list": [],
    }

    loaded = load_workspace_metadata(workspace, repo_config=repo_config)

    assert loaded["repo_src_dirs"] == ["dubbo-rpc"]
    assert loaded["test_dirs"] == ["**/src/test/**"]
    assert loaded["exclude_patterns"] == ["**/target/**"]
    assert loaded["generated_patterns"] == ["**/generated/**"]
    assert loaded["modifiable_test_patterns"] == []


def test_single_milestone_runner_freezes_config_and_rejects_live_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / REPO
    workspace.mkdir(parents=True)
    (workspace / "metadata.json").write_text(
        json.dumps({"repo_src_dirs": ["."], "test_dirs": ["tests/"]})
    )
    live_config = _write_config(
        data_root,
        "evaluation_go_module_closure: true\n"
        "generated_patterns:\n"
        "  - '**/*.pb.go'\n",
    )
    output_dir = workspace / "mstone_trial" / "trial_001" / "M001"

    class _Noop:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(run_milestone, "ContainerSetup", _Noop)
    monkeypatch.setattr(run_milestone, "AgentRunner", _Noop)

    runner = run_milestone.MilestoneRunner(
        workspace_root=workspace,
        milestone_id="M001",
        srs_path=workspace / "SRS.md",
        output_dir=output_dir,
    )

    assert runner.repo_config_binding.path == output_dir.parent / "repo_config.yaml"
    assert runner.repo_config_binding.path.read_bytes() == live_config.read_bytes()
    assert runner.generated_patterns == ["**/*.pb.go"]

    live_config.write_text("evaluation_go_module_closure: false\n")
    with pytest.raises(RepoConfigBindingError, match="refusing to overwrite"):
        run_milestone.MilestoneRunner(
            workspace_root=workspace,
            milestone_id="M001",
            srs_path=workspace / "SRS.md",
            output_dir=output_dir,
        )


@pytest.mark.parametrize("value", ["../outside", "/absolute", "a/../../b", "a\\b"])
def test_resume_paths_reject_trial_root_escape(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        _resolve_trial_relative_path(tmp_path, value, field_name="snapshot_path")


def test_resume_paths_reject_parent_symlink_escape(tmp_path: Path) -> None:
    trial_root = tmp_path / "trial"
    outside = tmp_path / "outside"
    trial_root.mkdir()
    outside.mkdir()
    (trial_root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes trial root"):
        _resolve_trial_relative_path(
            trial_root,
            "link/result",
            field_name="result_dir",
        )


def test_resume_paths_accept_nested_trial_path(tmp_path: Path) -> None:
    expected = tmp_path / "evaluation" / "M001" / "source_snapshot.tar"
    assert _resolve_trial_relative_path(
        tmp_path,
        "evaluation/M001/source_snapshot.tar",
        field_name="snapshot_path",
    ) == expected


@pytest.mark.parametrize("content", ["[not, a, mapping]\n", "bad: [yaml\n"])
def test_first_existing_invalid_config_fails_without_fallback(
    tmp_path: Path, content: str
) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / REPO
    workspace.mkdir(parents=True)
    project = tmp_path / "project"
    _write_config(data_root, content)
    _write_config(project, "test_framework: go\n")

    with pytest.raises(RepoConfigBindingError):
        resolve_repo_config(REPO, workspace, project_root=project)


def test_dangling_high_priority_config_does_not_fall_back(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / REPO
    workspace.mkdir(parents=True)
    project = tmp_path / "project"
    data_path = data_root / "config" / f"{REPO}.yaml"
    data_path.parent.mkdir(parents=True)
    data_path.symlink_to(tmp_path / "missing.yaml")
    _write_config(project, "test_framework: go\n")

    with pytest.raises(RepoConfigBindingError, match="cannot read repository config"):
        resolve_repo_config(REPO, workspace, project_root=project)


@pytest.mark.parametrize("repo_name", ["", ".", "..", "a/b", "a\\b", "a\x00b"])
def test_resolve_rejects_unsafe_repo_name(tmp_path: Path, repo_name: str) -> None:
    with pytest.raises(RepoConfigBindingError):
        resolve_repo_config(repo_name, tmp_path, project_root=tmp_path)


def test_freeze_round_trip_and_sidecar_identity(tmp_path: Path) -> None:
    trial_root = tmp_path / "trial"
    resolved = _resolved()

    binding = freeze_repo_config(trial_root, resolved)
    metadata = _metadata(binding, trial_root)
    loaded = load_trial_repo_config_binding(
        trial_root, metadata, expected_repo_name=REPO
    )

    assert loaded is not None
    assert loaded.raw_bytes == resolved.raw_bytes
    assert loaded.config == resolved.config
    assert loaded.sha256 == resolved.sha256
    assert metadata["repo_config_binding"]["path"] == "repo_config.yaml"
    assert RepoConfigIdentity.from_mapping(binding.identity.to_dict()) == binding.identity
    assert binding.identity.to_dict() == {
        "schema_version": 1,
        "repo_name": REPO,
        "sha256": resolved.sha256,
    }


def test_freeze_reuses_identical_file_but_never_overwrites_drift(tmp_path: Path) -> None:
    trial_root = tmp_path / "trial"
    first = freeze_repo_config(trial_root, _resolved())
    second = freeze_repo_config(trial_root, _resolved())
    assert second.sha256 == first.sha256

    changed = _resolved(b"evaluation_go_module_closure: false\n")
    with pytest.raises(RepoConfigBindingError, match="refusing to overwrite"):
        freeze_repo_config(trial_root, changed)
    assert (trial_root / "repo_config.yaml").read_bytes() == first.raw_bytes


@pytest.mark.parametrize("action", ["mutate", "delete"])
def test_bound_trial_config_drift_fails_closed(tmp_path: Path, action: str) -> None:
    trial_root = tmp_path / "trial"
    binding = freeze_repo_config(trial_root, _resolved())
    metadata = _metadata(binding, trial_root)
    if action == "mutate":
        binding.path.write_text("evaluation_go_module_closure: false\n")
        expected = "digest mismatch"
    else:
        binding.path.unlink()
        expected = "missing or not a file"

    with pytest.raises(RepoConfigBindingError, match=expected):
        load_trial_repo_config_binding(trial_root, metadata)


def test_legacy_metadata_without_binding_returns_none(tmp_path: Path) -> None:
    assert load_trial_repo_config_binding(tmp_path, {}) is None
    assert (
        load_trial_repo_config_binding(
            tmp_path, {"trial_metadata_schema_version": 1, "repo_name": REPO}
        )
        is None
    )


def test_new_metadata_without_binding_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RepoConfigBindingError, match="requires repo_config_binding"):
        load_trial_repo_config_binding(
            tmp_path, {"trial_metadata_schema_version": 2, "repo_name": REPO}
        )


@pytest.mark.parametrize(
    "binding",
    [
        None,
        {},
        {"schema_version": 2, "repo_name": REPO, "sha256": "0" * 64, "path": "repo_config.yaml"},
        {"schema_version": 1, "repo_name": REPO, "sha256": "BAD", "path": "repo_config.yaml"},
        {"schema_version": 1, "repo_name": REPO, "sha256": "0" * 64, "path": "../repo_config.yaml"},
    ],
)
def test_declared_malformed_binding_never_downgrades_to_legacy(
    tmp_path: Path, binding: object
) -> None:
    with pytest.raises(RepoConfigBindingError):
        load_trial_repo_config_binding(
            tmp_path,
            {"trial_metadata_schema_version": 1, "repo_config_binding": binding},
        )


def test_binding_rejects_repo_name_mismatch(tmp_path: Path) -> None:
    binding = freeze_repo_config(tmp_path, _resolved())
    with pytest.raises(RepoConfigBindingError, match="repo_name mismatch"):
        load_trial_repo_config_binding(
            tmp_path,
            _metadata(binding, tmp_path),
            expected_repo_name="different_repo",
        )


def test_binding_rejects_symlink_even_when_target_bytes_match(tmp_path: Path) -> None:
    trial_root = tmp_path / "trial"
    trial_root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(_resolved().raw_bytes)
    (trial_root / "repo_config.yaml").symlink_to(outside)
    identity = _resolved().identity
    metadata = {
        "trial_metadata_schema_version": 2,
        "repo_config_binding": {
            **identity.to_dict(),
            "path": "repo_config.yaml",
        },
    }

    with pytest.raises(RepoConfigBindingError, match="must not be a symlink"):
        load_trial_repo_config_binding(trial_root, metadata)


def test_freeze_rejects_preexisting_symlink(tmp_path: Path) -> None:
    trial_root = tmp_path / "trial"
    trial_root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(_resolved().raw_bytes)
    (trial_root / "repo_config.yaml").symlink_to(outside)

    with pytest.raises(RepoConfigBindingError, match="must not be a symlink"):
        freeze_repo_config(trial_root, _resolved())


def test_to_metadata_rejects_binding_outside_trial_root(tmp_path: Path) -> None:
    binding = freeze_repo_config(tmp_path / "one", _resolved())
    with pytest.raises(RepoConfigBindingError, match="outside trial root"):
        binding.to_metadata(tmp_path / "two")


def test_evaluator_explicit_config_loader_verifies_digest_before_parsing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "repo_config.yaml"
    raw = b"evaluation_go_module_closure: true\n"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    assert load_bound_repo_config(REPO, path, digest) == {
        "evaluation_go_module_closure": True
    }

    path.write_text("evaluation_go_module_closure: false\n")
    with pytest.raises(RepoConfigBindingError, match="digest mismatch"):
        load_bound_repo_config(REPO, path, digest)


def test_evaluator_explicit_config_loader_rejects_half_or_unsafe_binding(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.yaml"
    target.write_text("{}\n")
    link = tmp_path / "repo_config.yaml"
    link.symlink_to(target)
    digest = hashlib.sha256(b"{}\n").hexdigest()

    with pytest.raises(RepoConfigBindingError, match="must not be a symlink"):
        load_bound_repo_config(REPO, link, digest)
