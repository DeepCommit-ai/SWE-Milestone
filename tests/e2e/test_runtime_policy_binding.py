from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path

import pytest
import yaml

from harness.e2e.quarantine import quarantine_env_from_config
from harness.e2e.container_setup import ContainerSetup
from harness.e2e.run_e2e import _activate_runtime_policy
from harness.e2e.runtime_policy_binding import (
    EMPTY_RUNTIME_POLICY_BYTES,
    RUNTIME_POLICY_BINDING_SCHEMA_VERSION,
    RUNTIME_POLICY_ENV_KEYS,
    RUNTIME_POLICY_MODE_ABSENT,
    RUNTIME_POLICY_MODE_PROTECTED,
    RUNTIME_POLICY_MODE_UNPROTECTED,
    TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING,
    TRIAL_RUNTIME_POLICY_FILENAME,
    RuntimePolicyBindingError,
    derive_runtime_policy_env,
    freeze_runtime_policy,
    load_bound_runtime_policy,
    load_trial_runtime_policy_binding,
    resolve_runtime_policy,
    runtime_policy_coverage_errors,
    runtime_policy_subprocess_env,
    verify_expected_runtime_policy,
)


REPO = "example_repo_v1_v2"
POLICY_BYTES = b"""# exact source bytes are part of the binding
ecosystem: [go, pip, maven]
deny_domains: [proxy.golang.org, repo1.maven.org]
deny_cidrs: [104.16.0.0/12]
firewall_exempt_domains: [proxy.golang.org]
go_offline: true
maven_offline: true
maven_repo_local: /root/.m2/repository
cache_forbid_globs: [/go/pkg/mod/example/*]
verify_fetch_urls: [https://proxy.golang.org/example/@v/v2.zip]
closure:
  cache_paths: [/go/pkg/mod/cache/download, /root/.m2/repository]
  toolchain: {go: 1.21.13, gotoolchain_local: true}
  maven_plugin_probes:
    - {pom: bom/pom.xml, goal: help:effective-pom, timeout_seconds: 90}
"""


def _policy_path(project_root: Path, repo: str = REPO) -> Path:
    path = project_root / "quarantine_configs" / f"{repo}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolved(project_root: Path, raw: bytes = POLICY_BYTES):
    _policy_path(project_root).write_bytes(raw)
    return resolve_runtime_policy(REPO, project_root)


def _frozen(tmp_path: Path):
    project_root = tmp_path / "project"
    trial_root = tmp_path / "trial"
    resolved = _resolved(project_root)
    binding = freeze_runtime_policy(trial_root, resolved)
    metadata = {"runtime_policy_binding": binding.to_metadata(trial_root)}
    return project_root, trial_root, binding, metadata


def test_schema_constants_are_distinct_and_current() -> None:
    assert RUNTIME_POLICY_BINDING_SCHEMA_VERSION == 1
    assert TRIAL_METADATA_SCHEMA_VERSION_WITH_RUNTIME_POLICY_BINDING == 3


def test_resolve_protected_reads_exact_bytes_and_uses_shared_derivation(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    resolved = _resolved(project_root)

    assert resolved.mode == RUNTIME_POLICY_MODE_PROTECTED
    assert resolved.raw_bytes == POLICY_BYTES
    assert resolved.source_path == _policy_path(project_root).resolve()
    assert resolved.policy == yaml.safe_load(POLICY_BYTES)
    assert resolved.effective_policy == resolved.policy
    assert resolved.env == quarantine_env_from_config(REPO, yaml.safe_load(POLICY_BYTES))
    assert set(resolved.env) <= RUNTIME_POLICY_ENV_KEYS
    assert resolved.env["SWE_MILESTONE_GO_TOOLCHAIN"] == "1.21.13"
    assert resolved.env["SWE_MILESTONE_CACHE_PATHS"] == (
        '["/go/pkg/mod/cache/download","/root/.m2/repository","/wheelhouse"]'
    )


def test_frozen_mapping_derivation_does_not_reread_live_yaml(tmp_path: Path) -> None:
    project_root, _, binding, _ = _frozen(tmp_path)
    original_env = binding.env
    _policy_path(project_root).write_text(
        "ecosystem: [npm]\nnpm_offline: true\n", encoding="utf-8"
    )

    assert binding.raw_bytes == POLICY_BYTES
    assert binding.env == original_env
    assert binding.env["SWE_MILESTONE_GO_OFFLINE"] == "1"
    assert "SWE_MILESTONE_NPM_OFFLINE" not in binding.env


def test_coverage_uses_resolved_mapping_after_live_policy_changes(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    raw = b"ecosystem: [none]\n"
    resolved = _resolved(project_root, raw)
    _policy_path(project_root).write_text(
        "ecosystem: [go]\ngo_offline: false\n", encoding="utf-8"
    )

    assert runtime_policy_coverage_errors(resolved) == []


def test_resume_binding_coverage_ignores_live_policy_changes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    trial_root = tmp_path / "trial"
    resolved = _resolved(project_root, b"ecosystem: [none]\n")
    binding = freeze_runtime_policy(trial_root, resolved)
    metadata = {"runtime_policy_binding": binding.to_metadata(trial_root)}
    _policy_path(project_root).write_text(
        "ecosystem: [go]\ngo_offline: false\n", encoding="utf-8"
    )

    resumed = load_trial_runtime_policy_binding(
        trial_root,
        metadata,
        expected_repo_name=REPO,
    )

    assert resumed.raw_bytes == b"ecosystem: [none]\n"
    assert runtime_policy_coverage_errors(resumed) == []


def test_parent_worker_policy_change_is_fail_closed(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    parent = _resolved(project_root, b"ecosystem: [none]\n")
    _policy_path(project_root).write_text(
        "ecosystem: [npm]\nnpm_offline: true\n", encoding="utf-8"
    )
    worker = resolve_runtime_policy(REPO, project_root)

    with pytest.raises(RuntimePolicyBindingError, match="changed between launcher and worker"):
        verify_expected_runtime_policy(
            worker,
            expected_sha256=parent.sha256,
            expected_mode=parent.mode,
        )


def test_expected_policy_identity_requires_complete_pair(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path / "project", b"ecosystem: [none]\n")
    with pytest.raises(RuntimePolicyBindingError, match="provided together"):
        verify_expected_runtime_policy(
            resolved,
            expected_sha256=resolved.sha256,
        )


def test_activation_replaces_partial_or_stale_managed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, binding, _ = _frozen(tmp_path)
    for key in RUNTIME_POLICY_ENV_KEYS:
        monkeypatch.setenv(key, "stale")
    monkeypatch.setenv("SWE_MILESTONE_UNPROTECTED", "1")

    _activate_runtime_policy(binding)

    assert {
        key: value
        for key, value in os.environ.items()
        if key in RUNTIME_POLICY_ENV_KEYS
    } == binding.env
    assert "SWE_MILESTONE_UNPROTECTED" not in os.environ


def test_activation_preserves_explicit_unprotected_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    trial_root = tmp_path / "trial"
    _policy_path(project_root).write_bytes(POLICY_BYTES)
    binding = freeze_runtime_policy(
        trial_root,
        resolve_runtime_policy(REPO, project_root, unprotected=True),
    )
    for key in RUNTIME_POLICY_ENV_KEYS:
        monkeypatch.setenv(key, "stale")

    _activate_runtime_policy(binding)

    assert not any(key in os.environ for key in RUNTIME_POLICY_ENV_KEYS)
    assert os.environ["SWE_MILESTONE_UNPROTECTED"] == "1"
    assert binding.effective_policy is None


def test_subprocess_env_replaces_inherited_policy_and_preserves_unprotected(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _policy_path(project_root).write_bytes(POLICY_BYTES)
    unprotected = resolve_runtime_policy(REPO, project_root, unprotected=True)
    inherited = {
        "PATH": "/bin",
        "SWE_MILESTONE_QUARANTINE": "1",
        "SWE_MILESTONE_GO_OFFLINE": "1",
        "SWE_MILESTONE_GO_TOOLCHAIN": "stale",
    }

    worker = runtime_policy_subprocess_env(unprotected, inherited)
    resumed = runtime_policy_subprocess_env(None, inherited)

    assert worker == {"PATH": "/bin", "SWE_MILESTONE_UNPROTECTED": "1"}
    assert resumed == {"PATH": "/bin"}


def test_container_setup_rejects_process_env_drift_from_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, binding, _ = _frozen(tmp_path)
    for key in RUNTIME_POLICY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SWE_MILESTONE_UNPROTECTED", raising=False)
    _activate_runtime_policy(binding)
    monkeypatch.setenv("UNIFIED_API_KEY", "test-key")
    monkeypatch.setenv("UNIFIED_BASE_URL", "https://example.invalid")
    setup = ContainerSetup(
        container_name="binding-test",
        image_name="example/image:v1",
        repo_name=REPO,
        runtime_policy_binding=binding,
    )
    setup._verify_bound_runtime_policy_env()

    monkeypatch.setenv("SWE_MILESTONE_GO_TOOLCHAIN", "9.9.9")
    with pytest.raises(RuntimePolicyBindingError, match="environment drifted"):
        setup._verify_bound_runtime_policy_env()


def test_absent_policy_has_explicit_canonical_binding(tmp_path: Path) -> None:
    resolved = resolve_runtime_policy(REPO, tmp_path / "project")

    assert resolved.mode == RUNTIME_POLICY_MODE_ABSENT
    assert resolved.raw_bytes == EMPTY_RUNTIME_POLICY_BYTES
    assert resolved.policy == {}
    assert resolved.source_path is None
    assert resolved.env == {}
    assert resolved.effective_policy is None

    trial_root = tmp_path / "trial"
    binding = freeze_runtime_policy(trial_root, resolved)
    metadata = {"runtime_policy_binding": binding.to_metadata(trial_root)}
    loaded = load_trial_runtime_policy_binding(
        trial_root, metadata, expected_repo_name=REPO
    )
    assert loaded.mode == RUNTIME_POLICY_MODE_ABSENT
    assert loaded.env == {}


@pytest.mark.parametrize("policy_exists", [False, True])
def test_explicit_unprotected_is_distinct_and_derives_no_env(
    tmp_path: Path, policy_exists: bool
) -> None:
    project_root = tmp_path / "project"
    if policy_exists:
        _policy_path(project_root).write_bytes(POLICY_BYTES)

    resolved = resolve_runtime_policy(REPO, project_root, unprotected=True)

    assert resolved.mode == RUNTIME_POLICY_MODE_UNPROTECTED
    assert resolved.env == {}
    assert resolved.effective_policy is None
    if policy_exists:
        assert resolved.raw_bytes == POLICY_BYTES
        assert resolved.source_path == _policy_path(project_root).resolve()
    else:
        assert resolved.raw_bytes == EMPTY_RUNTIME_POLICY_BYTES
        assert resolved.source_path is None


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (b"key: [unterminated\n", "invalid runtime policy YAML"),
        (b"- one\n- two\n", "must contain a YAML mapping"),
    ],
)
def test_resolve_rejects_invalid_policy_yaml(
    tmp_path: Path, raw: bytes, match: str
) -> None:
    _policy_path(tmp_path).write_bytes(raw)
    with pytest.raises(RuntimePolicyBindingError, match=match):
        resolve_runtime_policy(REPO, tmp_path)


def test_resolve_rejects_dangling_policy_symlink(tmp_path: Path) -> None:
    source = _policy_path(tmp_path)
    source.symlink_to(tmp_path / "missing.yaml")

    with pytest.raises(RuntimePolicyBindingError, match="cannot read runtime policy"):
        resolve_runtime_policy(REPO, tmp_path)


def test_freeze_and_trial_metadata_round_trip(tmp_path: Path) -> None:
    _, trial_root, binding, metadata = _frozen(tmp_path)

    raw_metadata = metadata["runtime_policy_binding"]
    assert raw_metadata == {
        "schema_version": 1,
        "repo_name": REPO,
        "sha256": hashlib.sha256(POLICY_BYTES).hexdigest(),
        "mode": "protected",
        "path": TRIAL_RUNTIME_POLICY_FILENAME,
        "source_path": str(binding.source_path),
    }

    loaded = load_trial_runtime_policy_binding(
        trial_root, metadata, expected_repo_name=REPO
    )
    assert loaded.path == binding.path
    assert loaded.raw_bytes == POLICY_BYTES
    assert loaded.policy == binding.policy
    assert loaded.identity == binding.identity
    assert loaded.env == binding.env
    assert loaded.effective_policy == binding.policy


def test_freeze_reuses_identical_bytes_but_never_overwrites_drift(
    tmp_path: Path,
) -> None:
    project_root, trial_root, binding, _ = _frozen(tmp_path)
    same = freeze_runtime_policy(trial_root, resolve_runtime_policy(REPO, project_root))
    assert same.identity == binding.identity

    _policy_path(project_root).write_text("ecosystem: [npm]\n", encoding="utf-8")
    changed = resolve_runtime_policy(REPO, project_root)
    with pytest.raises(RuntimePolicyBindingError, match="refusing to overwrite"):
        freeze_runtime_policy(trial_root, changed)
    assert binding.path.read_bytes() == POLICY_BYTES


def test_load_bound_runtime_policy_round_trip(tmp_path: Path) -> None:
    _, _, binding, _ = _frozen(tmp_path)
    loaded = load_bound_runtime_policy(
        REPO, binding.path, binding.sha256, binding.mode
    )
    assert loaded.raw_bytes == POLICY_BYTES
    assert loaded.policy == binding.policy
    assert loaded.identity == binding.identity
    assert loaded.env == binding.env


def test_load_bound_rejects_hash_and_yaml_drift(tmp_path: Path) -> None:
    path = tmp_path / "runtime_policy.yaml"
    path.write_bytes(POLICY_BYTES)

    with pytest.raises(RuntimePolicyBindingError, match="digest mismatch"):
        load_bound_runtime_policy(REPO, path, "0" * 64, "protected")

    invalid = b"key: [unterminated\n"
    path.write_bytes(invalid)
    digest = hashlib.sha256(invalid).hexdigest()
    with pytest.raises(RuntimePolicyBindingError, match="invalid runtime policy YAML"):
        load_bound_runtime_policy(REPO, path, digest, "protected")


def test_load_bound_rejects_any_symlink_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "runtime_policy.yaml"
    target.write_bytes(POLICY_BYTES)
    digest = hashlib.sha256(POLICY_BYTES).hexdigest()

    final_link = tmp_path / "final-link.yaml"
    final_link.symlink_to(target)
    with pytest.raises(RuntimePolicyBindingError, match="contains a symlink"):
        load_bound_runtime_policy(REPO, final_link, digest, "protected")

    parent_link = tmp_path / "linked-dir"
    parent_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimePolicyBindingError, match="contains a symlink"):
        load_bound_runtime_policy(
            REPO, parent_link / target.name, digest, "protected"
        )


@pytest.mark.parametrize(
    "bad_path",
    ["/absolute/runtime_policy.yaml", "../runtime_policy.yaml", "a/../b", "a\\b"],
)
def test_trial_loader_rejects_unsafe_relative_paths(
    tmp_path: Path, bad_path: str
) -> None:
    _, trial_root, _, metadata = _frozen(tmp_path)
    metadata["runtime_policy_binding"]["path"] = bad_path

    with pytest.raises(RuntimePolicyBindingError, match="path"):
        load_trial_runtime_policy_binding(trial_root, metadata)


def test_trial_loader_rejects_path_escape_through_parent_symlink(
    tmp_path: Path,
) -> None:
    _, trial_root, binding, metadata = _frozen(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "policy.yaml").write_bytes(POLICY_BYTES)
    (trial_root / "linked").symlink_to(outside, target_is_directory=True)
    metadata["runtime_policy_binding"]["path"] = "linked/policy.yaml"
    metadata["runtime_policy_binding"]["sha256"] = binding.sha256

    with pytest.raises(RuntimePolicyBindingError, match="contains a symlink"):
        load_trial_runtime_policy_binding(trial_root, metadata)


def test_trial_loader_rejects_tampered_frozen_file(tmp_path: Path) -> None:
    _, trial_root, binding, metadata = _frozen(tmp_path)
    binding.path.write_text("ecosystem: [npm]\n", encoding="utf-8")

    with pytest.raises(RuntimePolicyBindingError, match="digest mismatch"):
        load_trial_runtime_policy_binding(trial_root, metadata)


def test_trial_loader_rejects_invalid_yaml_even_with_matching_hash(
    tmp_path: Path,
) -> None:
    _, trial_root, binding, metadata = _frozen(tmp_path)
    invalid = b"key: [unterminated\n"
    binding.path.write_bytes(invalid)
    metadata["runtime_policy_binding"]["sha256"] = hashlib.sha256(
        invalid
    ).hexdigest()

    with pytest.raises(RuntimePolicyBindingError, match="invalid runtime policy YAML"):
        load_trial_runtime_policy_binding(trial_root, metadata)


@pytest.mark.parametrize("schema", [0, 2, True, None])
def test_trial_loader_rejects_binding_schema_drift(
    tmp_path: Path, schema: object
) -> None:
    _, trial_root, _, metadata = _frozen(tmp_path)
    metadata["runtime_policy_binding"]["schema_version"] = schema

    with pytest.raises(RuntimePolicyBindingError, match="schema_version"):
        load_trial_runtime_policy_binding(trial_root, metadata)


def test_trial_loader_rejects_repo_and_mode_drift(tmp_path: Path) -> None:
    _, trial_root, _, metadata = _frozen(tmp_path)
    with pytest.raises(RuntimePolicyBindingError, match="repo_name mismatch"):
        load_trial_runtime_policy_binding(
            trial_root, metadata, expected_repo_name="different_repo"
        )

    changed = deepcopy(metadata)
    changed["runtime_policy_binding"]["mode"] = "unknown"
    with pytest.raises(RuntimePolicyBindingError, match="invalid runtime policy mode"):
        load_trial_runtime_policy_binding(trial_root, changed)


@pytest.mark.parametrize(
    "metadata",
    [{}, {"runtime_policy_binding": None}, {"runtime_policy_binding": []}],
)
def test_trial_loader_requires_binding_object(
    tmp_path: Path, metadata: dict
) -> None:
    with pytest.raises(RuntimePolicyBindingError, match="runtime_policy_binding"):
        load_trial_runtime_policy_binding(tmp_path, metadata)


def test_trial_loader_rejects_noncanonical_absent_binding(tmp_path: Path) -> None:
    _, trial_root, _, metadata = _frozen(tmp_path)
    metadata["runtime_policy_binding"]["mode"] = RUNTIME_POLICY_MODE_ABSENT
    metadata["runtime_policy_binding"].pop("source_path")

    with pytest.raises(RuntimePolicyBindingError, match="absent.*canonical"):
        load_trial_runtime_policy_binding(trial_root, metadata)


def test_trial_loader_treats_source_path_as_optional_provenance(tmp_path: Path) -> None:
    _, trial_root, _, metadata = _frozen(tmp_path)
    metadata["runtime_policy_binding"].pop("source_path")

    loaded = load_trial_runtime_policy_binding(trial_root, metadata)
    assert loaded.mode == RUNTIME_POLICY_MODE_PROTECTED
    assert loaded.source_path is None


def test_invalid_maven_probe_raises_typed_error_not_system_exit() -> None:
    policy = {
        "ecosystem": ["maven"],
        "maven_offline": True,
        "closure": {"maven_plugin_probes": [{"pom": "../pom.xml", "goal": "x:y"}]},
    }
    with pytest.raises(RuntimePolicyBindingError, match="maven_plugin_probes"):
        derive_runtime_policy_env(policy)


def test_invalid_repo_names_fail_before_filesystem_access(tmp_path: Path) -> None:
    for repo_name in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
        with pytest.raises(RuntimePolicyBindingError, match="repo_name"):
            resolve_runtime_policy(repo_name, tmp_path)
