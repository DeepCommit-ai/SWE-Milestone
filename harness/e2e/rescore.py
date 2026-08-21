"""Re-tally stored evaluation cells under the identity-preserving scoring key.

Issue #24: the scorer's canonical key dropped file/module paths for every
non-Maven framework, so same-named tests in different files shared one outcome
slot. Test execution was never affected — the raw report records every test by
its full nodeid — so a cell is corrected by re-running only the scoring step on
its stored payload. This module does that with the provenance the stored
results lack:

1. **Replay selection.** ``evaluation_result.json`` records neither the artifact
   PID it was tallied from nor a payload digest, and some cells keep several
   ``eval_summary.json`` candidates. For each candidate, the *legacy* policies
   (prefix-drop with fail-close aggregation, and the pre-2026-07-15 pass-wins
   aggregation) are replayed through ``tally_scoring``; only a candidate that
   reproduces the stored scoring projection exactly is accepted. Zero or
   several reproducing candidates make the cell ``non-replayable`` — never
   chosen by timestamp or directory order.
2. **Envelope patching.** The stored JSON is deep-copied and only the
   score-owned fields are replaced; evaluator-state metadata (locks, build and
   infrastructure facts, ``partial_test_universe``, unknown keys) is preserved.
   Resolution locks recorded in the envelope keep ``resolved`` False.
3. **Derived outputs.** ``evaluation_result_filtered.json`` is regenerated with
   the milestone's filter list and the *selected* artifact only. A manifest
   pins every input by SHA-256 and the scorer revision. Nothing is ever written
   into the source cell: ``report`` mode writes records only, ``mirror`` mode
   writes corrected outputs under the campaign directory for a later,
   human-approved promotion (docs/re-evaluation.md).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from harness.e2e.evaluator import (
    SCORING_ID_POLICY_IDENTITY,
    SCORING_ID_POLICY_LEGACY,
    SCORING_ID_POLICY_LEGACY_PASSWINS,
    EvaluationResult,
    ScoringTally,
    _harness_revision,
    _resolve_test_framework,
    filter_evaluation_result,
    load_filter_list,
    load_repo_config,
    select_classification,
    tally_scoring,
)
from harness.utils.test_id_normalizer import TestIdNormalizer

RESCORE_TOOL_VERSION = "rescore/1"
LEGACY_POLICIES = (SCORING_ID_POLICY_LEGACY, SCORING_ID_POLICY_LEGACY_PASSWINS)
ERA_BY_POLICY = {
    SCORING_ID_POLICY_LEGACY: "fail-close",
    SCORING_ID_POLICY_LEGACY_PASSWINS: "pass-wins",
}

# Score-owned fields of evaluation_result.json that a re-tally may replace.
# Everything else in the envelope is evaluator state and is preserved verbatim.
SCORE_OWNED_FIELDS = (
    "resolved",
    "tests_status",
    "test_summary",
    "scoring_identity",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Optional[Path]) -> str:
    if not path:
        return ""
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError:
        return ""


def _projection(tests_status: Dict[str, Any], test_summary: Dict[str, Any]) -> Dict[str, Any]:
    """The score-owned view of a result, with list order removed."""
    f2p = tests_status.get("FAIL_TO_PASS") or {}
    n2p = tests_status.get("NONE_TO_PASS") or {}
    p2p = tests_status.get("PASS_TO_PASS") or {}
    return {
        "f2p_success": sorted(f2p.get("success") or []),
        "f2p_failure": sorted(f2p.get("failure") or []),
        "p2p_failure": sorted(p2p.get("failure") or []),
        "p2p_success_count": p2p.get("success_count"),
        "p2p_missing": p2p.get("missing"),
        "n2p_success": sorted(n2p.get("success") or []),
        "n2p_failure": sorted(n2p.get("failure") or []),
        "n2p_missing": n2p.get("missing"),
        "total": test_summary.get("total"),
        "passed": test_summary.get("passed"),
        "failed": test_summary.get("failed"),
        "error": test_summary.get("error"),
        "skipped": test_summary.get("skipped"),
    }


def _tally_projection(t: ScoringTally) -> Dict[str, Any]:
    return {
        "f2p_success": sorted(t.fail_to_pass_success),
        "f2p_failure": sorted(t.fail_to_pass_failure),
        "p2p_failure": sorted(t.pass_to_pass_failure),
        "p2p_success_count": t.pass_to_pass_success_count,
        "p2p_missing": t.pass_to_pass_missing,
        "n2p_success": sorted(t.none_to_pass_success),
        "n2p_failure": sorted(t.none_to_pass_failure),
        "n2p_missing": t.none_to_pass_missing,
        "total": t.total_tests,
        "passed": t.passed_tests,
        "failed": t.failed_tests,
        "error": t.error_tests,
        "skipped": t.skipped_tests,
    }


def _projection_diff(want: Dict[str, Any], got: Dict[str, Any]) -> Dict[str, Any]:
    diff = {}
    for key, expected in want.items():
        actual = got.get(key)
        if expected == actual:
            continue
        if isinstance(expected, list):
            diff[key] = {"stored": len(expected), "replayed": len(actual or [])}
        else:
            diff[key] = {"stored": expected, "replayed": actual}
    return diff


@dataclass
class CandidatePayload:
    path: Path
    relpath: str
    sha256: str
    payload: Dict[str, Any]
    eval_json_path: Optional[Path]


@dataclass
class CellRecord:
    cell: str
    repo: str
    trial: str
    milestone: str
    framework: Optional[str]
    status: str  # replayable | non-replayable | error
    reason: str = ""
    era: str = ""
    candidates: int = 0
    reproducing: List[Dict[str, Any]] = field(default_factory=list)
    selected_payload: str = ""
    selected_sha256: str = ""
    stored_resolved: Optional[bool] = None
    new_resolved: Optional[bool] = None
    resolution_locked: bool = False
    delta: Dict[str, Any] = field(default_factory=dict)
    changed_ids: Dict[str, List[str]] = field(default_factory=dict)
    collisions_legacy: int = 0
    collisions_new: int = 0
    untrusted_new: bool = False
    absent_suites_changed: bool = False
    filtered_regenerated: bool = False
    manifest: Dict[str, Any] = field(default_factory=dict)
    invariant_failures: List[str] = field(default_factory=list)


# --- inputs -----------------------------------------------------------------


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _candidate_payloads(cell_dir: Path, scratch: Path) -> List[CandidatePayload]:
    """Every eval_summary.json the cell still holds, from artifacts/ or the tarball."""
    found: Dict[str, CandidatePayload] = {}
    artifacts_dir = cell_dir / "artifacts"
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.glob("*/eval_summary.json")):
            rel = str(path.relative_to(cell_dir))
            eval_json = path.parent / "eval.json"
            try:
                data = path.read_bytes()
                found[rel] = CandidatePayload(
                    path=path,
                    relpath=rel,
                    sha256=_sha256_bytes(data),
                    payload=json.loads(data),
                    eval_json_path=eval_json if eval_json.exists() else None,
                )
            except (OSError, ValueError):
                continue
    tarball = cell_dir / "artifacts.tar.gz"
    if tarball.exists():
        try:
            with tarfile.open(tarball) as tf:
                members = [
                    m for m in tf.getmembers()
                    if m.isfile() and m.name.endswith("/eval_summary.json")
                ]
                for m in members:
                    rel = m.name.lstrip("./")
                    if rel in found:
                        continue
                    extracted = tf.extractfile(m)
                    if extracted is None:
                        continue
                    data = extracted.read()
                    out_dir = scratch / rel
                    out_dir.parent.mkdir(parents=True, exist_ok=True)
                    out_dir.write_bytes(data)
                    eval_name = m.name[: -len("eval_summary.json")] + "eval.json"
                    eval_json_path: Optional[Path] = None
                    try:
                        ej = tf.extractfile(tf.getmember(eval_name))
                        if ej is not None:
                            eval_json_path = scratch / eval_name.lstrip("./")
                            eval_json_path.parent.mkdir(parents=True, exist_ok=True)
                            eval_json_path.write_bytes(ej.read())
                    except KeyError:
                        pass
                    found[rel] = CandidatePayload(
                        path=out_dir,
                        relpath=rel,
                        sha256=_sha256_bytes(data),
                        payload=json.loads(data),
                        eval_json_path=eval_json_path,
                    )
        except (OSError, tarfile.TarError, ValueError):
            pass
    return [found[k] for k in sorted(found)]


def _ran_test_ids(eval_json_path: Optional[Path]) -> Optional[set]:
    if not eval_json_path or not eval_json_path.exists():
        return None
    try:
        data = _load_json(eval_json_path)
    except (OSError, ValueError):
        return None
    ids = set()
    for test in data.get("tests", []) or []:
        if isinstance(test, dict) and test.get("nodeid"):
            ids.add(test["nodeid"])
    return ids


def _baseline_ids(classification: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for cat in ("none_to_pass", "fail_to_pass", "pass_to_pass"):
        for t in classification.get(cat, []) or []:
            ids.append(t if isinstance(t, str) else t.get("test_id", ""))
    return ids


# --- the per-cell procedure ------------------------------------------------


def _resolution_locked(envelope: Dict[str, Any]) -> Tuple[bool, str]:
    """Evaluate the envelope's own locks through EvaluationResult (no rewrite)."""
    try:
        parsed = EvaluationResult.from_result_dict(envelope)
    except (KeyError, TypeError, ValueError) as exc:
        return True, f"envelope-unparseable:{exc.__class__.__name__}"
    if parsed.resolution_locked_false:
        reasons = []
        if parsed.infrastructure_failure:
            reasons.append("infrastructure_failure")
        if parsed.infra_invalid_reason:
            reasons.append("infra_invalid")
        if parsed.scored_failure_reason:
            reasons.append("scored_failure")
        if parsed.residue_prune_skipped_reason:
            reasons.append(f"residue_prune:{parsed.residue_prune_skipped_reason}")
        if parsed.go_module_production_compile_error:
            reasons.append("go_production_compile_error")
        if parsed.go_module_test_graph_contract_error:
            reasons.append("go_test_graph_contract_error")
        if parsed.identity_collision_untrusted:
            reasons.append("identity_collision")
        return True, ",".join(reasons) or "locked"
    if parsed.scored_failure_reason:
        return True, "scored_failure"
    return False, ""


def _apply_tally(envelope: Dict[str, Any], tally: ScoringTally, resolved: bool,
                 scoring_identity: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-copy the stored envelope and replace only score-owned fields."""
    out = copy.deepcopy(envelope)
    out["resolved"] = resolved
    tests_status = out.setdefault("tests_status", {})
    tests_status["FAIL_TO_PASS"] = {
        "success": list(tally.fail_to_pass_success),
        "failure": list(tally.fail_to_pass_failure),
    }
    n2p = {
        "success": list(tally.none_to_pass_success),
        "failure": list(tally.none_to_pass_failure),
    }
    if "missing" in (tests_status.get("NONE_TO_PASS") or {}):
        n2p["missing"] = tally.none_to_pass_missing
    tests_status["NONE_TO_PASS"] = n2p
    tests_status["PASS_TO_PASS"] = {
        "success_count": tally.pass_to_pass_success_count,
        "failure": list(tally.pass_to_pass_failure),
        "missing": tally.pass_to_pass_missing,
    }
    summary = out.setdefault("test_summary", {})
    summary.update(
        {
            "total": tally.total_tests,
            "passed": tally.passed_tests,
            "failed": tally.failed_tests,
            "error": tally.error_tests,
            "skipped": tally.skipped_tests,
            "fail_to_pass_required": len(tally.fail_to_pass_ids),
            "fail_to_pass_achieved": len(tally.fail_to_pass_success),
            "none_to_pass_required": len(tally.none_to_pass_ids),
            "none_to_pass_achieved": len(tally.none_to_pass_success),
            "pass_to_pass_required": len(tally.pass_to_pass_ids),
            "pass_to_pass_achieved": tally.pass_to_pass_success_count,
            "pass_to_pass_failed": len(tally.pass_to_pass_failure),
            "pass_to_pass_missing": tally.pass_to_pass_missing,
        }
    )
    if "none_to_pass_missing" in summary:
        summary["none_to_pass_missing"] = tally.none_to_pass_missing
    out["scoring_identity"] = scoring_identity
    return out


def _conservation_failures(t: ScoringTally) -> List[str]:
    failures = []
    if len(t.fail_to_pass_ids) != len(t.fail_to_pass_success) + len(t.fail_to_pass_failure):
        failures.append("f2p-conservation")
    if len(t.pass_to_pass_ids) != (
        t.pass_to_pass_success_count + len(t.pass_to_pass_failure) + t.pass_to_pass_missing
    ):
        failures.append("p2p-conservation")
    if len(t.none_to_pass_ids) != len(t.none_to_pass_success) + len(t.none_to_pass_failure):
        failures.append("n2p-conservation")
    if t.none_to_pass_missing > len(t.none_to_pass_failure):
        failures.append("n2p-missing-bound")
    return failures


def rescore_cell(
    cell_dir: Path,
    *,
    data_root: Path,
    repo_config: Dict[str, Any],
    repo_config_sha256: str,
    mirror_dir: Optional[Path],
    scratch: Path,
) -> CellRecord:
    cell_dir = cell_dir.resolve()
    milestone = cell_dir.name
    # Retry attempts live in sibling dirs named <MID>-retry<N>; they are real
    # cells scored against <MID>'s classification. Which attempt is
    # authoritative is decided downstream (summary.json), not here.
    classification_milestone = re.sub(r"-retry\d+$", "", milestone)
    trial = cell_dir.parent.parent.name
    repo = data_root.name
    rec = CellRecord(
        cell=str(cell_dir), repo=repo, trial=trial, milestone=milestone, framework=None, status="error"
    )
    result_path = cell_dir / "evaluation_result.json"
    if not result_path.exists():
        rec.status, rec.reason = "non-replayable", "no-evaluation_result.json"
        return rec
    try:
        envelope = _load_json(result_path)
    except (OSError, ValueError) as exc:
        rec.status, rec.reason = "non-replayable", f"unreadable-result:{exc.__class__.__name__}"
        return rec
    rec.stored_resolved = envelope.get("resolved")

    classification_path = (
        data_root / "test_results" / classification_milestone
        / f"{classification_milestone}_classification.json"
    )
    if not classification_path.exists():
        rec.status, rec.reason = "non-replayable", "no-classification"
        return rec
    baseline = _load_json(classification_path)
    classification_to_use, _ = select_classification(baseline)
    try:
        framework = _resolve_test_framework(
            repo_config, data_root, classification_milestone, _baseline_ids(classification_to_use)
        )
    except ValueError as exc:
        rec.status, rec.reason = "non-replayable", f"framework-unresolvable:{exc}"
        return rec
    rec.framework = framework
    normalizer = TestIdNormalizer(framework=framework, enable_normalization=True)

    candidates = _candidate_payloads(cell_dir, scratch / trial / milestone)
    rec.candidates = len(candidates)
    if not candidates:
        rec.status = "non-replayable"
        rec.reason = "no-payload"
        return rec

    stored_proj = _projection(envelope.get("tests_status") or {}, envelope.get("test_summary") or {})
    # Only compare what the stored result actually recorded (older scorers
    # lacked e.g. none_to_pass.missing).
    want = {k: v for k, v in stored_proj.items() if v is not None}

    reproducing: List[Tuple[CandidatePayload, str]] = []
    nearest: List[Dict[str, Any]] = []
    for cand in candidates:
        for policy in LEGACY_POLICIES:
            try:
                t = tally_scoring(
                    cand.payload, baseline, classification_to_use=classification_to_use,
                    framework=framework, normalizer=normalizer, policy=policy,
                )
            except Exception as exc:  # noqa: BLE001 — recorded, never hidden
                nearest.append({"payload": cand.relpath, "policy": policy, "error": repr(exc)})
                continue
            got = {k: _tally_projection(t)[k] for k in want}
            if got == want:
                reproducing.append((cand, policy))
            else:
                nearest.append(
                    {"payload": cand.relpath, "policy": policy, "diff": _projection_diff(want, got)}
                )
    rec.reproducing = [{"payload": c.relpath, "policy": p} for c, p in reproducing]
    distinct = {c.relpath for c, _ in reproducing}
    if len(distinct) != 1:
        rec.status = "non-replayable"
        rec.reason = "no-candidate-reproduces" if not distinct else "ambiguous-candidates"
        rec.manifest = {"nearest": nearest[:6]}
        return rec

    selected, _ = reproducing[0]
    eras = sorted({ERA_BY_POLICY[p] for c, p in reproducing if c is selected or c.relpath == selected.relpath})
    # A cell that replays under both policies has no collision that the
    # aggregation order could decide; call it fail-close (the current semantics).
    rec.era = "fail-close" if "fail-close" in eras else eras[0]
    rec.selected_payload = selected.relpath
    rec.selected_sha256 = selected.sha256

    legacy_t = tally_scoring(
        selected.payload, baseline, classification_to_use=classification_to_use,
        framework=framework, normalizer=normalizer, policy=SCORING_ID_POLICY_LEGACY,
    )
    new_t = tally_scoring(
        selected.payload, baseline, classification_to_use=classification_to_use,
        framework=framework, normalizer=normalizer, policy=SCORING_ID_POLICY_IDENTITY,
    )
    rec.collisions_legacy = len(legacy_t.identity_collisions)
    rec.collisions_new = len(new_t.identity_collisions)
    rec.untrusted_new = new_t.identity_untrusted
    rec.invariant_failures.extend(_conservation_failures(new_t))
    if (new_t.total_tests, new_t.passed_tests, new_t.failed_tests) != (
        stored_proj.get("total"), stored_proj.get("passed"), stored_proj.get("failed")
    ):
        rec.invariant_failures.append("raw-totals-changed")

    locked, lock_reason = _resolution_locked(envelope)
    rec.resolution_locked = locked
    new_resolved = bool(new_t.strict_resolved and not locked and not new_t.identity_untrusted)
    rec.new_resolved = new_resolved

    new_proj = _tally_projection(new_t)
    delta: Dict[str, Any] = {}
    for key in ("f2p_success", "f2p_failure", "p2p_failure", "n2p_success", "n2p_failure"):
        before = set(stored_proj.get(key) or [])
        after = set(new_proj[key])
        if before != after:
            delta[key] = {"before": len(before), "after": len(after)}
            rec.changed_ids[key] = {
                "gained": sorted(after - before),
                "lost": sorted(before - after),
            }
    for key in ("p2p_success_count", "p2p_missing", "n2p_missing"):
        if stored_proj.get(key) is not None and stored_proj.get(key) != new_proj[key]:
            delta[key] = {"before": stored_proj.get(key), "after": new_proj[key]}
    if (rec.stored_resolved is not None) and bool(rec.stored_resolved) != new_resolved:
        delta["resolved"] = {"before": bool(rec.stored_resolved), "after": new_resolved}
    rec.delta = delta
    rec.absent_suites_changed = sorted(new_t.absent_suites) != sorted(
        (envelope.get("build_failure_policy") or {}).get("absent_suites") or []
    )
    rec.status = "replayable"
    rec.reason = lock_reason

    filter_list = load_filter_list(data_root, classification_milestone)
    filter_sha = _sha256_path(
        data_root / "test_results" / classification_milestone
        / f"{classification_milestone}_filter_list.json"
    )
    rec.manifest = {
        "tool": RESCORE_TOOL_VERSION,
        "scorer_revision": _harness_revision(),
        "legacy_policy": SCORING_ID_POLICY_LEGACY,
        "replay_era": rec.era,
        "new_policy": SCORING_ID_POLICY_IDENTITY,
        "payload_path": selected.relpath,
        "payload_sha256": selected.sha256,
        "classification_path": str(classification_path),
        "classification_sha256": _sha256_path(classification_path),
        "repo_config_sha256": repo_config_sha256,
        "filter_list_sha256": filter_sha,
        "framework": framework,
        "stored_result_sha256": _sha256_path(result_path),
    }

    if mirror_dir is not None:
        scoring_identity = {
            "policy": SCORING_ID_POLICY_IDENTITY,
            "framework": framework,
            "payload_path": selected.relpath,
            "payload_sha256": selected.sha256,
            "classification_file": classification_path.name,
            "classification_sha256": rec.manifest["classification_sha256"],
            "collision_count": len(new_t.identity_collisions),
            "collisions": new_t.identity_collisions,
            "untrusted": new_t.identity_untrusted,
            "retally": {
                "tool": RESCORE_TOOL_VERSION,
                "scorer_revision": rec.manifest["scorer_revision"],
                "replay_era": rec.era,
                "stored_result_sha256": rec.manifest["stored_result_sha256"],
            },
        }
        patched = _apply_tally(envelope, new_t, new_resolved, scoring_identity)
        out_cell = mirror_dir / repo / trial / milestone
        out_cell.mkdir(parents=True, exist_ok=True)
        (out_cell / "evaluation_result.json").write_text(json.dumps(patched, indent=2) + "\n")
        if filter_list and any(
            filter_list.get(k) for k in ("invalid_fail_to_pass", "invalid_none_to_pass", "invalid_pass_to_pass")
        ):
            filtered = filter_evaluation_result(
                copy.deepcopy(patched), filter_list, ran_test_ids=_ran_test_ids(selected.eval_json_path)
            )
            (out_cell / "evaluation_result_filtered.json").write_text(
                json.dumps(filtered, indent=2) + "\n"
            )
            rec.filtered_regenerated = True
        (out_cell / "rescore_manifest.json").write_text(json.dumps(rec.manifest, indent=2, sort_keys=True) + "\n")
    return rec


# --- campaign driver ------------------------------------------------------


def _iter_cells(trial_roots: Iterable[Path], cells: Iterable[Path]) -> List[Path]:
    out: List[Path] = []
    for c in cells:
        out.append(Path(c))
    for root in trial_roots:
        root = Path(root)
        eval_dir = root / "evaluation"
        if eval_dir.is_dir():
            for cell in sorted(eval_dir.iterdir()):
                if cell.is_dir() and (cell / "evaluation_result.json").exists():
                    out.append(cell)
    return out


def run_campaign(
    *,
    data_root: Path,
    cells: List[Path],
    out_dir: Path,
    mirror: bool,
) -> Dict[str, Any]:
    data_root = data_root.resolve()
    repo = data_root.name
    repo_config = load_repo_config(repo, workspace_root=data_root)
    cfg_path = data_root.parent / "config" / f"{repo}.yaml"
    repo_config_sha = _sha256_path(cfg_path) if cfg_path.exists() else ""
    out_dir.mkdir(parents=True, exist_ok=True)
    mirror_dir = (out_dir / "mirror") if mirror else None
    records: List[CellRecord] = []
    with tempfile.TemporaryDirectory(prefix="rescore-") as tmp:
        scratch = Path(tmp)
        for cell in cells:
            try:
                rec = rescore_cell(
                    cell,
                    data_root=data_root,
                    repo_config=repo_config,
                    repo_config_sha256=repo_config_sha,
                    mirror_dir=mirror_dir,
                    scratch=scratch,
                )
            except Exception as exc:  # noqa: BLE001 — a campaign must not die on one cell
                rec = CellRecord(
                    cell=str(cell), repo=repo, trial=cell.parent.parent.name, milestone=cell.name,
                    framework=None, status="error", reason=f"{exc.__class__.__name__}:{exc}",
                )
            records.append(rec)
    summary = summarize(records)
    (out_dir / "records.jsonl").write_text(
        "".join(json.dumps(asdict(r), sort_keys=True) + "\n" for r in records)
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def summarize(records: List[CellRecord]) -> Dict[str, Any]:
    s: Dict[str, Any] = {
        "cells": len(records),
        "replayable": 0,
        "non_replayable": 0,
        "error": 0,
        "era": {"fail-close": 0, "pass-wins": 0},
        "non_replayable_reasons": {},
        "changed_cells": 0,
        "resolved_false_to_true": [],
        "resolved_true_to_false": [],
        "delta_totals": {
            "f2p_success": 0, "f2p_failure": 0, "p2p_failure": 0, "p2p_missing": 0,
            "n2p_success": 0, "n2p_failure": 0, "n2p_missing": 0,
        },
        "untrusted_new": 0,
        "absent_suites_changed": [],
        "invariant_failures": [],
        "locked_cells_with_strict_flip": [],
    }
    for r in records:
        if r.status == "replayable":
            s["replayable"] += 1
            s["era"][r.era] = s["era"].get(r.era, 0) + 1
            if r.delta:
                s["changed_cells"] += 1
            for key, d in r.delta.items():
                if key == "resolved":
                    target = "resolved_false_to_true" if d["after"] else "resolved_true_to_false"
                    s[target].append(f"{r.trial}/{r.milestone}")
                elif key in s["delta_totals"]:
                    s["delta_totals"][key] += d["after"] - d["before"]
            if r.untrusted_new:
                s["untrusted_new"] += 1
            if r.absent_suites_changed:
                s["absent_suites_changed"].append(f"{r.trial}/{r.milestone}")
            if r.invariant_failures:
                s["invariant_failures"].append({"cell": f"{r.trial}/{r.milestone}", "failures": r.invariant_failures})
        elif r.status == "non-replayable":
            s["non_replayable"] += 1
            s["non_replayable_reasons"][r.reason] = s["non_replayable_reasons"].get(r.reason, 0) + 1
        else:
            s["error"] += 1
            s["non_replayable_reasons"][f"error:{r.reason[:60]}"] = (
                s["non_replayable_reasons"].get(f"error:{r.reason[:60]}", 0) + 1
            )
    return s


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-root", required=True, type=Path,
                    help="repo data root, e.g. .../SWE-Milestone-data/<repo_key>")
    ap.add_argument("--trial-root", action="append", default=[], type=Path,
                    help="trial directory containing evaluation/<MID>/ (repeatable)")
    ap.add_argument("--cell", action="append", default=[], type=Path, help="one cell dir (repeatable)")
    ap.add_argument("--out", required=True, type=Path, help="campaign output directory")
    ap.add_argument("--mode", choices=("report", "mirror"), default="report",
                    help="report: records only; mirror: also write corrected outputs under --out/mirror")
    args = ap.parse_args(argv)
    cells = _iter_cells(args.trial_root, args.cell)
    if not cells:
        print("no cells found", file=sys.stderr)
        return 2
    summary = run_campaign(
        data_root=args.data_root, cells=cells, out_dir=args.out, mirror=args.mode == "mirror"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
