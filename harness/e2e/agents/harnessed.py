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

    def __init__(self, max_bounces: int = 10, **kwargs):
        super().__init__(**kwargs)
        # Per-role retry budget (Reviewer / QA / CI-fix each get this many independently). Large by
        # default so the refinement loop runs to genuine convergence instead of force-passing early.
        self._max_bounces = int(max_bounces)

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
        # In-container Gitea URL, in preference order:
        # 1) GITEA_INCONTAINER_URL — Gitea's FIXED IP on the coordination network (gitea_up.sh); stable
        #    across Gitea restarts and reachable because container_setup joins the agent to that net.
        # 2) docker inspect the Gitea container's current bridge IP (fallback for older setups).
        # localhost is NOT usable in-container, so we never fall back to it silently.
        url = os.environ.get("GITEA_INCONTAINER_URL", "").strip()
        if not url:
            ip = self._gitea_container_ip()
            port = (re.search(r":(\d+)", os.environ.get("GITEA_URL", "")) or [None, "3000"])[1]
            url = f"http://{ip}:{port}" if ip else ""
        if not url:
            raise RuntimeError("harnessed: cannot resolve in-container Gitea URL — set GITEA_INCONTAINER_URL "
                               "(run scripts/gitea_up.sh) or ensure the Gitea container is up.")
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
            "--max-bounces", os.environ.get("HARNESSED_MAX_BOUNCES", str(self._max_bounces)),
            # WIP cap (0 = unlimited). yaml harnessed_wip_limit -> HARNESSED_WIP_LIMIT (run_all.py).
            # WIP=1 = strict one-PR-at-a-time flow (1R-w1 setup).
            "--wip-limit", os.environ.get("HARNESSED_WIP_LIMIT", "0"),
            # Per-role session strategy (fresh / milestone / persistent), configurable per agent via the
            # HARNESSED_SESSION env or the trial yaml's harnessed_session field.
            "--session-config", os.environ.get("HARNESSED_SESSION", "dev:persistent,reviewer:milestone,qa:milestone"),
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
