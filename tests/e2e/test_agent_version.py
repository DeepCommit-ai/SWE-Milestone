import json
import subprocess
from types import SimpleNamespace

import pytest

from harness.e2e.agents.claude_code import (
    ClaudeCodeFramework,
    parse_claude_code_version,
    validate_claude_code_version,
)
from harness.e2e.container_setup import ContainerSetup
from harness.e2e.orchestrator import E2EOrchestrator


@pytest.mark.parametrize("value", ["2.1.158", "stable", "latest"])
def test_validate_claude_code_version_accepts_installer_selectors(value):
    assert validate_claude_code_version(value) == value


@pytest.mark.parametrize("value", ["", "2.1", "v2.1.158", "stable; echo bad"])
def test_validate_claude_code_version_rejects_invalid_selectors(value):
    with pytest.raises(ValueError, match="agent_version"):
        validate_claude_code_version(value)


def test_parse_claude_code_version_normalizes_cli_output():
    assert parse_claude_code_version("2.1.158 (Claude Code)\n") == "2.1.158"
    assert parse_claude_code_version("Claude Code version unavailable") is None


def test_exact_version_disables_updates_and_targets_installer(monkeypatch):
    monkeypatch.delenv("SWE_MILESTONE_QUARANTINE", raising=False)
    framework = ClaudeCodeFramework(agent_version="2.1.158")

    env = framework.get_container_env_vars()
    assert ["-e", "DISABLE_AUTOUPDATER=1"] == env[-2:]

    script = framework.get_container_init_script("claude-code")
    assert "requested_version = '2.1.158'" in script
    assert "install_cmd.append(requested_version)" in script


def test_release_channel_keeps_normal_auto_updates(monkeypatch):
    monkeypatch.delenv("SWE_MILESTONE_QUARANTINE", raising=False)
    framework = ClaudeCodeFramework(agent_version="stable")
    assert "DISABLE_AUTOUPDATER=1" not in framework.get_container_env_vars()
    assert framework.version_matches_request("2.1.158")


def test_container_setup_rejects_mismatched_exact_version(monkeypatch):
    setup = ContainerSetup(
        container_name="trial-container",
        image_name="invalid-image-ref",
        agent_framework_name="claude-code",
        agent_version="2.1.158",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="2.1.157 (Claude Code)\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="version mismatch"):
        setup.get_agent_version(verify_requested=True)


def test_orchestrator_records_actual_agent_version(tmp_path):
    metadata_path = tmp_path / "trial_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "requested_agent_version": "stable",
                "agent_version": None,
            }
        )
    )
    orchestrator = object.__new__(E2EOrchestrator)
    orchestrator.trial_root = tmp_path
    orchestrator.container_setup = SimpleNamespace(
        get_agent_version=lambda **kwargs: "2.1.158"
    )

    assert orchestrator._record_agent_version() == "2.1.158"
    metadata = json.loads(metadata_path.read_text())
    assert metadata == {
        "requested_agent_version": "stable",
        "agent_version": "2.1.158",
    }
