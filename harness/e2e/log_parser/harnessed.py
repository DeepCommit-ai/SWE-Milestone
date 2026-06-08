"""Harnessed multi-role agent log parser.

The harnessed runner drives N independent `claude` calls (one per Dev/Reviewer/QA role step),
each with a fresh --session-id, all in /testbed — so every role transcript is stored under
~/.claude/projects/-testbed/. ClaudeCodeLogParser.extract_raw_logs copies that whole project dir,
so it already captures EVERY role transcript. We subclass it and only add two things:

  1. Derive the trial's total cost from the role session JSONLs. (The harnessed agent_stdout.txt
     holds the runner's stage prints, not claude's result JSON, so the stdout-based cost is 0;
     the per-message usage in the JSONLs is the real source.)
  2. Organize the per-session JSONLs into log/roles/<label>.jsonl, using the `session=<id>` markers
     the runner prints in agent_stdout.txt, so each role call's raw trace is directly browsable.

Everything else (tool-call parsing, native usage units, milestone attribution) is inherited
verbatim — the roles are just multiple ordinary Claude Code sessions.
"""
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from harness.e2e.log_parser.base import register_parser
from harness.e2e.log_parser.claude_code import ClaudeCodeLogParser

logger = logging.getLogger(__name__)

# Matches the runner's stage line: "-> claude [dev-milestone_002-0] session=<uuid> (...)"
_ROLE_LINE = re.compile(r"\[([^\]]+)\]\s+session=([0-9a-fA-F-]{36})")


@register_parser("harnessed")
class HarnessedLogParser(ClaudeCodeLogParser):
    """Reuse Claude Code parsing; harnessed roles are just multiple claude sessions."""

    FRAMEWORK_NAME = "harnessed"

    def extract_raw_logs(self, container_name: str, output_dir: Path, session_id: Optional[str] = None) -> Path:
        logs_dir = super().extract_raw_logs(container_name, output_dir, session_id)
        try:
            self._organize_role_sessions(Path(output_dir), logs_dir)
        except Exception as e:  # the per-role view is a nicety — never block stats on it
            logger.warning(f"[harnessed] failed to organize per-role sessions: {e}")
        return logs_dir

    def _organize_role_sessions(self, output_dir: Path, logs_dir: Path) -> None:
        """Copy claude_code/<session>.jsonl → roles/<role-label>.jsonl using runner markers."""
        stdout = output_dir / "agent_stdout.txt"
        if not stdout.exists():
            return
        label_by_sid = {}
        with open(stdout, encoding="utf-8") as f:
            for line in f:
                m = _ROLE_LINE.search(line)
                if m:
                    label_by_sid[m.group(2)] = m.group(1)
        if not label_by_sid:
            return
        roles_dir = output_dir / "roles"
        roles_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for sid, label in label_by_sid.items():
            src = logs_dir / f"{sid}.jsonl"
            if src.exists():
                shutil.copy2(src, roles_dir / f"{label}.jsonl")
                n += 1
        logger.info(f"[harnessed] organized {n}/{len(label_by_sid)} role transcripts → {roles_dir}")

    def parse_stdout_stats(self, stdout_file: Path, logs_dir: Optional[Path] = None) -> dict:
        stats = super().parse_stdout_stats(stdout_file, logs_dir)
        # harnessed agent_stdout has the runner's stage prints, not claude's result JSON → cost 0.
        # Derive the real total cost from the role session JSONLs' per-message usage.
        if not stats.get("total_cost_usd") and logs_dir and Path(logs_dir).exists():
            units = self.parse_native_usage_units(Path(logs_dir), stdout_file)
            total = sum(float(u.cost_usd or 0.0) for u in units)
            if total > 0:
                stats["total_cost_usd"] = total
                if not stats.get("session_count"):
                    n_sessions = len(list(Path(logs_dir).rglob("*.jsonl")))
                    stats["session_count"] = n_sessions
                    stats["unique_session_count"] = n_sessions
                logger.info(f"[harnessed] derived total cost from {len(units)} role usage units: ${total:.2f}")
        return stats
