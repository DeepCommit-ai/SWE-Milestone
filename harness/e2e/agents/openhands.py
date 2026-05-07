"""OpenHands SDK agent framework implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Dict, List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework
from harness.e2e.model_aliases import MODEL_ALIASES


logger = logging.getLogger(__name__)


SDK_RUNNER = r'''#!/usr/bin/env python3
"""Run one OpenHands SDK conversation inside an EvoClaw container."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from openhands.sdk import Agent, Conversation, LLM, Tool
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent, MessageEvent
from openhands.sdk.tool.builtins.finish import FinishAction
from openhands.tools.preset.default import get_default_tools


logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def fake_user_response(_conversation):
    return (
        "Please continue working on the task on whatever approach you think is "
        "suitable.\nWhen you think you have solved the question, please use the "
        "finish tool and include your final answer in the message parameter of "
        "the finish tool.\nIMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP.\n"
    )


def _agent_finished_with_finish_action(events):
    for event in reversed(events):
        if isinstance(event, ActionEvent):
            return event.action is not None and isinstance(event.action, FinishAction)
    return False


def _agent_sent_message(events):
    for event in reversed(events):
        if isinstance(event, MessageEvent) and event.source == "agent":
            return True
        if isinstance(event, ActionEvent):
            return False
    return False


def run_conversation_with_fake_user_response(conversation, max_fake_responses):
    run_timeout = int(os.environ.get("CONVERSATION_TIMEOUT", "3600"))
    fake_response_count = 0

    while True:
        conversation.run(timeout=run_timeout)
        status = conversation.state.execution_status

        if status != ConversationExecutionStatus.FINISHED:
            logger.info(
                "Conversation ended with status: %s after %d fake responses",
                status.value,
                fake_response_count,
            )
            break

        events = list(conversation.state.events)
        if _agent_finished_with_finish_action(events):
            logger.info(
                "Agent finished with FinishAction after %d fake responses",
                fake_response_count,
            )
            break

        if not _agent_sent_message(events):
            logger.warning("Conversation finished without FinishAction or agent message")
            break

        if fake_response_count >= max_fake_responses:
            logger.warning(
                "Reached maximum fake responses (%d), stopping conversation",
                max_fake_responses,
            )
            break

        conversation.send_message(fake_user_response(conversation))
        fake_response_count += 1

    logger.info(
        "Conversation completed. Total fake responses sent: %d",
        fake_response_count,
    )


def build_llm(request):
    llm_kwargs = {
        "model": request["model"],
        "api_key": SecretStr(os.environ["LLM_API_KEY"])
        if os.environ.get("LLM_API_KEY")
        else None,
        "timeout": request["llm_timeout"],
        "max_output_tokens": request["max_output_tokens"],
    }
    if os.environ.get("LLM_BASE_URL"):
        llm_kwargs["base_url"] = os.environ["LLM_BASE_URL"]
    if request.get("reasoning_effort"):
        llm_kwargs["reasoning_effort"] = request["reasoning_effort"]
    return LLM(**llm_kwargs)


def build_agent(llm, request):
    tools = get_default_tools(enable_browser=False)
    if request["enable_delegation"]:
        from openhands.tools.delegate import DelegateTool

        tools.append(Tool(name=DelegateTool.name))

    condenser = None
    if request["enable_condenser"]:
        from openhands.sdk.context.condenser import LLMSummarizingCondenser

        condenser = LLMSummarizingCondenser(
            llm=llm,
            max_size=request["condenser_max_size"],
            keep_first=request["condenser_keep_first"],
        )

    return Agent(
        llm=llm,
        tools=tools,
        condenser=condenser,
        system_prompt_kwargs={"cli_mode": True},
    )


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: openhands_sdk_runner.py REQUEST_JSON")

    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_text())
    prompt = Path(request["prompt_path"]).read_text(encoding="utf-8").strip()

    print("=== OpenHands SDK Runner ===")
    print(f"Model: {request['model']}")
    print(f"Workspace: {request['workspace']}")
    print(f"Prompt file: {request['prompt_path']}")

    llm = build_llm(request)
    agent = build_agent(llm, request)

    persistence_dir = Path(request["persistence_dir"]).expanduser()
    persistence_dir.mkdir(parents=True, exist_ok=True)

    conv_kwargs = {
        "agent": agent,
        "workspace": request["workspace"],
        "persistence_dir": persistence_dir,
        "max_iteration_per_run": request["max_iterations"],
    }
    if request.get("conversation_id"):
        conv_kwargs["conversation_id"] = UUID(request["conversation_id"])

    conversation = Conversation(**conv_kwargs)
    print(f"Conversation ID: {conversation.id}")

    start_time = datetime.now()
    conversation.send_message(prompt)
    run_conversation_with_fake_user_response(
        conversation,
        max_fake_responses=request["max_fake_responses"],
    )
    duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

    results = {
        "success": True,
        "model": request["model"],
        "conversation_id": str(conversation.id),
        "duration_ms": duration_ms,
        "stats": {
            "events_count": len(conversation.state.events)
            if hasattr(conversation.state, "events")
            else 0,
        },
    }

    try:
        stats = conversation.conversation_stats
        if stats:
            results["stats"].update(
                {
                    "total_cost_usd": getattr(stats, "total_cost_usd", 0.0),
                    "total_turns": getattr(stats, "total_turns", 0),
                }
            )
    except Exception as exc:
        print(f"Could not get conversation stats: {exc}")

    print("--SDK Result--")
    print(json.dumps(results, indent=2))
    print("--End SDK Result--")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        print(f"ERROR: {exc}")
        traceback.print_exc()
        print("--SDK Result--")
        print(json.dumps({"success": False, "error": str(exc)}, indent=2))
        print("--End SDK Result--")
        raise SystemExit(1)
'''


@register_framework("openhands")
class OpenHandsFramework(AgentFramework):
    """SDK-only OpenHands framework implementation for EvoClaw."""

    FRAMEWORK_NAME = "openhands"
    DEFAULT_MODEL = "litellm_proxy/gemini-3-flash-preview"

    _MODEL_ALIAS_MAP: Dict[str, str] = MODEL_ALIASES
    _PASSTHROUGH_MODELS = {"gpt-5.3-codex"}

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        enable_delegation: bool = False,
        enable_condenser: bool = True,
        reasoning_effort: Optional[str] = None,
        include_directories: Optional[list[str]] = None,
        **_kwargs,
    ):
        self._api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self._base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        self._model = model or self.DEFAULT_MODEL
        self._enable_delegation = enable_delegation
        self._enable_condenser = enable_condenser
        self._reasoning_effort = self._normalize_reasoning_effort(reasoning_effort)
        self._include_directories = include_directories or []

        if not self._api_key:
            logger.warning(
                "OpenHands API key is not set. Export UNIFIED_API_KEY or pass --api-key."
            )
        if not self._base_url:
            logger.warning(
                "OpenHands base URL is not set. Export UNIFIED_BASE_URL or pass --base-url."
            )

    def get_container_mounts(self) -> List[str]:
        return []

    def get_effective_reasoning_effort(self) -> Optional[str]:
        return self._reasoning_effort

    def get_container_env_vars(self) -> List[str]:
        env_vars: list[str] = []
        if self._api_key:
            env_vars.extend(["-e", f"LLM_API_KEY={self._api_key}"])

        base_url = self._get_effective_base_url()
        if base_url:
            logger.info("OpenHands LLM_BASE_URL: %s", base_url)
            env_vars.extend(["-e", f"LLM_BASE_URL={base_url}"])

        return env_vars

    def get_container_init_script(self, agent_name: str) -> str:
        return r"""
import os
import pwd
import shutil
import subprocess


def run_cmd(cmd, env=None):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.stdout.strip()


target_user = "fakeroot"
target_home = "/home/fakeroot"
target_local_bin = f"{target_home}/.local/bin"
os.makedirs(target_local_bin, exist_ok=True)

try:
    pw = pwd.getpwnam(target_user)
    uid, gid = pw.pw_uid, pw.pw_gid
except KeyError:
    uid, gid = 1000, 1000

if not shutil.which("uv"):
    subprocess.run(["apt-get", "update"], check=True)
    subprocess.run(
        ["apt-get", "install", "-y", "python3-pip", "curl"],
        check=True,
    )
    subprocess.run(
        ["pip3", "install", "--break-system-packages", "uv"],
        check=True,
    )

run_cmd(["uv", "python", "install", "3.12"])

install_env = os.environ.copy()
install_env["UV_TOOL_DIR"] = f"{target_home}/.local/share/uv/tools"
install_env["UV_TOOL_BIN_DIR"] = target_local_bin
install_env["HOME"] = target_home
install_env["PATH"] = f"{target_local_bin}:/root/.local/bin:{install_env.get('PATH', '')}"

try:
    run_cmd(["uv", "tool", "install", "openhands", "--python", "3.12"], env=install_env)
except RuntimeError as exc:
    if "already installed" not in str(exc).lower():
        raise

for path in [
    "/root",
    "/root/.local",
    "/root/.local/share",
    "/root/.local/share/uv",
    "/root/.local/share/uv/python",
]:
    if os.path.exists(path):
        os.chmod(path, 0o755)

for root, dirs, files in os.walk(f"{target_home}/.local"):
    for dirname in dirs:
        os.chown(os.path.join(root, dirname), uid, gid)
    for filename in files:
        os.chown(os.path.join(root, filename), uid, gid)

openhands_home = f"{target_home}/.openhands"
os.makedirs(f"{openhands_home}/conversations", exist_ok=True)
os.chown(openhands_home, uid, gid)
os.chown(f"{openhands_home}/conversations", uid, gid)

python_bin = next(
    p
    for p in sorted(os.listdir("/root/.local/share/uv/python"))
    if p.startswith("cpython-3.12")
)
python_path = f"/root/.local/share/uv/python/{python_bin}/bin/python3"
site_packages = f"{target_home}/.local/share/uv/tools/openhands/lib/python3.12/site-packages"
verify_env = os.environ.copy()
verify_env["PYTHONPATH"] = site_packages
run_cmd(
    [
        python_path,
        "-c",
        "from openhands.sdk import Agent, Conversation, LLM; print('OpenHands SDK ready')",
    ],
    env=verify_env,
)
"""

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        actual_model = self._normalize_model(model if model else self._model)
        return self._build_sdk_run_command(actual_model, prompt_path)

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        actual_model = self._normalize_model(model if model else self._model)
        return self._build_sdk_run_command(
            actual_model,
            message_path,
            conversation_id=session_id,
        )

    def extract_session_id_from_container(self, container_name: str) -> Optional[str]:
        conversations_dir = "/home/fakeroot/.openhands/conversations"
        try:
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
                    "%T@ %f\n",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, Exception) as exc:
            logger.warning("Failed to extract OpenHands conversation id: %s", exc)
            return None

        if result.returncode != 0 or not result.stdout.strip():
            return None

        entries: list[tuple[float, str]] = []
        for line in result.stdout.strip().splitlines():
            timestamp, _, name = line.partition(" ")
            try:
                entries.append((float(timestamp), name))
            except ValueError:
                continue

        if not entries:
            return None

        entries.sort(key=lambda item: item[0], reverse=True)
        return self._normalize_conversation_id(entries[0][1])

    @staticmethod
    def extract_session_id(stdout_content: str) -> Optional[str]:
        cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", stdout_content.strip())
        if not cleaned:
            return None

        matches = re.findall(r"Conversation ID:\s*([a-f0-9-]+)", cleaned)
        if matches:
            return OpenHandsFramework._normalize_conversation_id(matches[-1])
        return None

    def _build_sdk_run_command(
        self,
        model: str,
        prompt_path: str,
        conversation_id: Optional[str] = None,
    ) -> str:
        request = {
            "model": model,
            "prompt_path": prompt_path,
            "workspace": "/testbed",
            "conversation_id": conversation_id,
            "persistence_dir": "/home/fakeroot/.openhands/conversations",
            "enable_delegation": self._enable_delegation,
            "enable_condenser": self._enable_condenser,
            "reasoning_effort": self._reasoning_effort,
            "max_iterations": 3000,
            "max_fake_responses": 10,
            "llm_timeout": 600 if self._reasoning_effort == "xhigh" else 300,
            "max_output_tokens": 32768,
            "condenser_max_size": 100,
            "condenser_keep_first": 4,
        }
        request_json = json.dumps(request, indent=2)

        return f"""export PATH="$HOME/.local/bin:$PATH" && \\
PYTHON_BIN=$(ls -d /root/.local/share/uv/python/cpython-3.12*/bin/python3 2>/dev/null | head -1) && \\
SITE_PACKAGES="$HOME/.local/share/uv/tools/openhands/lib/python3.12/site-packages" && \\
cat > /tmp/openhands_sdk_runner.py << 'OPENHANDS_SDK_RUNNER'
{SDK_RUNNER}
OPENHANDS_SDK_RUNNER
cat > /tmp/openhands_sdk_request.json << 'OPENHANDS_SDK_REQUEST'
{request_json}
OPENHANDS_SDK_REQUEST
PYTHONPATH="$SITE_PACKAGES" "$PYTHON_BIN" /tmp/openhands_sdk_runner.py /tmp/openhands_sdk_request.json"""

    def _get_effective_base_url(self) -> Optional[str]:
        if self._base_url and self._model in self._PASSTHROUGH_MODELS:
            return self._base_url.rstrip("/") + "/openai_passthrough/v1"
        return self._base_url

    def _normalize_model(self, model: str) -> str:
        normalized = (model or "").strip()
        if not normalized:
            return normalized

        parts = normalized.split("/")
        leaf = parts[-1]
        if normalized in self._MODEL_ALIAS_MAP:
            normalized = self._MODEL_ALIAS_MAP[normalized]
        elif leaf in self._MODEL_ALIAS_MAP:
            parts[-1] = self._MODEL_ALIAS_MAP[leaf]
            normalized = "/".join(parts)

        parts = normalized.split("/")
        leaf = parts[-1]
        if leaf in self._PASSTHROUGH_MODELS:
            return f"openai/{leaf}"

        if leaf.startswith("gemini-3") and "-preview" not in leaf:
            parts[-1] = f"{leaf}-preview"
            normalized = "/".join(parts)

        if not normalized.startswith("litellm_proxy/"):
            normalized = f"litellm_proxy/{normalized}"

        return normalized

    @staticmethod
    def _normalize_reasoning_effort(reasoning_effort: Optional[str]) -> Optional[str]:
        if reasoning_effort is None:
            return None
        value = str(reasoning_effort).strip()
        if value.lower() in {"", "none", "off", "false"}:
            return None
        return value

    @staticmethod
    def _normalize_conversation_id(conversation_id: str) -> str:
        if "-" not in conversation_id and len(conversation_id) == 32:
            return (
                f"{conversation_id[:8]}-{conversation_id[8:12]}-"
                f"{conversation_id[12:16]}-{conversation_id[16:20]}-"
                f"{conversation_id[20:]}"
            )
        return conversation_id
