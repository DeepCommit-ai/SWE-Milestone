"""Harnessed multi-role agent framework (EvoHarness v0.2-B integration).

Subclasses ClaudeCodeFramework to reuse its container setup VERBATIM — claude install, OAuth
credential mounting, env vars, model resolution. Only *what runs* changes: instead of a single
claude session, build_run_command launches an in-container Python runner (harnessed_runtime/runner.py)
that drives a Dev -> Reviewer -> QA refinement pipeline and creates the `git tag agent-impl-<mid>`
submission signal. EvoClaw's watcher + grader then score the tag exactly as for the bare arm, so the
multi-role-vs-bare A/B is decided by the same grader on the same milestones in the same container.
"""
import os
import re
import subprocess
from pathlib import Path
from typing import List

from harness.e2e.agents.base import register_framework
from harness.e2e.agents.claude_code import ClaudeCodeFramework
from harness.e2e.model_aliases import resolve_model_alias

# Host dir holding runner.py + roles/*.md, bind-mounted read-only into the container.
_RUNTIME_DIR = Path(__file__).resolve().parent / "harnessed_runtime"
_CONTAINER_RUNTIME = "/tmp/harnessed"


@register_framework("harnessed")
class HarnessedFramework(ClaudeCodeFramework):
    """Multi-role (Dev/Reviewer/QA) orchestration over Claude Code, run in-container."""

    FRAMEWORK_NAME = "harnessed"

    def __init__(self, max_review_iters: int = 1, max_qa_iters: int = 1, **kwargs):
        super().__init__(**kwargs)
        # Per-milestone refinement budget. Low default keeps debug runs cheap; raise via the
        # trial config (passed through as framework kwargs) for the real run.
        self._max_review_iters = int(max_review_iters)
        self._max_qa_iters = int(max_qa_iters)

    def get_container_mounts(self) -> List[str]:
        # Inherit claude credential/share/extract mounts, then add the runner + role prompts.
        mounts = super().get_container_mounts()
        mounts.extend(["-v", f"{_RUNTIME_DIR}:{_CONTAINER_RUNTIME}:ro"])
        return mounts

    @staticmethod
    def _gitea_container_ip() -> str:
        """Gitea's docker-bridge container IP — the in-container agent reaches it directly there
        (the gateway / host-published-port path is blocked on this host)."""
        name = os.environ.get("GITEA_CONTAINER", "evoharness-gitea")
        try:
            r = subprocess.run(
                ["docker", "inspect", name, "--format",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
                capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""

    def get_container_env_vars(self) -> List[str]:
        env = super().get_container_env_vars()
        # Point GITEA_URL at Gitea's bridge container IP (not localhost / not the gateway).
        port = "3000"
        m = re.search(r":(\d+)", os.environ.get("GITEA_URL", ""))
        if m:
            port = m.group(1)
        ip = self._gitea_container_ip()
        url = f"http://{ip}:{port}" if ip else os.environ.get("GITEA_URL", "http://172.17.0.3:3000")
        env.extend([
            "-e", f"GITEA_URL={url}",
            "-e", f"GITEA_TOKEN={os.environ.get('GITEA_TOKEN', '')}",
            "-e", f"GITEA_ORG={os.environ.get('GITEA_ORG', 'evoclaw')}",
        ])
        return env

    def _controller_cmd(self, model: str, session_id: str) -> str:
        parts = [
            "python3", f"{_CONTAINER_RUNTIME}/controller.py",
            "--model", resolve_model_alias(model),
            "--trial", session_id,
            "--roles-dir", f"{_CONTAINER_RUNTIME}/roles",
            "--workspace", "/e2e_workspace",
            "--testbed", "/testbed",
            "--event-log", "/e2e_workspace/harnessed_events.jsonl",
            "--max-bounces", str(self._max_review_iters + self._max_qa_iters),
        ]
        effort = self.get_effective_reasoning_effort()
        if effort:
            parts.extend(["--effort", effort])
        return " ".join(parts)

    def build_run_command(self, model: str, session_id: str, prompt_path: str) -> str:
        return self._controller_cmd(model, session_id)

    def build_resume_command(self, model: str, session_id: str, message_path: str) -> str:
        # Idempotent: the controller skips milestones already tagged agent-impl-* in /testbed.
        return self._controller_cmd(model, session_id)
