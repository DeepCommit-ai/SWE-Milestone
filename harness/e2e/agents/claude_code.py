"""Claude Code agent framework implementation."""

import logging
import os
import re
from pathlib import Path
from typing import List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework
from harness.e2e.model_aliases import resolve_model_alias

logger = logging.getLogger(__name__)

_CLAUDE_CODE_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_CLAUDE_CODE_VERSION_CHANNELS = {"stable", "latest"}


def validate_claude_code_version(value: Optional[str]) -> Optional[str]:
    """Validate and normalize a Claude Code installer version selector."""
    if value is None:
        return None
    value = str(value).strip()
    if value in _CLAUDE_CODE_VERSION_CHANNELS or _CLAUDE_CODE_VERSION_RE.fullmatch(value):
        return value
    raise ValueError(
        "Claude Code agent_version must be a semantic version such as "
        "'2.1.158', or one of: latest, stable"
    )


def parse_claude_code_version(output: str) -> Optional[str]:
    """Extract the numeric version from ``claude --version`` output."""
    match = _CLAUDE_CODE_VERSION_RE.search(output or "")
    return match.group(1) if match else None


def validate_tool_search_setting(value) -> Optional[str]:
    """Validate/normalize the trial-config ``enable_tool_search`` value.

    Returns the string claude-code's native ENABLE_TOOL_SEARCH env var
    accepts: "true", "false", "auto", or "auto:N" (N = percent of the
    context window, 0-100). YAML booleans are normalized to "true"/"false".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in ("true", "false", "auto"):
        return text
    if text.startswith("auto:"):
        try:
            pct = float(text[5:])
        except ValueError:
            pct = None
        if pct is not None and 0 <= pct <= 100:
            return text
    raise ValueError(
        "enable_tool_search must be true, false, auto, or auto:N "
        f"(N = 0-100, percent of the context window); got {value!r}"
    )


# Vertex (native CLAUDE_CODE_USE_VERTEX): copy the mounted host ADC into
# fakeroot's home so Claude Code's google-auth discovers it at the well-known
# path. A 0600 bind mount owned by the host uid isn't readable by the in-
# container fakeroot uid, so we copy + chown + chmod. Mirrors the gemini-cli
# Vertex path (harness/e2e/agents/gemini.py).
_CLAUDE_ADC_COPY = '''
# === Claude Code: install ADC for native Vertex auth ===
try:
    import os, shutil, pwd
    _fake = pwd.getpwnam('fakeroot')
    _uid, _gid = _fake.pw_uid, _fake.pw_gid
    _src = '/tmp/host-adc/application_default_credentials.json'
    _dst_dir = '/home/fakeroot/.config/gcloud'
    if os.path.exists(_src):
        os.makedirs(_dst_dir, exist_ok=True)
        _dst = os.path.join(_dst_dir, 'application_default_credentials.json')
        shutil.copy2(_src, _dst)
        os.chmod(_dst, 0o600)
        for _root, _dirs, _files in os.walk('/home/fakeroot/.config'):
            os.chown(_root, _uid, _gid)
            for _fn in _files:
                os.chown(os.path.join(_root, _fn), _uid, _gid)
        print("Installed ADC for Vertex (claude-code)")
    else:
        print("WARNING: ADC not mounted at /tmp/host-adc - Vertex auth will fail")
except Exception as _e:
    print(f"Error installing ADC: {_e}")
'''


@register_framework("claude-code")
class ClaudeCodeFramework(AgentFramework):
    """Agent framework implementation for Claude Code CLI.

    Supports three authentication modes:
    1. API mode: Uses UNIFIED_API_KEY and UNIFIED_BASE_URL environment variables
    2. File mode: Uses ~/.claude/.credentials.json file mount
    3. Vertex mode: Claude Code's native CLAUDE_CODE_USE_VERTEX path — talks to
       Vertex AI's Anthropic endpoint via ADC (no API key). Selected when
       run_all.py sets SWE_MILESTONE_VERTEX (vertex_ai: true in the trial config).

    API mode takes precedence when UNIFIED_API_KEY is set; Vertex mode is
    selected by SWE_MILESTONE_VERTEX and ignores the API key / base URL.

    Environment variables:
        UNIFIED_API_KEY: API key (mapped to ANTHROPIC_API_KEY in container)
        UNIFIED_BASE_URL: Base URL (mapped to ANTHROPIC_BASE_URL in container)
        UNIFIED_DEFAULT_AGENT_MODEL: Override for ALL of Claude Code's
            class-based model slots (haiku / sonnet / opus / fable /
            subagent / global default). A single yaml field
            (`default_agent_model`) drives all these env vars to the same
            value — see get_container_env_vars().
    """

    FRAMEWORK_NAME = "claude-code"

    # Mapping from harness reasoning effort levels to Claude Code CLI --effort values.
    # Claude Code accepts: low, medium, high, xhigh, max.
    # Harness uses: low, medium, high, xhigh, max (pass-through).
    EFFORT_MAP = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        agent_version: Optional[str] = None,
        **kwargs,
    ):
        """Initialize Claude Code framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL. If not provided, uses UNIFIED_BASE_URL env var.
            reasoning_effort: Reasoning effort level ("low", "medium", "high", "xhigh").
                             Mapped to Claude Code CLI --effort flag.
                             "xhigh" is mapped to "max" for Claude Code.
            agent_version: Exact Claude Code CLI version, or ``stable``/``latest``.
            **kwargs: Additional arguments (ignored for compatibility).
        """
        self._api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self._base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        # None means "don't set anything" — let the model use its own default
        # (Opus: xhigh). Historically forcing high/max also tripped claude-code
        # #48051 (effort collapsed to medium); fixed in current builds, where
        # effort is sent server-side via output_config.effort (verified 2.1.158:
        # max transmits faithfully and is honored, ~2.6x low's thinking). Unset
        # stays a safe default for any older pinned claude.
        self._reasoning_effort = reasoning_effort
        self._agent_version = validate_claude_code_version(agent_version)
        # Apply short-name aliasing (e.g. "kimi-k2.6" →
        # "openrouter/moonshotai/kimi-k2.6") so every env var and --model flag
        # downstream carries the canonical ID the all-hands LiteLLM proxy
        # expects. Passthrough for unknown/native names.
        raw_agent_model = os.environ.get("UNIFIED_DEFAULT_AGENT_MODEL")
        self._default_agent_model = (
            resolve_model_alias(raw_agent_model) if raw_agent_model else None
        )
        # Vertex AI mode (run_all.py sets SWE_MILESTONE_VERTEX when vertex_ai: true).
        # Claude Code has built-in Vertex support (CLAUDE_CODE_USE_VERTEX): it
        # talks to Vertex's Anthropic endpoint directly using ADC — no API key,
        # no proxy. The `model` stays the bare Vertex id (e.g. claude-opus-4-8);
        # CLOUD_ML_REGION may be a region (us-east5, ...) or "global".
        self._vertex = bool(os.environ.get("SWE_MILESTONE_VERTEX"))
        self._vertex_project = os.environ.get("SWE_MILESTONE_VERTEX_PROJECT")
        self._vertex_location = os.environ.get("SWE_MILESTONE_VERTEX_LOCATION", "global")
        # Auto-compaction window: run_all.py sets SWE_MILESTONE_AUTO_COMPACT_WINDOW
        # from the trial config `auto_compact_window`. Passed through to the
        # container as the native CLAUDE_CODE_AUTO_COMPACT_WINDOW so claude-code
        # compacts context at this token budget. Native agent behaviour — does
        # not alter the model ID or request payload sent to the provider.
        self._auto_compact_window = os.environ.get("SWE_MILESTONE_AUTO_COMPACT_WINDOW")
        # Tool Search (deferred tool loading): run_all.py sets
        # SWE_MILESTONE_ENABLE_TOOL_SEARCH from the trial config
        # `enable_tool_search`, already validated to "true"/"false"/"auto"/
        # "auto:N". Passed through as claude-code's native ENABLE_TOOL_SEARCH.
        # Third-party Anthropic-compatible endpoints that don't forward
        # tool_reference blocks (e.g. Kimi) require "false"; pinning it also
        # keeps the tool-schema context composition identical across endpoints
        # instead of depending on claude-code's endpoint auto-detection.
        self._enable_tool_search = os.environ.get("SWE_MILESTONE_ENABLE_TOOL_SEARCH")

    def get_effective_reasoning_effort(self) -> Optional[str]:
        """Return effective reasoning effort, or None if unset (model default)."""
        return self._reasoning_effort

    def get_requested_version(self) -> Optional[str]:
        return self._agent_version

    def get_version_command(self) -> List[str]:
        return ["claude", "--version"]

    def parse_version_output(self, output: str) -> Optional[str]:
        return parse_claude_code_version(output)

    def version_matches_request(self, actual_version: str) -> bool:
        if self._agent_version in _CLAUDE_CODE_VERSION_CHANNELS:
            return True
        return self._agent_version is None or actual_version == self._agent_version

    def _build_effort_args(self) -> List[str]:
        """Return Claude Code CLI args for reasoning effort.

        Maps harness reasoning effort levels to Claude Code --effort values.
        Unknown effort values log a warning and are dropped (CLI default used)
        — they previously failed silently, which masked a serious misconfig.
        """
        if not self._reasoning_effort:
            return []
        if self._reasoning_effort in self.EFFORT_MAP:
            return ["--effort", self.EFFORT_MAP[self._reasoning_effort]]
        import logging
        logging.getLogger(__name__).warning(
            "Unknown reasoning_effort '%s' — dropped, CLI default used. "
            "Valid values: %s",
            self._reasoning_effort, sorted(self.EFFORT_MAP.keys()),
        )
        return []

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps unified env vars to Claude-specific env vars:
        - UNIFIED_API_KEY -> ANTHROPIC_API_KEY
        - UNIFIED_BASE_URL -> ANTHROPIC_BASE_URL
        - UNIFIED_DEFAULT_AGENT_MODEL -> all of Claude Code's class-based
          model slots (see below)

        Returns:
            List of -e arguments for docker run
        """
        env_vars = []
        if self._vertex:
            # Claude Code's native Vertex mode: talk to Vertex's Anthropic
            # endpoint directly via ADC. Do NOT set ANTHROPIC_API_KEY /
            # ANTHROPIC_BASE_URL — those would route back to the Anthropic API
            # or a custom proxy instead of Vertex. CLOUD_ML_REGION accepts a
            # region (e.g. us-east5) or "global".
            env_vars.extend(["-e", "CLAUDE_CODE_USE_VERTEX=1"])
            if self._vertex_project:
                env_vars.extend(["-e", f"ANTHROPIC_VERTEX_PROJECT_ID={self._vertex_project}"])
            env_vars.extend(["-e", f"CLOUD_ML_REGION={self._vertex_location}"])
            # Point google-auth straight at the ADC the init script installs, so
            # token minting doesn't depend on HOME/well-known-path discovery
            # inside the container (init copies ADC to fakeroot's ~/.config/gcloud).
            env_vars.extend([
                "-e",
                "GOOGLE_APPLICATION_CREDENTIALS=/home/fakeroot/.config/gcloud/"
                "application_default_credentials.json",
            ])
        else:
            if self._api_key:
                env_vars.extend(["-e", f"ANTHROPIC_API_KEY={self._api_key}"])
            if self._base_url:
                env_vars.extend(["-e", f"ANTHROPIC_BASE_URL={self._base_url}"])
        if self._default_agent_model:
            # Route ALL of Claude Code's class-based model slots to the same
            # model. Claude Code has several decision points where it picks a
            # model by "class" rather than using --model:
            #   HAIKU  — background tasks (auto-memory, skill listing,
            #            context management)
            #   SONNET — mid-tier fallback; some skills/subagents target
            #            "sonnet class"
            #   OPUS   — reasoning-heavy fallback
            #   SUBAGENT — Agent/Task-tool spawns (claude-code specific)
            #   FABLE  — Fable/Mythos-class slot (added in newer claude-code;
            #            harmless no-op for older versions)
            #   ANTHROPIC_MODEL — global default when --model is not passed
            #                     (affects nested claude invocations)
            # Leaving any of these unset lets Claude Code fall back to
            # api.anthropic.com with its hard-coded default (e.g.,
            # claude-haiku-4-5), which (a) bypasses the configured
            # UNIFIED_BASE_URL and (b) bills a separate Anthropic account.
            # Pointing all of them at default_agent_model keeps every request
            # on the same endpoint, which is especially critical for
            # third-party proxies (Z.AI, all-hands, OpenRouter).
            for env_name in (
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_FABLE_MODEL",
                "CLAUDE_CODE_SUBAGENT_MODEL",
                "ANTHROPIC_MODEL",
            ):
                env_vars.extend(["-e", f"{env_name}={self._default_agent_model}"])
        # Belt-and-suspenders: also set CLAUDE_CODE_EFFORT_LEVEL alongside the
        # `--effort` CLI flag. Workaround for github.com/anthropics/claude-code
        # issue #41028 where the CLI flag is parsed but not propagated to the
        # API request — env var path is reliable.
        if self._reasoning_effort and self._reasoning_effort in self.EFFORT_MAP:
            env_vars.extend([
                "-e", f"CLAUDE_CODE_EFFORT_LEVEL={self.EFFORT_MAP[self._reasoning_effort]}",
            ])
        # Auto-compaction window (native claude-code env var). Set from trial
        # config `auto_compact_window` via run_all.py's SWE_MILESTONE_AUTO_COMPACT_WINDOW.
        # claude-code triggers context compaction at this token budget; the value
        # is capped at the model's context window (may be 200K for a third-party
        # model whose ID claude-code can't pattern-match).
        if self._auto_compact_window:
            env_vars.extend([
                "-e", f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={self._auto_compact_window}",
            ])
        # Tool Search pin (native claude-code env var). Set from trial config
        # `enable_tool_search` via run_all.py's SWE_MILESTONE_ENABLE_TOOL_SEARCH.
        # Re-validate here so a hand-set env var can't smuggle an arbitrary
        # string into the container environment.
        if self._enable_tool_search:
            env_vars.extend([
                "-e",
                f"ENABLE_TOOL_SEARCH={validate_tool_search_setting(self._enable_tool_search)}",
            ])
        # An exact version is a reproducibility pin, so prevent background
        # self-updates between recovery invocations. Do not use
        # DISABLE_UPDATES here: the official installer also honors it and then
        # silently skips the initial installation. Release channels retain
        # their normal within-channel updates.
        if self._agent_version and self._agent_version not in _CLAUDE_CODE_VERSION_CHANNELS:
            env_vars.extend(["-e", "DISABLE_AUTOUPDATER=1"])
        # Quarantine mode: force pip to the offline wheelhouse (shared base
        # helper so gemini-cli & co. get the same treatment).
        env_vars.extend(self.get_quarantine_env_vars())
        return env_vars

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for Claude credentials.

        When API key is provided via environment, credential file mounts are optional.

        Returns:
            List of -v arguments for docker run
        """
        mounts = []
        home = Path.home()

        # Vertex (native CLAUDE_CODE_USE_VERTEX): mount the host ADC read-only so
        # Claude Code can mint Vertex tokens; the init script copies it into the
        # agent user's home. No API key / credentials file is used in this mode.
        if self._vertex:
            adc_dir = os.environ.get("CLOUDSDK_CONFIG") or str(home / ".config/gcloud")
            adc_file = os.path.join(adc_dir, "application_default_credentials.json")
            if os.path.isfile(adc_file):
                # Mount ONLY the ADC file, not the whole ~/.config/gcloud dir
                # (which also holds access_tokens.db, legacy_credentials/, and
                # possibly SA-key JSONs) into the --yolo container. The init
                # script copies it into the agent user's home.
                mounts.extend([
                    "-v",
                    f"{adc_file}:/tmp/host-adc/application_default_credentials.json:ro",
                ])
            else:
                logger.warning(f"Vertex mode but ADC file not found: {adc_file}")

        # Claude credentials (optional when using API mode)
        claude_creds = home / ".claude/.credentials.json"
        if claude_creds.exists():
            mounts.extend(["-v", f"{claude_creds}:/tmp/host-claude-credentials/.credentials.json:ro"])
        elif not self._api_key and not self._vertex:
            logger.warning("No API key and no credentials file found - authentication may fail")

        # Claude share directory (config files)
        claude_share = home / ".local/share/claude"
        if claude_share.exists():
            mounts.extend(["-v", f"{claude_share}:/tmp/host-claude-share:ro"])

        # Note: Claude binary is installed inside the container via the init script
        # using the standalone installer (no Node.js dependency).

        # extract_claude_logs.py for claude-extract tool
        extract_script = self._find_extract_script()
        if extract_script and extract_script.exists():
            mounts.extend(["-v", f"{extract_script}:/tmp/extract_claude_logs.py:ro"])
            logger.debug(f"Mounted extract_claude_logs.py from {extract_script}")
        else:
            logger.warning("extract_claude_logs.py not found - claude-extract will not be available")

        # Quarantine mode: mount an offline pip wheelhouse (shared base helper).
        mounts.extend(self.get_quarantine_mounts())

        return mounts

    def _find_extract_script(self) -> Optional[Path]:
        """Find extract_claude_logs.py script."""
        # Try venv first
        venv_root = Path(__file__).parent.parent.parent.parent / ".venv"
        extract_script = venv_root / "lib" / "python3.11" / "site-packages" / "extract_claude_logs.py"
        if extract_script.exists():
            return extract_script

        # Try system packages
        try:
            import extract_claude_logs

            return Path(extract_claude_logs.__file__)
        except ImportError:
            return None

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for Claude Code setup.

        The script:
        1. Sets up Claude directories and copies credentials
        2. Creates claude-extract wrapper

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        script = f'''
# === Claude Code: Install standalone binary ===
try:
    import subprocess
    import shutil
    import re

    def run_cmd(cmd, shell=False):
        try:
            result = subprocess.run(
                cmd, shell=shell, capture_output=True, text=True, timeout=300
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, '', str(e)

    requested_version = {self._agent_version!r}

    # Check if claude is already installed and working. Exact matches can be
    # reused; a release channel is re-applied so the installer selects the
    # current version for that channel.
    success, version, _ = run_cmd(['claude', '--version'])
    installed_match = re.search(r'\\b(\\d+\\.\\d+\\.\\d+)\\b', version) if success else None
    installed_version = installed_match.group(1) if installed_match else None
    needs_install = not success
    if requested_version in ('stable', 'latest'):
        needs_install = True
    elif requested_version and installed_version != requested_version:
        needs_install = True

    if not needs_install:
        print(f"Claude Code already installed: {{version}}")
    else:
        target_label = requested_version or 'latest'
        print(f"Installing Claude Code standalone binary (target={{target_label}})...")

        # Ensure curl is available
        if not shutil.which('curl'):
            print("Installing curl...")
            run_cmd(['apt-get', 'update'])
            run_cmd(['apt-get', 'install', '-y', 'curl', 'ca-certificates'])

        # Install via standalone installer (no Node.js required). Download and
        # invoke separately so the validated version is passed as an argv item,
        # without interpolating it into a shell command.
        installer = subprocess.run(
            ['curl', '-fsSL', 'https://claude.ai/install.sh'],
            capture_output=True, text=True, timeout=300
        )
        if installer.returncode != 0:
            raise RuntimeError(f"Failed to download Claude Code installer: {{installer.stderr}}")
        install_cmd = ['bash', '-s']
        if requested_version:
            install_cmd.append(requested_version)
        install_result = subprocess.run(
            install_cmd, input=installer.stdout, capture_output=True, text=True, timeout=300
        )
        success = install_result.returncode == 0
        stdout, stderr = install_result.stdout.strip(), install_result.stderr.strip()
        if success:
            import os

            # Resolve the actual binary path (installer creates symlink chains under /root/)
            claude_link = '/root/.local/bin/claude'
            claude_real = os.path.realpath(claude_link)
            print(f"Claude Code installed at: {{claude_real}}")

            # Copy the actual binary to /usr/local/bin/ so fakeroot user can access it
            # (fakeroot cannot traverse /root/ directory)
            shutil.copy2(claude_real, '/usr/local/bin/claude')
            os.chmod('/usr/local/bin/claude', 0o755)
            print("Copied claude binary to /usr/local/bin/claude")

            success, version, _ = run_cmd(['/usr/local/bin/claude', '--version'])
            print(f"Claude Code ready: {{version}}")
            ready_match = re.search(r'\\b(\\d+\\.\\d+\\.\\d+)\\b', version) if success else None
            ready_version = ready_match.group(1) if ready_match else None
            if requested_version not in (None, 'stable', 'latest') and ready_version != requested_version:
                raise RuntimeError(
                    f"Claude Code version mismatch: requested {{requested_version}}, "
                    f"installed {{ready_version or version or 'unknown'}}"
                )
        else:
            print(f"Failed to install Claude Code: {{stderr}}")
            raise Exception("Claude Code installation failed")

except Exception as e:
    print(f"Error installing Claude Code: {{e}}")

# === Claude Code: Setup Claude directories ===
try:
    import os
    import pwd
    import shutil
    from pathlib import Path

    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid

    # Create Claude directories
    claude_dir = Path('/home/fakeroot/.claude')
    claude_debug = claude_dir / 'debug'
    claude_share = Path('/home/fakeroot/.local/share/claude')

    claude_debug.mkdir(parents=True, exist_ok=True)
    claude_share.mkdir(parents=True, exist_ok=True)

    # Copy credentials file
    cred_src = Path('/tmp/host-claude-credentials/.credentials.json')
    cred_dst = claude_dir / '.credentials.json'
    if cred_src.exists():
        shutil.copy2(cred_src, cred_dst)
        os.chmod(cred_dst, 0o600)
        os.chown(cred_dst, uid, gid)
        print(f"Copied credentials to {{cred_dst}}")

    # Copy config files from share directory
    share_src = Path('/tmp/host-claude-share')
    if share_src.exists() and share_src.is_dir():
        for item in share_src.iterdir():
            dst = claude_share / item.name
            if item.is_file():
                shutil.copy2(item, dst)
            elif item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
        print(f"Copied config files from {{share_src}}")

    # Set ownership for Claude directories
    for root, dirs, files in os.walk('/home/fakeroot/.claude'):
        os.chown(root, uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)
    for root, dirs, files in os.walk('/home/fakeroot/.local'):
        os.chown(root, uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)

except Exception as e:
    print(f"Error setting up Claude directories: {{e}}")

# === Claude Code: Create claude-extract wrapper ===
try:
    extract_script = Path('/tmp/extract_claude_logs.py')
    if extract_script.exists():
        wrapper_content = """#!/usr/bin/env python3
import sys
import os

# Add the script directory to Python path
sys.path.insert(0, '/tmp')

# Import and run the extraction tool
from extract_claude_logs import launch_interactive
sys.exit(launch_interactive())
"""
        wrapper_path = Path('/usr/local/bin/claude-extract')
        with open(wrapper_path, 'w') as f:
            f.write(wrapper_content)
        os.chmod(wrapper_path, 0o755)
        print("Created claude-extract wrapper")
    else:
        print("extract_claude_logs.py not found, claude-extract will not be available")
except Exception as e:
    print(f"Error creating claude-extract wrapper: {{e}}")
'''
        # Vertex (native CLAUDE_CODE_USE_VERTEX): install the mounted host ADC
        # into the agent user's home so Claude Code can mint Vertex tokens.
        if self._vertex:
            script += _CLAUDE_ADC_COPY
        return script

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the Claude CLI command for running the agent.

        Args:
            model: Model identifier
            session_id: Session ID for conversation tracking
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        cmd_parts = [
            "claude",
            "--model",
            resolve_model_alias(model),
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--session-id",
            session_id,
        ]

        cmd_parts.extend(self._build_effort_args())

        cmd_parts.extend(["<", prompt_path])

        return " ".join(cmd_parts)

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the Claude CLI command for resuming a session.

        Args:
            model: Model identifier
            session_id: Session ID to resume
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        cmd_parts = [
            "claude",
            "--model",
            resolve_model_alias(model),
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--resume",
            session_id,
        ]

        cmd_parts.extend(self._build_effort_args())

        cmd_parts.extend(["<", message_path])

        return " ".join(cmd_parts)
