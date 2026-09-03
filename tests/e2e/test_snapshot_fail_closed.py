import json
import io
import hashlib
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

import harness.e2e.evaluator as evaluator_module
import harness.e2e.orchestrator as orchestrator_module
import harness.e2e.run_milestone as run_milestone_module
from harness.e2e.orchestrator import E2EOrchestrator, _run_evaluation_once
from harness.e2e.run_milestone import MilestoneRunner
from harness.e2e.evaluator import (
    OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION,
    EvaluationResult,
    InfrastructureFailureError,
    PatchEvaluator,
    _configured_go_toolchain_version,
    _render_offline_cache_overlay_dockerfile,
    _validated_cache_paths,
    ensure_internal_evaluation_network,
    ensure_offline_evaluation_image,
    load_bound_fallback_test_graft_policy,
)
from harness.e2e.repo_config_binding import RepoConfigIdentity
from harness.e2e.runtime_policy_binding import RuntimePolicyIdentity
from harness.utils.src_filter import SrcFileFilter
from harness.utils.snapshot import ManifestOverlay, make_snapshot_metadata


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _all_pass_result(**overrides):
    values = dict(
        milestone_id="M001",
        patch_is_None=False,
        patch_exists=True,
        patch_successfully_applied=True,
        resolved=True,
        fail_to_pass_success=["test"],
        fail_to_pass_failure=[],
        pass_to_pass_success_count=1,
        pass_to_pass_failure=[],
        pass_to_pass_missing=0,
        none_to_pass_success=[],
        none_to_pass_failure=[],
        total_tests=2,
        passed_tests=2,
        failed_tests=0,
        error_tests=0,
        skipped_tests=0,
        fail_to_pass_required=1,
        fail_to_pass_achieved=1,
        pass_to_pass_required=1,
        none_to_pass_required=0,
        none_to_pass_achieved=0,
    )
    values.update(overrides)
    return EvaluationResult(**values)


def _write_snapshot(tmp_path, members, overlay):
    snapshot = tmp_path / "source_snapshot.tar"
    with tarfile.open(snapshot, "w") as archive:
        for name, payload in members.items():
            data = payload.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    sidecar = tmp_path / "source_snapshot.integrity.json"
    sidecar.write_text(json.dumps(make_snapshot_metadata(
        tag="agent-impl-M001",
        snapshot_file=snapshot,
        manifest_overlay=overlay,
        extra={
            "ok": True,
            "capture_filter": {},
            "agent_base_image_id": "b" * 64,
            "agent_tag_commit": "c" * 40,
        },
    )))
    return snapshot, sidecar


def _bare_evaluator(snapshot):
    evaluator = object.__new__(PatchEvaluator)
    evaluator.patch_file = snapshot
    evaluator.milestone_id = "M001"
    evaluator._snapshot_metadata = None
    evaluator._manifest_overlay = None
    evaluator._go_manifest_inventory = None
    evaluator._eval_meta = {}
    evaluator.repo_config = {}
    evaluator._fallback_test_graft_policy_override = None
    evaluator.fallback_test_graft_policy_binding_mode = "absent-legacy"
    evaluator.fallback_test_graft_policy_sha256 = ""
    evaluator.repo_name = "example_owner_repo_v1_v2"
    evaluator.repo_config_binding_mode = "legacy-unbound"
    evaluator.repo_config_sha256 = ""
    evaluator.runtime_policy_binding_mode = "legacy-live"
    evaluator.runtime_policy_sha256 = ""
    evaluator.runtime_policy_mode = ""
    evaluator.quarantine_config = {
        "go_offline": True,
        "closure": {"cache_paths": ["/go/pkg/mod/cache/download"]},
    }
    evaluator.build_failure_fail_closed = False
    evaluator.container_name = "eval-container"
    return evaluator


def test_rust_filter_failure_aborts_snapshot_application(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator._load_and_validate_snapshot_metadata = lambda: ({}, None)
    evaluator._maybe_prune_residue = lambda **_kwargs: (True, "")
    evaluator._restore_agent_manifest_upserts = lambda: (True, "")
    evaluator._merge_manifest_upserts = lambda **_kwargs: (True, "")
    evaluator._apply_manifest_deletions = lambda: (True, "")
    evaluator._apply_exact_go_manifest_projection = lambda: (True, "")
    evaluator._run_post_snapshot_script = lambda **_kwargs: (True, "")
    evaluator._run_go_module_closure = lambda: (True, "")
    monkeypatch.setattr(
        evaluator_module.subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(),
    )
    monkeypatch.setattr(
        evaluator_module,
        "get_rust_files_from_tar",
        lambda _path: ["src/lib.rs"],
    )
    monkeypatch.setattr(
        evaluator_module,
        "process_rust_files_in_container",
        lambda **_kwargs: {
            "processed": 0,
            "skipped": 0,
            "failed": 1,
            "total_agent_tests_removed": 0,
            "total_gt_tests_appended": 0,
            "details": [
                {
                    "file": "src/lib.rs",
                    "success": False,
                    "skipped": False,
                    "reason": "synthetic detector failure",
                }
            ],
        },
    )

    ok, error = evaluator._apply_tar_to_container()

    assert ok is False
    assert "Rust test filtering failed closed" in error
    assert "synthetic detector failure" in error


def _bare_single_snapshot_runner(tmp_path, monkeypatch, tag_commits):
    """Create a single-milestone capture with observable Git references."""
    runner = object.__new__(MilestoneRunner)
    runner.milestone_id = "M001"
    runner.output_dir = tmp_path
    runner.container_name = "agent-container"
    runner.repo_src_dirs = ["core"]
    runner.src_filter = SrcFileFilter(src_dirs=["core"], test_dirs=[])

    observed = {
        "src": [],
        "root": [],
        "overlay": [],
        "manifest_inventory": [],
        "archive": [],
        "integrity": [],
    }

    def record(key, ref, value):
        observed[key].append(ref)
        return value

    runner._get_existing_src_dirs = lambda ref: record("src", ref, ["core"])
    runner._get_existing_root_files_in_git = (
        lambda ref, _files: record("root", ref, set())
    )
    runner._get_build_manifest_overlay_in_git = (
        lambda ref: record(
            "overlay", ref, ManifestOverlay.create("b" * 40)
        )
    )
    runner._get_existing_build_manifests_in_git = (
        lambda ref: record("manifest_inventory", ref, set())
    )
    runner._filter_tar_archive = lambda *_args, **_kwargs: 0

    remaining_commits = iter(tag_commits)

    def git(*args):
        if args and args[0] == "rev-parse":
            return _completed(stdout=f"{next(remaining_commits)}\n")
        if "ls-tree" in args:
            observed["integrity"].append(args[-1])
            return _completed(stdout="core/main.go\0")
        if "status" in args:
            return _completed(stdout="")
        if args and args[0] == "tag":
            return _completed(stdout="agent-impl-M001\n")
        raise AssertionError(f"unexpected git invocation: {args}")

    runner.container_setup = SimpleNamespace(docker_exec_git=git)

    def docker_run(cmd, **kwargs):
        archive_index = cmd.index("--format=tar")
        observed["archive"].append(cmd[archive_index + 1])
        with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
            payload = b"package core\n"
            info = tarfile.TarInfo("core/main.go")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        return _completed()

    monkeypatch.setattr(run_milestone_module.subprocess, "run", docker_run)
    monkeypatch.setattr(
        run_milestone_module,
        "inspect_docker_image_id",
        lambda *_args, **_kwargs: "i" * 64,
    )
    return runner, observed


def _mock_go_graph_contract(evaluator, monkeypatch, *, added=None):
    """Keep closure unit tests focused while satisfying exact-MVS witnesses."""
    submitted = {"example.com/x": ("", "", "")}
    private = dict(submitted)
    end = dict(submitted)
    for path, version in (added or {}).items():
        private[path] = (version, "", "")
        end[path] = (version, "", "")
    graph_calls = iter([submitted, private])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graph_calls), ""),
    )
    semantics = {
        "ModulePath": "example.com/x",
        "Go": "1.21",
        "Toolchain": None,
        "Replace": [],
        "Exclude": [],
        "Retract": [],
    }
    monkeypatch.setattr(
        evaluator,
        "_read_go_mod_semantics",
        lambda **_kwargs: (True, semantics, ""),
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (True, end, set(), ""),
    )


def _contract_gate_evaluator(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path
    evaluator.repo_config = {
        "evaluation_go_module_closure": True,
        "evaluation_go_module_dirs": ["."],
    }
    evaluator._go_manifest_inventory = frozenset({"go.mod", "go.sum"})
    evaluator._go_exec_env = {}
    evaluator._eval_meta.update({
        "go_module_closure_enabled": False,
        "go_module_closure_applied": False,
        "go_module_production_compile_checked": False,
        "go_module_production_compile_error": "",
        "go_module_test_graph_contract_error": "",
        "go_module_test_graph_added_modules": [],
        "go_module_test_mod_changed": False,
        "go_module_sum_changed": False,
        "go_module_closure_error": "",
        "partial_test_universe": False,
        "build_failure_diagnostics": [],
    })
    monkeypatch.setattr(
        evaluator, "_hash_go_manifest_state", lambda _dirs: (True, "a" * 64)
    )
    monkeypatch.setattr(
        evaluator, "_hash_go_test_graph", lambda: (True, "b" * 64)
    )
    monkeypatch.setattr(
        evaluator, "_go_exec", lambda *_args, **_kwargs: _completed()
    )
    return evaluator


def test_go_closure_rejects_snapshot_without_exact_manifest_projection(tmp_path):
    overlay = ManifestOverlay.create("a" * 40, upserts=["go.mod", "go.sum"])
    snapshot, sidecar = _write_snapshot(
        tmp_path,
        {"go.mod": "module example.com/x\n", "go.sum": ""},
        overlay,
    )
    metadata = json.loads(sidecar.read_text())
    metadata.pop("go_manifest_projection")
    sidecar.write_text(json.dumps(metadata))
    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config = {"evaluation_go_module_closure": True}

    with pytest.raises(RuntimeError, match="go_manifest_projection.*recapture required"):
        evaluator._load_and_validate_snapshot_metadata()


def test_snapshot_metadata_requires_exact_tag_and_go_provenance(tmp_path):
    overlay = ManifestOverlay.create("a" * 40, upserts=["go.mod"])
    snapshot, sidecar = _write_snapshot(
        tmp_path,
        {"go.mod": "module example.com/x\n"},
        overlay,
    )
    metadata = json.loads(sidecar.read_text())
    metadata["tag"] = "agent-impl-M001.1"
    sidecar.write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError, match="does not match milestone"):
        _bare_evaluator(snapshot)._load_and_validate_snapshot_metadata()

    metadata["tag"] = "agent-impl-M001"
    metadata.pop("agent_base_image_id")
    sidecar.write_text(json.dumps(metadata))
    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config = {"evaluation_go_module_closure": True}
    with pytest.raises(RuntimeError, match="agent_base_image_id.*recapture required"):
        evaluator._load_and_validate_snapshot_metadata()


def test_snapshot_repo_config_identity_requires_matching_pinned_binding(tmp_path):
    overlay = ManifestOverlay.create("a" * 40, upserts=["go.mod"])
    snapshot, sidecar = _write_snapshot(
        tmp_path,
        {"go.mod": "module example.com/x\n"},
        overlay,
    )
    expected_sha = "d" * 64
    metadata = json.loads(sidecar.read_text())

    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config_binding_mode = "trial-pinned"
    evaluator.repo_config_sha256 = expected_sha
    with pytest.raises(RuntimeError, match="binding is missing"):
        evaluator._load_and_validate_snapshot_metadata()

    metadata["repo_config_binding"] = None
    sidecar.write_text(json.dumps(metadata))
    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config_binding_mode = "trial-pinned"
    evaluator.repo_config_sha256 = expected_sha
    with pytest.raises(RuntimeError, match="binding is missing"):
        evaluator._load_and_validate_snapshot_metadata()

    evaluator = _bare_evaluator(snapshot)
    with pytest.raises(RuntimeError, match="Invalid snapshot repo config binding"):
        evaluator._load_and_validate_snapshot_metadata()

    metadata["repo_config_binding"] = RepoConfigIdentity(
        repo_name="example_owner_repo_v1_v2",
        sha256=expected_sha,
    ).to_dict()
    sidecar.write_text(json.dumps(metadata))

    evaluator = _bare_evaluator(snapshot)
    with pytest.raises(RuntimeError, match="trial-pinned repo config"):
        evaluator._load_and_validate_snapshot_metadata()

    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config_binding_mode = "trial-pinned"
    evaluator.repo_config_sha256 = "e" * 64
    with pytest.raises(RuntimeError, match="binding digest mismatch"):
        evaluator._load_and_validate_snapshot_metadata()

    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config_binding_mode = "trial-pinned"
    evaluator.repo_config_sha256 = expected_sha
    loaded, loaded_overlay = evaluator._load_and_validate_snapshot_metadata()
    assert loaded["repo_config_binding"]["sha256"] == expected_sha
    assert loaded_overlay == overlay


def test_snapshot_runtime_policy_identity_requires_matching_pinned_binding(tmp_path):
    overlay = ManifestOverlay.create("a" * 40, upserts=["go.mod"])
    snapshot, sidecar = _write_snapshot(
        tmp_path,
        {"go.mod": "module example.com/x\n"},
        overlay,
    )
    expected_sha = "f" * 64

    evaluator = _bare_evaluator(snapshot)
    evaluator.runtime_policy_binding_mode = "trial-pinned"
    evaluator.runtime_policy_sha256 = expected_sha
    evaluator.runtime_policy_mode = "protected"
    with pytest.raises(RuntimeError, match="runtime policy binding is missing"):
        evaluator._load_and_validate_snapshot_metadata()

    metadata = json.loads(sidecar.read_text())
    metadata["runtime_policy_binding"] = RuntimePolicyIdentity(
        repo_name="example_owner_repo_v1_v2",
        sha256=expected_sha,
        mode="protected",
    ).to_dict()
    sidecar.write_text(json.dumps(metadata))

    evaluator = _bare_evaluator(snapshot)
    with pytest.raises(RuntimeError, match="requires a trial-pinned runtime policy"):
        evaluator._load_and_validate_snapshot_metadata()

    evaluator = _bare_evaluator(snapshot)
    evaluator.runtime_policy_binding_mode = "trial-pinned"
    evaluator.runtime_policy_sha256 = "e" * 64
    evaluator.runtime_policy_mode = "protected"
    with pytest.raises(RuntimeError, match="runtime policy digest mismatch"):
        evaluator._load_and_validate_snapshot_metadata()

    evaluator = _bare_evaluator(snapshot)
    evaluator.runtime_policy_binding_mode = "trial-pinned"
    evaluator.runtime_policy_sha256 = expected_sha
    evaluator.runtime_policy_mode = "unprotected"
    with pytest.raises(RuntimeError, match="runtime policy mode mismatch"):
        evaluator._load_and_validate_snapshot_metadata()

    evaluator = _bare_evaluator(snapshot)
    evaluator.runtime_policy_binding_mode = "trial-pinned"
    evaluator.runtime_policy_sha256 = expected_sha
    evaluator.runtime_policy_mode = "protected"
    loaded, loaded_overlay = evaluator._load_and_validate_snapshot_metadata()
    assert loaded["runtime_policy_binding"]["sha256"] == expected_sha
    assert loaded_overlay == overlay


def test_exact_go_projection_removes_only_absent_product_manifests(monkeypatch):
    evaluator = _bare_evaluator("unused.tar")
    evaluator.repo_config = {"evaluation_go_module_closure": True}
    evaluator._go_manifest_inventory = frozenset({"go.mod"})
    evaluator._eval_meta = {"go_manifest_projection_removed": []}
    overlay = ManifestOverlay.create("base", upserts=["go.mod"])
    evaluator._load_and_validate_snapshot_metadata = lambda: (
        {
            "capture_filter": {
                "src_dirs": ["core"],
                "test_dirs": ["**/testdata/**"],
            }
        },
        overlay,
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "find" in command:
            return _completed(
                stdout=(
                    "/testbed/go.mod\0/testbed/go.sum\0"
                    "/testbed/core/testdata/fixture/go.mod\0"
                    "/testbed/tools/goctl/go.mod\0"
                )
            )
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._apply_exact_go_manifest_projection()

    assert ok and not error
    assert calls[-1][1]["input"] == "/testbed/go.sum\0"
    assert evaluator._eval_meta["go_manifest_projection_removed"] == ["go.sum"]


def test_evaluator_bypasses_legacy_git_checkout_wrapper(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        stdout = "pom.xml\0module/pom.xml\0" if "ls-tree" in command[-3] else ""
        return _completed(stdout=stdout)

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._checkout_to_tag("end")
    assert ok and not error
    assert "/usr/bin/git.real" in calls[-1][-1]

    files = evaluator._git_ls_tree("milestone-M001-end")
    assert files == {"pom.xml", "module/pom.xml"}
    assert "/usr/bin/git.real" in calls[-1][-3]


def test_agent_manifests_are_restored_after_environment_hook(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    overlay = ManifestOverlay.create(
        "baseline",
        upserts=["pom.xml", "module/pom.xml"],
    )
    evaluator._load_and_validate_snapshot_metadata = lambda: ({}, overlay)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._restore_agent_manifest_upserts()

    assert ok and not error
    command, kwargs = calls[0]
    assert "tar --extract --file=snapshot.tar" in command[-1]
    assert kwargs["input"] == "module/pom.xml\0pom.xml\0"


def test_agent_authoritative_manifest_config_is_milestone_scoped_and_snapshot_conditional(
    tmp_path,
):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.milestone_id = "M025"
    evaluator.repo_config = {
        "evaluation_manifest_agent_authoritative": {
            "M025": ["dubbo-dependencies-bom/pom.xml"],
            "M018": ["pom.xml"],
        }
    }
    overlay = ManifestOverlay.create(
        "baseline",
        upserts=["pom.xml", "dubbo-dependencies-bom/pom.xml"],
    )

    assert evaluator._manifest_agent_authoritative_paths(overlay) == frozenset(
        {"dubbo-dependencies-bom/pom.xml"}
    )

    evaluator.repo_config["evaluation_manifest_agent_authoritative"]["M025"] = [
        "missing/pom.xml"
    ]
    assert evaluator._manifest_agent_authoritative_paths(overlay) == frozenset()

    deleted_overlay = ManifestOverlay.create(
        "baseline",
        deletes=["missing/pom.xml"],
    )
    assert evaluator._manifest_agent_authoritative_paths(deleted_overlay) == frozenset()


def test_go_closure_makes_all_submitted_go_manifests_agent_authoritative(tmp_path):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.repo_config = {"evaluation_go_module_closure": True}
    overlay = ManifestOverlay.create(
        "baseline",
        upserts=["go.mod", "go.sum", "tools/goctl/go.mod", "pom.xml"],
    )

    assert evaluator._manifest_agent_authoritative_paths(overlay) == frozenset(
        {"go.mod", "go.sum", "tools/goctl/go.mod"}
    )


def test_offline_cache_overlay_dockerfile_is_data_only_and_paths_are_validated():
    paths = _validated_cache_paths(
        ["/go/pkg/mod/cache/download", "/go/pkg/mod/cache/download"]
    )
    assert paths == ["/go/pkg/mod/cache/download"]
    dockerfile = _render_offline_cache_overlay_dockerfile(
        "milestone:image",
        "closure:image",
        paths,
    )
    assert dockerfile == (
        "FROM closure:image AS closure\n"
        "FROM milestone:image\n"
        "RUN rm -rf /go/pkg/mod/cache/download\n"
        "COPY --from=closure /go/pkg/mod/cache/download "
        "/go/pkg/mod/cache/download\n"
    )
    replaced = _render_offline_cache_overlay_dockerfile(
        "milestone:image",
        "closure:image",
        paths,
        ["/usr/local/go"],
    )
    assert "RUN rm -rf /usr/local/go\n" in replaced
    assert "COPY --from=closure /usr/local/go /usr/local/go\n" in replaced
    with pytest.raises(ValueError, match="unsafe|unsupported"):
        _validated_cache_paths(["/go/pkg/../answer"])


def test_offline_overlay_build_is_exact_content_addressed_and_labeled(monkeypatch):
    milestone_id = "a" * 64
    closure_id = "b" * 64
    effective_id = "c" * 64
    built = {}
    labels = {}

    def image_id(image):
        if image == "milestone:tag":
            return milestone_id
        if image == f"sha256:{closure_id}":
            return closure_id
        if "milestone-parent" in image:
            return milestone_id
        if "closure-parent" in image:
            return closure_id
        return effective_id

    def run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return _completed(returncode=1)
        if command[:3] == ["docker", "image", "tag"]:
            return _completed()
        if command[:2] == ["docker", "build"]:
            built["dockerfile"] = kwargs["input"]
            for line in kwargs["input"].splitlines():
                if line.startswith("LABEL "):
                    key, value = line.removeprefix("LABEL ").split("=", 1)
                    labels[key] = value
            return _completed()
        raise AssertionError(command)

    monkeypatch.setattr("harness.e2e.evaluator._docker_image_id", image_id)
    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    monkeypatch.setattr("harness.e2e.evaluator._docker_image_labels", lambda _image: labels)
    monkeypatch.setattr("harness.e2e.evaluator.resolve_image", lambda _ref: "mutable:ignored")

    effective, milestone, closure, effective_hash = ensure_offline_evaluation_image(
        repo_name="example_repo_1_2",
        milestone_id="M001",
        milestone_image="milestone:tag",
        quarantine_config={
            "closure": {
                "cache_paths": ["/go/pkg/mod/cache/download"],
                "toolchain": {"go": "1.21.13"},
            }
        },
        expected_closure_image_id=closure_id,
    )

    dockerfile = built["dockerfile"]
    assert f"closure-parent:sha256-{closure_id} AS closure" in dockerfile
    assert f"milestone-parent:sha256-{milestone_id}" in dockerfile
    assert "RUN rm -rf /go/pkg/mod/cache/download" in dockerfile
    assert "RUN rm -rf /usr/local/go" in dockerfile
    assert "COPY --from=closure /usr/local/go /usr/local/go" in dockerfile
    assert labels["org.evoclaw.evaluation-closure.schema"] == str(
        OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION
    )
    assert effective == f"sha256:{effective_id}"
    assert (milestone, closure, effective_hash) == (
        milestone_id,
        closure_id,
        effective_id,
    )


def test_internal_evaluator_network_is_created_and_verified(monkeypatch):
    calls = []
    responses = iter([
        _completed(returncode=1, stderr="not found"),
        _completed(stdout="network-id\n"),
        _completed(stdout=json.dumps([{
            "Internal": True,
            "Driver": "bridge",
            "Labels": {"org.evoclaw.evaluation-network.schema": "1"},
        }])),
    ])

    def run(command, **_kwargs):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    name = ensure_internal_evaluation_network()

    assert name == "evoclaw-eval-internal-v1"
    assert calls[1][:5] == [
        "docker", "network", "create", "--driver", "bridge"
    ]
    assert "--internal" in calls[1]


def test_internal_evaluator_network_rejects_name_collision(monkeypatch):
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(stdout=json.dumps([{
            "Internal": False,
            "Driver": "bridge",
            "Labels": {"org.evoclaw.evaluation-network.schema": "1"},
        }])),
    )

    with pytest.raises(RuntimeError, match="not the expected internal bridge"):
        ensure_internal_evaluation_network()


def test_evaluator_go_toolchain_policy_is_exact_and_observable(monkeypatch):
    policy = {"closure": {"toolchain": {"go": "go1.21.13"}}}
    assert _configured_go_toolchain_version(policy) == "1.21.13"
    with pytest.raises(ValueError, match="closure.toolchain.go"):
        _configured_go_toolchain_version(
            {"closure": {"toolchain": {"go": "latest"}}}
        )

    evaluator = _bare_evaluator("unused.tar")
    evaluator.quarantine_config = policy
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(
            stdout=(
                "executable=/usr/local/go/bin/go\n"
                "go version go1.21.13 linux/amd64\n"
                "goroot=/usr/local/go\n"
                "golang_version=1.21.13\n"
            )
        ),
    )
    evaluator._verify_evaluator_go_toolchain()
    assert evaluator._eval_meta["go_toolchain_expected"] == "1.21.13"
    assert evaluator._eval_meta["go_toolchain_actual"] == "1.21.13"
    assert evaluator._eval_meta["go_toolchain_executable"] == "/usr/local/go/bin/go"
    assert evaluator._eval_meta["go_toolchain_goroot"] == "/usr/local/go"


def test_evaluator_go_toolchain_mismatch_fails_closed(monkeypatch):
    evaluator = _bare_evaluator("unused.tar")
    evaluator.quarantine_config = {
        "closure": {"toolchain": {"go": "1.21.13"}}
    }
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(
            stdout=(
                "executable=/usr/local/go/bin/go\n"
                "go version go1.19.13 linux/amd64\n"
                "goroot=/usr/local/go\n"
                "golang_version=1.21.13\n"
            )
        ),
    )

    with pytest.raises(RuntimeError, match="toolchain mismatch"):
        evaluator._verify_evaluator_go_toolchain()


def test_go_module_gate_uses_readonly_agent_graph_and_isolated_test_modfile(
    tmp_path, monkeypatch
):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path
    evaluator.repo_config = {
        "evaluation_go_module_closure": True,
        "evaluation_go_module_dirs": ["."],
    }
    evaluator._eval_meta.update(
        {
            "go_module_closure_enabled": False,
            "go_module_closure_applied": False,
            "go_module_production_compile_checked": False,
            "go_module_production_compile_error": "",
            "go_module_test_mod_changed": False,
            "go_module_sum_changed": False,
            "go_module_manifest_sha256_before": "",
            "go_module_manifest_sha256_after": "",
            "go_module_closure_error": "",
        }
    )
    evaluator._go_exec_env = {}
    evaluator._go_manifest_inventory = frozenset({"go.mod", "go.sum"})
    monkeypatch.setattr(
        evaluator,
        "_hash_go_manifest_state",
        lambda _dirs: (True, "a" * 64),
    )
    monkeypatch.setattr(
        evaluator,
        "_hash_go_test_graph",
        lambda: (True, "c" * 64),
    )
    _mock_go_graph_contract(evaluator, monkeypatch)
    commands = []

    def go_exec(command, **_kwargs):
        commands.append(command)
        return _completed()

    monkeypatch.setattr(evaluator, "_go_exec", go_exec)

    def run(command, **_kwargs):
        # The final cmp says the evaluator-only test graph changed.
        if command[-2:] == ["go.mod", "/tmp/evoclaw-evaluation.mod"]:
            return _completed(returncode=1)
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._run_go_module_closure()

    assert ok and not error
    assert commands[0] == "go list -mod=readonly -deps ./..."
    assert commands[1] == "go build -buildvcs=false -mod=readonly ./..."
    assert "GO111MODULE=off go run" in commands[2]
    assert "-mod=mod -modfile=/tmp/evoclaw-evaluation.mod -test" in commands[3]
    assert "-mod=readonly -modfile=/tmp/evoclaw-evaluation.mod -test" in commands[4]
    assert evaluator._eval_meta["go_module_closure_applied"] is True
    assert evaluator._eval_meta["go_module_production_compile_checked"] is True
    assert evaluator._eval_meta["go_module_production_compile_error"] == ""
    assert evaluator._eval_meta["go_module_test_mod_changed"] is True
    assert "-mod=readonly" in evaluator._go_exec_env["GOFLAGS"]
    assert "-modfile=/tmp/evoclaw-evaluation.mod" in evaluator._go_exec_env["GOFLAGS"]


def test_go_module_gate_never_repairs_invalid_agent_production_manifest(
    tmp_path, monkeypatch
):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path
    evaluator.repo_config = {"evaluation_go_module_closure": True}
    evaluator._eval_meta.update(
        {
            "go_module_closure_enabled": False,
            "go_module_closure_error": "",
            "go_module_manifest_sha256_before": "",
        }
    )
    evaluator._go_exec_env = {}
    evaluator._go_manifest_inventory = frozenset({"go.mod", "go.sum"})
    monkeypatch.setattr(
        evaluator,
        "_hash_go_manifest_state",
        lambda _dirs: (True, "b" * 64),
    )
    commands = []

    def go_exec(command, **_kwargs):
        commands.append(command)
        return _completed(returncode=1, stderr="go: updates to go.mod needed")

    monkeypatch.setattr(evaluator, "_go_exec", go_exec)

    ok, error = evaluator._run_go_module_closure()

    assert ok is False
    assert "will not repair it" in error
    assert commands == ["go list -mod=readonly -deps ./..."]
    assert evaluator._go_exec_env == {}


def test_go_module_gate_never_hides_submitted_production_type_failure(
    tmp_path, monkeypatch
):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path
    evaluator.repo_config = {
        "evaluation_go_module_closure": True,
        "evaluation_go_module_dirs": ["."],
    }
    evaluator._go_manifest_inventory = frozenset({"go.mod", "go.sum"})
    evaluator._go_exec_env = {}
    evaluator._eval_meta.update(
        {
            "go_module_closure_enabled": False,
            "go_module_closure_applied": False,
            "go_module_production_compile_checked": False,
            "go_module_production_compile_error": "",
            "go_module_test_mod_changed": False,
            "go_module_sum_changed": False,
            "go_module_closure_error": "",
        }
    )
    monkeypatch.setattr(
        evaluator, "_hash_go_manifest_state", lambda _dirs: (True, "a" * 64)
    )
    monkeypatch.setattr(
        evaluator, "_hash_go_test_graph", lambda: (True, "b" * 64)
    )
    _mock_go_graph_contract(evaluator, monkeypatch)
    commands = []

    def go_exec(command, **_kwargs):
        commands.append(command)
        if command.startswith("go build"):
            return _completed(
                returncode=1,
                stderr="pkg/file.go:12: undefined: assert.NotImplements",
            )
        return _completed()

    monkeypatch.setattr(evaluator, "_go_exec", go_exec)
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(),
    )

    ok, error = evaluator._run_go_module_closure()

    assert ok and not error
    assert evaluator._eval_meta["go_module_closure_applied"] is True
    assert "NotImplements" in evaluator._eval_meta[
        "go_module_production_compile_error"
    ]
    compile_ok, compile_error = evaluator._check_compilation()
    assert compile_ok is False
    assert "Submitted Go production graph failed type-check" in compile_error
    assert any("-modfile=/tmp/evoclaw-evaluation.mod" in cmd for cmd in commands)


@pytest.mark.parametrize(
    "locked_field",
    ["go_module_production_compile_error", "go_module_test_graph_contract_error"],
)
def test_orchestrator_cannot_override_go_resolution_lock(
    tmp_path, monkeypatch, locked_field
):
    result = _all_pass_result(**{locked_field: "contract failed"})
    constructed = {}

    class FakeEvaluator:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

        def evaluate(self):
            return result

    monkeypatch.setattr(orchestrator_module, "PatchEvaluator", FakeEvaluator)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    _, resolved, actual_passed, saved, error = _run_evaluation_once(
        milestone_id="M001",
        snapshot_path=tmp_path / "snapshot.tar",
        result_dir=output_dir,
        workspace_root=tmp_path,
        fail_to_pass_threshold=1.0,
        pass_to_pass_threshold=1.0,
        none_to_pass_threshold=1.0,
        baseline_json=tmp_path / "baseline.json",
        eval_result_path=output_dir / "evaluation_result.json",
        repo_config_path=tmp_path / "repo_config.yaml",
        repo_config_sha256="f" * 64,
    )

    assert error is None
    assert resolved is False
    assert actual_passed is False
    assert saved.resolved is False
    assert constructed["repo_config_path"] == tmp_path / "repo_config.yaml"
    assert constructed["repo_config_sha256"] == "f" * 64


def test_orchestrator_retries_infra_invalid_zero_test_result(tmp_path, monkeypatch):
    result = _all_pass_result(
        total_tests=0,
        passed_tests=0,
        pass_to_pass_success_count=0,
        pass_to_pass_required=1,
        infra_invalid_reason="zero-tests-with-required-tests",
    )

    class FakeEvaluator:
        def __init__(self, **_kwargs):
            pass

        def evaluate(self):
            return result

    monkeypatch.setattr(orchestrator_module, "PatchEvaluator", FakeEvaluator)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(
        InfrastructureFailureError,
        match="evaluation result is not safe to score",
    ):
        _run_evaluation_once(
            milestone_id="M001",
            snapshot_path=tmp_path / "snapshot.tar",
            result_dir=output_dir,
            workspace_root=tmp_path,
            fail_to_pass_threshold=1.0,
            pass_to_pass_threshold=1.0,
            none_to_pass_threshold=1.0,
            baseline_json=tmp_path / "baseline.json",
            eval_result_path=output_dir / "evaluation_result.json",
        )

    saved = json.loads((output_dir / "evaluation_result.json").read_text())
    assert saved["eval_status"] == "infra-invalid"


def test_go_test_import_discovery_is_static_and_marks_external_imports(monkeypatch):
    evaluator = _bare_evaluator("unused.tar")
    stream = "\n".join([
        json.dumps({"Import": "fmt", "Directory": "./core/a"}),
        json.dumps({
            "Import": "github.com/pelletier/go-toml/v2",
            "Directory": "./core/a",
        }),
        json.dumps({
            "Import": "github.com/stretchr/testify/require",
            "Directory": "./core/b",
        }),
    ]) + "\n"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return _completed(stdout=stream)

    monkeypatch.setattr(
        evaluator,
        "_go_exec",
        run,
    )

    ok, imports, missing, error = evaluator._discover_go_test_imports(
        workdir="/testbed",
        env={},
        modfile="/tmp/evoclaw-evaluation.mod",
    )

    assert ok and not error
    assert imports == {
        "fmt",
        "github.com/pelletier/go-toml/v2",
        "github.com/stretchr/testify/require",
    }
    assert missing == {
        "github.com/pelletier/go-toml/v2",
        "github.com/stretchr/testify/require",
    }
    assert evaluator._go_test_import_owners[
        "github.com/pelletier/go-toml/v2"
    ] == {"./core/a"}
    assert "GO111MODULE=off go run" in commands[0]
    assert "go/parser" in commands[0]
    assert "go/build" in commands[0]
    assert "build.Default.MatchFile" in commands[0]
    assert 'filepath.Join(path, "go.mod")' in commands[0]
    assert "filepath.SkipDir" in commands[0]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("v1.2.3", "v1.2.3-rc.1", 1),
        ("v1.2.3-rc.10", "v1.2.3-rc.2", 1),
        (
            "v0.0.0-20240201120000-bbbbbbbbbbbb",
            "v0.0.0-20240101120000-aaaaaaaaaaaa",
            1,
        ),
        ("v2.0.0+incompatible", "v2.0.0", 0),
    ],
)
def test_go_semver_comparison_covers_module_version_forms(
    left, right, expected
):
    assert PatchEvaluator._compare_go_semver(left, right) == expected
    assert PatchEvaluator._compare_go_semver(right, left) == -expected


def test_go_module_graph_reconstructs_mvs_and_ignores_language_nodes():
    older = "v0.0.0-20240101120000-aaaaaaaaaaaa"
    newer = "v0.0.0-20240201120000-bbbbbbbbbbbb"
    graph = PatchEvaluator._parse_go_module_graph(
        "\n".join(
            [
                "example.com/main example.com/a@v1.2.3-rc.1",
                f"example.com/main example.com/pseudo@{older}",
                "example.com/main go@1.21",
                "example.com/main toolchain@go1.21.13",
                "example.com/a@v1.2.3-rc.1 example.com/a@v1.2.3",
                f"example.com/a@v1.2.3 example.com/pseudo@{newer}",
            ]
        ),
        {"Module": {"Path": "example.com/main"}, "Replace": None},
    )

    assert graph == {
        "example.com/main": ("", "", ""),
        "example.com/a": ("v1.2.3", "", ""),
        "example.com/pseudo": (newer, "", ""),
    }


def test_go_module_graph_prefers_version_specific_replace():
    graph = PatchEvaluator._parse_go_module_graph(
        "example.com/main example.com/dependency@v1.2.3",
        {
            "Module": {"Path": "example.com/main"},
            "Replace": [
                {
                    "Old": {"Path": "example.com/dependency"},
                    "New": {"Path": "../local-dependency"},
                },
                {
                    "Old": {
                        "Path": "example.com/dependency",
                        "Version": "v1.2.3",
                    },
                    "New": {
                        "Path": "example.com/fork",
                        "Version": "v1.2.4",
                    },
                },
            ],
        },
    )

    assert graph["example.com/dependency"] == (
        "v1.2.3",
        "example.com/fork",
        "v1.2.4",
    )


def test_private_graph_seeds_only_modules_imported_by_tests(tmp_path, monkeypatch):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    submitted = {
        "example.com/x": ("", "", ""),
        "github.com/stretchr/testify": ("v1.8.4", "", ""),
    }
    used = "github.com/pelletier/go-toml/v2"
    unrelated = "example.com/unrelated"
    end = {
        **submitted,
        used: ("v2.2.2", "", ""),
        unrelated: ("v1.0.0", "", ""),
    }
    private = {**submitted, used: end[used]}
    graphs = iter([submitted, private, private])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = {
        "ModulePath": "example.com/x", "Go": "1.21", "Toolchain": None,
        "Replace": [], "Exclude": [], "Retract": [],
    }
    monkeypatch.setattr(
        evaluator, "_read_go_mod_semantics", lambda **_kwargs: (True, semantics, "")
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (True, end, set(), ""),
    )
    monkeypatch.setattr(
        evaluator,
        "_discover_go_test_imports",
        lambda **_kwargs: (True, {used + "/unstable"}, set(), ""),
    )
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._run_go_module_closure()

    assert ok and not error
    seed = next(command for command in calls if "mod" in command and "edit" in command)
    assert f"-require={used}@v2.2.2" in seed
    assert not any(unrelated in str(item) for item in seed)


def test_private_test_graph_without_safe_package_mapping_fails_closed(
    tmp_path, monkeypatch
):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    submitted = {
        "example.com/x": ("", "", ""),
        "github.com/stretchr/testify": ("v1.8.4", "", ""),
    }
    private = {
        **submitted,
        "github.com/pelletier/go-toml/v2": ("v2.2.2", "", ""),
    }
    private["github.com/stretchr/testify"] = ("v1.9.0", "", "")
    end = {
        **submitted,
        "github.com/pelletier/go-toml/v2": ("v2.2.2", "", ""),
    }
    graphs = iter([submitted, private])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = {
        "ModulePath": "example.com/x", "Go": "1.21", "Toolchain": None,
        "Replace": [], "Exclude": [], "Retract": [],
    }
    monkeypatch.setattr(
        evaluator,
        "_read_go_mod_semantics",
        lambda **_kwargs: (True, semantics, ""),
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (True, end, set(), ""),
    )
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._run_go_module_closure()

    assert not ok
    assert "safe package narrowing was unavailable" in error
    assert "cannot map incompatible evaluator test import" in error
    assert "change an existing submitted MVS selection" in evaluator._eval_meta[
        "go_module_test_graph_contract_error"
    ]
    assert evaluator._eval_meta["partial_test_universe"] is True
    assert evaluator._go_exec_env["GOFLAGS"] == "-buildvcs=false -mod=readonly"
    assert "-modfile" not in evaluator._go_exec_env["GOFLAGS"]
    assert any(command[-5:] == [
        "rm", "-f", "--", "/tmp/evoclaw-evaluation.mod",
        "/tmp/evoclaw-evaluation.sum",
    ] for command in calls)
    assert evaluator._eval_meta["go_test_graph_sha256_before"] == "b" * 64


def test_private_seed_upgrade_preflights_to_exact_safe_package_subset(
    tmp_path, monkeypatch
):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    main = "example.com/x"
    existing = "example.com/existing"
    seed = "example.com/private-tests"
    test_import = seed + "/fixture"
    submitted = {
        main: ("", "", ""),
        existing: ("v1.0.0", "", ""),
    }
    private = {
        main: ("", "", ""),
        existing: ("v1.1.0", "", ""),
        seed: ("v2.0.0", "", ""),
    }
    graphs = iter([submitted, private])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = {
        "ModulePath": main, "Go": "1.21", "Toolchain": None,
        "Replace": [], "Exclude": [], "Retract": [],
    }
    monkeypatch.setattr(
        evaluator, "_read_go_mod_semantics", lambda **_kwargs: (True, semantics, "")
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (True, private, set(), ""),
    )
    def discover(**_kwargs):
        evaluator._go_test_import_owners = {test_import: {"./core/private"}}
        return True, {test_import}, {test_import}, ""

    monkeypatch.setattr(
        evaluator,
        "_discover_go_test_imports",
        discover,
    )
    go_commands = []

    def go_exec(command, **_kwargs):
        go_commands.append(command)
        if "go list -mod=readonly -f" in command:
            return _completed(
                stdout=(
                    f"{main}/core/ok\t/testbed/core/ok\n"
                    f"{main}/core/private\t/testbed/core/private\n"
                )
            )
        return _completed()

    monkeypatch.setattr(evaluator, "_go_exec", go_exec)
    writes = []

    def run(command, **kwargs):
        if kwargs.get("input"):
            writes.append(kwargs["input"])
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._run_go_module_closure()

    assert ok and not error
    assert not any("-mod=mod" in command for command in go_commands)
    assert evaluator._eval_meta["go_partial_package_filter_applied"] is True
    assert evaluator._eval_meta["go_partial_package_filter_excluded"] == [
        "./core/private"
    ]
    assert evaluator._eval_meta["go_partial_package_filter_included"] == 1
    assert evaluator._go_exec_env["GOFLAGS"] == "-buildvcs=false -mod=readonly"
    assert evaluator._go_exec_env["EVOCLAW_GO_TEST_PACKAGE_FILE"] == (
        "/tmp/evoclaw-safe-test-packages"
    )
    assert writes == [f"{main}/core/ok\n"]
    assert "seed would change an existing submitted MVS selection" in (
        evaluator._eval_meta["go_module_test_graph_contract_error"]
    )


def test_private_test_graph_contract_conflict_is_fatal_in_strict_mode(
    tmp_path, monkeypatch
):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    evaluator.build_failure_fail_closed = True
    submitted = {
        "example.com/x": ("", "", ""),
        "github.com/stretchr/testify": ("v1.8.4", "", ""),
    }
    changed = dict(submitted)
    changed["github.com/stretchr/testify"] = ("v1.9.0", "", "")
    graphs = iter([submitted, changed])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = {
        "ModulePath": "example.com/x", "Go": "1.21", "Toolchain": None,
        "Replace": [], "Exclude": [], "Retract": [],
    }
    monkeypatch.setattr(
        evaluator, "_read_go_mod_semantics", lambda **_kwargs: (True, semantics, "")
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (True, submitted, set(), ""),
    )
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(),
    )

    ok, error = evaluator._run_go_module_closure()

    assert ok is False
    assert "change an existing submitted MVS selection" in error
    assert evaluator._go_exec_env == {}


def test_private_test_graph_allows_only_exact_end_pinned_additions(
    tmp_path, monkeypatch
):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    submitted = {"example.com/x": ("", "", "")}
    addition = {"github.com/pelletier/go-toml/v2": ("v2.2.2", "", "")}
    graphs = iter([submitted, {**submitted, **addition}])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = {
        "ModulePath": "example.com/x", "Go": "1.21", "Toolchain": None,
        "Replace": [], "Exclude": [], "Retract": [],
    }
    monkeypatch.setattr(
        evaluator,
        "_read_go_mod_semantics",
        lambda **_kwargs: (True, semantics, ""),
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (True, {**submitted, **addition}, set(), ""),
    )

    def run(command, **_kwargs):
        if command[-2:] == ["go.mod", "/tmp/evoclaw-evaluation.mod"]:
            return _completed(returncode=1)
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._run_go_module_closure()

    assert ok and not error
    assert evaluator._eval_meta["go_module_test_graph_contract_error"] == ""
    assert evaluator._eval_meta["go_module_test_graph_added_modules"] == [
        "github.com/pelletier/go-toml/v2"
    ]
    assert "-modfile=/tmp/evoclaw-evaluation.mod" in evaluator._go_exec_env[
        "GOFLAGS"
    ]


def test_private_test_graph_rejects_future_union_version_and_directive_change(
    tmp_path, monkeypatch
):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    submitted = {"example.com/x": ("", "", "")}
    future = {"github.com/grafana/pyroscope-go": ("v1.2.7", "", "")}
    graphs = iter([submitted, {**submitted, **future}])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = iter([
        {
            "ModulePath": "example.com/x", "Go": "1.21", "Toolchain": None,
            "Replace": [], "Exclude": [], "Retract": [],
        },
        {
            "ModulePath": "example.com/x", "Go": "1.22", "Toolchain": None,
            "Replace": [], "Exclude": [], "Retract": [],
        },
    ])
    monkeypatch.setattr(
        evaluator,
        "_read_go_mod_semantics",
        lambda **_kwargs: (True, next(semantics), ""),
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (
            True,
            {**submitted, "github.com/grafana/pyroscope-go": ("v1.2.4", "", "")},
            set(),
            "",
        ),
    )
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(),
    )

    ok, error = evaluator._run_go_module_closure()

    assert not ok
    assert "safe package narrowing was unavailable" in error
    assert "contract directives" in evaluator._eval_meta[
        "go_module_test_graph_contract_error"
    ]
    assert "-modfile" not in evaluator._go_exec_env["GOFLAGS"]


def test_private_test_graph_rejects_future_union_addition_with_same_directives(
    tmp_path, monkeypatch
):
    evaluator = _contract_gate_evaluator(tmp_path, monkeypatch)
    submitted = {"example.com/x": ("", "", "")}
    module = "github.com/grafana/pyroscope-go"
    graphs = iter([
        submitted,
        {**submitted, module: ("v1.2.7", "", "")},
    ])
    monkeypatch.setattr(
        evaluator,
        "_read_go_module_graph",
        lambda **_kwargs: (True, next(graphs), ""),
    )
    semantics = {
        "ModulePath": "example.com/x", "Go": "1.21", "Toolchain": None,
        "Replace": [], "Exclude": [], "Retract": [],
    }
    monkeypatch.setattr(
        evaluator, "_read_go_mod_semantics", lambda **_kwargs: (True, semantics, "")
    )
    monkeypatch.setattr(
        evaluator,
        "_prepare_end_go_graph",
        lambda **_kwargs: (
            True,
            {**submitted, module: ("v1.2.4", "", "")},
            set(),
            "",
        ),
    )
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(),
    )

    ok, error = evaluator._run_go_module_closure()

    assert not ok
    assert "safe package narrowing was unavailable" in error
    contract = evaluator._eval_meta["go_module_test_graph_contract_error"]
    assert "not pinned by this milestone END graph" in contract
    assert "v1.2.7" in contract
    assert "v1.2.4" in contract


def test_go_module_topology_fails_closed_for_workspace_and_nested_modules(tmp_path):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator._go_manifest_inventory = frozenset({"go.mod", "go.sum", "go.work"})
    ok, error = evaluator._validate_go_module_topology(["."])
    assert ok is False
    assert "workspace" in error

    evaluator._go_manifest_inventory = frozenset(
        {"go.mod", "go.sum", "core/plugin/go.mod", "core/plugin/go.sum"}
    )
    ok, error = evaluator._validate_go_module_topology(["."])
    assert ok is False
    assert "Multiple scoped Go module roots" in error


def test_final_go_state_gate_detects_test_graph_mutation(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.repo_config = {
        "evaluation_go_module_closure": True,
        "evaluation_go_module_dirs": ["."],
    }
    evaluator._eval_meta = {
        "go_module_closure_enabled": True,
        "go_module_manifest_sha256_before": "a" * 64,
        "go_test_graph_sha256_before": "b" * 64,
        "go_module_closure_error": "",
    }
    monkeypatch.setattr(
        evaluator,
        "_hash_go_manifest_state",
        lambda _dirs: (True, "a" * 64),
    )
    monkeypatch.setattr(
        evaluator,
        "_hash_go_test_graph",
        lambda: (True, "c" * 64),
    )

    ok, error = evaluator._verify_go_evaluation_state("test execution")

    assert ok is False
    assert "mutated evaluator-private Go test graph" in error


def test_gt_test_dependency_lookup_uses_only_vetted_local_proxy(
    tmp_path, monkeypatch
):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path
    evaluator.repo_config = {
        "evaluation_go_module_closure": True,
        "evaluation_go_module_dirs": ["."],
    }
    evaluator.quarantine_config = {
        "closure": {"cache_paths": ["/go/pkg/mod/cache/download"]}
    }
    evaluator._go_manifest_inventory = frozenset({"go.mod", "go.sum"})
    evaluator._go_exec_env = {}
    evaluator._eval_meta.update(
        {
            "go_module_closure_enabled": False,
            "go_module_closure_applied": False,
            "go_test_local_proxy_used": False,
            "go_module_test_mod_changed": False,
            "go_module_sum_changed": False,
            "go_module_manifest_sha256_before": "",
            "go_module_manifest_sha256_after": "",
            "go_test_graph_sha256_before": "",
            "go_test_graph_sha256_after": "",
            "go_module_closure_error": "",
        }
    )
    monkeypatch.setattr(
        evaluator, "_hash_go_manifest_state", lambda _dirs: (True, "a" * 64)
    )
    monkeypatch.setattr(
        evaluator, "_hash_go_test_graph", lambda: (True, "b" * 64)
    )
    _mock_go_graph_contract(evaluator, monkeypatch)
    calls = []

    def go_exec(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return _completed()

    monkeypatch.setattr(evaluator, "_go_exec", go_exec)
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(),
    )

    ok, error = evaluator._run_go_module_closure()

    assert ok and not error
    assert calls
    assert all(
        env["GOPROXY"] == "file:///go/pkg/mod/cache/download"
        for _, env in calls
    )
    assert all(env["GOMODCACHE"] == "/tmp/evoclaw-gomodcache" for _, env in calls)
    assert evaluator._eval_meta["go_test_local_proxy_used"] is True


def test_go_module_gate_rejects_mutating_test_command(tmp_path):
    config = tmp_path / "dockerfiles" / "M001" / "test_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps([{"test_cmd": "go mod tidy && go test ./..."}]))
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path

    ok, error = evaluator._validate_immutable_go_test_config()

    assert ok is False
    assert "manifests are immutable" in error


@pytest.mark.parametrize(
    "configured, message",
    [
        (["src/Main.java"], "non-manifest"),
        (["../pom.xml"], "unsafe snapshot path"),
        (["pom.xml", "./pom.xml"], "duplicate"),
    ],
)
def test_agent_authoritative_manifest_config_rejects_unsafe_values(
    tmp_path, configured, message
):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.milestone_id = "M025"
    evaluator.repo_config = {
        "evaluation_manifest_agent_authoritative": {"M025": configured}
    }
    overlay = ManifestOverlay.create("baseline", upserts=["pom.xml"])

    with pytest.raises(ValueError, match=message):
        evaluator._manifest_agent_authoritative_paths(overlay)


def test_start_fallback_grafts_exact_end_tests_without_manifests(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator._prune_filter = SrcFileFilter(
        src_dirs=["module"],
        test_dirs=["**/src/test/**"],
    )
    evaluator._git_ls_tree = lambda tag: (
        {
            "module/src/main/Main.java",
            "module/src/test/LegacyTest.java",
            "module/pom.xml",
        }
        if tag.endswith("-start")
        else {
            "module/src/main/Main.java",
            "module/src/test/NewTest.java",
            "module/src/test/resources/fixture.json",
            "module/src/test/testdata/go.mod",
            "module/src/test/pom.xml",
        }
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._graft_ground_truth_tests("end")

    assert ok and not error
    # No scoped-policy diff/audit subprocess is introduced for other repos.
    assert len(calls) == 2
    assert calls[0][1]["input"] == (
        "/testbed/module/src/test/LegacyTest.java\0"
        "/testbed/module/src/test/NewTest.java\0"
        "/testbed/module/src/test/resources/fixture.json\0"
        "/testbed/module/src/test/testdata/go.mod\0"
    )
    restored = calls[1][0]
    assert "module/src/test/NewTest.java" in restored
    assert "module/src/test/resources/fixture.json" in restored
    assert "module/src/test/testdata/go.mod" in restored
    assert "module/src/test/pom.xml" not in restored
    assert "module/src/main/Main.java" not in restored
    assert evaluator._eval_meta["gt_test_graft_restored_count"] == 3
    assert evaluator._eval_meta["gt_test_graft_mode"] == "legacy-test-dirs"


def test_scoped_fallback_grafts_tests_and_explicit_fixture_only(tmp_path, monkeypatch):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator._prune_filter = SrcFileFilter(
        src_dirs=["dubbo-demo", "dubbo-test"],
        test_dirs=["**/src/test/**", "dubbo-test/**", "dubbo-demo/**"],
    )
    fixture = "dubbo-demo/example/src/main/proto/message.proto"
    evaluator.repo_config = {
        "evaluation_fallback_test_graft": {
            "mode": "scoped",
            "authoritative_patterns": ["**/src/test/**", "dubbo-test/**"],
            "fixture_paths": {"M001": [fixture]},
            "fail_closed_unlisted_patterns": ["dubbo-demo/**/src/main/**"],
        }
    }
    evaluator._git_ls_tree = lambda tag: (
        {
            "dubbo-demo/example/src/main/Demo.java",
            "dubbo-demo/example/src/test/LegacyTest.java",
            "dubbo-test/harness/src/main/TestHarness.java",
        }
        if tag.endswith("-start")
        else {
            "dubbo-demo/example/src/main/Demo.java",
            fixture,
            "dubbo-demo/example/src/test/NewTest.java",
            "dubbo-test/harness/src/main/TestHarness.java",
        }
    )
    evaluator._git_changed_paths = lambda _old, _new: {
        "dubbo-demo/example/src/test/LegacyTest.java",
        "dubbo-demo/example/src/test/NewTest.java",
        fixture,
    }
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._graft_ground_truth_tests("end")

    assert ok and not error
    removed = calls[0][1]["input"]
    assert "/testbed/dubbo-demo/example/src/main/Demo.java\0" not in removed
    assert f"/testbed/{fixture}\0" in removed
    assert "/testbed/dubbo-demo/example/src/test/LegacyTest.java\0" in removed
    assert "/testbed/dubbo-demo/example/src/test/NewTest.java\0" in removed
    assert "/testbed/dubbo-test/harness/src/main/TestHarness.java\0" in removed
    restored = calls[1][0]
    assert fixture in restored
    assert "dubbo-demo/example/src/test/NewTest.java" in restored
    assert "dubbo-test/harness/src/main/TestHarness.java" in restored
    assert "dubbo-demo/example/src/main/Demo.java" not in restored
    assert evaluator._eval_meta["gt_test_graft_mode"] == "scoped"
    assert evaluator._eval_meta["gt_test_graft_fixture_paths"] == [fixture]
    assert evaluator._eval_meta["gt_test_graft_unlisted_changed_paths"] == []


def test_scoped_fallback_rejects_unlisted_changed_production_fixture(tmp_path):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator._prune_filter = SrcFileFilter(
        src_dirs=["dubbo-demo"],
        test_dirs=["**/src/test/**", "dubbo-demo/**"],
    )
    unlisted = "dubbo-demo/example/src/main/proto/message.proto"
    evaluator.repo_config = {
        "evaluation_fallback_test_graft": {
            "mode": "scoped",
            "authoritative_patterns": ["**/src/test/**"],
            "fixture_paths": {},
            "fail_closed_unlisted_patterns": ["dubbo-demo/**/src/main/**"],
        }
    }
    evaluator._git_ls_tree = lambda tag: (
        {"dubbo-demo/example/src/test/LegacyTest.java"}
        if tag.endswith("-start")
        else {
            "dubbo-demo/example/src/test/NewTest.java",
            unlisted,
        }
    )
    evaluator._git_changed_paths = lambda _old, _new: {
        "dubbo-demo/example/src/test/LegacyTest.java",
        "dubbo-demo/example/src/test/NewTest.java",
        unlisted,
    }

    ok, error = evaluator._graft_ground_truth_tests("end")

    assert ok is False
    assert "outside the explicit authority contract" in error
    assert unlisted in error
    assert evaluator._eval_meta["gt_test_graft_unlisted_changed_paths"] == [
        unlisted
    ]


@pytest.mark.parametrize(
    "policy, message",
    [
        ({"mode": "other"}, "mode must be 'scoped'"),
        (
            {
                "mode": "scoped",
                "authoritative_patterns": ["../src/test/**"],
                "fail_closed_unlisted_patterns": ["**/src/main/**"],
            },
            "unsafe repository glob",
        ),
        (
            {
                "mode": "scoped",
                "authoritative_patterns": ["**/src/test/**"],
                "fail_closed_unlisted_patterns": ["**/src/main/**"],
                "fixture_paths": {"M001": ["pom.xml"]},
            },
            "cannot own build manifest",
        ),
    ],
)
def test_scoped_fallback_policy_rejects_invalid_config(tmp_path, policy, message):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.repo_config = {"evaluation_fallback_test_graft": policy}

    with pytest.raises(ValueError, match=message):
        evaluator._fallback_test_graft_policy()


def test_fallback_graft_policy_is_separately_hash_pinned_and_repo_scoped(tmp_path):
    path = tmp_path / "fallback_policy.yaml"
    path.write_text(
        "schema_version: 1\n"
        "repo_name: example_owner_repo_v1_v2\n"
        "policy:\n"
        "  mode: scoped\n"
        "  authoritative_patterns:\n"
        "    - '**/src/test/**'\n"
        "  fixture_paths: {}\n"
        "  fail_closed_unlisted_patterns:\n"
        "    - '**/src/main/**'\n"
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    policy = load_bound_fallback_test_graft_policy(
        "example_owner_repo_v1_v2", path, digest
    )

    assert policy["mode"] == "scoped"
    with pytest.raises(ValueError, match="digest mismatch"):
        load_bound_fallback_test_graft_policy(
            "example_owner_repo_v1_v2", path, "0" * 64
        )
    with pytest.raises(ValueError, match="repo mismatch"):
        load_bound_fallback_test_graft_policy("another_repo", path, digest)


def test_explicit_fallback_graft_policy_overrides_absent_repo_policy(tmp_path):
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator._fallback_test_graft_policy_override = {
        "mode": "scoped",
        "authoritative_patterns": ["**/src/test/**"],
        "fixture_paths": {},
        "fail_closed_unlisted_patterns": ["**/src/main/**"],
    }

    policy = evaluator._fallback_test_graft_policy()

    assert policy["mode"] == "scoped"
    assert policy["authoritative_patterns"] == ("**/src/test/**",)


def test_post_snapshot_script_is_workspace_pinned_hashed_and_fail_closed(tmp_path, monkeypatch):
    script = tmp_path / "dockerfiles" / "closure.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n")
    evaluator = _bare_evaluator(tmp_path / "source_snapshot.tar")
    evaluator.workspace_root = tmp_path
    evaluator.repo_config = {"evaluation_post_snapshot_script": "dockerfiles/closure.sh"}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._run_post_snapshot_script()

    assert ok and not error
    assert calls[0][:2] == ["docker", "cp"]
    assert calls[1][-2:] == ["bash", "/tmp/evaluation-post-snapshot.sh"]
    # Evaluation context must reach the script via env, not container-side
    # inference (reordered datasets pin several milestone tags to one commit).
    exec_cmd = " ".join(calls[1])
    assert "SWE_MILESTONE_ID=M001" in exec_cmd
    assert "SWE_MILESTONE_BASE_TAG=milestone-M001-end" in exec_cmd
    assert "SWE_MILESTONE_LEGACY_SNAPSHOT=0" in exec_cmd
    assert evaluator._eval_meta["post_snapshot_script_applied"] is True
    assert len(evaluator._eval_meta["post_snapshot_script_sha256"]) == 64

    # START fallback passes its base through; legacy downgrade is visible.
    evaluator.snapshot_legacy_unverified = True
    calls.clear()
    ok, error = evaluator._run_post_snapshot_script(base_suffix="start")
    assert ok and not error
    exec_cmd = " ".join(calls[1])
    assert "SWE_MILESTONE_BASE_TAG=milestone-M001-start" in exec_cmd
    assert "SWE_MILESTONE_LEGACY_SNAPSHOT=1" in exec_cmd

    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(returncode=23, stderr="closure broke"),
    )
    ok, error = evaluator._run_post_snapshot_script()
    assert ok is False
    assert "closure broke" in error

    evaluator.repo_config["evaluation_post_snapshot_script"] = "../escape.sh"
    ok, error = evaluator._run_post_snapshot_script()
    assert ok is False
    assert "escapes workspace_root" in error


def test_orchestrator_root_manifest_discovery_failure_is_fatal(monkeypatch):
    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator.container_name = "agent-container"
    monkeypatch.setattr(
        orchestrator_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed(128, stderr="fatal: bad revision"),
    )

    with pytest.raises(RuntimeError, match="root-manifest discovery failed"):
        orchestrator._get_existing_root_files_in_git("agent-impl-M001", ["go.mod"])


def test_orchestrator_source_directory_discovery_failure_is_fatal(monkeypatch):
    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator.container_name = "agent-container"
    monkeypatch.setattr(
        orchestrator_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed(128, stderr="fatal: missing object"),
    )

    with pytest.raises(RuntimeError, match="source-directory discovery failed"):
        orchestrator._get_existing_src_dirs_in_git("agent-impl-M001", ["core"])


def test_manifest_discovery_treats_non_ancestor_as_normal():
    orchestrator = object.__new__(E2EOrchestrator)

    def git(*args):
        if args[:3] == ("tag", "-l", "agent-impl-*"):
            return _completed(stdout="agent-impl-other\nagent-impl-M001\n")
        if args[:2] == ("merge-base", "--is-ancestor"):
            return _completed(returncode=1 if args[2] == "agent-impl-other" else 0)
        if args[:2] == ("rev-list", "--count"):
            return _completed(stdout="0\n")
        if args[0] == "rev-parse":
            return _completed(stdout="base-commit\n")
        if args[0] == "-c" and "diff" in args:
            if "--diff-filter=D" in args:
                return _completed(stdout="legacy-module/pom.xml\0")
            return _completed(stdout="dubbo-dependencies-bom/pom.xml\0core/main.java\0")
        raise AssertionError(f"unexpected git invocation: {args}")

    orchestrator._docker_exec_git = git

    assert orchestrator._get_changed_build_manifests_in_git("agent-impl-M001") == {
        "dubbo-dependencies-bom/pom.xml"
    }
    overlay = orchestrator._get_build_manifest_overlay_in_git("agent-impl-M001")
    assert overlay.baseline_commit == "base-commit"
    assert overlay.deletes == {"legacy-module/pom.xml"}


def test_manifest_discovery_command_failure_is_fatal_in_both_runners():
    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator._docker_exec_git = lambda *args: _completed(2, stderr="git unavailable")
    with pytest.raises(RuntimeError, match="could not list agent tags"):
        orchestrator._get_changed_build_manifests_in_git("agent-impl-M001")

    runner = object.__new__(MilestoneRunner)
    runner.container_setup = SimpleNamespace(
        docker_exec_git=lambda *args: _completed(2, stderr="git unavailable")
    )
    with pytest.raises(RuntimeError, match="could not list agent tags"):
        runner._get_changed_build_manifests_in_git("agent-impl-M001")


def test_capture_integrity_rejects_even_one_missing_expected_file(tmp_path):
    snapshot = tmp_path / "source_snapshot.tar"
    with tarfile.open(snapshot, "w"):
        pass

    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator.src_filter = SrcFileFilter(src_dirs=["core"], test_dirs=[])

    def git(*args):
        if "ls-tree" in args:
            return _completed(stdout="core/main.go\0")
        if "status" in args:
            return _completed(stdout="")
        if args and args[0] == "tag":
            return _completed(stdout="agent-impl-M001\n")
        raise AssertionError(f"unexpected git invocation: {args}")

    orchestrator._docker_exec_git = git

    with pytest.raises(RuntimeError, match="1/1 expected files are missing"):
        orchestrator._check_snapshot_capture_integrity(
            "agent-impl-M001",
            snapshot,
            ["core"],
            ManifestOverlay.create("base-commit"),
        )

    sidecar = tmp_path / "source_snapshot.integrity.json"
    assert json.loads(sidecar.read_text())["ok"] is False


def test_resume_runtime_gate_fails_before_dag_or_model_state_is_restored(tmp_path):
    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator.container_name = "trial-container"
    orchestrator.trial_root = tmp_path
    orchestrator._container_exists = lambda: True
    orchestrator._is_container_running = lambda: True
    events = []
    orchestrator._record_agent_image_provenance = lambda: events.append("image")

    def fail_runtime():
        events.append("runtime")
        raise RuntimeError("canonical Go cache changed")

    orchestrator.container_setup = SimpleNamespace(
        verify_runtime_environment=fail_runtime,
    )
    orchestrator.dag = SimpleNamespace(
        restore_state=lambda **_kwargs: events.append("dag"),
    )

    with pytest.raises(RuntimeError, match="canonical Go cache changed"):
        orchestrator.setup_environment_for_resume(set(), set(), set(), set(), {})

    assert events == ["image", "runtime"]


def test_tar_filter_still_strips_unchanged_manifest_without_test_patterns(tmp_path):
    snapshot = tmp_path / "source_snapshot.tar"
    with tarfile.open(snapshot, "w") as archive:
        for name, payload in {
            "modules/Main.java": "class Main {}",
            "modules/stale/pom.xml": "<project/>",
        }.items():
            data = payload.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator.src_filter = SrcFileFilter(src_dirs=["modules"], test_dirs=[])
    removed = orchestrator._filter_tar_archive(snapshot, extra_build_manifests=set())

    with tarfile.open(snapshot) as archive:
        names = set(archive.getnames())
    assert removed == 1
    assert "modules/Main.java" in names
    assert "modules/stale/pom.xml" not in names


@pytest.mark.parametrize("capture_type", [E2EOrchestrator, MilestoneRunner])
def test_tar_filter_preserves_in_scope_links_and_filters_by_path(tmp_path, capture_type):
    snapshot = tmp_path / "source_snapshot.tar"
    with tarfile.open(snapshot, "w") as archive:
        payload = b"submitted source"
        regular = tarfile.TarInfo("modules/real.txt")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))

        symlink = tarfile.TarInfo("modules/link.txt")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "real.txt"
        archive.addfile(symlink)

        hardlink = tarfile.TarInfo("modules/hard.txt")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "modules/real.txt"
        archive.addfile(hardlink)

        excluded = tarfile.TarInfo("modules/tests/hidden-link.txt")
        excluded.type = tarfile.SYMTYPE
        excluded.linkname = "../real.txt"
        archive.addfile(excluded)

    capture = object.__new__(capture_type)
    capture.src_filter = SrcFileFilter(
        src_dirs=["modules"],
        test_dirs=["modules/tests/**"],
    )
    removed = capture._filter_tar_archive(
        snapshot,
        extra_build_manifests=set(),
    )

    with tarfile.open(snapshot) as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert removed == 1
    assert members["modules/link.txt"].issym()
    assert members["modules/link.txt"].linkname == "real.txt"
    assert members["modules/hard.txt"].islnk()
    assert members["modules/hard.txt"].linkname == "modules/real.txt"
    assert "modules/tests/hidden-link.txt" not in members


def test_single_runner_root_manifest_discovery_failure_is_fatal(monkeypatch):
    runner = object.__new__(MilestoneRunner)
    runner.container_name = "agent-container"
    monkeypatch.setattr(
        run_milestone_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed(128, stderr="fatal: bad revision"),
    )

    with pytest.raises(RuntimeError, match="root-manifest discovery failed"):
        runner._get_existing_root_files_in_git("agent-impl-M001", ["go.mod"])


def test_single_runner_snapshot_uses_one_immutable_submission_commit(
    tmp_path, monkeypatch
):
    commit = "a" * 40
    runner, observed = _bare_single_snapshot_runner(
        tmp_path,
        monkeypatch,
        tag_commits=[commit, commit],
    )

    snapshot = runner._extract_snapshot(expected_tag_hash=commit)

    assert snapshot.exists()
    for operation in (
        "src",
        "root",
        "overlay",
        "manifest_inventory",
        "archive",
        "integrity",
    ):
        assert observed[operation] == [commit]
    sidecar = json.loads(
        (tmp_path / "evaluation" / "source_snapshot.integrity.json").read_text()
    )
    assert sidecar["agent_tag_commit"] == commit


def test_single_runner_snapshot_rejects_tag_move_during_capture(
    tmp_path, monkeypatch
):
    original = "a" * 40
    moved = "c" * 40
    runner, _observed = _bare_single_snapshot_runner(
        tmp_path,
        monkeypatch,
        tag_commits=[original, moved],
    )

    with pytest.raises(RuntimeError, match="Submission tag moved during snapshot capture"):
        runner._extract_snapshot(expected_tag_hash=original)

    assert not (
        tmp_path / "evaluation" / "source_snapshot.integrity.json"
    ).exists()


def test_evaluator_accepts_tar_bound_three_way_manifest_metadata(tmp_path):
    overlay = ManifestOverlay.create(
        "a" * 40,
        upserts={"pom.xml", "modules/new/pom.xml"},
        deletes={"modules/old/pom.xml"},
    )
    snapshot, _ = _write_snapshot(
        tmp_path,
        {
            "core/Main.java": "class Main {}",
            "pom.xml": "<project/>",
            "modules/new/pom.xml": "<project/>",
        },
        overlay,
    )
    _, loaded = _bare_evaluator(snapshot)._load_and_validate_snapshot_metadata()
    assert loaded == overlay


def test_evaluator_rejects_missing_sidecar_and_v2_hash_mismatch(tmp_path):
    snapshot = tmp_path / "source_snapshot.tar"
    with tarfile.open(snapshot, "w") as archive:
        payload = b"<project/>"
        pom = tarfile.TarInfo("pom.xml")
        pom.size = len(payload)
        archive.addfile(pom, io.BytesIO(payload))
    evaluator = _bare_evaluator(snapshot)
    with pytest.raises(RuntimeError, match="sidecar is missing.*recapture required"):
        evaluator._load_and_validate_snapshot_metadata()

    overlay = ManifestOverlay.create("b" * 40)
    _, sidecar = _write_snapshot(tmp_path, {}, overlay)
    data = json.loads(sidecar.read_text())
    data["snapshot_sha256"] = "0" * 64
    sidecar.write_text(json.dumps(data))
    evaluator = _bare_evaluator(snapshot)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        evaluator._load_and_validate_snapshot_metadata()


def test_evaluator_rejects_pre_v2_sidecar(tmp_path):
    snapshot = tmp_path / "source_snapshot.tar"
    with tarfile.open(snapshot, "w") as archive:
        for name, payload in {
            "core/main.go": "package core\n",
            "go.mod": "module example.com/legacy\n",
        }.items():
            data = payload.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    (tmp_path / "source_snapshot.integrity.json").write_text(
        json.dumps({"tag": "agent-impl-M001", "ok": True})
    )

    evaluator = _bare_evaluator(snapshot)
    evaluator.repo_config = {"evaluation_go_module_closure": True}
    with pytest.raises(RuntimeError, match="schema_version.*recapture required"):
        evaluator._load_and_validate_snapshot_metadata()


def test_evaluator_rejects_unchanged_pom_smuggled_through_source_path(tmp_path):
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"core/Main.java": "class Main {}", "modules/stale/pom.xml": "<project/>"},
        ManifestOverlay.create("c" * 40),
    )
    with pytest.raises(RuntimeError, match="inventory does not match"):
        _bare_evaluator(snapshot)._load_and_validate_snapshot_metadata()


def test_evaluator_applies_manifest_tombstones_with_safe_exact_paths(tmp_path, monkeypatch):
    overlay = ManifestOverlay.create(
        "d" * 40,
        deletes={"modules/old/pom.xml", ".mvn/extensions.xml"},
    )
    snapshot, _ = _write_snapshot(tmp_path, {"core/Main.java": "class Main {}"}, overlay)
    evaluator = _bare_evaluator(snapshot)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed()

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._apply_manifest_deletions()
    assert ok and not error
    assert calls[0][0][-5:] == ["xargs", "-0", "rm", "-f", "--"]
    assert calls[0][1]["input"] == (
        "/testbed/.mvn/extensions.xml\0/testbed/modules/old/pom.xml\0"
    )


def test_evaluator_three_way_merges_changed_manifests(tmp_path, monkeypatch):
    baseline = "e" * 40
    evaluator_base = "2" * 40
    prepared_head = "3" * 40
    overlay = ManifestOverlay.create(
        baseline,
        upserts={"pom.xml", "modules/new/pom.xml", "modules/removed-upstream/pom.xml"},
    )
    snapshot, _ = _write_snapshot(
        tmp_path,
        {
            "pom.xml": "<project><agent/></project>",
            "modules/new/pom.xml": "<project><new/></project>",
            "modules/removed-upstream/pom.xml": "<project><agent/></project>",
        },
        overlay,
    )
    evaluator = _bare_evaluator(snapshot)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(
                stdout=f"{prepared_head}\t{evaluator_base}\tprepared-parent\n"
            )
        path = command[-1]
        state = {
            "pom.xml": "merged\n",
            "modules/new/pom.xml": "agent-added\n",
            "modules/removed-upstream/pom.xml": "evaluator-missing\n",
        }[path]
        return _completed(stdout=state)

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._merge_manifest_upserts()

    assert ok and not error
    assert evaluator._eval_meta["manifest_merged_count"] == 1
    assert evaluator._eval_meta["manifest_agent_exact_count"] == 0
    assert evaluator._eval_meta["manifest_agent_added_count"] == 1
    assert evaluator._eval_meta["manifest_evaluator_missing_count"] == 1
    assert [call[0][-1] for call in calls[1:]] == [
        "modules/new/pom.xml",
        "modules/removed-upstream/pom.xml",
        "pom.xml",
    ]


def test_evaluator_manifest_merge_does_not_require_trial_base_object(tmp_path, monkeypatch):
    baseline = "f" * 40
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"pom.xml": "<project/>"},
        ManifestOverlay.create(baseline, upserts={"pom.xml"}),
    )
    evaluator = _bare_evaluator(snapshot)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _completed(stdout=f"{'2' * 40}\t{'3' * 40}\traw-tag\n")
        return _completed(stdout="agent-exact\n")

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._merge_manifest_upserts()

    assert ok and not error
    assert evaluator._eval_meta["manifest_agent_exact_count"] == 1
    assert baseline not in " ".join(calls[0])


def test_evaluator_manifest_merge_accepts_versioned_environment_patch_marker(
    tmp_path, monkeypatch
):
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"pom.xml": "<project/>"},
        ManifestOverlay.create("f" * 40, upserts={"pom.xml"}),
    )
    evaluator = _bare_evaluator(snapshot)
    calls = []
    real_run = subprocess.run

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            # Exercise the real resolver shell rather than mocking its output.
            script = command[5]
            completed = real_run(
                [
                    "bash",
                    "-c",
                    script.replace(
                        'git_cmd=("$git_bin" -C /testbed -c safe.directory=/testbed)',
                        'git_cmd=("$git_bin" -c safe.directory=/testbed)',
                    ),
                    "evoclaw-resolve-manifest-base",
                    evaluator.milestone_id,
                    "end",
                ],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            return completed
        return _completed(stdout="agent-exact\n")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "pom.xml").write_text("<project/>\n")
    subprocess.run(["git", "add", "pom.xml"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "legacy prepared base"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "pom.xml").write_text("<project><prepared/></project>\n")
    subprocess.run(["git", "add", "pom.xml"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "[ENV-PATCH-v0.91] prepared"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)

    ok, error = evaluator._merge_manifest_upserts()

    assert ok and not error
    assert evaluator._eval_meta["manifest_base_reason"] == "prepared-subject"


def test_evaluator_manifest_merge_fails_closed_without_preparation_base(tmp_path, monkeypatch):
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"pom.xml": "<project/>"},
        ManifestOverlay.create("f" * 40, upserts={"pom.xml"}),
    )
    evaluator = _bare_evaluator(snapshot)
    monkeypatch.setattr(
        "harness.e2e.evaluator.subprocess.run",
        lambda *args, **kwargs: _completed(43, stderr="cannot identify raw end state"),
    )

    ok, error = evaluator._merge_manifest_upserts()

    assert not ok
    assert "manifest preparation baseline" in error
    assert "cannot identify raw end state" in error


def test_evaluator_manifest_merge_conflict_prefers_prepared_environment(tmp_path, monkeypatch):
    baseline = "1" * 40
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"pom.xml": "<project><agent/></project>"},
        ManifestOverlay.create(baseline, upserts={"pom.xml"}),
    )
    evaluator = _bare_evaluator(snapshot)
    invocations = 0

    def run(command, **kwargs):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return _completed(stdout=f"{'2' * 40}\t{'3' * 40}\tprepared-parent\n")
        return _completed(stdout="merged-evaluator-conflict:2\n")

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._merge_manifest_upserts()

    assert ok and not error
    assert evaluator._eval_meta["manifest_conflict_files_count"] == 1
    assert evaluator._eval_meta["manifest_conflict_hunks_count"] == 2


def test_evaluator_manifest_add_add_prefers_prepared_environment(tmp_path, monkeypatch):
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"modules/new/pom.xml": "<project><agent/></project>"},
        ManifestOverlay.create("1" * 40, upserts={"modules/new/pom.xml"}),
    )
    evaluator = _bare_evaluator(snapshot)
    invocations = 0

    def run(command, **kwargs):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return _completed(stdout=f"{'2' * 40}\t{'3' * 40}\tprepared-parent\n")
        return _completed(stdout="evaluator-added-conflict\n")

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._merge_manifest_upserts()

    assert ok and not error
    assert evaluator._eval_meta["manifest_conflict_files_count"] == 1
    assert evaluator._eval_meta["manifest_conflict_hunks_count"] == 0


def test_evaluator_manifest_merge_tool_error_is_fail_closed(tmp_path, monkeypatch):
    snapshot, _ = _write_snapshot(
        tmp_path,
        {"pom.xml": "<project><agent/></project>"},
        ManifestOverlay.create("1" * 40, upserts={"pom.xml"}),
    )
    evaluator = _bare_evaluator(snapshot)
    invocations = 0

    def run(command, **kwargs):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            return _completed(stdout=f"{'2' * 40}\t{'3' * 40}\tprepared-parent\n")
        return _completed(255, stderr="three-way manifest merge failed for pom.xml")

    monkeypatch.setattr("harness.e2e.evaluator.subprocess.run", run)
    ok, error = evaluator._merge_manifest_upserts()

    assert not ok
    assert "Failed to merge build manifest pom.xml" in error
    assert "three-way manifest merge failed" in error
