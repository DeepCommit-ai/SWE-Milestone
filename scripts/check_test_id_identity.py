#!/usr/bin/env python3
"""Check the test-ID identity contract for one repo (issue #24 guard).

The evaluator scores a cell by matching baseline classification IDs against
runtime report IDs through a canonical key. That key is identity-preserving for
every framework except Maven/Gradle (hashcode folding) and Ginkgo (module
prefix bridge). The contract this script enforces for a repo's dataset:

  1. every milestone resolves to a known test framework (never None);
  2. no classification universe contains the same raw test ID twice
     (a duplicate means the parser already lost identity, e.g. two Cargo
     crates with the same full test name);
  3. the scoring key is injective over every universe's IDs (all buckets) —
     for Ginkgo, merges are reported as the known bridge residual, not failed;
  4. optionally, injectivity also holds over expected ∪ runtime IDs for a
     supplied runtime report (``--runtime-payload``).

Run it when a repo is added and on every dataset rebuild:

    python scripts/check_test_id_identity.py \
        --data-root /data2/gangda/SWE-Milestone-data/<repo_key> [--json report.json]

Exit status 1 on any hard failure (1–3 for non-Ginkgo, 4), 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.e2e.evaluator import (  # noqa: E402
    _PREFIX_DROP_FRAMEWORKS,
    _resolve_test_framework,
    load_repo_config,
    normalize_scoring_nodeid,
    select_classification,
)

BUCKETS = (
    "fail_to_pass",
    "none_to_pass",
    "pass_to_pass",
    "fail_to_fail",
    "pass_to_fail",
    "none_to_fail",
    "error_to_pass",
    "skipped_to_pass",
)


def _ids(entries: Any) -> List[str]:
    out: List[str] = []
    for e in entries or []:
        tid = e if isinstance(e, str) else (e.get("test_id") or e.get("nodeid") or "")
        if tid:
            out.append(tid)
    return out


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


def check_repo(data_root: Path, framework_override: Optional[str], runtime_payload: Optional[Path]) -> Dict[str, Any]:
    data_root = data_root.resolve()
    repo = data_root.name
    repo_config = load_repo_config(repo, workspace_root=data_root)
    universes = sorted((data_root / "test_results").glob("*/*_classification.json"))
    report: Dict[str, Any] = {"repo": repo, "universes": [], "hard_failures": 0, "warnings": 0}
    runtime_ids = _runtime_ids(runtime_payload)
    for path in universes:
        mid = path.parent.name
        doc = json.loads(path.read_text())
        classification, source = select_classification(doc)
        all_ids: List[str] = []
        for bucket in BUCKETS:
            all_ids.extend(_ids(classification.get(bucket)))
        try:
            framework = framework_override or _resolve_test_framework(repo_config, data_root, mid, all_ids)
        except ValueError as exc:
            framework = None
            framework_error = str(exc)
        else:
            framework_error = ""
        entry: Dict[str, Any] = {
            "milestone": mid,
            "classification_view": source,
            "framework": framework,
            "ids": len(all_ids),
            "hard": [],
            "warn": [],
        }
        if framework is None:
            entry["hard"].append("framework-unresolved" + (f": {framework_error}" if framework_error else ""))
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
            example = next(iter(merged.values()))
            msg = f"identity-key-merges: {len(merged)} keys / {sum(len(v) for v in merged.values())} ids (e.g. {example[:2]})"
            if framework in ("maven", "gradle"):
                # Raw IDs that differ only by a JVM hashcode are one logical
                # test (the key folds @<hash> by design); record, do not fail.
                entry.setdefault("info", []).append("java-hashcode-instances " + msg)
            elif framework in _PREFIX_DROP_FRAMEWORKS:
                entry["warn"].append("ginkgo-bridge-residual " + msg)
            else:
                entry["hard"].append(msg)
            entry["merged_examples"] = dict(list(merged.items())[:10])
        if runtime_ids:
            union = set(seen) | set(runtime_ids)
            by_key_rt: Dict[str, List[str]] = defaultdict(list)
            for tid in union:
                by_key_rt[normalize_scoring_nodeid(tid, framework)].append(tid)
            merged_rt = {k: v for k, v in by_key_rt.items() if len(v) > 1}
            if merged_rt and framework not in _PREFIX_DROP_FRAMEWORKS:
                entry["hard"].append(f"expected∪runtime-key-merges: {len(merged_rt)}")
        report["hard_failures"] += len(entry["hard"])
        report["warnings"] += len(entry["warn"])
        report["universes"].append(entry)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-root", required=True, type=Path)
    ap.add_argument("--framework", default=None, help="override framework resolution (diagnostics only)")
    ap.add_argument("--runtime-payload", type=Path, default=None, help="an eval_summary.json to include in the injectivity check")
    ap.add_argument("--json", type=Path, default=None, help="write the full report here")
    args = ap.parse_args(argv)
    report = check_repo(args.data_root, args.framework, args.runtime_payload)
    print(f"{report['repo']}: {len(report['universes'])} universes, "
          f"{report['hard_failures']} hard failure(s), {report['warnings']} warning(s)")
    for u in report["universes"]:
        flag = "FAIL" if u["hard"] else ("warn" if u["warn"] else "ok")
        print(f"  {u['milestone']:34s} fw={str(u['framework']):10s} ids={u['ids']:6d} {flag}")
        for h in u["hard"]:
            print(f"      hard: {h}")
        for w in u["warn"]:
            print(f"      warn: {w}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 1 if report["hard_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
