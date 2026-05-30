"""Google Gemini CLI agent framework implementation."""

import json
import logging
import os
import subprocess
from typing import List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework

logger = logging.getLogger(__name__)


# Route B: copy the mounted host ADC into fakeroot's home so gemini-cli's
# google-genai SDK can discover it (a 0600 bind mount owned by the host user
# isn't readable by the in-container fakeroot uid, so we copy + chown + chmod).
_GEMINI_ADC_COPY = '''
# === Gemini: install ADC for native Vertex auth ===
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
        print("Installed ADC for Vertex (gemini-cli)")
    else:
        print("WARNING: ADC not mounted at /tmp/host-adc - Vertex auth will fail")
except Exception as _e:
    print(f"Error installing ADC: {_e}")
'''


@register_framework("gemini-cli")
class GeminiFramework(AgentFramework):
    """Agent framework implementation for Google Gemini CLI.

    Gemini CLI is Google's coding agent that runs in the terminal.
    https://github.com/google-gemini/gemini-cli

    This implementation uses unified environment variables for API access:
    - UNIFIED_API_KEY -> GEMINI_API_KEY
    - UNIFIED_BASE_URL -> GOOGLE_GEMINI_BASE_URL

    Environment variables:
        UNIFIED_API_KEY: API key (mapped to GEMINI_API_KEY in container)
        UNIFIED_BASE_URL: Base URL (mapped to GOOGLE_GEMINI_BASE_URL in container)
    """

    FRAMEWORK_NAME = "gemini-cli"

    # Default model for Gemini
    DEFAULT_MODEL = "gemini-3-flash-preview"
    # Some models are only healthy via provider-prefixed alias on our unified proxy.
    _PREFER_PREFIX_MODELS: set[str] = set()

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        include_directories: Optional[List[str]] = None,
        **kwargs,  # Accept and ignore extra params like reasoning_effort
    ):
        """Initialize Gemini framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL. If not provided, uses UNIFIED_BASE_URL env var.
            include_directories: Extra directories for --include-directories.
                E2E mode passes ["/e2e_workspace"]; mstone mode passes nothing.
            **kwargs: Additional arguments (ignored for compatibility).
        """
        self._api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self._base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        self._include_directories = include_directories or []
        # Vertex AI mode (run_all.py sets EVOCLAW_VERTEX when vertex_ai: true).
        # In this mode the `model` is the exact Vertex publisher id, so the
        # "-preview" alias rewrite below must be skipped (Vertex has no
        # "gemini-3.5-flash-preview"; the bridge registers the bare id).
        self._vertex = bool(os.environ.get("EVOCLAW_VERTEX"))
        # Route B (direct Vertex): gemini-cli's built-in Vertex mode talks native
        # Gemini to Vertex via ADC — no LiteLLM bridge (the bridge double-prefixes
        # SSE and breaks gemini-cli's stream parser). run_all.py supplies these.
        self._vertex_project = os.environ.get("EVOCLAW_VERTEX_PROJECT")
        self._vertex_location = os.environ.get("EVOCLAW_VERTEX_LOCATION", "global")
        # gemini-cli runs thinkingLevel HIGH (dynamic budget, thinkingBudget=-1)
        # by default and only exposes a thinking on/off toggle — there are no
        # graded levels and no CLI/settings knob to set one in this version. So
        # the harness reasoning_effort here is normalized to gemini's range:
        # default and maximum is "high" (xhigh/max collapse to high); thinking
        # stays on at the model's native HIGH. Recorded for reporting/parity.
        _eff = (kwargs.get("reasoning_effort") or "high").lower()
        self._reasoning_effort = "high" if _eff in ("high", "xhigh", "max") else _eff

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for Gemini.

        For API mode, no credential files need to be mounted.
        The API key is passed via environment variable.

        Returns:
            List of -v arguments for docker run (empty for API mode)
        """
        # Route B (direct Vertex): mount the host's ADC credentials so gemini-cli
        # can mint Vertex tokens. Mounted read-only at /tmp/host-adc; the init
        # script copies it to fakeroot's home + chowns (a 0600 bind mount owned by
        # the host user isn't readable by the in-container fakeroot uid).
        if self._vertex:
            adc_dir = os.environ.get("CLOUDSDK_CONFIG") or os.path.expanduser("~/.config/gcloud")
            if os.path.isdir(adc_dir):
                return ["-v", f"{adc_dir}:/tmp/host-adc:ro"]
            logger.warning(f"Vertex mode but ADC dir not found: {adc_dir}")
        # API mode doesn't need file mounts - key is passed via env var
        return []

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps unified env vars to Gemini-specific env vars:
        - UNIFIED_API_KEY -> GEMINI_API_KEY
        - UNIFIED_BASE_URL -> GOOGLE_GEMINI_BASE_URL

        Returns:
            List of -e arguments for docker run
        """
        env_vars = []
        # Route B (direct Vertex): gemini-cli's native Vertex mode via ADC.
        # No GEMINI_API_KEY / GOOGLE_GEMINI_BASE_URL (those route to the bridge).
        if self._vertex:
            env_vars.extend(["-e", "GOOGLE_GENAI_USE_VERTEXAI=true"])
            if self._vertex_project:
                env_vars.extend(["-e", f"GOOGLE_CLOUD_PROJECT={self._vertex_project}"])
            env_vars.extend(["-e", f"GOOGLE_CLOUD_LOCATION={self._vertex_location}"])
            return env_vars
        if self._api_key:
            env_vars.extend(["-e", f"GEMINI_API_KEY={self._api_key}"])
            # Non-interactive runs must pre-select the auth method, else gemini
            # errors "Invalid auth method selected" (code 41). API-key auth maps
            # to "gemini-api-key".
            env_vars.extend(["-e", "GEMINI_DEFAULT_AUTH_TYPE=gemini-api-key"])
        if self._base_url:
            env_vars.extend(["-e", f"GOOGLE_GEMINI_BASE_URL={self._base_url}"])
        return env_vars

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for Gemini CLI setup.

        The script:
        1. Installs Node.js 20+ (required for Gemini CLI 0.25.1+)
        2. Installs Gemini CLI via npm
        3. Verifies installation

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        script = """
# === Gemini: Install Node.js 20+ and Gemini CLI ===
try:
    import subprocess
    import os
    import shutil

    def run_cmd(cmd, shell=False):
        '''Run command and return (success, stdout, stderr)'''
        try:
            result = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, '', str(e)

    # Check current Node.js version
    success, node_version, _ = run_cmd(['node', '--version'])
    if success:
        print(f"Current Node.js version: {node_version}")
        try:
            major = int(node_version.lstrip('v').split('.')[0])
            need_upgrade = major < 20
        except:
            need_upgrade = True
    else:
        print("Node.js not found")
        need_upgrade = True

    # Install Node.js 20 if needed
    if need_upgrade:
        print("Installing Node.js 20 via NodeSource...")

        # Ensure curl is available
        if not shutil.which('curl'):
            print("Installing curl...")
            run_cmd(['apt-get', 'update'])
            run_cmd(['apt-get', 'install', '-y', 'curl', 'ca-certificates'])

        # Add NodeSource repository and install Node.js 20
        success, stdout, stderr = run_cmd(
            'curl -fsSL https://deb.nodesource.com/setup_20.x | bash -',
            shell=True
        )
        if not success:
            print(f"Warning: NodeSource setup output: {stderr}")

        success, stdout, stderr = run_cmd(['apt-get', 'install', '-y', 'nodejs'])
        if success:
            success, node_version, _ = run_cmd(['node', '--version'])
            print(f"Node.js installed: {node_version}")
        else:
            print(f"Failed to install Node.js: {stderr}")
            raise Exception("Node.js installation failed")

    # Check if gemini is already installed and working
    gemini_path = shutil.which('gemini')
    if gemini_path:
        success, version, _ = run_cmd(['gemini', '--version'])
        if success:
            print(f"Gemini CLI already installed: {version}")
        else:
            # Gemini exists but doesn't work (probably wrong Node version)
            print("Reinstalling Gemini CLI...")
            run_cmd(['npm', 'uninstall', '-g', '@google/gemini-cli'])
            gemini_path = None

    # Install Gemini CLI if needed
    if not gemini_path:
        print("Installing Gemini CLI via npm...")
        success, stdout, stderr = run_cmd(['npm', 'install', '-g', '@google/gemini-cli'])
        if success:
            print("Gemini CLI installed successfully")
        else:
            print(f"npm install output: {stderr}")

    # Verify final installation
    success, version, stderr = run_cmd(['gemini', '--version'])
    if success:
        print(f"Gemini CLI ready: {version}")
    else:
        print(f"Gemini verification failed: {stderr}")
        raise Exception("Gemini CLI installation failed")

    # Patch defaultModelConfigs.js to register gemini-3.1-pro-preview.
    # Without this, gemini-3.1-pro-preview falls back to 'chat-base' config
    # which lacks thinkingLevel: HIGH, causing degraded thinking quality.
    # Search only the npm global node_modules. Globbing from root_dir='/'
    # recurses into /sys and /proc and effectively hangs (many minutes of
    # 100% CPU walking virtual filesystems).
    import glob as _glob
    _ok, _npm_root, _ = run_cmd(['npm', 'root', '-g'])
    _search_roots = [r for r in [
        _npm_root.strip() if _ok else '',
        '/usr/lib/node_modules', '/usr/local/lib/node_modules',
    ] if r and os.path.isdir(r)]
    _cfg_paths = []
    for _sr in _search_roots:
        _cfg_paths += _glob.glob(_sr + '/**/@google/gemini-cli-core/dist/src/config/defaultModelConfigs.js', recursive=True)
    for cfg_path in dict.fromkeys(_cfg_paths):
        try:
            with open(cfg_path) as _f:
                cfg_content = _f.read()
            if "'gemini-3.1-pro-preview'" not in cfg_content:
                old_marker = "'gemini-3-flash-preview': {"
                new_block = (
                    "'gemini-3.1-pro-preview': {\\n"
                    "            extends: 'chat-base-3',\\n"
                    "            modelConfig: {\\n"
                    "                model: 'gemini-3.1-pro-preview',\\n"
                    "            },\\n"
                    "        },\\n"
                    "        'gemini-3.1-pro-preview-customtools': {\\n"
                    "            extends: 'chat-base-3',\\n"
                    "            modelConfig: {\\n"
                    "                model: 'gemini-3.1-pro-preview-customtools',\\n"
                    "            },\\n"
                    "        },\\n"
                    "        " + old_marker
                )
                cfg_content = cfg_content.replace(old_marker, new_block, 1)
                with open(cfg_path, 'w') as _f:
                    _f.write(cfg_content)
                print(f"Patched Gemini CLI model configs: {cfg_path}")
            else:
                print(f"Gemini CLI model configs already patched: {cfg_path}")
        except Exception as patch_err:
            print(f"Warning: Failed to patch {cfg_path}: {patch_err}")

except Exception as e:
    print(f"Error setting up Gemini: {e}")
    import traceback
    traceback.print_exc()

# === Gemini: pre-select auth method for headless runs ===
# Non-interactive `gemini` reads the auth method from settings.json
# (security.auth.selectedType); the GEMINI_DEFAULT_AUTH_TYPE env var is only
# consulted by the interactive picker. Without this, headless runs fail with
# "Invalid auth method selected" (code 41).
try:
    import os, json, pwd
    fake = pwd.getpwnam('fakeroot')
    uid, gid = fake.pw_uid, fake.pw_gid
    gdir = '/home/fakeroot/.gemini'
    os.makedirs(gdir, exist_ok=True)
    settings = {
        "selectedAuthType": "__AUTH_TYPE__",
        "security": {"auth": {"selectedType": "__AUTH_TYPE__"}},
    }
    with open(os.path.join(gdir, 'settings.json'), 'w') as f:
        json.dump(settings, f, indent=2)
    for root, dirs, files in os.walk(gdir):
        os.chown(root, uid, gid)
        for fn in files:
            os.chown(os.path.join(root, fn), uid, gid)
    print("Wrote Gemini settings.json (auth: __AUTH_TYPE__)")
except Exception as e:
    print(f"Error writing Gemini settings.json: {e}")
__ADC_BLOCK__
"""
        # Vertex (Route B) selects "vertex-ai" auth + installs the mounted ADC;
        # otherwise API-key auth ("gemini-api-key").
        auth_type = "vertex-ai" if self._vertex else "gemini-api-key"
        adc_block = _GEMINI_ADC_COPY if self._vertex else ""
        return script.replace("__AUTH_TYPE__", auth_type).replace("__ADC_BLOCK__", adc_block)

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the Gemini CLI command for running the agent.

        Uses positional prompt for non-interactive execution.

        Args:
            model: Model identifier (e.g., "gemini-2.5-flash")
            session_id: Session ID for conversation tracking (not used by Gemini)
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        actual_model = model if model else self.DEFAULT_MODEL
        actual_model = self._normalize_model_alias(actual_model)

        # Use positional prompt (--prompt is deprecated)
        cmd_parts = [
            "gemini",
            "--model",
            actual_model,
            "--output-format",
            "json",
            "--yolo",  # Auto-approve all operations (bypass sandbox)
            "--skip-trust",  # trust workspace, else --yolo is downgraded in untrusted folders
        ]
        for d in self._include_directories:
            cmd_parts.extend(["--include-directories", d])
        cmd_parts.append(f'"$(cat {prompt_path})"')  # Positional prompt at the end

        return " ".join(cmd_parts)

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the Gemini CLI command for resuming a session.

        Uses `gemini --resume <session_id>` for session resumption.

        Args:
            model: Model identifier
            session_id: Session ID to resume (use "latest" or index number)
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        actual_model = model if model else self.DEFAULT_MODEL
        actual_model = self._normalize_model_alias(actual_model)

        cmd_parts = [
            "gemini",
            "--resume",
            session_id,
            "--model",
            actual_model,
            "--output-format",
            "json",
            "--yolo",
            "--skip-trust",
        ]
        for d in self._include_directories:
            cmd_parts.extend(["--include-directories", d])
        cmd_parts.append(f'"$(cat {message_path})"')  # Positional prompt at the end

        return " ".join(cmd_parts)

    def _normalize_model_alias(self, model: str) -> str:
        """Normalize model alias for proxy compatibility.

        Normalization rules:
        1. For Gemini 3 models without preview suffix, append "-preview".
           Example: gemini-3.1-pro -> gemini-3.1-pro-preview
        2. For known models that require provider prefix on unified proxy,
           add "gemini/" prefix.
        """
        normalized = (model or "").strip()
        if not normalized:
            return normalized

        # Vertex mode: the model id is already the exact Vertex publisher id
        # (e.g. gemini-3.5-flash). Don't append "-preview" or add prefixes —
        # the bridge registers this exact name.
        if self._vertex:
            return normalized

        original = normalized

        # Rule 1: Ensure Gemini 3 model aliases use "-preview" suffix.
        # Supports both plain model names and provider-prefixed forms.
        parts = normalized.split("/")
        leaf = parts[-1]
        if leaf.startswith("gemini-3") and "-preview" not in leaf:
            parts[-1] = f"{leaf}-preview"
            normalized = "/".join(parts)

        # Rule 2: Some aliases are only healthy via gemini/ prefix.
        if "/" not in normalized and normalized in self._PREFER_PREFIX_MODELS:
            normalized = f"gemini/{normalized}"

        if normalized != original:
            logger.info(f"Normalized Gemini model alias: {original} -> {normalized}")
            return normalized

        return normalized

    def extract_session_id_from_container(self, container_name: str) -> Optional[str]:
        """Extract the latest session_id from Gemini session files inside the container.

        Gemini stores session files at:
          ~/.gemini/tmp/<project_hash>/session-{ISO_timestamp}{id_prefix}.json

        We find the latest session file (by sorted filename) and read the
        sessionId field from its JSON content.

        Args:
            container_name: Name of the Docker container

        Returns:
            sessionId from the latest session file, or None
        """
        tmp_dir = "/home/fakeroot/.gemini/tmp"
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "find", tmp_dir, "-name", "session-*.json", "-type", "f"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            files = sorted(result.stdout.strip().split("\n"))
            if not files:
                return None

            # Latest file is last after sorting (filenames contain timestamps)
            latest_file = files[-1]

            # Read the file content from inside the container
            cat_result = subprocess.run(
                ["docker", "exec", container_name, "cat", latest_file],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if cat_result.returncode != 0 or not cat_result.stdout.strip():
                return None

            data = json.loads(cat_result.stdout)
            session_id = data.get("sessionId") or data.get("session_id")
            if session_id:
                return session_id
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to extract session from Gemini container: {e}")

        return None

    @staticmethod
    def extract_session_id(stdout_content: str) -> Optional[str]:
        """Extract session_id from Gemini JSON output.

        Gemini outputs session info in its JSON response (pretty-printed).

        Args:
            stdout_content: Content of agent_stdout.txt

        Returns:
            session_id if found, None otherwise
        """
        import json
        import re

        content = (stdout_content or "").strip()
        if not content:
            return None

        # Keep the latest seen session id because agent_stdout/agent_stderr are append-only.
        latest_session_id = None

        # Try parsing as a single JSON object first (pretty-printed)
        try:
            event = json.loads(content)
            if isinstance(event, dict):
                session_id = event.get("session_id") or event.get("sessionId")
                if session_id:
                    latest_session_id = session_id
        except json.JSONDecodeError:
            pass

        # Try parsing concatenated JSON objects using raw_decode.
        # Keep scanning and return the most recent session id.
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(content):
            # Skip whitespace
            while idx < len(content) and content[idx] in " \t\n\r":
                idx += 1
            if idx >= len(content):
                break

            try:
                obj, end_idx = decoder.raw_decode(content, idx)
                if isinstance(obj, dict):
                    session_id = obj.get("session_id") or obj.get("sessionId")
                    if session_id:
                        latest_session_id = session_id
                idx = end_idx
            except json.JSONDecodeError:
                # Find next potential JSON start
                next_brace = content.find("{", idx + 1)
                if next_brace == -1:
                    break
                idx = next_brace

        if latest_session_id:
            return latest_session_id

        # Fallback for partially malformed output blocks.
        # Example: lines containing `"session_id": "..."` that are not valid full JSON.
        matches = re.findall(r'"(?:session_id|sessionId)"\s*:\s*"([^"]+)"', content)
        if matches:
            return matches[-1]

        return None
