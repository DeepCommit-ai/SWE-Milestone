"""OpenHands agent framework implementation with CLI and SDK modes."""

import logging
import os
import subprocess
from typing import Dict, List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework
from harness.e2e.model_aliases import MODEL_ALIASES

logger = logging.getLogger(__name__)


@register_framework("openhands")
class OpenHandsFramework(AgentFramework):
    """Agent framework implementation for OpenHands.

    OpenHands is an open-source AI coding agent platform.
    https://github.com/All-Hands-AI/OpenHands

    This implementation supports two modes:
    - CLI mode: Uses `openhands` CLI with --headless flag (default)
    - SDK mode: Uses OpenHands Python SDK with full feature support

    SDK mode advantages:
    - DelegateTool support for sub-agent spawning
    - BrowsingAgent support (if browser environment available)
    - More control over agent configuration
    - Condenser support for context compression

    Environment variables:
        UNIFIED_API_KEY: API key (mapped to LLM_API_KEY in container)
        UNIFIED_BASE_URL: Base URL (mapped to LLM_BASE_URL in container)

    Attributes:
        use_sdk: If True, use SDK mode; if False, use CLI mode (default: False)
        enable_delegation: If True, enable DelegateTool in SDK mode (default: True)
        enable_condenser: If True, enable LLMSummarizingCondenser for context compression (default: True)
    """

    FRAMEWORK_NAME = "openhands"

    # Default model for OpenHands (uses LiteLLM proxy format)
    DEFAULT_MODEL = "litellm_proxy/gemini-3-flash-preview"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        use_sdk: bool = True,
        enable_delegation: bool = False,
        enable_condenser: bool = True,
        reasoning_effort: Optional[str] = None,
        include_directories: Optional[list[str]] = None,
    ):
        """Initialize OpenHands framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL. If not provided, uses UNIFIED_BASE_URL env var.
            model: LLM model to use. If not provided, uses DEFAULT_MODEL.
            use_sdk: If True, use SDK mode instead of CLI mode.
            enable_delegation: If True, enable DelegateTool in SDK mode.
            enable_condenser: If True, enable LLMSummarizingCondenser for context compression.
            reasoning_effort: Reasoning effort level ("low", "medium", "high").
        """
        self._api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self._base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        self._model = model or self.DEFAULT_MODEL

        import logging

        _logger = logging.getLogger(__name__)
        if not self._api_key:
            _logger.warning(
                "⚠️  OpenHands API key not set. "
                "Export UNIFIED_API_KEY or pass --api-key. "
                "The agent will fail to call the LLM."
            )
        if not self._base_url:
            _logger.warning(
                "⚠️  OpenHands base URL not set. "
                "Export UNIFIED_BASE_URL or pass --base-url. "
                "The agent will use the container's default LLM_BASE_URL, "
                "which may lack passthrough routing for models like gpt-5.3-codex."
            )
        self._use_sdk = use_sdk
        self._enable_delegation = enable_delegation
        self._enable_condenser = enable_condenser
        self._effective_model = model or self.DEFAULT_MODEL
        # Resolve reasoning effort.
        # - Explicit "none"/"off"/"" → disable (don't send reasoning_effort to LLM)
        # - Explicit other value → use as-is
        # - Unset → model-aware default:
        #     GPT / Codex    → "xhigh"
        #     Moonshot / kimi → None. kimi-k2.6 ALWAYS runs with thinking
        #                       enabled internally (it's not a toggle), and
        #                       upstream LiteLLM/OpenRouter returns HTTP 400
        #                       UnsupportedParamsError if we send
        #                       reasoning_effort. So the correct setting is
        #                       explicit None — disables the field at build
        #                       time so OpenHands' SDK never attaches it to
        #                       the /v1/chat/completions request. The
        #                       trial_config `reasoning_effort: none` is
        #                       belt-and-suspenders; this auto-default
        #                       protects configs that forgot to set it.
        #     anything else  → "high"
        if reasoning_effort is not None and str(reasoning_effort).lower() in ("none", "off", ""):
            self._reasoning_effort = None
        elif reasoning_effort:
            self._reasoning_effort = reasoning_effort
        else:
            leaf_model = self._effective_model.split("/")[-1].lower()
            if "gpt" in leaf_model or "codex" in leaf_model:
                self._reasoning_effort = "xhigh"
            elif "kimi" in leaf_model or "moonshot" in leaf_model:
                self._reasoning_effort = None
            else:
                self._reasoning_effort = "high"

    @property
    def use_sdk(self) -> bool:
        """Whether to use SDK mode instead of CLI mode."""
        return self._use_sdk

    @use_sdk.setter
    def use_sdk(self, value: bool) -> None:
        """Set whether to use SDK mode."""
        self._use_sdk = value

    def get_effective_reasoning_effort(self) -> Optional[str]:
        """Return effective reasoning effort.

        Always returns a value (default: xhigh for GPT, high for others).
        """
        return self._reasoning_effort

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for OpenHands.

        For API mode, no credential files need to be mounted.
        The API key is passed via environment variable.

        Returns:
            List of -v arguments for docker run (empty for API mode)
        """
        # API mode doesn't need file mounts - key is passed via env var.
        # Quarantine mode: offline pip wheelhouse (shared base helper).
        return self.get_quarantine_mounts()

    def _get_effective_base_url(self) -> Optional[str]:
        """Return the effective base URL, using passthrough for supported models."""
        if self._base_url and self._model in self._PASSTHROUGH_MODELS:
            return self._base_url.rstrip("/") + "/openai_passthrough/v1"
        return self._base_url

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps unified env vars to OpenHands-specific env vars:
        - UNIFIED_API_KEY -> LLM_API_KEY
        - UNIFIED_BASE_URL -> LLM_BASE_URL (uses passthrough endpoint for supported models)

        Returns:
            List of -e arguments for docker run
        """
        import logging

        _logger = logging.getLogger(__name__)

        env_vars = []
        if self._api_key:
            env_vars.extend(["-e", f"LLM_API_KEY={self._api_key}"])
        else:
            _logger.error(
                "❌ UNIFIED_API_KEY is not set — LLM_API_KEY will NOT be injected into the container. "
                "The agent will fail. Set UNIFIED_API_KEY in your environment."
            )
        base_url = self._get_effective_base_url()
        if base_url:
            _logger.info("OpenHands LLM_BASE_URL: %s", base_url)
            env_vars.extend(["-e", f"LLM_BASE_URL={base_url}"])
        else:
            _logger.error(
                "❌ UNIFIED_BASE_URL is not set — LLM_BASE_URL will NOT be injected into the container. "
                "Passthrough models (e.g. gpt-5.3-codex) will fail with 'no healthy deployments'. "
                "Set UNIFIED_BASE_URL in your environment."
            )
        # Quarantine mode: force pip offline to the wheelhouse (shared base helper).
        env_vars.extend(self.get_quarantine_env_vars())
        return env_vars

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for OpenHands setup.

        The script:
        1. Installs uv package manager
        2. Uses uv to install Python 3.12 and OpenHands
        3. Verifies installation

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        return """
# === OpenHands: Install via uv with Python 3.12 ===
try:
    import subprocess
    import os
    import json
    import shutil
    import pwd

    def run_cmd(cmd, shell=False, env=None, user=None):
        '''Run command and return (success, stdout, stderr)'''
        try:
            cmd_env = os.environ.copy()
            if env:
                cmd_env.update(env)

            # If user is specified, run with su
            if user and user != 'root':
                if shell:
                    cmd = f"su - {user} -c '{cmd}'"
                else:
                    cmd = ['su', '-', user, '-c', ' '.join(cmd) if isinstance(cmd, list) else cmd]
                    shell = False

            result = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=600,
                env=cmd_env
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, '', str(e)

    # Target user for installation (fakeroot is the agent user)
    target_user = 'fakeroot'
    target_home = '/home/fakeroot'
    target_local_bin = f'{target_home}/.local/bin'

    # Add local bin to PATH for this session
    os.environ['PATH'] = f"{target_local_bin}:/root/.local/bin:{os.environ.get('PATH', '')}"

    # Check if openhands is already installed for fakeroot user
    openhands_path = os.path.join(target_local_bin, 'openhands')
    if os.path.exists(openhands_path):
        success, version, _ = run_cmd([openhands_path, '--version'])
        if success:
            print(f"OpenHands CLI already installed: {version}")
            raise SystemExit(0)

    # Install uv system-wide if not present
    uv_path = shutil.which('uv')
    if not uv_path:
        print("Installing uv package manager...")
        run_cmd(['apt-get', 'update'])
        run_cmd(['apt-get', 'install', '-y', 'python3-pip', 'curl'])

        # Install uv via pip system-wide
        success, stdout, stderr = run_cmd(['pip3', 'install', '--break-system-packages', 'uv'])
        if not success:
            print("Trying uv curl installer...")
            success, stdout, stderr = run_cmd('curl -LsSf https://astral.sh/uv/install.sh | sh', shell=True)

        if success:
            print("uv installed successfully")
            # Symlink to /usr/local/bin if installed to root's local bin
            if os.path.exists('/root/.local/bin/uv') and not os.path.exists('/usr/local/bin/uv'):
                os.symlink('/root/.local/bin/uv', '/usr/local/bin/uv')
        else:
            print(f"Failed to install uv: {stderr}")
            raise Exception("uv installation failed")

    # Ensure target user's .local/bin exists
    os.makedirs(target_local_bin, exist_ok=True)

    # Get fakeroot uid/gid for ownership
    try:
        pw = pwd.getpwnam(target_user)
        uid, gid = pw.pw_uid, pw.pw_gid
    except KeyError:
        uid, gid = 1000, 1000  # Default if user doesn't exist

    # Install Python 3.12 via uv (system-wide, shared)
    print("Installing Python 3.12 via uv...")
    success, stdout, stderr = run_cmd(['uv', 'python', 'install', '3.12'])
    if success:
        print("Python 3.12 installed via uv")
    else:
        print(f"Python 3.12 install output: {stderr}")

    # Install openhands for fakeroot user using uv
    print(f"Installing OpenHands for user {target_user}...")

    # Set UV_TOOL_DIR to install in fakeroot's directory
    install_env = os.environ.copy()
    install_env['UV_TOOL_DIR'] = f'{target_home}/.local/share/uv/tools'
    install_env['UV_TOOL_BIN_DIR'] = target_local_bin
    install_env['HOME'] = target_home

    success, stdout, stderr = run_cmd(
        ['uv', 'tool', 'install', 'openhands', '--python', '3.12'],
        env=install_env
    )
    if success:
        print("OpenHands installed successfully")
    else:
        print(f"uv tool install output: {stderr}")
        if 'already installed' in stderr.lower():
            print("OpenHands already installed")
        else:
            raise Exception(f"OpenHands installation failed: {stderr}")

    # Fix ownership of installed files
    for root, dirs, files in os.walk(f'{target_home}/.local'):
        for d in dirs:
            os.chown(os.path.join(root, d), uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)

    # Make /root and uv python directory accessible to fakeroot
    # (uv installs Python to /root/.local/share/uv/python/)
    os.chmod('/root', 0o755)
    uv_python_dir = '/root/.local/share/uv/python'
    if os.path.exists(uv_python_dir):
        for root, dirs, files in os.walk(uv_python_dir):
            os.chmod(root, os.stat(root).st_mode | 0o005)  # Add read+execute for others
            for f in files:
                fpath = os.path.join(root, f)
                os.chmod(fpath, os.stat(fpath).st_mode | 0o004)  # Add read for others
    # Also make the parent directories accessible
    for p in ['/root/.local', '/root/.local/share', '/root/.local/share/uv']:
        if os.path.exists(p):
            os.chmod(p, 0o755)

    # Verify final installation
    if os.path.exists(openhands_path):
        success, version, _ = run_cmd([openhands_path, '--version'])
        if success:
            print(f"OpenHands ready: {version}")
            print(f"Installed at: {openhands_path}")
        else:
            raise Exception("OpenHands verification failed")
    else:
        raise Exception(f"OpenHands not found at {openhands_path}")

    # Create default settings file for headless mode
    print("Creating default OpenHands settings...")
    openhands_dir = f'{target_home}/.openhands'
    os.makedirs(openhands_dir, exist_ok=True)

    # Create minimal agent settings file
    # Use values from UNIFIED_BASE_URL / UNIFIED_API_KEY if available
    llm_base_url = os.environ.get("UNIFIED_BASE_URL", "https://llm-proxy.eval.all-hands.dev")
    llm_api_key = os.environ.get("UNIFIED_API_KEY", "placeholder")
    settings_content = json.dumps({
        "llm": {
            "model": "litellm_proxy/gemini-3-flash-preview",
            "api_key": llm_api_key,
            "base_url": llm_base_url,
            "num_retries": 5,
            "timeout": 300,
            "temperature": 0.0,
            "stream": False,
            "native_tool_calling": True,
            "reasoning_effort": "high",
        },
        "tools": [],
        "mcp_config": {},
        "include_default_tools": ["FinishTool", "ThinkTool"],
        "kind": "Agent",
    }, indent=2)
    settings_path = os.path.join(openhands_dir, 'agent_settings.json')
    with open(settings_path, 'w') as f:
        f.write(settings_content)

    # Fix ownership
    os.chown(openhands_dir, uid, gid)
    os.chown(settings_path, uid, gid)
    print(f"Settings created at: {settings_path}")

    # Create cache directories to avoid permission errors
    cache_dirs = [
        f'{target_home}/.cache',
        f'{target_home}/.cache/chat_templates',
        f'{target_home}/.cache/huggingface',
    ]
    for cache_dir in cache_dirs:
        os.makedirs(cache_dir, exist_ok=True)
        os.chown(cache_dir, uid, gid)
    print(f"Cache directories created")

except SystemExit:
    pass  # Already installed
except Exception as e:
    print(f"Error setting up OpenHands: {e}")
    import traceback
    traceback.print_exc()
"""

    def _get_sdk_runner_script(
        self,
        model: str,
        prompt_path: str,
        workspace: str,
        session_id: Optional[str] = None,
        enable_delegation: bool = True,
        enable_condenser: bool = True,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """Generate Python script for SDK mode execution.

        Args:
            model: Model identifier
            prompt_path: Path to prompt file inside container
            workspace: Workspace directory
            session_id: Optional session ID for resuming
            enable_delegation: Whether to enable DelegateTool
            enable_condenser: Whether to enable LLMSummarizingCondenser
            reasoning_effort: Reasoning effort level ("low", "medium", "high", "xhigh")

        Returns:
            Python script as string
        """
        # Build tools setup code (with proper indentation for inside main())
        tools_setup_lines = [
            "    # Setup tools (disable browser to avoid BrowserGoBackActionWithRisk bug)",
            "    from openhands.tools.preset.default import get_default_tools",
            "    tools = get_default_tools(enable_browser=False)",
        ]
        if enable_delegation:
            tools_setup_lines.extend(
                [
                    "",
                    "    # Enable DelegateTool for sub-agent spawning",
                    "    try:",
                    "        from openhands.tools.delegate import DelegateTool",
                    "        from openhands.sdk.tool import register_tool",
                    '        register_tool("DelegateTool", DelegateTool)',
                    '        tools.append(Tool(name="DelegateTool"))',
                    '        print("DelegateTool enabled")',
                    "    except ImportError as e:",
                    '        print(f"DelegateTool not available: {e}")',
                ]
            )
        tools_setup = "\n".join(tools_setup_lines)

        # Build resume logic (with proper indentation)
        if session_id:
            resume_logic = f"""    # Resume existing conversation
    from uuid import UUID
    conversation_id = UUID("{session_id}")
    print(f"Resuming conversation: {{conversation_id}}")
    # Heal poisoned events before Conversation constructor loads them
    _heal_poisoned_events(PERSISTENCE_DIR, conversation_id)
    # Reset STUCK execution_status so the conversation loop actually processes
    # new messages.  OpenHands persists "stuck" to base_state.json and has no
    # code path to clear it on resume, making the session permanently dead.
    for _subdir in [str(conversation_id), conversation_id.hex]:
        _bs_path = PERSISTENCE_DIR / _subdir / "base_state.json"
        if _bs_path.exists():
            try:
                _bs = json.loads(_bs_path.read_text())
                _old_status = _bs.get("execution_status", "")
                if _old_status in ("stuck", "error"):
                    _bs["execution_status"] = "finished"
                    _bs_path.write_text(json.dumps(_bs))
                    print(f"[UNSTUCK] Reset execution_status '{{_old_status}}' -> 'finished' in {{_bs_path.name}}")
            except Exception as _e:
                print(f"[UNSTUCK] Warning: could not patch base_state.json: {{_e}}")"""
        else:
            resume_logic = """    # New conversation
    conversation_id = None"""

        # Build reasoning_effort config
        # Pass to all models — LiteLLM handles mapping per provider:
        #   OpenAI: native reasoning_effort parameter
        #   Anthropic: mapped to extended thinking / budget_tokens
        #   Others: silently ignored if unsupported
        if reasoning_effort:
            reasoning_effort_str = f'"{reasoning_effort}"'
        else:
            reasoning_effort_str = "None"

        # Build condenser setup code
        # For 200K context models: max_size=100 is conservative (avg ~1500 tokens/event)
        # For 128K context models: max_size=60-80 recommended
        # For 32K context models: max_size=20-30 recommended
        if enable_condenser:
            condenser_setup = """
    # Setup LLMSummarizingCondenser for context compression
    # - max_size=100: trigger when events exceed 100
    # - max_tokens: trigger when total tokens exceed threshold (model-dependent)
    # - keep_first=4: preserve initial context (task description, etc.)
    condenser = None
    try:
        from openhands.sdk.context.condenser import LLMSummarizingCondenser
        import re

        # Determine max_tokens based on model context window
        # Strip provider prefixes to get the leaf model name
        _leaf_model = MODEL.split("/")[-1]
        _LARGE_CONTEXT_PATTERNS = [
            r"^gemini-3-flash",
            r"^gemini-3-pro",
            r"^gemini-3\\.1-pro",
        ]
        if any(re.match(p, _leaf_model) for p in _LARGE_CONTEXT_PATTERNS):
            _condenser_max_tokens = 500000
        else:
            _condenser_max_tokens = 160000

        condenser = LLMSummarizingCondenser(
            llm=llm,
            max_size=100,
            max_tokens=_condenser_max_tokens,
            keep_first=4,
        )
        print(f"Condenser enabled: LLMSummarizingCondenser (max_size=100, max_tokens={_condenser_max_tokens}, keep_first=4)")
    except ImportError as e:
        print(f"Condenser not available: {e}")
    except Exception as e:
        print(f"Failed to initialize condenser: {e}")
"""
            condenser_agent_param = "condenser=condenser,"
        else:
            condenser_setup = """
    # Condenser disabled
    condenser = None
"""
            condenser_agent_param = ""

        script = f'''#!/usr/bin/env python3
"""OpenHands SDK runner script - auto-generated."""
import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

# Ensure we can import openhands
sys.path.insert(0, "/home/fakeroot/.local/share/uv/tools/openhands/lib/python3.12/site-packages")

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.sdk.conversation import ConversationExecutionStatus
except ImportError as e:
    print(f"ERROR: Failed to import OpenHands SDK: {{e}}")
    print("Make sure openhands-ai is installed with SDK support")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Monkey-patch: repair malformed JSON in tool call arguments
# ---------------------------------------------------------------------------
# Some models (e.g. kimi-k2.5, minimax-m2.5) emit literal control characters
# (raw newlines, tabs) in tool_call.arguments instead of proper JSON escape
# sequences.  json.loads() rejects them ("Invalid control character",
# "Unterminated string"), which poisons the conversation permanently.
#
# Fix: intercept _get_action_event, and when the raw arguments fail to parse,
# escape all control chars (U+0000-U+001F) and retry.  If the repaired string
# parses, substitute it in-place so the rest of the SDK proceeds normally.
# ---------------------------------------------------------------------------
import re as _re

_CTRL_CHAR_RE = _re.compile(r'[\\x00-\\x1f]')
_CTRL_CHAR_MAP = {{
    0x09: '\\\\t',   # TAB
    0x0a: '\\\\n',   # LF
    0x0d: '\\\\r',   # CR
}}

def _escape_ctrl(m):
    code = ord(m.group(0))
    return _CTRL_CHAR_MAP.get(code, f'\\\\u{{code:04x}}')

_orig_get_action_event = Agent._get_action_event

def _patched_get_action_event(self, tool_call, *args, **kwargs):
    raw = getattr(tool_call, 'arguments', None)
    if raw:
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            repaired = _CTRL_CHAR_RE.sub(_escape_ctrl, raw)
            try:
                json.loads(repaired)
                tool_call.arguments = repaired
                print(f"[JSON-REPAIR] Repaired malformed args for {{tool_call.name}}")
            except json.JSONDecodeError:
                pass  # let original error propagate
    return _orig_get_action_event(self, tool_call, *args, **kwargs)

Agent._get_action_event = _patched_get_action_event
print("JSON repair patch enabled for tool call arguments")

# ---------------------------------------------------------------------------
# Monkey-patch: fix KeyError in tool_call_matching.py
# ---------------------------------------------------------------------------
# ToolCallMatchingProperty.manipulation_indices() uses set.remove() on
# pending_tool_call_ids when processing ObservationBaseEvent.  If an
# observation arrives whose tool_call_id was never added (orphaned tool
# calls from condensed/truncated context), remove() raises KeyError and
# crashes the session.  Fix: use discard() which is a no-op for missing
# keys.
# ---------------------------------------------------------------------------
try:
    from openhands.sdk.context.view.properties.tool_call_matching import (
        ToolCallMatchingProperty,
    )
    from openhands.sdk.event import ActionEvent, ObservationBaseEvent

    _orig_manipulation_indices = ToolCallMatchingProperty.manipulation_indices

    def _patched_manipulation_indices(self, current_view_events):
        from openhands.sdk.context.view.manipulation_indices import ManipulationIndices

        manipulation_indices = ManipulationIndices.complete(current_view_events)
        pending_tool_call_ids = set()

        for index, event in enumerate(current_view_events):
            match event:
                case ActionEvent():
                    pending_tool_call_ids.add(event.tool_call_id)
                case ObservationBaseEvent():
                    pending_tool_call_ids.discard(event.tool_call_id)

            if pending_tool_call_ids:
                manipulation_indices.remove(index + 1)

        return manipulation_indices

    ToolCallMatchingProperty.manipulation_indices = _patched_manipulation_indices
    print("tool_call_matching patch enabled (discard instead of remove)")
except ImportError:
    print("WARNING: Could not patch tool_call_matching (import failed)")

# ---------------------------------------------------------------------------
# Monkey-patch: fix KeyError in event_store.py _path() / _get_single_item()
# ---------------------------------------------------------------------------
# EventLog._path(idx) crashes with KeyError when self._idx_to_id[idx] is
# missing.  This happens when events are removed by the healer or when the
# in-memory index drifts from disk (e.g. after condensation).
#
# Fix: patch _get_single_item to catch KeyError from _path(), trigger a
# re-scan of the event directory, and retry once.  If still missing, skip
# the event gracefully instead of crashing the entire session.
# ---------------------------------------------------------------------------
try:
    from openhands.sdk.conversation.event_store import EventLog

    _orig_get_single_item = EventLog._get_single_item

    def _patched_get_single_item(self, idx):
        import operator as _op
        i = _op.index(idx)
        if i < 0:
            i += self._length
        if i < 0 or i >= self._length:
            raise IndexError("Event index out of range")
        try:
            txt = self._fs.read(self._path(i))
            if not txt:
                raise FileNotFoundError(f"Missing event file at index {{i}}")
            from openhands.sdk.event import Event as _Evt
            return _Evt.model_validate_json(txt)
        except KeyError:
            # idx not in _idx_to_id -- re-scan disk and retry once
            try:
                self._length = self._scan_and_build_index()
            except Exception:
                pass
            if i >= self._length:
                raise IndexError(f"Event index {{i}} out of range after re-scan (length={{self._length}})")
            try:
                txt = self._fs.read(self._path(i))
                if not txt:
                    raise FileNotFoundError(f"Missing event file at index {{i}} after re-scan")
                from openhands.sdk.event import Event as _Evt
                return _Evt.model_validate_json(txt)
            except (KeyError, FileNotFoundError) as exc:
                raise IndexError(f"Event index {{i}} unrecoverable after re-scan: {{exc}}") from exc

    EventLog._get_single_item = _patched_get_single_item
    print("event_store patch enabled (re-scan on KeyError in _get_single_item)")
except ImportError:
    print("WARNING: Could not patch event_store (import failed)")

# ---------------------------------------------------------------------------
# Conversation healer: remove poisoned AgentErrorEvent entries on resume
# ---------------------------------------------------------------------------
# When an AgentErrorEvent was persisted (malformed JSON in tool_call.arguments),
# the conversation is "poisoned" -- on resume the LLM sees the error in its
# context and the agent exits immediately (0ms, 0 turns).
#
# This healer scans the events directory before the Conversation constructor
# loads events, finds AgentErrorEvent entries caused by JSON validation
# failures, and removes them together with their paired ActionEvent.
# ---------------------------------------------------------------------------

def _heal_poisoned_events(persistence_dir, conv_id):
    """Remove poisoned AgentErrorEvent + paired ActionEvent from event log."""
    for subdir in [str(conv_id), conv_id.hex]:
        events_dir = persistence_dir / subdir / "events"
        if events_dir.exists():
            break
    else:
        return 0

    event_files = sorted(events_dir.glob("event-*.json"))
    if not event_files:
        return 0

    # Pass 1: find AgentErrorEvent files with JSON validation errors
    poison_tcids = set()
    error_event_files = set()

    for ef in event_files:
        try:
            data = json.loads(ef.read_text())
        except Exception:
            continue
        if (data.get("kind") == "AgentErrorEvent"
                and "Error validating args" in data.get("error", "")):
            tcid = data.get("tool_call_id")
            if tcid:
                poison_tcids.add(tcid)
            error_event_files.add(ef)

    if not poison_tcids:
        print("[HEAL] No poisoned events found - conversation is clean")
        return 0

    # Pass 2: find paired ActionEvent files (same tool_call_id)
    action_event_files = set()
    for ef in event_files:
        if ef in error_event_files:
            continue
        try:
            data = json.loads(ef.read_text())
        except Exception:
            continue
        if data.get("kind") != "ActionEvent":
            continue
        # Check top-level tool_call_id (error-handler ActionEvents)
        if data.get("tool_call_id") in poison_tcids:
            action_event_files.add(ef)
            continue
        # Also check nested tool_calls list
        for tc in data.get("tool_calls", []):
            if tc.get("id") in poison_tcids:
                action_event_files.add(ef)
                break

    # Overwrite poisoned files with harmless no-op events (instead of deleting,
    # which creates index gaps that crash EventLog._scan_and_build_index).
    import uuid as _uuid
    all_to_heal = error_event_files | action_event_files
    for ef in sorted(all_to_heal):
        try:
            old_data = json.loads(ef.read_text())
        except Exception:
            old_data = {{}}
        # Build a minimal ObservationEvent that keeps the same id, tool_call_id,
        # action_id so the event file is structurally valid but harmless to the LLM context.
        # Must use a valid SDK observation kind (TerminalObservation) and include tool_name.
        _heal_id = old_data.get("id", str(_uuid.uuid4()))
        noop = {{
            "id": _heal_id,
            "timestamp": old_data.get("timestamp", ""),
            "source": "environment",
            "tool_call_id": old_data.get("tool_call_id", str(_uuid.uuid4())),
            "tool_name": old_data.get("tool_name", "terminal"),
            "action_id": old_data.get("action_id", _heal_id),
            "observation": {{
                "content": [{{
                    "type": "text",
                    "text": "[healed: poisoned event removed]",
                    "cache_prompt": False,
                }}],
                "is_error": False,
                "command": "echo healed",
                "exit_code": 0,
                "timeout": False,
                "metadata": {{"exit_code": 0}},
                "kind": "TerminalObservation",
            }},
            "kind": "ObservationEvent",
        }}
        ef.write_text(json.dumps(noop))
        print(f"  [HEAL] Overwrote: {{ef.name}}")

    print(f"[HEAL] Overwrote {{len(all_to_heal)}} poisoned events ({{len(error_event_files)}} errors + {{len(action_event_files)}} actions)")
    return len(all_to_heal)

# Configuration
MODEL = "{model}"
PROMPT_PATH = "{prompt_path}"
WORKSPACE = "{workspace}"
API_KEY = os.environ.get("LLM_API_KEY", "")
BASE_URL = os.environ.get("LLM_BASE_URL", "")
PERSISTENCE_DIR = Path.home() / ".openhands" / "conversations"
REASONING_EFFORT = {reasoning_effort_str}
ENABLE_DELEGATION = {str(enable_delegation)}
ENABLE_CONDENSER = {str(enable_condenser)}
MAX_ITERATION_PER_RUN = 3000
MAX_FAKE_RESPONSES = 10


def fake_user_response(conversation):
    """Generate a fake user response to continue the conversation."""
    return "Please continue. If you have completed the task, use the finish tool to submit your answer."


def _agent_finished_with_finish_action(events):
    """Check if the last action was a FinishAction."""
    for event in reversed(list(events)):
        if hasattr(event, 'action_type'):
            return event.action_type == 'FinishAction'
    return False


def _agent_sent_message(events):
    """Check if agent sent a message (needs user response)."""
    for event in reversed(list(events)):
        if hasattr(event, 'action_type'):
            # MessageAction means agent is waiting for user input
            return event.action_type == 'MessageAction'
        # Some models (e.g. kimi-k2.5) emit bare MessageEvent (thinking out loud)
        # which has 'kind' but no 'action_type'.  Treat it as a message too,
        # otherwise the runner exits prematurely thinking the conversation ended.
        if getattr(event, 'kind', None) == 'MessageEvent':
            return True
    return False


def run_conversation_with_fake_user_response(
    conversation,
    fake_user_response_fn=fake_user_response,
    max_fake_responses=MAX_FAKE_RESPONSES,
):
    """Run a conversation with automatic fake user responses.

    This function runs the conversation and automatically sends fake user responses
    when the agent tries to communicate with the user (sends a message instead of
    using tools). This mimics the behavior of the v0 OpenHands evaluation framework.

    The conversation continues until:
    - The agent calls the finish tool
    - The maximum number of fake responses is reached
    - The conversation enters an error or stuck state
    """
    fake_response_count = 0

    while True:
        # Run the conversation
        conversation.run()

        # Check the execution status
        status = conversation.state.execution_status

        # If not finished, we're done (error, stuck, paused, etc.)
        if status != ConversationExecutionStatus.FINISHED:
            logger.info(
                "Conversation ended with status: %s after %d fake responses",
                status.value,
                fake_response_count,
            )
            break

        # Check if agent finished with FinishAction (proper completion)
        events = list(conversation.state.events)
        if _agent_finished_with_finish_action(events):
            logger.info(
                "Agent finished with FinishAction after %d fake responses",
                fake_response_count,
            )
            break

        # Check if agent sent a message (needs fake response)
        if not _agent_sent_message(events):
            # Agent didn't send a message, but conversation is finished
            # This shouldn't happen normally, but handle it gracefully
            logger.warning(
                "Conversation finished without FinishAction or agent message"
            )
            break

        # Check if we've reached the maximum number of fake responses
        if fake_response_count >= max_fake_responses:
            logger.warning(
                "Reached maximum fake responses (%d), stopping conversation",
                max_fake_responses,
            )
            break

        # Generate and send fake user response
        fake_response = fake_user_response_fn(conversation)

        # Check for exit signal
        if fake_response == "/exit":
            logger.info("Fake user response function returned /exit, stopping")
            break

        logger.info(
            "Sending fake user response #%d: %s...",
            fake_response_count + 1,
            fake_response[:50],
        )
        conversation.send_message(fake_response)
        fake_response_count += 1

    logger.info(
        "Conversation completed. Total fake responses sent: %d", fake_response_count
    )


def main():
    print(f"=== OpenHands SDK Runner ===")
    print(f"Model: {{MODEL}}")
    print(f"Workspace: {{WORKSPACE}}")
    print(f"Prompt file: {{PROMPT_PATH}}")
    print(f"Max iterations per run: {{MAX_ITERATION_PER_RUN}}")
    if REASONING_EFFORT:
        print(f"Reasoning effort: {{REASONING_EFFORT}}")

    # Read prompt
    with open(PROMPT_PATH, "r") as f:
        prompt = f.read().strip()
    print(f"Prompt: {{prompt[:100]}}...")

    # Create LLM
    # Use longer timeout for extended thinking models
    timeout = 600 if REASONING_EFFORT == "xhigh" else 300
    llm_kwargs = {{
        "model": MODEL,
        "api_key": SecretStr(API_KEY) if API_KEY else None,
        "temperature": 0.0,
        "timeout": timeout,
        "max_output_tokens": 32768,  # Explicit cap; LiteLLM defaults can be too large
    }}
    if BASE_URL:
        llm_kwargs["base_url"] = BASE_URL
    if REASONING_EFFORT:
        llm_kwargs["reasoning_effort"] = REASONING_EFFORT

    llm = LLM(**llm_kwargs)
    print(f"LLM initialized: {{MODEL}}")

{tools_setup}
{condenser_setup}
    # Create agent with cli_mode system prompt
    agent = Agent(
        llm=llm,
        tools=tools,
        {condenser_agent_param}
        system_prompt_kwargs={{"cli_mode": True}},
    )
    print(f"Agent created with {{len(tools)}} tools (cli_mode=True, condenser={{'enabled' if condenser else 'disabled'}})")

{resume_logic}

    # Create or resume conversation
    try:
        # Ensure persistence directory exists
        PERSISTENCE_DIR.mkdir(parents=True, exist_ok=True)

        # Create conversation with optional conversation_id for resuming
        conv_kwargs = {{
            "agent": agent,
            "workspace": WORKSPACE,
            "persistence_dir": PERSISTENCE_DIR,
            "max_iteration_per_run": MAX_ITERATION_PER_RUN,
        }}
        if conversation_id:
            conv_kwargs["conversation_id"] = conversation_id
            print(f"Resuming conversation: {{conversation_id}}")

        conversation = Conversation(**conv_kwargs)
        print(f"Conversation ID: {{conversation.id}}")

        # Send message and run with fake user response handler
        conversation.send_message(prompt)
        print("Message sent, running agent with fake user response handler...")

        start_time = datetime.now()
        run_conversation_with_fake_user_response(conversation)
        end_time = datetime.now()

        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        print(f"Agent completed in {{duration_ms}}ms")

        # Output results in JSON format for parsing
        results = {{
            "success": True,
            "model": MODEL,
            "enable_delegation": ENABLE_DELEGATION,
            "enable_condenser": ENABLE_CONDENSER,
            "conversation_id": str(conversation.id),
            "duration_ms": duration_ms,
            "stats": {{
                "events_count": len(conversation.state.events) if hasattr(conversation.state, 'events') else 0,
            }}
        }}

        # Try to get conversation stats
        try:
            stats = conversation.conversation_stats
            if stats:
                results["stats"].update({{
                    "total_cost_usd": getattr(stats, 'total_cost_usd', 0.0),
                    "total_turns": getattr(stats, 'total_turns', 0),
                }})
        except Exception as e:
            print(f"Could not get stats: {{e}}")

        print("--SDK Result--")
        print(json.dumps(results, indent=2))
        print("--End SDK Result--")

        # Also print for CLI compatibility
        print(f"\\nConversation ID: {{conversation.id}}")
        print(f"Hint: run openhands --resume {{conversation.id}} to continue")

    except Exception as e:
        import traceback
        print(f"ERROR: {{e}}")
        traceback.print_exc()
        results = {{
            "success": False,
            "error": str(e),
        }}
        print("--SDK Result--")
        print(json.dumps(results, indent=2))
        print("--End SDK Result--")
        sys.exit(1)

if __name__ == "__main__":
    main()
'''
        return script

    # -- Model alias mapping for LiteLLM proxy --------------------------------
    # Sourced from harness.e2e.model_aliases so the claude-code framework sees
    # the same short-name → proxy-ID mapping. Re-bound as a class attr here so
    # existing references in _normalize_model / tests keep working unchanged.
    _MODEL_ALIAS_MAP: Dict[str, str] = MODEL_ALIASES

    # Models that need litellm's /openai_passthrough endpoint.
    # These bypass litellm's model routing and forward directly to OpenAI,
    # avoiding SSE stream reconstruction issues and missing model group errors.
    _PASSTHROUGH_MODELS = {"gpt-5.3-codex"}

    def _normalize_model(self, model: str) -> str:
        """Normalize model name for LiteLLM proxy compatibility.

        Rules applied in order:
        1. Apply explicit alias mapping (e.g. kimi-k2.5 -> openrouter/moonshotai/kimi-k2.5).
        2. Passthrough models: use 'openai/' prefix (direct OpenAI routing via passthrough).
        3. Gemini 3.x models: append '-preview' if missing.
        4. Add 'litellm_proxy/' prefix if not already present.
        """
        normalized = (model or "").strip()
        if not normalized:
            return normalized

        # Rule 1: explicit alias
        parts = normalized.split("/")
        leaf = parts[-1]
        if normalized in self._MODEL_ALIAS_MAP:
            normalized = self._MODEL_ALIAS_MAP[normalized]
        elif leaf in self._MODEL_ALIAS_MAP:
            parts[-1] = self._MODEL_ALIAS_MAP[leaf]
            normalized = "/".join(parts)

        # Rule 2: passthrough models use openai/ prefix, skip litellm_proxy/
        parts = normalized.split("/")
        leaf = parts[-1]
        if leaf in self._PASSTHROUGH_MODELS:
            normalized = f"openai/{leaf}"
            logger.info(f"Normalized OpenHands model (passthrough): {model} -> {normalized}")
            return normalized

        # Rule 3: gemini-3* needs -preview suffix
        parts = normalized.split("/")
        leaf = parts[-1]
        if leaf.startswith("gemini-3") and "-preview" not in leaf:
            parts[-1] = f"{leaf}-preview"
            normalized = "/".join(parts)

        # Rule 4: litellm_proxy/ prefix
        if not normalized.startswith("litellm_proxy/"):
            normalized = f"litellm_proxy/{normalized}"

        if normalized != f"litellm_proxy/{model}":
            logger.info(f"Normalized OpenHands model: {model} -> {normalized}")

        return normalized

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the command for running the agent.

        Uses CLI mode or SDK mode based on use_sdk setting.

        Args:
            model: Model identifier (e.g., "claude-haiku-4-5-20251001")
            session_id: Session ID for conversation tracking
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        actual_model = self._normalize_model(model if model else self._model)

        if self._use_sdk:
            # For new runs, don't pass session_id - it's just a tracking ID from harness
            # SDK will create its own conversation_id
            return self._build_sdk_run_command(actual_model, prompt_path, session_id=None)
        else:
            return self._build_cli_run_command(actual_model, prompt_path)

    def _build_cli_run_command(self, model: str, prompt_path: str) -> str:
        """Build CLI mode run command.

        Args:
            model: Model identifier
            prompt_path: Path to prompt file

        Returns:
            Shell command string
        """
        cmd = f"""export PATH="$HOME/.local/bin:$PATH" && \\
LLM_MODEL={model} openhands \\
  --headless \\
  --override-with-envs \\
  --always-approve \\
  --json \\
  -f {prompt_path}"""
        return cmd

    def _build_sdk_run_command(
        self,
        model: str,
        prompt_path: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Build SDK mode run command.

        Generates a Python script and runs it.

        Args:
            model: Model identifier
            prompt_path: Path to prompt file
            session_id: Optional session ID for resuming

        Returns:
            Shell command string
        """
        workspace = "/testbed"
        script = self._get_sdk_runner_script(
            model=model,
            prompt_path=prompt_path,
            workspace=workspace,
            session_id=session_id,
            enable_delegation=self._enable_delegation,
            enable_condenser=self._enable_condenser,
            reasoning_effort=self._reasoning_effort,
        )

        # Write script to temp file and execute
        script_path = "/tmp/openhands_sdk_runner.py"

        # Use the Python from uv's openhands tool environment
        # Python is at: /root/.local/share/uv/python/cpython-3.12.*/bin/python3
        # Site-packages at: ~/.local/share/uv/tools/openhands/lib/python3.12/site-packages
        cmd = f"""export PATH="$HOME/.local/bin:$PATH" && \\
PYTHON_BIN=$(ls -d /root/.local/share/uv/python/cpython-3.12*/bin/python3 2>/dev/null | head -1) && \\
SITE_PACKAGES="$HOME/.local/share/uv/tools/openhands/lib/python3.12/site-packages" && \\
cat > {script_path} << 'OPENHANDS_SDK_SCRIPT'
{script}
OPENHANDS_SDK_SCRIPT
PYTHONPATH="$SITE_PACKAGES" $PYTHON_BIN {script_path}"""

        return cmd

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the command for resuming a session.

        Uses CLI mode or SDK mode based on use_sdk setting.

        Args:
            model: Model identifier
            session_id: Session ID to resume
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        actual_model = self._normalize_model(model if model else self._model)

        if self._use_sdk:
            return self._build_sdk_run_command(actual_model, message_path, session_id=session_id)
        else:
            return self._build_cli_resume_command(actual_model, session_id, message_path)

    def _build_cli_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build CLI mode resume command.

        Args:
            model: Model identifier
            session_id: Session ID to resume
            message_path: Path to message file

        Returns:
            Shell command string
        """
        cmd = f"""export PATH="$HOME/.local/bin:$PATH" && \\
LLM_MODEL={model} openhands \\
  --headless \\
  --override-with-envs \\
  --always-approve \\
  --json \\
  --resume {session_id} \\
  -f {message_path}"""
        return cmd

    def extract_session_id_from_container(self, container_name: str) -> Optional[str]:
        """Extract the latest conversation_id from OpenHands conversation dirs in the container.

        OpenHands stores conversations at:
          ~/.openhands/conversations/<conversation_id>/

        Directory names are UUIDs (with or without hyphens), so we sort by
        modification time to find the most recent conversation.

        Args:
            container_name: Name of the Docker container

        Returns:
            conversation_id from the latest conversation directory, or None
        """
        conversations_dir = "/home/fakeroot/.openhands/conversations"
        try:
            # List conversation dirs sorted by modification time (newest first)
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "find",
                    conversations_dir,
                    "-mindepth",
                    "1",
                    "-maxdepth",
                    "1",
                    "-type",
                    "d",
                    "-printf",
                    "%T@ %f\\n",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            # Parse "timestamp dirname" lines and pick the latest
            entries = []
            for line in result.stdout.strip().split("\n"):
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    try:
                        entries.append((float(parts[0]), parts[1]))
                    except ValueError:
                        continue

            if not entries:
                return None

            # Sort by timestamp descending, take the latest
            entries.sort(key=lambda x: x[0], reverse=True)
            conversation_id = entries[0][1]

            # Normalize: add hyphens if missing
            if "-" not in conversation_id and len(conversation_id) == 32:
                conversation_id = (
                    f"{conversation_id[:8]}-{conversation_id[8:12]}-"
                    f"{conversation_id[12:16]}-{conversation_id[16:20]}-"
                    f"{conversation_id[20:]}"
                )

            return conversation_id
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.warning(f"Failed to extract session from OpenHands container: {e}")

        return None

    @staticmethod
    def extract_session_id(stdout_content: str) -> Optional[str]:
        """Extract session_id (conversation_id) from OpenHands output.

        Works for both CLI and SDK modes.

        OpenHands outputs the Conversation ID at the end of execution:
        ```
        Conversation ID: 539c3d7307ba4e3490aa12c9a2ef5cb9
        Hint: run openhands --resume 539c3d73-07ba-4e34-90aa-12c9a2ef5cb9 ...
        ```

        Returns the LAST conversation_id found, since stdout is append-only
        across resumes and earlier entries may be stale.

        Args:
            stdout_content: Content of agent_stdout.txt

        Returns:
            session_id if found, None otherwise
        """
        import re

        content = stdout_content.strip()
        if not content:
            return None

        # Clean ANSI escape codes
        cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", content)

        def _normalize_conversation_id(cid: str) -> str:
            """Add hyphens to bare 32-char hex IDs."""
            if "-" not in cid and len(cid) == 32:
                return f"{cid[:8]}-{cid[8:12]}-{cid[12:16]}-{cid[16:20]}-{cid[20:]}"
            return cid

        # Look for "Conversation ID: xxx" pattern (this is the authoritative source)
        # Works for both CLI and SDK modes — return the LAST match
        matches = re.findall(r"Conversation ID:\s*([a-f0-9-]+)", cleaned)
        if matches:
            return _normalize_conversation_id(matches[-1])

        # Fallback: look for "openhands --resume <id>" hint — return the LAST match
        matches = re.findall(r"openhands --resume\s+([a-f0-9-]+)", cleaned)
        if matches:
            return matches[-1]

        return None

    @staticmethod
    def _fix_json_for_parsing(json_text: str) -> str:
        """Fix malformed JSON with unescaped newlines in string values.

        Args:
            json_text: Raw JSON text

        Returns:
            Fixed JSON text
        """
        result = []
        in_string = False
        escape_next = False

        for char in json_text:
            if escape_next:
                result.append(char)
                escape_next = False
                continue

            if char == "\\":
                result.append(char)
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                result.append(char)
                continue

            if in_string and char == "\n":
                result.append("\\n")
                continue

            if in_string and char == "\t":
                result.append("\\t")
                continue

            result.append(char)

        return "".join(result)
