#!/usr/bin/env python3
"""Release gate: every published cell the leaderboard reads must be self-consistent.

A cell is consistent when its stored evaluation_result.json is what the current scorer
produces from the cell's own stored raw report (rescore report mode says
``already-identity``, or ``replayable`` with no delta), and when its stored
evaluation_result_filtered.json (if any) is what the CURRENT filter list produces from
that result.  Anything else is a defect that used to surface months later as a
"non-replayable" or "filter-list drift" cell during an audit.  Such cells must either
be repaired (re-tally / re-evaluate / promote through scripts/promote_cells.py) or be
listed, with a reason, in the accepted-legacy file:

    ACCEPTED_LEGACY.tsv      repo<TAB>trial<TAB>cell<TAB>reason

Cells are enumerated through collect_results.authoritative_cells (the attempt the
collector serves per milestone), so a retry directory that carries the board number is
checked and a superseded attempt is not.  Exit 1 when any unaccepted failure exists.
Intended as step 0 of SWE-Milestone-log/scripts/release.sh.

Usage:
    python scripts/check_record_consistency.py --log-root <SWE-Milestone-log> --data-root <SWE-Milestone-data> \
        [--repo <key>]... [--accepted <ACCEPTED_LEGACY.tsv>] [--out <dir>] [--jobs N] [--all-trials]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.e2e.collect_results import authoritative_cells  # noqa: E402
from harness.e2e.evaluator import filter_evaluation_result, load_filter_list  # noqa: E402
from harness.e2e.rescore import _ran_test_ids, run_campaign  # noqa: E402

FILTER_COMPARE_KEYS = (
    "total", "passed", "failed", "error", "fail_to_pass_required", "fail_to_pass_achieved",
    "none_to_pass_required", "none_to_pass_achieved", "pass_to_pass_required", "pass_to_pass_achieved",
    "pass_to_pass_failed", "pass_to_pass_missing",
)


def load_accepted(path: Optional[Path]) -> Dict[Tuple[str, str, str], str]:
    accepted: Dict[Tuple[str, str, str], str] = {}
    if path is None or not path.exists():
        return accepted
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            raise SystemExit(f"{path}: malformed line (need repo<TAB>trial<TAB>cell<TAB>reason): {line!r}")
        accepted[(parts[0], parts[1], parts[2])] = parts[3]
    return accepted


def _filtered_drift(cell: Path, data_root: Path, selected_payload: str) -> Optional[str]:
    """'' when the stored filtered file equals what the current filter list produces,
    None when the cell has no filtered file, else a reason string."""
    stored_f = cell / "evaluation_result_filtered.json"
    if not stored_f.exists():
        return None
    base = cell.name.split("-retry", 1)[0]
    filter_list = load_filter_list(data_root, base)
    if not filter_list or not any(filter_list.get(k) for k in ("invalid_fail_to_pass", "invalid_none_to_pass", "invalid_pass_to_pass")):
        return "stale filtered file: the milestone has no filter list now (it shadows the unfiltered result)"
    eval_json = (cell / selected_payload).with_name("eval.json") if selected_payload else None
    ran = _ran_test_ids(eval_json) if eval_json is not None and eval_json.exists() else None
    if ran is None:
        return "cannot regenerate the filtered result (no eval.json next to the selected report)"
    unfiltered = json.load(open(cell / "evaluation_result.json"))
    expected = filter_evaluation_result(copy.deepcopy(unfiltered), filter_list, ran_test_ids=ran)
    stored = json.load(open(stored_f))
    es, ss = expected.get("test_summary") or {}, stored.get("test_summary") or {}
    diffs = [k for k in FILTER_COMPARE_KEYS if k in ss and ss.get(k) != es.get(k)]
    for cat in ("FAIL_TO_PASS", "NONE_TO_PASS", "PASS_TO_PASS"):
        for sub in ("success", "failure"):
            a = (stored.get("tests_status") or {}).get(cat, {}).get(sub)
            b = (expected.get("tests_status") or {}).get(cat, {}).get(sub)
            if isinstance(a, list) and isinstance(b, list) and sorted(a) != sorted(b):
                diffs.append(f"{cat}.{sub}")
    return "" if not diffs else "filtered file disagrees with the current filter list: " + ", ".join(diffs)


def check_repo(args: Tuple[str, str, str, str, bool]) -> Dict:
    log_root, data_root, repo, out_dir, all_trials = (Path(args[0]), Path(args[1]), args[2], Path(args[3]), args[4])
    repo_log = log_root / repo
    trials = sorted(t for t in (repo_log / "e2e_trial").iterdir() if t.is_dir() and (all_trials or t.name.startswith("_")))
    cells: List[Path] = []
    for t in trials:
        cells.extend(authoritative_cells(t / "evaluation").values())
    out = out_dir / repo
    out.mkdir(parents=True, exist_ok=True)
    if not cells:
        return {"repo": repo, "cells": 0, "rows": []}
    run_campaign(data_root=data_root / repo, cells=cells, out_dir=out, mirror=False)
    rows = []
    for line in open(out / "records.jsonl"):
        rec = json.loads(line)
        cell = Path(rec["cell"])
        if rec["status"] == "already-identity":
            verdict, why = "ok", ""
        elif rec["status"] == "replayable" and not rec.get("delta"):
            verdict, why = "ok", ""
        elif rec["status"] == "replayable":
            verdict = "fail"
            why = ("stored tally differs from the identity re-tally (" + ("frozen pass-wins era" if rec.get("frozen") else "needs promotion of the re-tally") + ")")
        else:
            verdict, why = "fail", f"{rec['status']}: {rec['reason']}"
        fdrift = None
        if verdict == "ok":
            try:
                fdrift = _filtered_drift(cell, data_root / repo, rec.get("selected_payload") or "")
            except Exception as exc:  # noqa: BLE001 — a gate must report, not crash
                fdrift = f"filtered check raised {exc.__class__.__name__}: {exc}"
            if fdrift:
                verdict, why = "fail", fdrift
        rows.append({"repo": repo, "trial": rec["trial"], "cell": rec["milestone"], "verdict": verdict, "why": why,
                     "status": rec["status"], "era": rec.get("era", ""), "frozen": bool(rec.get("frozen"))})
    return {"repo": repo, "cells": len(cells), "rows": rows}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log-root", required=True, type=Path)
    ap.add_argument("--data-root", required=True, type=Path)
    ap.add_argument("--repo", action="append", default=[], help="repo key (repeatable; default: every repo dir with e2e_trial/)")
    ap.add_argument("--accepted", type=Path, default=None, help="ACCEPTED_LEGACY.tsv (default <log-root>/ACCEPTED_LEGACY.tsv)")
    ap.add_argument("--out", type=Path, default=None, help="scratch/output dir (default <log-root>/.cache/record_consistency)")
    ap.add_argument("--jobs", type=int, default=7)
    ap.add_argument("--all-trials", action="store_true", help="also check local (non-published) trials")
    args = ap.parse_args(argv)
    repos = args.repo or sorted(p.name for p in args.log_root.iterdir() if (p / "e2e_trial").is_dir())
    accepted = load_accepted(args.accepted or (args.log_root / "ACCEPTED_LEGACY.tsv"))
    out_dir = args.out or (args.log_root / ".cache" / "record_consistency")
    out_dir.mkdir(parents=True, exist_ok=True)
    work = [(str(args.log_root), str(args.data_root), r, str(out_dir), args.all_trials) for r in repos]
    with ProcessPoolExecutor(max_workers=max(1, min(args.jobs, len(work)))) as ex:
        results = list(ex.map(check_repo, work))
    failures: List[Dict] = []
    used_accept = set()
    print(f"{'repo':42s} {'cells':>6s} {'ok':>6s} {'accepted':>9s} {'FAIL':>6s}")
    for res in results:
        c = Counter()
        for row in res["rows"]:
            key = (row["repo"], row["trial"], row["cell"])
            if row["verdict"] == "ok":
                c["ok"] += 1
            elif key in accepted:
                c["accepted"] += 1
                used_accept.add(key)
            else:
                c["fail"] += 1
                failures.append(row)
        print(f"{res['repo']:42s} {res['cells']:6d} {c['ok']:6d} {c['accepted']:9d} {c['fail']:6d}")
    (out_dir / "FAILURES.tsv").write_text("".join(f"{r['repo']}\t{r['trial']}\t{r['cell']}\t{r['why']}\n" for r in failures))
    unused = sorted(set(accepted) - used_accept)
    if unused:
        print(f"note: {len(unused)} accepted-legacy entries no longer match a failing cell (stale entries; prune them)")
    if failures:
        print(f"\nFAIL: {len(failures)} cell(s) are not self-consistent and not accepted (see {out_dir / 'FAILURES.tsv'}):")
        by_reason = defaultdict(int)
        for r in failures:
            by_reason[r["why"].split(" (")[0]] += 1
        for why, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {why}")
        for r in failures[:40]:
            print(f"  {r['repo']} {r['trial']} {r['cell']}: {r['why']}")
        return 1
    print("\nOK: every served published cell is self-consistent (or explicitly accepted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
