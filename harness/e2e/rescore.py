"""Re-tally stored evaluation cells under the identity-preserving scoring key.

Issue #24: the scorer's canonical key dropped file/module paths for every
non-Maven framework, so same-named tests in different files shared one outcome
slot. Test execution was never affected — the raw report records every test by
its full nodeid — so a cell is corrected by re-running only the scoring step on
its stored payload. This module does that with the provenance the stored
results lack:

1. **Replay selection.** ``evaluation_result.json`` records neither the artifact
   it was tallied from nor a payload digest, and some cells keep several
   ``eval_summary.json`` candidates. Every candidate (from ``artifacts/`` and
   from ``artifacts.tar.gz``, keyed by path *and* digest) is replayed through
   ``tally_scoring`` under the legacy policies: ``legacy-prefix-drop`` (the
   scorer on main before this fix) and ``legacy-prefix-drop-passwins`` (the
   scorer before 0a779f0, 2026-07-15). A cell is replayable only if exactly one
   candidate reproduces the stored scoring projection. Zero or several
   reproducing candidates, an unreadable candidate, or drifted inputs make it
   ``non-replayable`` — never chosen by timestamp or directory order.
2. **Era.** A cell reproducible only under the pass-wins policy is a
   ``pass-wins``-era cell: its stored value may be lucky rather than correct.
   Such cells are reported but **frozen** (not mirrored) unless the campaign
   is run with ``--include-pass-wins``. Cells reproducible under both policies
   carry no aggregation-sensitive collision and are ``era-agnostic``.
3. **Inputs.** The classification, repo config and milestone test config used
   for the replay are hashed into the manifest; a stored repo-config binding
   that disagrees with the current config, or a classification that differs
   from the one at the trial's recorded data commit, marks the cell
   non-replayable.
4. **Envelope patching.** The stored JSON is deep-copied and only score-owned
   fields are replaced; evaluator-state metadata (locks, build and
   infrastructure facts, ``partial_test_universe``, ``absent_suites``, unknown
   keys) is preserved. Resolution locks recorded in the envelope keep
   ``resolved`` False, also after filtering.
5. **Derived outputs.** ``mirror`` mode writes the corrected result, the
   regenerated filtered result (when the selected artifact's ``eval.json`` is
   available), a copy of the selected artifact directory, a manifest and
   ``PROMOTION_NOTES.md`` listing what the mirror does *not* regenerate
   (trial ``summary.json`` / ``summary_filtered.json``, ``feedback_report.md``,
   ``artifacts.tar.gz``, stale filtered results) under the campaign directory,
   for a later human-approved promotion (docs/re-evaluation.md). Nothing is
   ever written into the source cell. Re-running on an already corrected cell
   is a verified no-op (``already-identity``).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
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

RESCORE_TOOL_VERSION = "rescore/2"
LEGACY_POLICIES = (SCORING_ID_POLICY_LEGACY, SCORING_ID_POLICY_LEGACY_PASSWINS)
ERA_BY_POLICY = {
    SCORING_ID_POLICY_LEGACY: "fail-close",
    SCORING_ID_POLICY_LEGACY_PASSWINS: "pass-wins",
}
ERA_AGNOSTIC = "era-agnostic"

# Top-level keys of evaluation_result.json that a re-tally may replace. The
# mirror writer asserts that nothing outside this list changed.
SCORE_OWNED_TOP_LEVEL = ("resolved", "tests_status", "test_summary", "scoring_identity")

STALE_AFTER_RETALLY = (
    "trial summary.json: results[<milestone>] (eval_status, test_summary, attempt) must be synced",
    "trial summary_filtered.json: same, when a filtered result exists",
    "<cell>/feedback_report.md: embeds the old status/regressions; regenerate or mark superseded",
    "<cell>/artifacts.tar.gz: regenerate from the promoted artifacts/ (they must agree)",
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


# --- projections -------------------------------------------------------------

_SUMMARY_KEYS = (
    "total", "passed", "failed", "error", "skipped",
    "fail_to_pass_required", "fail_to_pass_achieved",
    "none_to_pass_required", "none_to_pass_achieved", "none_to_pass_missing",
    "pass_to_pass_required", "pass_to_pass_achieved", "pass_to_pass_failed", "pass_to_pass_missing",
)


def _projection(tests_status: Dict[str, Any], test_summary: Dict[str, Any]) -> Dict[str, Any]:
    """The score-owned view of a result, with list order removed."""
    f2p = tests_status.get("FAIL_TO_PASS") or {}
    n2p = tests_status.get("NONE_TO_PASS") or {}
    p2p = tests_status.get("PASS_TO_PASS") or {}
    proj = {
        "f2p_success": sorted(f2p.get("success") or []),
        "f2p_failure": sorted(f2p.get("failure") or []),
        "p2p_failure": sorted(p2p.get("failure") or []),
        "p2p_success_count": p2p.get("success_count"),
        "p2p_missing": p2p.get("missing"),
        "n2p_success": sorted(n2p.get("success") or []),
        "n2p_failure": sorted(n2p.get("failure") or []),
        "n2p_missing": n2p.get("missing"),
    }
    for key in _SUMMARY_KEYS:
        proj[f"summary.{key}"] = test_summary.get(key)
    return proj


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
        "summary.total": t.total_tests,
        "summary.passed": t.passed_tests,
        "summary.failed": t.failed_tests,
        "summary.error": t.error_tests,
        "summary.skipped": t.skipped_tests,
        "summary.fail_to_pass_required": len(t.fail_to_pass_ids),
        "summary.fail_to_pass_achieved": len(t.fail_to_pass_success),
        "summary.none_to_pass_required": len(t.none_to_pass_ids),
        "summary.none_to_pass_achieved": len(t.none_to_pass_success),
        "summary.none_to_pass_missing": t.none_to_pass_missing,
        "summary.pass_to_pass_required": len(t.pass_to_pass_ids),
        "summary.pass_to_pass_achieved": t.pass_to_pass_success_count,
        "summary.pass_to_pass_failed": len(t.pass_to_pass_failure),
        "summary.pass_to_pass_missing": t.pass_to_pass_missing,
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


# --- records ------------------------------------------------------------------


@dataclass
class CandidatePayload:
    source: str  # artifacts-dir | tarball
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
    status: str  # replayable | already-identity | non-replayable | invariant-failed | error
    reason: str = ""
    era: str = ""
    frozen: bool = False
    candidates: int = 0
    candidates_unreadable: int = 0
    reproducing: List[Dict[str, Any]] = field(default_factory=list)
    selected_payload: str = ""
    selected_sha256: str = ""
    stored_resolved: Optional[bool] = None
    new_resolved: Optional[bool] = None
    resolution_locked: bool = False
    delta: Dict[str, Any] = field(default_factory=dict)
    changed_ids: Dict[str, Any] = field(default_factory=dict)
    collisions_legacy: int = 0
    collisions_new: int = 0
    untrusted_new: bool = False
    absent_suites_changed: bool = False
    ambiguous_consistent: bool = False
    reproducing_shas: List[str] = field(default_factory=list)
    filtered_regenerated: bool = False
    filtered_reason: str = ""
    mirrored: bool = False
    inputs: Dict[str, Any] = field(default_factory=dict)
    manifest: Dict[str, Any] = field(default_factory=dict)
    invariant_failures: List[str] = field(default_factory=list)


# --- inputs -------------------------------------------------------------------


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _candidate_payloads(cell_dir: Path, scratch: Path) -> Tuple[List[CandidatePayload], List[str]]:
    """Every eval_summary.json the cell still holds, from artifacts/ and the
    tarball, keyed by (relpath, sha256). Byte-identical copies are one
    candidate; same-path different-bytes copies are distinct candidates.
    Unreadable entries are returned separately: they make the cell
    non-replayable, never silently dropped."""
    found: Dict[Tuple[str, str], CandidatePayload] = {}
    unreadable: List[str] = []
    artifacts_dir = cell_dir / "artifacts"
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.glob("*/eval_summary.json")):
            rel = str(path.relative_to(cell_dir))
            eval_json = path.parent / "eval.json"
            try:
                data = path.read_bytes()
                payload = json.loads(data)
            except (OSError, ValueError) as exc:
                unreadable.append(f"artifacts-dir:{rel}:{exc.__class__.__name__}")
                continue
            key = (rel, _sha256_bytes(data))
            found.setdefault(
                key,
                CandidatePayload(
                    source="artifacts-dir",
                    relpath=rel,
                    sha256=key[1],
                    payload=payload,
                    eval_json_path=eval_json if eval_json.exists() else None,
                ),
            )
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
                    try:
                        extracted = tf.extractfile(m)
                        data = extracted.read() if extracted is not None else b""
                        payload = json.loads(data)
                    except (OSError, ValueError, tarfile.TarError) as exc:
                        unreadable.append(f"tarball:{rel}:{exc.__class__.__name__}")
                        continue
                    key = (rel, _sha256_bytes(data))
                    if key in found:
                        continue  # byte-identical to the on-disk copy
                    out_path = scratch / rel
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(data)
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
                    found[key] = CandidatePayload(
                        source="tarball",
                        relpath=rel,
                        sha256=key[1],
                        payload=payload,
                        eval_json_path=eval_json_path,
                    )
        except (OSError, tarfile.TarError) as exc:
            unreadable.append(f"tarball:{exc.__class__.__name__}")
    return [found[k] for k in sorted(found)], unreadable


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


def _classification_pin(
    data_root: Path, trial_dir: Path, classification_rel: str, current_sha: str
) -> Dict[str, Any]:
    """Compare the current classification with the one at the data commit the
    trial recorded (trial_metadata.json → data_version.commit), if any."""
    pin: Dict[str, Any] = {"status": "unavailable", "commit": ""}
    meta_path = trial_dir / "trial_metadata.json"
    if not meta_path.exists():
        return pin
    try:
        meta = _load_json(meta_path)
    except (OSError, ValueError):
        return pin
    commit = ((meta.get("data_version") or {}).get("commit") or "").strip()
    if not commit:
        return pin
    pin["commit"] = commit
    # ``<commit>:./<path>`` is resolved relative to the git work-tree cwd (the
    # data root is normally a sub-directory of the data repository); a bare
    # ``<commit>:<path>`` would be repo-root-relative and silently fail.
    try:
        proc = subprocess.run(
            ["git", "-C", str(data_root), "show", f"{commit}:./{classification_rel}"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pin["status"] = "unresolvable"
        pin["error"] = exc.__class__.__name__
        return pin
    if proc.returncode != 0:
        # A recorded commit that cannot be read is a broken pin, not "no pin":
        # the caller treats it as non-replayable (fail closed).
        pin["status"] = "unresolvable"
        pin["error"] = proc.stderr.decode(errors="replace").strip()[:200]
        return pin
    pin["status"] = "match" if _sha256_bytes(proc.stdout) == current_sha else "mismatch"
    return pin


# --- the per-cell procedure ---------------------------------------------------


def _resolution_locked(envelope: Dict[str, Any]) -> Tuple[bool, str]:
    """Evaluate the envelope's own locks through EvaluationResult (no rewrite)."""
    try:
        parsed = EvaluationResult.from_result_dict(envelope)
    except (KeyError, TypeError, ValueError) as exc:
        return True, f"envelope-unparseable:{exc.__class__.__name__}"
    reasons = []
    if parsed.infrastructure_failure:
        reasons.append("infrastructure_failure")
    if parsed.infra_invalid_reason:
        reasons.append("infra_invalid")
    if parsed.scored_failure_reason:
        reasons.append("scored_failure")
    if parsed.residue_prune_skipped_reason and parsed.scoring_untrusted:
        reasons.append(f"residue_prune:{parsed.residue_prune_skipped_reason}")
    if parsed.go_module_production_compile_error:
        reasons.append("go_production_compile_error")
    if parsed.go_module_test_graph_contract_error:
        reasons.append("go_test_graph_contract_error")
    if parsed.identity_collision_untrusted:
        reasons.append("identity_collision")
    locked = bool(parsed.resolution_locked_false or parsed.scored_failure_reason)
    return locked, ",".join(reasons) if locked else ""


def _apply_tally(
    envelope: Dict[str, Any],
    tally: ScoringTally,
    resolved: bool,
    scoring_identity: Dict[str, Any],
) -> Dict[str, Any]:
    """Deep-copy the stored envelope and replace only score-owned fields.
    absent_suites / partial_test_universe are preserved (evaluator state)."""
    out = copy.deepcopy(envelope)
    out["resolved"] = resolved
    tests_status = out.setdefault("tests_status", {})
    tests_status["FAIL_TO_PASS"] = {
        "success": list(tally.fail_to_pass_success),
        "failure": list(tally.fail_to_pass_failure),
    }
    tests_status["NONE_TO_PASS"] = {
        "success": list(tally.none_to_pass_success),
        "failure": list(tally.none_to_pass_failure),
        "missing": tally.none_to_pass_missing,
    }
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
            "none_to_pass_missing": tally.none_to_pass_missing,
        }
    )
    out["scoring_identity"] = scoring_identity
    return out


def _assert_only_score_owned_changed(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    return sorted(k for k in changed if k not in SCORE_OWNED_TOP_LEVEL)


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


def _regenerate_filtered(
    patched: Dict[str, Any],
    filter_list: Dict[str, List[str]],
    eval_json_path: Optional[Path],
    tally: ScoringTally,
    locked: bool,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Filtered result from the patched envelope; locks re-applied; N2P
    missing re-derived from the tally (the generic filter only adjusts
    success/failure)."""
    ran = _ran_test_ids(eval_json_path)
    if ran is None:
        return None, "selected-eval.json-unavailable"
    filtered = filter_evaluation_result(copy.deepcopy(patched), filter_list, ran_test_ids=ran)
    missing_n2p = set(tally.missing_n2p_ids)
    n2p = filtered.get("tests_status", {}).get("NONE_TO_PASS", {})
    remaining_missing = sum(1 for t in n2p.get("failure", []) or [] if t in missing_n2p)
    n2p["missing"] = remaining_missing
    filtered.setdefault("test_summary", {})["none_to_pass_missing"] = remaining_missing
    if locked or tally.identity_untrusted:
        filtered["resolved"] = False
    filtered.setdefault("scoring_identity", {}).update(
        {"filtered_locks_reapplied": bool(locked or tally.identity_untrusted)}
    )
    return filtered, ""


def rescore_cell(
    cell_dir: Path,
    *,
    data_root: Path,
    repo_config: Dict[str, Any],
    repo_config_sha256: str,
    mirror_dir: Optional[Path],
    scratch: Path,
    include_pass_wins: bool = False,
) -> CellRecord:
    cell_dir = cell_dir.resolve()
    milestone = cell_dir.name
    # Retry attempts live in sibling dirs named <MID>-retry<N>; they are real
    # cells scored against <MID>'s classification. Which attempt is
    # authoritative is decided downstream (summary.json), not here.
    classification_milestone = re.sub(r"-retry\d+$", "", milestone)
    trial_dir = cell_dir.parent.parent
    trial = trial_dir.name
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

    classification_rel = (
        f"test_results/{classification_milestone}/{classification_milestone}_classification.json"
    )
    classification_path = data_root / classification_rel
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

    # --- inputs: hash everything the replay depends on, detect drift -------
    classification_sha = _sha256_path(classification_path)
    test_config_path = data_root / "dockerfiles" / classification_milestone / "test_config.json"
    stored_env = envelope.get("evaluation_environment") or {}
    stored_rc_sha = str(stored_env.get("repo_config_sha256") or "")
    pin = _classification_pin(data_root, trial_dir, classification_rel, classification_sha)
    rec.inputs = {
        "classification_sha256": classification_sha,
        "classification_pin": pin,
        "test_config_sha256": _sha256_path(test_config_path),
        "repo_config_sha256_current": repo_config_sha256,
        "repo_config_sha256_stored": stored_rc_sha,
        "repo_config_binding_mode_stored": str(stored_env.get("repo_config_binding_mode") or ""),
    }
    if stored_rc_sha and repo_config_sha256 and stored_rc_sha != repo_config_sha256:
        rec.status, rec.reason = "non-replayable", "repo-config-drift"
        return rec
    if pin["status"] == "mismatch":
        rec.status, rec.reason = "non-replayable", "classification-drift"
        return rec
    if pin["status"] == "unresolvable":
        rec.status, rec.reason = "non-replayable", "classification-pin-unresolvable"
        return rec

    candidates, unreadable = _candidate_payloads(cell_dir, scratch / trial / milestone)
    rec.candidates = len(candidates)
    rec.candidates_unreadable = len(unreadable)
    if unreadable:
        rec.status, rec.reason = "non-replayable", "unreadable-candidate"
        rec.manifest = {"unreadable": unreadable}
        return rec
    if not candidates:
        rec.status, rec.reason = "non-replayable", "no-payload"
        return rec

    stored_proj = _projection(envelope.get("tests_status") or {}, envelope.get("test_summary") or {})
    # Only compare what the stored result actually recorded (older scorers
    # lacked e.g. none_to_pass.missing).
    want = {k: v for k, v in stored_proj.items() if v is not None}

    def replay(cand: CandidatePayload, policy: str) -> Optional[ScoringTally]:
        try:
            return tally_scoring(
                cand.payload, baseline, classification_to_use=classification_to_use,
                framework=framework, normalizer=normalizer, policy=policy,
            )
        except Exception:  # noqa: BLE001 — recorded by the caller
            return None

    # --- already corrected? verified no-op ----------------------------------
    stored_identity = envelope.get("scoring_identity") or {}
    if stored_identity.get("policy") == SCORING_ID_POLICY_IDENTITY:
        recorded_sha = str(stored_identity.get("payload_sha256") or "")
        # Try the recorded digest first; a result tallied from an in-memory
        # merged report records a digest no stored file has, so the other
        # candidates are tried afterwards (the projection check is the proof).
        ordered = sorted(candidates, key=lambda c: (0 if c.sha256 == recorded_sha else 1, c.relpath))
        for cand in ordered:
            t = replay(cand, SCORING_ID_POLICY_IDENTITY)
            if t is not None and {k: _tally_projection(t)[k] for k in want} == want:
                rec.status, rec.reason = "already-identity", "identity result reproduced; no change"
                rec.selected_payload, rec.selected_sha256 = cand.relpath, cand.sha256
                rec.new_resolved = rec.stored_resolved
                return rec
        rec.status, rec.reason = "non-replayable", "identity-result-not-reproduced"
        return rec

    # --- replay selection under the legacy scorers ---------------------------
    reproducing: List[Tuple[CandidatePayload, str]] = []
    nearest: List[Dict[str, Any]] = []
    for cand in candidates:
        for policy in LEGACY_POLICIES:
            t = replay(cand, policy)
            if t is None:
                nearest.append({"payload": cand.relpath, "policy": policy, "error": "tally-raised"})
                continue
            got = {k: _tally_projection(t)[k] for k in want}
            if got == want:
                reproducing.append((cand, policy))
            else:
                nearest.append(
                    {"payload": cand.relpath, "sha256": cand.sha256[:12], "policy": policy,
                     "diff": _projection_diff(want, got)}
                )
    rec.reproducing = [{"payload": c.relpath, "sha256": c.sha256[:12], "policy": p} for c, p in reproducing]
    distinct = {(c.relpath, c.sha256) for c, _ in reproducing}
    if not distinct:
        rec.status, rec.reason = "non-replayable", "no-candidate-reproduces"
        rec.manifest = {"nearest": nearest[:6]}
        return rec
    if len(distinct) > 1:
        # Several byte-different payloads reproduce the stored result. The
        # ambiguity is immaterial only if they also agree under the identity
        # key; then any of them is the same evidence and the first by path is
        # selected (recorded as consistent). Otherwise it stays ambiguous.
        by_key = {(c.relpath, c.sha256): c for c, _ in reproducing}
        projections = []
        for key in sorted(by_key):
            t = replay(by_key[key], SCORING_ID_POLICY_IDENTITY)
            projections.append(None if t is None else _tally_projection(t))
        if any(pr is None for pr in projections) or any(pr != projections[0] for pr in projections):
            rec.status, rec.reason = "non-replayable", "ambiguous-candidates"
            rec.manifest = {"nearest": nearest[:6]}
            return rec
        rec.ambiguous_consistent = True
        rec.reproducing_shas = [sha for _, sha in sorted(by_key)]
        reproducing = [(c, p) for c, p in reproducing if (c.relpath, c.sha256) == sorted(by_key)[0]] + [
            (c, p) for c, p in reproducing if (c.relpath, c.sha256) != sorted(by_key)[0]
        ]

    selected = reproducing[0][0]
    policies = sorted({p for c, p in reproducing})
    if len(policies) == 2:
        rec.era = ERA_AGNOSTIC
    else:
        rec.era = ERA_BY_POLICY[policies[0]]
    rec.frozen = rec.era == "pass-wins" and not include_pass_wins
    rec.selected_payload = selected.relpath
    rec.selected_sha256 = selected.sha256

    legacy_t = replay(selected, SCORING_ID_POLICY_LEGACY)
    new_t = replay(selected, SCORING_ID_POLICY_IDENTITY)
    if new_t is None:
        rec.status, rec.reason = "error", "identity-tally-raised"
        return rec
    rec.collisions_legacy = len(legacy_t.identity_collisions) if legacy_t else 0
    rec.collisions_new = len(new_t.identity_collisions)
    rec.untrusted_new = new_t.identity_untrusted
    rec.invariant_failures.extend(_conservation_failures(new_t))
    if (new_t.total_tests, new_t.passed_tests, new_t.failed_tests, new_t.error_tests, new_t.skipped_tests) != (
        stored_proj.get("summary.total"), stored_proj.get("summary.passed"), stored_proj.get("summary.failed"),
        stored_proj.get("summary.error") if stored_proj.get("summary.error") is not None else new_t.error_tests,
        stored_proj.get("summary.skipped") if stored_proj.get("summary.skipped") is not None else new_t.skipped_tests,
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
            rec.changed_ids[key] = {"gained": sorted(after - before), "lost": sorted(before - after)}
    for key in ("p2p_success_count", "p2p_missing", "n2p_missing"):
        if stored_proj.get(key) is not None and stored_proj.get(key) != new_proj[key]:
            delta[key] = {"before": stored_proj.get(key), "after": new_proj[key]}
    if (rec.stored_resolved is not None) and bool(rec.stored_resolved) != new_resolved:
        delta["resolved"] = {"before": bool(rec.stored_resolved), "after": new_resolved}
    rec.delta = delta
    stored_policy = envelope.get("build_failure_policy") or {}
    rec.absent_suites_changed = "absent_suites" in stored_policy and sorted(
        new_t.absent_suites
    ) != sorted(stored_policy.get("absent_suites") or [])
    rec.status = "invariant-failed" if rec.invariant_failures else "replayable"
    rec.reason = lock_reason

    filter_list = load_filter_list(data_root, classification_milestone)
    filter_path = (
        data_root / "test_results" / classification_milestone
        / f"{classification_milestone}_filter_list.json"
    )
    rec.manifest = {
        "tool": RESCORE_TOOL_VERSION,
        "scorer_revision": _harness_revision(),
        "replay_policies_reproducing": policies,
        "replay_era": rec.era,
        "frozen": rec.frozen,
        "new_policy": SCORING_ID_POLICY_IDENTITY,
        "payload_path": selected.relpath,
        "payload_source": selected.source,
        "payload_sha256": selected.sha256,
        "ambiguous_consistent": rec.ambiguous_consistent,
        "reproducing_payload_sha256s": rec.reproducing_shas,
        "classification_path": classification_rel,
        "classification_sha256": classification_sha,
        "classification_pin": pin,
        "test_config_sha256": rec.inputs["test_config_sha256"],
        "repo_config_sha256": repo_config_sha256,
        "filter_list_sha256": _sha256_path(filter_path) if filter_path.exists() else "",
        "framework": framework,
        "stored_result_sha256": _sha256_path(result_path),
        "stale_after_retally": list(STALE_AFTER_RETALLY),
    }

    if mirror_dir is None or rec.status != "replayable" or rec.frozen:
        return rec

    scoring_identity = {
        "policy": SCORING_ID_POLICY_IDENTITY,
        "framework": framework,
        "payload_path": selected.relpath,
        "payload_sha256": selected.sha256,
        "classification_file": classification_path.name,
        "classification_sha256": classification_sha,
        "collision_count": len(new_t.identity_collisions),
        "collisions": new_t.identity_collisions,
        "untrusted": new_t.identity_untrusted,
        "match_trace": new_t.match_trace,
        "retally": {
            "tool": RESCORE_TOOL_VERSION,
            "scorer_revision": rec.manifest["scorer_revision"],
            "replay_era": rec.era,
            "replay_policies_reproducing": policies,
            "stored_result_sha256": rec.manifest["stored_result_sha256"],
        },
    }
    patched = _apply_tally(envelope, new_t, new_resolved, scoring_identity)
    leaked = _assert_only_score_owned_changed(envelope, patched)
    if leaked:
        rec.status, rec.reason = "error", f"non-score-field-changed:{','.join(leaked)}"
        return rec
    out_cell = mirror_dir / repo / trial / milestone
    out_cell.mkdir(parents=True, exist_ok=True)
    (out_cell / "evaluation_result.json").write_text(json.dumps(patched, indent=2) + "\n")

    # Copy the selected artifact directory so the promoted cell's artifacts
    # agree with its result (docs/re-evaluation.md, promotion rule 2). A
    # payload that only survives inside artifacts.tar.gz is extracted from it.
    artifacts_note = _copy_selected_artifacts(cell_dir, selected, out_cell)

    stale = list(STALE_AFTER_RETALLY)
    if filter_list and any(
        filter_list.get(k) for k in ("invalid_fail_to_pass", "invalid_none_to_pass", "invalid_pass_to_pass")
    ):
        filtered, why = _regenerate_filtered(patched, filter_list, selected.eval_json_path, new_t, locked)
        if filtered is not None:
            (out_cell / "evaluation_result_filtered.json").write_text(json.dumps(filtered, indent=2) + "\n")
            rec.filtered_regenerated = True
        else:
            rec.filtered_reason = why
            stale.append(f"<cell>/evaluation_result_filtered.json could not be regenerated ({why}); "
                         "a stale stored filtered file would shadow the corrected result: delete or regenerate it")
    elif (cell_dir / "evaluation_result_filtered.json").exists():
        stale.append("<cell>/evaluation_result_filtered.json exists but the milestone has no filter list now; "
                     "it would shadow the corrected result: delete it on promotion")
    rec.manifest["stale_after_retally"] = stale
    (out_cell / "rescore_manifest.json").write_text(json.dumps(rec.manifest, indent=2, sort_keys=True) + "\n")
    (out_cell / "PROMOTION_NOTES.md").write_text(
        "# Promotion notes (issue #24 re-tally)\n\n"
        f"- source cell: `{cell_dir}`\n"
        f"- replay era: {rec.era}; policies reproducing the stored result: {', '.join(policies)}\n"
        f"- selected payload: `{selected.relpath}` ({selected.source}, sha256 {selected.sha256})\n"
        "- this mirror contains: evaluation_result.json"
        + (", evaluation_result_filtered.json" if rec.filtered_regenerated else "")
        + f", {artifacts_note}, rescore_manifest.json\n"
        "- NOT regenerated here (must be handled at promotion):\n"
        + "".join(f"  - {s}\n" for s in stale)
    )
    rec.mirrored = True
    return rec


def _copy_selected_artifacts(cell_dir: Path, selected: CandidatePayload, out_cell: Path) -> str:
    """Materialise the selected payload's artifact directory in the mirror cell
    (from artifacts/ or from artifacts.tar.gz) and describe what was copied."""
    art_dir = Path(selected.relpath).parent  # artifacts/<pid>
    dst_dir = out_cell / art_dir
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    if selected.source == "artifacts-dir":
        shutil.copytree(cell_dir / art_dir, dst_dir)
        return f"the selected artifact directory `{art_dir}` (copied from artifacts/)"
    tarball = cell_dir / "artifacts.tar.gz"
    prefix = str(art_dir).strip("/") + "/"
    with tarfile.open(tarball) as tf:
        members = [
            m for m in tf.getmembers()
            if m.isfile() and m.name.lstrip("./").startswith(prefix)
            and not (".." in Path(m.name).parts or Path(m.name).is_absolute())
        ]
        for m in members:
            rel = m.name.lstrip("./")
            target = out_cell / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(m)
            if extracted is not None:
                target.write_bytes(extracted.read())
    return f"the selected artifact directory `{art_dir}` (extracted from artifacts.tar.gz, {len(members)} file(s))"


# --- campaign driver ------------------------------------------------------------


def _iter_cells(trial_roots: Iterable[Path], cells: Iterable[Path]) -> List[Path]:
    out: List[Path] = [Path(c) for c in cells]
    for root in trial_roots:
        eval_dir = Path(root) / "evaluation"
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
    include_pass_wins: bool = False,
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
                    include_pass_wins=include_pass_wins,
                )
            except Exception as exc:  # noqa: BLE001 — a campaign must not die on one cell
                rec = CellRecord(
                    cell=str(cell), repo=repo, trial=Path(cell).parent.parent.name, milestone=Path(cell).name,
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
        "already_identity": 0,
        "non_replayable": 0,
        "invariant_failed": 0,
        "error": 0,
        "frozen_pass_wins": 0,
        "era": {"fail-close": 0, "pass-wins": 0, ERA_AGNOSTIC: 0},
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
        "mirrored": 0,
        "filtered_not_regenerated": {},
    }
    for r in records:
        if r.status in ("replayable", "invariant-failed"):
            if r.status == "replayable":
                s["replayable"] += 1
            else:
                s["invariant_failed"] += 1
                s["invariant_failures"].append({"cell": f"{r.trial}/{r.milestone}", "failures": r.invariant_failures})
            s["era"][r.era] = s["era"].get(r.era, 0) + 1
            if r.frozen:
                s["frozen_pass_wins"] += 1
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
            if r.mirrored:
                s["mirrored"] += 1
            if r.filtered_reason:
                s["filtered_not_regenerated"][r.filtered_reason] = s["filtered_not_regenerated"].get(r.filtered_reason, 0) + 1
        elif r.status == "already-identity":
            s["already_identity"] += 1
        elif r.status == "non-replayable":
            s["non_replayable"] += 1
            s["non_replayable_reasons"][r.reason] = s["non_replayable_reasons"].get(r.reason, 0) + 1
        else:
            s["error"] += 1
            key = f"error:{r.reason[:60]}"
            s["non_replayable_reasons"][key] = s["non_replayable_reasons"].get(key, 0) + 1
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
    ap.add_argument("--include-pass-wins", action="store_true",
                    help="also mirror cells reproducible only under the pre-2026-07-15 pass-wins scorer "
                         "(frozen by default: their stored values may be lucky, not correct)")
    args = ap.parse_args(argv)
    cells = _iter_cells(args.trial_root, args.cell)
    if not cells:
        print("no cells found", file=sys.stderr)
        return 2
    summary = run_campaign(
        data_root=args.data_root, cells=cells, out_dir=args.out,
        mirror=args.mode == "mirror", include_pass_wins=args.include_pass_wins,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
