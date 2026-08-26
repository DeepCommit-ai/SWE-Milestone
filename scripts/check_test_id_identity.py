#!/usr/bin/env python3
"""Check the test-ID identity contract for one repo (issue #24 guard).

The evaluator scores a cell by matching baseline classification IDs against
runtime report IDs through a canonical key. That key is identity-preserving for
every framework except Maven/Gradle (hashcode folding) and Ginkgo (the legacy
prefix-dropping bridge). The contract this script enforces for a repo's dataset:

  1. every milestone resolves to a *known* test framework (never None, never
     an unrecognised string);
  2. no classification universe contains the same raw test ID twice across its
     transition buckets (the classifier itself overwrites duplicate raw
     observations before they reach a universe — see DeepCommit-Env
     ``test_runner/core/classifier.py`` — so cross-crate duplicates must also be
     gated there; this check catches what survives);
  3. the scoring key is injective over every universe's IDs, all transition
     buckets — Maven hashcode instances are reported as approved equivalences,
     Ginkgo merges as the known bridge residual (warning), anything else fails;
  4. optionally, injectivity also holds over expected ∪ runtime IDs for one
     runtime report associated with one milestone (``--runtime-payload`` +
     ``--runtime-milestone``), classified the same way.

Run it when a repo is added and on every dataset rebuild:

    python scripts/check_test_id_identity.py \
        --data-root /data2/gangda/SWE-Milestone-data/<repo_key> [--json report.json]

Exit status 1 on any hard failure, or when no universe was found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.e2e.evaluator import (  # noqa: E402
    _PREFIX_DROP_FRAMEWORKS,
    _resolve_test_framework,
    load_repo_config,
    normalize_scoring_nodeid,
    select_classification,
)

# Frameworks the report parser and scorer know about. Anything else is a
# configuration defect: the scorer would key it by identity, but the dataset
# should name a supported framework.
KNOWN_FRAMEWORKS = frozenset({
    "pytest", "jest", "vitest", "playwright", "mocha", "unittest", "django_runtests",
    "nushell_script", "cargo", "go_test", "ginkgo", "maven", "gradle",
})


def _ids(entries: Any) -> List[str]:
    out: List[str] = []
    for e in entries or []:
        tid = e if isinstance(e, str) else (e.get("test_id") or e.get("nodeid") or "")
        if tid:
            out.append(tid)
    return out


_TRANSITION_BUCKET = re.compile(r"^[a-z]+_to_[a-z]+$")


def _universe_ids(classification: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """All IDs across every transition bucket (``<start>_to_<end>``) of a
    classification view, plus the bucket names used. Bucket names are derived
    from the data, not hard-coded; auxiliary lists that mirror transitions
    (``new_tests``, ``removed_tests``, ``flaky_tests``) are not buckets."""
    ids: List[str] = []
    buckets: List[str] = []
    for key, value in classification.items():
        if isinstance(value, list) and _TRANSITION_BUCKET.match(key):
            got = _ids(value)
            if got:
                buckets.append(key)
                ids.extend(got)
    return ids, sorted(buckets)


def _runtime_ids(payload_path: Optional[Path]) -> List[str]:
    if not payload_path:
        return []
    data = json.loads(payload_path.read_text())
    results = data.get("results") or {}
    ids: List[str] = []
    for key in ("passed", "failed", "error", "xpassed", "xfailed"):
        for item in results.get(key) or []:
            ids.append(item if isinstance(item, str) else item.get("nodeid", ""))
    for group in results.get("skipped") or []:
        if isinstance(group, dict):
            ids.extend(group.get("tests") or [])
    return [i for i in ids if i]


def _classify_merges(framework: Optional[str], merged: Dict[str, List[str]], label: str) -> Tuple[str, str]:
    """Return (severity, message) for a set of key merges: 'info' | 'warn' | 'hard'."""
    example = next(iter(merged.values()))
    msg = (f"{label}: {len(merged)} keys / {sum(len(v) for v in merged.values())} ids "
           f"(e.g. {example[:2]})")
    if framework in ("maven", "gradle"):
        return "info", "java-hashcode-instances " + msg
    if framework in _PREFIX_DROP_FRAMEWORKS:
        return "warn", "ginkgo-bridge-residual " + msg
    return "hard", msg


def check_repo(
    data_root: Path,
    framework_override: Optional[str],
    runtime_payload: Optional[Path],
    runtime_milestone: Optional[str],
) -> Dict[str, Any]:
    data_root = data_root.resolve()
    repo = data_root.name
    repo_config = load_repo_config(repo, workspace_root=data_root)
    universes = sorted((data_root / "test_results").glob("*/*_classification.json"))
    report: Dict[str, Any] = {"repo": repo, "universes": [], "hard_failures": 0, "warnings": 0}
    if not universes:
        report["hard_failures"] += 1
        report["error"] = f"no classification universes under {data_root / 'test_results'}"
        return report
    runtime_ids = _runtime_ids(runtime_payload)
    for path in universes:
        mid = path.parent.name
        doc = json.loads(path.read_text())
        classification, source = select_classification(doc)
        all_ids, buckets = _universe_ids(classification)
        framework_error = ""
        try:
            framework = framework_override or _resolve_test_framework(repo_config, data_root, mid, all_ids)
        except ValueError as exc:
            framework, framework_error = None, str(exc)
        entry: Dict[str, Any] = {
            "milestone": mid,
            "classification_view": source,
            "buckets": buckets,
            "framework": framework,
            "ids": len(all_ids),
            "hard": [],
            "warn": [],
            "info": [],
        }
        if framework is None:
            entry["hard"].append("framework-unresolved" + (f": {framework_error}" if framework_error else ""))
        elif framework not in KNOWN_FRAMEWORKS:
            entry["hard"].append(f"framework-unknown: {framework!r}")

        seen: Dict[str, int] = defaultdict(int)
        for tid in all_ids:
            seen[tid] += 1
        dup = sorted(t for t, n in seen.items() if n > 1)
        if dup:
            entry["hard"].append(f"duplicate-raw-ids: {len(dup)} (e.g. {dup[0]})")
            entry["duplicate_raw_ids"] = dup[:20]

        by_key: Dict[str, List[str]] = defaultdict(list)
        for tid in seen:
            by_key[normalize_scoring_nodeid(tid, framework)].append(tid)
        merged = {k: sorted(v) for k, v in by_key.items() if len(v) > 1}
        if merged:
            sev, msg = _classify_merges(framework, merged, "identity-key-merges")
            entry[sev].append(msg)
            entry["merged_examples"] = dict(list(merged.items())[:10])

        if runtime_ids and runtime_milestone == mid:
            union = set(seen) | set(runtime_ids)
            by_key_rt: Dict[str, List[str]] = defaultdict(list)
            for tid in union:
                by_key_rt[normalize_scoring_nodeid(tid, framework)].append(tid)
            merged_rt = {k: sorted(v) for k, v in by_key_rt.items() if len(v) > 1}
            # Merges already present in the expected universe are counted above;
            # report only the ones the runtime report introduces.
            new_merges = {k: v for k, v in merged_rt.items() if k not in merged}
            if new_merges:
                sev, msg = _classify_merges(framework, new_merges, "expected∪runtime-key-merges")
                entry[sev].append(msg)
        report["hard_failures"] += len(entry["hard"])
        report["warnings"] += len(entry["warn"])
        report["universes"].append(entry)
    if runtime_ids and runtime_milestone and not any(u["milestone"] == runtime_milestone for u in report["universes"]):
        report["hard_failures"] += 1
        report["error"] = f"--runtime-milestone {runtime_milestone!r} has no classification universe"
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-root", required=True, type=Path)
    ap.add_argument("--framework", default=None, help="override framework resolution (diagnostics only)")
    ap.add_argument("--runtime-payload", type=Path, default=None,
                    help="an eval_summary.json to include in the injectivity check (requires --runtime-milestone)")
    ap.add_argument("--runtime-milestone", default=None, help="milestone the runtime payload belongs to")
    ap.add_argument("--json", type=Path, default=None, help="write the full report here")
    args = ap.parse_args(argv)
    if bool(args.runtime_payload) != bool(args.runtime_milestone):
        ap.error("--runtime-payload and --runtime-milestone must be given together")
    report = check_repo(args.data_root, args.framework, args.runtime_payload, args.runtime_milestone)
    print(f"{report['repo']}: {len(report['universes'])} universes, "
          f"{report['hard_failures']} hard failure(s), {report['warnings']} warning(s)")
    if report.get("error"):
        print(f"  error: {report['error']}")
    for u in report["universes"]:
        flag = "FAIL" if u["hard"] else ("warn" if u["warn"] else "ok")
        print(f"  {u['milestone']:34s} fw={str(u['framework']):10s} ids={u['ids']:6d} {flag}")
        for h in u["hard"]:
            print(f"      hard: {h}")
        for w in u["warn"]:
            print(f"      warn: {w}")
        for i in u["info"]:
            print(f"      info: {i}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 1 if report["hard_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
