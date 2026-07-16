from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_all as run_all
from harness.e2e.runtime_policy_binding import (
    RUNTIME_POLICY_MODE_PROTECTED,
    ResolvedRuntimePolicy,
    resolve_runtime_policy,
)


REPO = "owner_repo_v1_v2"


def _policy(project_root: Path, raw: str, *, unprotected: bool = False):
    path = project_root / "quarantine_configs" / f"{REPO}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return resolve_runtime_policy(REPO, project_root, unprotected=unprotected)


def _build(repo: Path, project_root: Path, policy=None):
    return run_all.build_cmd(
        repo,
        "claude-code",
        "model",
        100,
        "trial_001",
        None,
        None,
        False,
        project_root=project_root,
        runtime_policy=policy,
    )


def test_fresh_command_uses_exact_policy_for_image_and_worker_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "data" / REPO
    repo.mkdir(parents=True)
    project_root = tmp_path / "project"
    policy = _policy(project_root, "ecosystem: [none]\n")
    seen = []
    monkeypatch.setattr(
        "harness.e2e.runtime_policy_binding.image_for_resolved_policy",
        lambda repo_name, *, protected: seen.append((repo_name, protected))
        or "image:pin",
    )

    cmd, mode = _build(repo, project_root, policy)

    assert mode == "fresh"
    assert cmd[cmd.index("--image") + 1] == "image:pin"
    assert cmd[cmd.index("--expected-runtime-policy-sha256") + 1] == policy.sha256
    assert cmd[cmd.index("--expected-runtime-policy-mode") + 1] == "protected"
    assert "--unprotected" not in cmd
    assert seen == [(REPO, True)]


def test_unprotected_fresh_command_selects_plain_base_and_passes_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "data" / REPO
    repo.mkdir(parents=True)
    project_root = tmp_path / "project"
    policy = _policy(
        project_root,
        "ecosystem: [none]\n",
        unprotected=True,
    )
    seen = []
    monkeypatch.setattr(
        "harness.e2e.runtime_policy_binding.image_for_resolved_policy",
        lambda repo_name, *, protected: seen.append((repo_name, protected))
        or "plain:pin",
    )

    cmd, _ = _build(repo, project_root, policy)

    assert cmd[cmd.index("--image") + 1] == "plain:pin"
    assert "--unprotected" in cmd
    assert cmd[cmd.index("--expected-runtime-policy-mode") + 1] == "unprotected"
    assert seen == [(REPO, False)]


def test_resume_command_does_not_resolve_or_select_from_live_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "data" / REPO
    trial = repo / "e2e_trial" / "trial_001"
    trial.mkdir(parents=True)
    (trial / "trial_metadata.json").write_text("{}\n", encoding="utf-8")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("resume must not consult live runtime policy")

    monkeypatch.setattr(run_all, "resolve_runtime_policy", unexpected)

    cmd, mode = _build(repo, tmp_path / "project")

    assert mode == "resume"
    assert cmd == [
        run_all.sys.executable,
        "-m",
        "harness.e2e.run_e2e",
        "--resume-trial",
        str(trial),
    ]


def test_run_all_resolves_each_fresh_repo_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "scripts").mkdir(parents=True)
    data_root = tmp_path / "data"
    repo = data_root / REPO
    repo.mkdir(parents=True)
    (repo / "metadata.json").write_text("{}\n", encoding="utf-8")
    config = tmp_path / "trial.yaml"
    config.write_text(
        f"data_root: {data_root}\ntrial_name: policy_once_001\n",
        encoding="utf-8",
    )
    raw = b"ecosystem: [none]\n"
    resolved = ResolvedRuntimePolicy(
        repo_name=REPO,
        raw_bytes=raw,
        policy={"ecosystem": ["none"]},
        mode=RUNTIME_POLICY_MODE_PROTECTED,
        source_path=project_root / "quarantine_configs" / f"{REPO}.yaml",
    )
    calls = []
    launches = []

    def resolve_once(repo_name, root, *, unprotected=False):
        calls.append((repo_name, Path(root), unprotected))
        return resolved

    monkeypatch.setattr(run_all, "__file__", str(project_root / "scripts" / "run_all.py"))
    monkeypatch.setattr(run_all, "resolve_runtime_policy", resolve_once)
    monkeypatch.setattr(run_all, "image_for_runtime_policy", lambda _policy: "image:pin")
    monkeypatch.setattr(run_all, "reject_legacy_env", lambda: None)
    # The version gate shells out to git; this test's Popen stub would break it.
    monkeypatch.setattr(run_all, "check_data_version", lambda *_a, **_k: None)
    monkeypatch.setattr(
        run_all.subprocess,
        "Popen",
        lambda cmd, **kwargs: launches.append((cmd, kwargs))
        or SimpleNamespace(pid=1234),
    )
    monkeypatch.setenv("UNIFIED_API_KEY", "test-key")
    monkeypatch.setattr(
        run_all.sys,
        "argv",
        ["run_all.py", "--config", str(config)],
    )

    run_all.main()

    assert calls == [(REPO, project_root, False)]
    assert len(launches) == 1
    command = launches[0][0]
    assert command[command.index("--expected-runtime-policy-sha256") + 1] == resolved.sha256
    assert command[command.index("--expected-runtime-policy-mode") + 1] == "protected"
