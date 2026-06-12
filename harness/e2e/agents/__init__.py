"""Agent framework abstraction for the E2E harness.

This module provides a strategy pattern for agent frameworks, allowing
different agents (Claude Code, OpenHands, etc.) to be used interchangeably.

Usage:
    from harness.e2e.agents import get_agent_framework

    framework = get_agent_framework("claude-code")
    mounts = framework.get_container_mounts()
    cmd = framework.build_run_command(model, session_id, prompt_path)
"""

from harness.e2e.agents.base import (
    AgentFramework,
    get_agent_framework,
    register_framework,
)
from harness.e2e.agents.claude_code import ClaudeCodeFramework
from harness.e2e.agents.codex import CodexFramework
from harness.e2e.agents.gemini import GeminiFramework
from harness.e2e.agents.harnessed import HarnessedFramework
from harness.e2e.agents.openhands import OpenHandsFramework

__all__ = [
    "AgentFramework",
    "ClaudeCodeFramework",
    "CodexFramework",
    "GeminiFramework",
    "HarnessedFramework",
    "OpenHandsFramework",
    "get_agent_framework",
    "register_framework",
]
