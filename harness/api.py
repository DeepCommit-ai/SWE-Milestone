"""harness/api.py — stable integration surface for external training / eval stacks.

DeepCommit-RL's Polar RL pipeline imports ONLY this module from EvoClaw. Internal
refactors of e2e/ are free as long as these signatures hold semantically. See the
consumer-side contract doc: docs/design/task_source_integration_zh.md.

Layering: EvoClaw owns ALL domain knowledge (prompting, container env, leak
masking, grading). The training stack owns execution (Polar concurrency/trace/
on-policy serving) and optimization (verl GRPO). This module is the seam.

Two-state contract: TaskRecord is the single cross-layer structure — its fields
mirror the parquet row the training stack persists offline; in-process it is the
same dict reconstructed. One schema, two states.

Heavy e2e modules (docker, container surgery) are imported lazily inside the
functions that need them, so importing this module is cheap and side-effect free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

API_VERSION = "1.0"
PROMPT_DIR = Path(__file__).parent / "e2e" / "prompt"
# The node runtime Claude Code needs. go/java/rust testbed images ship none;
# runtime_spec() bootstraps this static build when npm is absent.
NODE_VERSION = "v22.21.1"
CLAUDE_CODE_PKG = "@anthropic-ai/claude-code@2.1.111"


# ───────────────────────────── data contracts ──────────────────────────────
@dataclass
class TaskRecord:
    """One milestone task. Field set == the parquet `extra_info` contract."""
    instance_id: str
    docker_image: str
    problem_statement: str
    fail_to_pass: list = field(default_factory=list)
    pass_to_pass: list = field(default_factory=list)
    framework: str = "ginkgo"
    test_cmd: str = ""
    test_configs: list = field(default_factory=list)
    fail_to_pass_by_framework: dict = field(default_factory=dict)
    pass_to_pass_by_framework: dict = field(default_factory=dict)
    # source-private; the training stack carries it through opaquely. Holds e.g.
    # new_tests (for masking), filter_list (flaky exclusion), repo_config
    # (src/test dirs), quarantine policy name.
    source_spec: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, ei: dict) -> "TaskRecord":
        """Build from a parquet extra_info dict (numpy-array fields tolerated)."""
        def _list(v):
            # numpy-safe: parquet round-trips list fields as ndarray, whose
            # truthiness raises — never use `if v` on these.
            if v is None:
                return []
            if hasattr(v, "tolist"):
                v = v.tolist()
            return list(v)

        def _plain(v):
            # recursively coerce numpy arrays/scalars to plain python so
            # downstream `x or []` truthiness is safe everywhere (parquet
            # round-trips nested lists as ndarray).
            if v is None or isinstance(v, (str, int, float, bool)):
                return v
            if hasattr(v, "item") and not hasattr(v, "__len__"):
                return v.item()
            if hasattr(v, "tolist") and not isinstance(v, (str, bytes)):
                return _plain(v.tolist())
            if isinstance(v, dict):
                return {str(k): _plain(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_plain(x) for x in v]
            return v

        def _dict(v):
            v = _plain(v)
            return v if isinstance(v, dict) else {}

        return cls(
            instance_id=str(ei.get("instance_id", "")),
            docker_image=str(ei["docker_image"]),
            problem_statement=str(ei.get("problem_statement", "")),
            fail_to_pass=_list(ei.get("FAIL_TO_PASS") if "FAIL_TO_PASS" in ei else ei.get("fail_to_pass")),
            pass_to_pass=_list(ei.get("PASS_TO_PASS") if "PASS_TO_PASS" in ei else ei.get("pass_to_pass")),
            framework=str(ei.get("framework") or "ginkgo"),
            test_cmd=str(ei.get("test_cmd") or ""),
            test_configs=_list(ei.get("test_configs")),
            fail_to_pass_by_framework=_dict(ei.get("fail_to_pass_by_framework")),
            pass_to_pass_by_framework=_dict(ei.get("pass_to_pass_by_framework")),
            source_spec=_dict(ei.get("source_spec")),
        )


@dataclass
class RuntimeSpec:
    """How to prepare the work container (the consumer maps this onto its fabric)."""
    prepare: list[str]                      # idempotent shell steps
    artifact: dict = field(default_factory=dict)      # {mode, baseline, excludes}
    requirements: dict = field(default_factory=dict)  # {network, privileged, mounts}


@dataclass
class AgentSessionSpec:
    """How the agent runs inside the container (the 'driver's manual')."""
    run_as: str = "fakeroot"
    cli_args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    completion: dict = field(default_factory=dict)    # {signal_cmd, nudge_prompt, max_nudges}


@dataclass
class MaskReport:
    masked_test_files: int = 0
    masked_src_files: int = 0
    skipped: bool = False
    reason: str = ""


# ───────────────────────────── prompt / instruction ─────────────────────────
def get_prompt_template(version: str = "milestone_v1") -> str:
    """Raw template text ({srs_content}/{milestone_id} placeholders)."""
    p = PROMPT_DIR / f"{version}.md"
    if not p.exists():
        raise FileNotFoundError(f"prompt template not found: {p}")
    return p.read_text(encoding="utf-8")


def build_instruction(task: TaskRecord, version: str = "milestone_v1") -> str:
    """Final rendered instruction. .replace (not .format): SRS bodies contain braces."""
    return (get_prompt_template(version)
            .replace("{srs_content}", task.problem_statement)
            .replace("{milestone_id}", task.instance_id))


# ───────────────────────────── container runtime ────────────────────────────
def node_bootstrap_command() -> str:
    """Idempotent: install node iff absent (go/java/rust images ship none).
    Static .tar.gz (dubbo lacks xz); every milestone image has curl."""
    return (
        "command -v npm >/dev/null 2>&1 || { echo '[evoclaw] image lacks node - installing'; "
        f"curl -fsSL https://nodejs.org/dist/{NODE_VERSION}/node-{NODE_VERSION}-linux-x64.tar.gz "
        "| tar -xz -C /usr/local --strip-components=1; }"
    )


def runtime_spec(task: TaskRecord, *, agent: str = "claude-code",
                 baseline_tag: str = "polar-baseline") -> RuntimeSpec:
    """Work-container recipe. The solution-artifact is a git-archive SNAPSHOT
    (official PatchEvaluator consumes a tar, not a diff) taken against the
    baseline tag set here. quarantine (if any) is declared, not executed."""
    prepare = [
        node_bootstrap_command(),
        f"npm install -g {CLAUDE_CODE_PKG}",
        ("cd /testbed && git config user.email evoclaw@test && git config user.name EvoClaw && "
         "git add -A && git commit -qm baseline 2>/dev/null; "
         f"git tag -f {baseline_tag}; true"),
    ]
    requirements = {"network": "host"}
    q = task.source_spec.get("quarantine")
    if q:
        # declarative: the fabric satisfies it (bridge + iptables allowlist).
        requirements["network"] = f"allowlist:{q.get('name', task.instance_id)}"
        requirements["quarantine"] = q
    return RuntimeSpec(
        prepare=prepare,
        artifact={"mode": "snapshot", "baseline": baseline_tag,
                  "excludes": [".claude/**", "**/.claude/**", "node_modules/**", "**/node_modules/**"]},
        requirements=requirements,
    )


def agent_session_spec(task: TaskRecord, *, agent: str = "claude-code") -> AgentSessionSpec:
    """Declarative session 'driver's manual'. The consumer's harness (Polar)
    stays the session driver (transport/trace); this supplies the knobs that are
    EvoClaw domain knowledge: completion signal + the official nudge."""
    if agent != "claude-code":
        raise NotImplementedError(f"agent_session_spec: only claude-code wired, got {agent!r}")
    mid = task.instance_id
    return AgentSessionSpec(
        run_as="fakeroot",
        cli_args=["--dangerously-skip-permissions"],
        env={},  # training-side overlays CLAUDE_CODE_* knobs; source sets none by default
        completion={
            "signal_cmd": f"cd /testbed && git tag -l agent-impl-{mid}",
            "nudge_prompt": (
                "You have not created the submission tag yet. Please commit your "
                f"changes and create the tag:\n```bash\ngit add .\ngit commit -m "
                f'"Implement {mid}"\ngit tag agent-impl-{mid}\n```\n\n'
                f"**IMPORTANT**: The `git tag agent-impl-{mid}` command signals task completion."
            ),
            "max_nudges": 1,
        },
    )


# ───────────────────────────── leak masking ────────────────────────────────
def _src_filter_for(task: TaskRecord):
    """Build a SrcFileFilter from repo_config in source_spec (src/test/exclude
    dirs, carried from the EvoClaw-data config/<repo>.yaml)."""
    from harness.utils.src_filter import SrcFileFilter  # noqa: PLC0415
    rc = task.source_spec.get("repo_config") or {}
    return SrcFileFilter(
        src_dirs=rc.get("src_dirs") or [],
        test_dirs=rc.get("test_dirs") or [],
        exclude_patterns=rc.get("exclude_patterns"),
    )


def mask_tests(container_name: str, task: TaskRecord, *, workdir: str = "/testbed",
               strict: bool = False) -> MaskReport:
    """Pre-session leak/tamper guard: hide the milestone's graded + new tests
    (chmod 000; rust inline #[cfg(test)] removal) so the agent can neither read
    expected assertions nor overwrite them. Runs host-side against the live
    container — the consumer wires this as a pre-agent hook.

    test_names = fail_to_pass + new_tests (from source_spec). strict=False keeps
    a parse failure from aborting the whole session (logged in the report)."""
    from harness.e2e.test_masking import mask_tests_by_names, TestMappingError  # noqa: PLC0415
    test_names = [str(t) for t in task.fail_to_pass]
    for nt in (task.source_spec.get("new_tests") or []):
        test_names.append(nt.get("test_id") if isinstance(nt, dict) else str(nt))
    test_names = [t for t in test_names if t]
    if not test_names:
        return MaskReport(skipped=True, reason="no fail_to_pass/new_tests to mask")
    try:
        r = mask_tests_by_names(container_name=container_name, test_names=test_names,
                                src_filter=_src_filter_for(task), workdir=workdir, strict=strict)
    except TestMappingError as e:
        return MaskReport(skipped=True, reason=f"unmapped tests (new framework?): {e}")
    return MaskReport(masked_test_files=r.get("masked_test_files", 0),
                      masked_src_files=r.get("masked_src_files", 0))


# ───────────────────────────── grading ─────────────────────────────────────
def evaluate(task: TaskRecord, artifact: Path, *, scratch: Path,
             timeout_s: float = 1500.0) -> dict:
    """Official grading via PatchEvaluator. De-workspaced signature: the
    classification is materialized from the TaskRecord into `scratch` (training
    machines have no EvoClaw-data tree). Returns an EvalResult-shaped dict.
    Concurrency-safe: all outputs land under the per-call `scratch`."""
    import json  # noqa: PLC0415
    from harness.e2e.evaluator import PatchEvaluator  # noqa: PLC0415

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    classification = {"stable_classification": {
        "fail_to_pass": [str(x) for x in task.fail_to_pass],
        "pass_to_pass": [str(x) for x in task.pass_to_pass],
        "none_to_pass": [nt.get("test_id") if isinstance(nt, dict) else str(nt)
                         for nt in (task.source_spec.get("new_tests") or [])],
    }}
    if task.source_spec.get("filter_list"):
        classification["filter_list"] = task.source_spec["filter_list"]
    baseline_json = scratch / "baseline_classification.json"
    baseline_json.write_text(json.dumps(classification))

    # PatchEvaluator derives the milestone image from workspace_root.parent.name;
    # name the scratch workspace so it resolves (<repo>_<ver>/<milestone>).
    ws = scratch / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    ev = PatchEvaluator(workspace_root=ws, milestone_id=task.instance_id,
                        patch_file=Path(artifact), baseline_classification=baseline_json,
                        output_dir=scratch / "evaluation")
    res = ev.evaluate()
    # Normalize to the cross-source EvalResult schema (counting primitives +
    # failure flags; reward formula stays a training-side config).
    return {
        "resolved": bool(getattr(res, "resolved", False)),
        "n_f2p_inscope": len(getattr(res, "fail_to_pass_success", []) or []) + len(getattr(res, "fail_to_pass_failure", []) or []),
        "n_f2p_fixed": len(getattr(res, "fail_to_pass_success", []) or []),
        "n_p2p_inscope": getattr(res, "pass_to_pass_required", 0),
        "n_p2p_broken": len(getattr(res, "pass_to_pass_failure", []) or []),
        "failed_apply_patch": not bool(getattr(res, "patch_successfully_applied", True)),
        "total_tests": getattr(res, "total_tests", 0),
        "passed_tests": getattr(res, "passed_tests", 0),
    }
