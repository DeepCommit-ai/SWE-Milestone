"""Harnessed multi-role agent framework (EvoHarness v0.2-B integration).

Subclasses ClaudeCodeFramework to reuse its container setup VERBATIM — claude install, OAuth
credential mounting, env vars, model resolution. Only *what runs* changes: instead of a single
claude session, build_run_command launches an in-container Python runner (harnessed_runtime/runner.py)
that drives a Dev -> Reviewer -> QA refinement pipeline and creates the `git tag agent-impl-<mid>`
submission signal. EvoClaw's watcher + grader then score the tag exactly as for the bare arm, so the
multi-role-vs-bare A/B is decided by the same grader on the same milestones in the same container.
"""
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

    def _runner_cmd(self, model: str, session_id: str, prompt_path: str) -> str:
        parts = [
            "python3", f"{_CONTAINER_RUNTIME}/runner.py",
            "--model", resolve_model_alias(model),
            "--session-base", session_id,
            "--base-prompt", prompt_path,
            "--roles-dir", f"{_CONTAINER_RUNTIME}/roles",
            "--workspace", "/e2e_workspace",
            "--workdir", "/testbed",
            "--max-review-iters", str(self._max_review_iters),
            "--max-qa-iters", str(self._max_qa_iters),
        ]
        effort = self.get_effective_reasoning_effort()
        if effort:
            parts.extend(["--effort", effort])
        return " ".join(parts)

    def build_run_command(self, model: str, session_id: str, prompt_path: str) -> str:
        return self._runner_cmd(model, session_id, prompt_path)

    def build_resume_command(self, model: str, session_id: str, message_path: str) -> str:
        # The runner is idempotent — it skips milestones already tagged agent-impl-* and picks up
        # any newly-unlocked ones — so "resume" is simply re-running it.
        return self._runner_cmd(model, session_id, message_path)
