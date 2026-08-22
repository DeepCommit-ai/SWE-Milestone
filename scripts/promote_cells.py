#!/usr/bin/env python3
"""Promote evaluator outputs into the primary record, atomically and only through here.

The primary record (``<log-root>/<repo>/e2e_trial/<trial>/evaluation/<cell>/``) is
what the leaderboard reads.  Every earlier promotion was a one-off script and at
least one of them copied result files without artifacts, leaving cells whose
stored numbers no raw report reproduces.  This tool is the single way to write
a re-evaluation or re-tally result into the record:

  per cell   backup (append-only, never overwritten)
             evaluation_result.json            <- source
             evaluation_result_filtered.json   <- source, or DELETED when the source has none
                                                  (a stale filtered file shadows the new result)
             artifacts/ + artifacts.tar.gz     <- source artifacts when it ships them (repacked, verified)
             provenance files                  <- rescore_manifest.json, PROMOTION_NOTES.md, reeval.log, eval.log
  per trial  summary.json / summary_filtered.json: results[<exact cell key>] eval_status + test_summary
             (attempt kept; keys are never created: an attempt the collector does not know stays unknown),
             status lists recalculated
  never      source_snapshot.tar (frozen agent artifact)

Source layouts: ``mirror`` (rescore: <source-root>/<repo>/<trial>/<cell>/) or ``campaign``
(re-evaluation: <source-root>/<repo>/e2e_trial/<trial>/evaluation/<cell>/).
Dry run by default; ``--execute`` writes.  A second run over the same cells is a no-op
(cells whose stored result already equals the source are skipped).  With ``--data-root``
the promoted cells are re-checked with the rescore tool afterwards (expected:
``already-identity`` or ``replayable`` without delta).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROVENANCE_FILES = ("rescore_manifest.json", "PROMOTION_NOTES.md", "reeval.log", "eval.log")
RESULT_FILES = ("evaluation_result.json", "evaluation_result_filtered.json")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _write_json_atomic(path: Path, value) -> None:
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def _copy_atomic(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + f".tmp.{os.getpid()}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def recalculate_summary_status(summary: dict) -> None:
    """Recompute statistics / milestone_status / completed / failed / errors from results."""
    results = summary.get("results", {})
    passed = sorted(k for k, e in results.items() if e.get("eval_status") == "passed")
    failed = sorted(k for k, e in results.items() if e.get("eval_status") == "failed")
    errors = sorted(k for k, e in results.items() if e.get("eval_status") == "error")
    early = sorted(k for k, e in results.items() if e.get("dag_status") == "unlocked")
    old_status = summary.get("milestone_status", {})
    evaluated_bases = {k.split("-retry", 1)[0] for k in passed + failed + errors}

    def pending(name: str) -> List[str]:
        value = old_status.get(name, [])
        if not isinstance(value, list):
            return []
        return sorted(item for item in value if item not in evaluated_bases)

    available, submitted, blocked, skipped = (pending(n) for n in ("available", "submitted", "blocked", "skipped"))
    summary["statistics"] = {
        "passed": len(passed), "failed": len(failed), "error": len(errors), "available": len(available),
        "submitted": len(submitted), "blocked": len(blocked), "skipped": len(skipped), "early_unlocked": len(early),
    }
    summary["milestone_status"] = {
        "passed": passed, "failed": failed, "error": errors, "available": available,
        "submitted": submitted, "blocked": blocked, "skipped": skipped, "early_unlocked": early,
    }
    summary["completed"] = passed
    summary["failed"] = failed
    summary["errors"] = errors


def pack_artifacts(cell: Path) -> None:
    """(Re)create <cell>/artifacts.tar.gz from <cell>/artifacts (tar | pigz or gzip), then verify it."""
    tmp = cell / f".artifacts.tar.gz.tmp-{os.getpid()}"
    compressor = ["pigz", "-p", "2"] if shutil.which("pigz") else ["gzip"]
    with tmp.open("wb") as out:
        tar = subprocess.Popen(["tar", "-cf", "-", "-C", str(cell), "artifacts"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gz = subprocess.Popen(compressor, stdin=tar.stdout, stdout=out, stderr=subprocess.PIPE)
        assert tar.stdout is not None
        tar.stdout.close()
        _, gz_err = gz.communicate()
        tar_err = tar.stderr.read() if tar.stderr else b""
        tar_rc = tar.wait()
    if tar_rc != 0 or gz.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"pack failed for {cell}: tar={tar_rc} gz={gz.returncode} {tar_err!r} {gz_err!r}")
    os.replace(tmp, cell / "artifacts.tar.gz")
    with tarfile.open(cell / "artifacts.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
    if not members:
        raise RuntimeError(f"empty tarball {cell}")
    for m in members:
        name = m.name.removeprefix("./")
        if not (name == "artifacts" or name.startswith("artifacts/")) or ".." in Path(name).parts or m.name.startswith("/"):
            raise RuntimeError(f"unsafe/foreign tar member {m.name} in {cell}")


def source_cell(source_root: Path, layout: str, repo: str, trial: str, cell: str) -> Path:
    if layout == "mirror":
        return source_root / repo / trial / cell
    return source_root / repo / "e2e_trial" / trial / "evaluation" / cell


def plan_cells(log_root: Path, source_root: Path, layout: str, repo: str, cells: List[Tuple[str, str]]) -> Tuple[List[dict], List[dict]]:
    plan, skipped = [], []
    for trial, cell in cells:
        target = log_root / repo / "e2e_trial" / trial / "evaluation" / cell
        src = source_cell(source_root, layout, repo, trial, cell)
        if not (src / "evaluation_result.json").exists():
            skipped.append(dict(trial=trial, cell=cell, why="source has no evaluation_result.json")); continue
        if not target.is_dir():
            skipped.append(dict(trial=trial, cell=cell, why="target cell does not exist")); continue
        same_result = (target / "evaluation_result.json").exists() and _sha(target / "evaluation_result.json") == _sha(src / "evaluation_result.json")
        src_art = sorted(p.name for p in (src / "artifacts").iterdir()) if (src / "artifacts").is_dir() else []
        same_artifacts = True
        if src_art:
            same_artifacts = (target / "artifacts").is_dir() and all(
                (target / "artifacts" / p.relative_to(src / "artifacts")).exists() and _sha(p) == _sha(target / "artifacts" / p.relative_to(src / "artifacts"))
                for p in (src / "artifacts").rglob("*") if p.is_file()
            )
        new_filtered = (src / "evaluation_result_filtered.json").exists()
        stale_filtered = (target / "evaluation_result_filtered.json").exists() and not new_filtered
        same_filtered = (not new_filtered and not stale_filtered) or (
            new_filtered and (target / "evaluation_result_filtered.json").exists()
            and _sha(target / "evaluation_result_filtered.json") == _sha(src / "evaluation_result_filtered.json"))
        if same_result and same_artifacts and same_filtered:
            skipped.append(dict(trial=trial, cell=cell, why="already promoted")); continue
        plan.append(dict(trial=trial, cell=cell, target=target, source=src, write_result=not same_result,
                         write_artifacts=bool(src_art) and not same_artifacts, new_filtered=new_filtered,
                         stale_filtered=stale_filtered, src_artifacts=src_art,
                         stored_resolved=_load(target / "evaluation_result.json").get("resolved") if (target / "evaluation_result.json").exists() else None,
                         new_resolved=_load(src / "evaluation_result.json").get("resolved")))
    return plan, skipped


def promote(plan: List[dict], *, log_root: Path, repo: str, backup_dir: Path) -> List[dict]:
    manifest: List[dict] = []
    by_trial: Dict[str, List[dict]] = defaultdict(list)
    for p in plan:
        by_trial[p["trial"]].append(p)
    for trial, ps in sorted(by_trial.items()):
        for p in ps:
            target, src = p["target"], p["source"]
            bdir = backup_dir / repo / trial / p["cell"]
            bdir.mkdir(parents=True, exist_ok=False)  # append-only: an existing backup means a second promotion
            for name in RESULT_FILES + ("feedback_report.md",):
                if (target / name).exists():
                    shutil.copy2(target / name, bdir / name)
            if p["write_artifacts"]:
                if (target / "artifacts").is_dir():
                    shutil.copytree(target / "artifacts", bdir / "artifacts", symlinks=True)
                if (target / "artifacts.tar.gz").exists():
                    shutil.copy2(target / "artifacts.tar.gz", bdir / "artifacts.tar.gz")
            before = {n: _sha(target / n) for n in RESULT_FILES + ("artifacts.tar.gz",) if (target / n).exists()}
            if p["write_result"]:
                _copy_atomic(src / "evaluation_result.json", target / "evaluation_result.json")
            if p["new_filtered"]:
                _copy_atomic(src / "evaluation_result_filtered.json", target / "evaluation_result_filtered.json")
            elif p["stale_filtered"]:
                (target / "evaluation_result_filtered.json").unlink()
            if p["write_artifacts"]:
                if (target / "artifacts").is_dir():
                    shutil.rmtree(target / "artifacts")
                shutil.copytree(src / "artifacts", target / "artifacts")
                pack_artifacts(target)
            for name in PROVENANCE_FILES:
                if (src / name).exists():
                    _copy_atomic(src / name, target / name)
            after = {n: _sha(target / n) for n in RESULT_FILES + ("artifacts.tar.gz",) if (target / n).exists()}
            manifest.append(dict(repo=repo, trial=trial, cell=p["cell"], backup=str(bdir), before=before, after=after,
                                 result="replaced" if p["write_result"] else "unchanged",
                                 filtered="copied" if p["new_filtered"] else ("deleted-stale" if p["stale_filtered"] else "none"),
                                 artifacts="replaced+repacked" if p["write_artifacts"] else "unchanged",
                                 resolved=[p["stored_resolved"], p["new_resolved"]]))
        # trial summaries
        evaluation = log_root / repo / "e2e_trial" / trial / "evaluation"
        for sname in ("summary.json", "summary_filtered.json"):
            spath = evaluation / sname
            if not spath.exists():
                continue
            bs = backup_dir / repo / trial / (sname + ".before")
            bs.parent.mkdir(parents=True, exist_ok=True)
            if not bs.exists():
                shutil.copy2(spath, bs)
            summary = _load(spath)
            results = summary.setdefault("results", {})
            for p in ps:
                key = p["cell"]
                if key not in results:
                    manifest.append(dict(repo=repo, trial=trial, cell=key, note=f"{sname}: no results key for this attempt; not created"))
                    continue
                src_file = p["target"] / "evaluation_result.json"
                if sname == "summary_filtered.json" and (p["target"] / "evaluation_result_filtered.json").exists():
                    src_file = p["target"] / "evaluation_result_filtered.json"
                d = _load(src_file)
                results[key]["eval_status"] = "passed" if d.get("resolved") else "failed"
                results[key]["test_summary"] = copy.deepcopy(d.get("test_summary", {}))
                results[key].pop("error", None)
                results[key].pop("filter_stats", None)
                if "filter_stats" in d:
                    results[key]["filter_stats"] = copy.deepcopy(d["filter_stats"])
            recalculate_summary_status(summary)
            _write_json_atomic(spath, summary)
    return manifest


def post_check(log_root: Path, data_root: Path, repo: str, plan: List[dict], out_dir: Path) -> Dict[str, int]:
    from harness.e2e.rescore import run_campaign  # noqa: E402
    cells = [p["target"] for p in plan]
    run_campaign(data_root=data_root / repo, cells=cells, out_dir=out_dir, mirror=False)
    counts: Dict[str, int] = defaultdict(int)
    for line in open(out_dir / "records.jsonl"):
        rec = json.loads(line)
        label = rec["status"] if rec["status"] != "replayable" else ("replayable" + ("+delta" if rec.get("delta") else ""))
        counts[label] += 1
    return dict(counts)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log-root", required=True, type=Path)
    ap.add_argument("--repo", required=True, help="repo key, e.g. scikit-learn_scikit-learn_1.5.2_1.6.0")
    ap.add_argument("--source-root", required=True, type=Path)
    ap.add_argument("--layout", choices=("mirror", "campaign"), required=True)
    ap.add_argument("--cell", action="append", default=[], help="<trial>/<cell dir name> (repeatable)")
    ap.add_argument("--cells-file", type=Path, help="file with one <trial>/<cell> per line")
    ap.add_argument("--all-in-source", action="store_true", help="every cell the source root holds for this repo")
    ap.add_argument("--backup-root", required=True, type=Path, help="e.g. <log-root>/reeval/promotion_backup")
    ap.add_argument("--campaign", required=True, help="campaign name; backups go to <backup-root>/<campaign>/")
    ap.add_argument("--data-root", type=Path, help="data root; when given, promoted cells are re-checked with rescore")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    cells: List[Tuple[str, str]] = []
    for spec in args.cell:
        trial, cell = spec.split("/", 1)
        cells.append((trial, cell))
    if args.cells_file:
        for line in args.cells_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                trial, cell = line.split("/", 1)
                cells.append((trial, cell))
    if args.all_in_source:
        base = args.source_root / args.repo
        if args.layout == "mirror":
            cells += [(t.name, c.name) for t in sorted(base.iterdir()) if t.is_dir() for c in sorted(t.iterdir()) if (c / "evaluation_result.json").exists()]
        else:
            cells += [(t.name, c.name) for t in sorted((base / "e2e_trial").iterdir()) if (t / "evaluation").is_dir()
                      for c in sorted((t / "evaluation").iterdir()) if (c / "evaluation_result.json").exists()]
    cells = sorted(set(cells))
    if not cells:
        print("no cells given", file=sys.stderr)
        return 2
    plan, skipped = plan_cells(args.log_root, args.source_root, args.layout, args.repo, cells)
    print(f"cells given: {len(cells)}  to promote: {len(plan)}  skipped: {len(skipped)}")
    for s in skipped[:30]:
        print(f"  skip {s['trial']}/{s['cell']}: {s['why']}")
    for p in plan:
        print(f"  {p['trial']}/{p['cell']}: result={'replace' if p['write_result'] else 'same'} "
              f"filtered={'copy' if p['new_filtered'] else ('DELETE-stale' if p['stale_filtered'] else 'none')} "
              f"artifacts={'replace+repack ' + str(p['src_artifacts']) if p['write_artifacts'] else 'unchanged'} "
              f"resolved {p['stored_resolved']} -> {p['new_resolved']}")
    if not plan:
        print("nothing to promote")
        return 0
    if not args.execute:
        print("DRY RUN: nothing written (pass --execute)")
        return 0
    backup_dir = args.backup_root / args.campaign
    if backup_dir.exists() and any(backup_dir.iterdir()):
        print(f"refusing: {backup_dir} already holds a backup (append-only; use a new --campaign name)", file=sys.stderr)
        return 3
    backup_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    manifest = promote(plan, log_root=args.log_root, repo=args.repo, backup_dir=backup_dir)
    (backup_dir / f"PROMOTION_MANIFEST_{args.repo}.json").write_text(json.dumps(manifest, indent=1))
    print(f"promoted {len(plan)} cells in {time.time() - t0:.1f}s; manifest {backup_dir / f'PROMOTION_MANIFEST_{args.repo}.json'}")
    if args.data_root:
        counts = post_check(args.log_root, args.data_root, args.repo, plan, backup_dir / f"postcheck_{args.repo}")
        print("post-check (rescore report):", counts)
        bad = {k: v for k, v in counts.items() if k not in ("already-identity", "replayable")}
        if bad:
            print("WARNING: promoted cells that are not self-consistent under the current scorer:", bad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
