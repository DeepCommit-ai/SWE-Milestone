"""Tests for the trial-config `enable_tool_search` -> ENABLE_TOOL_SEARCH pin
and the per-trial endpoint fields it ships with (base_url / api_key_env)."""

import pytest

from harness.e2e.agents.claude_code import (
    ClaudeCodeFramework,
    validate_tool_search_setting,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, "true"),
        (False, "false"),
        ("true", "true"),
        ("FALSE", "false"),
        ("auto", "auto"),
        ("auto:5", "auto:5"),
        ("auto:0", "auto:0"),
        ("auto:100", "auto:100"),
    ],
)
def test_validate_tool_search_setting_accepts_native_values(value, expected):
    assert validate_tool_search_setting(value) == expected


def test_validate_tool_search_setting_passes_none_through():
    assert validate_tool_search_setting(None) is None


@pytest.mark.parametrize(
    "value",
    ["", "yes", "0", "1", "auto:", "auto:-1", "auto:101", "auto:x", "false; rm -rf /"],
)
def test_validate_tool_search_setting_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="enable_tool_search"):
        validate_tool_search_setting(value)


def test_tool_search_pin_reaches_container_env(monkeypatch):
    monkeypatch.delenv("SWE_MILESTONE_QUARANTINE", raising=False)
    monkeypatch.setenv("SWE_MILESTONE_ENABLE_TOOL_SEARCH", "false")
    framework = ClaudeCodeFramework(api_key="k")
    env = framework.get_container_env_vars()
    assert "ENABLE_TOOL_SEARCH=false" in env


def test_tool_search_unset_leaves_container_env_alone(monkeypatch):
    monkeypatch.delenv("SWE_MILESTONE_QUARANTINE", raising=False)
    monkeypatch.delenv("SWE_MILESTONE_ENABLE_TOOL_SEARCH", raising=False)
    framework = ClaudeCodeFramework(api_key="k")
    env = framework.get_container_env_vars()
    assert not any(e.startswith("ENABLE_TOOL_SEARCH=") for e in env)


def test_tool_search_env_is_revalidated_at_emission(monkeypatch):
    """A hand-set propagation env var can't smuggle arbitrary strings into
    the container environment — emission re-runs the validator."""
    monkeypatch.delenv("SWE_MILESTONE_QUARANTINE", raising=False)
    monkeypatch.setenv("SWE_MILESTONE_ENABLE_TOOL_SEARCH", "false && curl evil")
    framework = ClaudeCodeFramework(api_key="k")
    with pytest.raises(ValueError, match="enable_tool_search"):
        framework.get_container_env_vars()


def test_default_agent_model_fans_out_to_all_class_slots(monkeypatch):
    monkeypatch.delenv("SWE_MILESTONE_QUARANTINE", raising=False)
    monkeypatch.delenv("SWE_MILESTONE_ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.setenv("UNIFIED_DEFAULT_AGENT_MODEL", "kimi-k3")
    framework = ClaudeCodeFramework(api_key="k")
    env = framework.get_container_env_vars()
    for slot in (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "ANTHROPIC_MODEL",
    ):
        assert f"{slot}=kimi-k3" in env
