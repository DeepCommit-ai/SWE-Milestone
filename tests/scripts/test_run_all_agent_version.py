"""Tests for run_all agent-version validation."""

import pytest

from scripts.run_all import validate_agent_version


@pytest.mark.parametrize(
    "agent,value,expected",
    [
        ("claude-code", "stable", "stable"),
        ("codex", "0.145.0", "0.145.0"),
        ("codex", "latest", "latest"),
        ("gemini-cli", "1.2.3", "1.2.3"),
    ],
)
def test_validate_agent_version_accepts_supported_agents(agent, value, expected):
    assert validate_agent_version(agent, value) == expected


@pytest.mark.parametrize(
    "agent,value",
    [
        ("codex", "stable"),
        ("gemini-cli", "v1.2.3"),
        ("openhands", "1.2.3"),
    ],
)
def test_validate_agent_version_rejects_invalid_selector(agent, value):
    with pytest.raises(ValueError):
        validate_agent_version(agent, value)
