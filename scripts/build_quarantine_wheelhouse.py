#!/usr/bin/env python3
"""Build a quarantine pip wheelhouse (offline dependency closure) that is
guaranteed to EXCLUDE the repo-under-test's own package.

Run this INSIDE the repo's clean base image (so the freeze list is authoritative
and the editable dev checkout of the repo shows up as `-e /testbed`, not a
pinned `==target` line). Example:

    docker run --rm -v /host/sk_wheelhouse:/wh \
        scikit-learn_scikit-learn_1.5.2_1.6.0/base:latest \
        python3 /path/to/build_quarantine_wheelhouse.py \
            --out /wh --forbid scikit-learn scikit_learn sklearn

What it does:
  1. `pip freeze` the clean env, dropping editable (`-e`/`@ file://`), VCS
     (`git+`), comments, and any line whose distribution matches `--forbid`.
  2. `pip download` that allow-list + build bootstrap (pip/setuptools/wheel/...)
     into `--out`.
  3. POST-AUDIT (authoritative, fail-closed): scan `--out` for any artifact
     whose distribution matches `--forbid`. If found, delete it AND exit
     non-zero — the wheelhouse must never be able to serve the answer offline.

The same normalization/audit runs at trial startup in scripts/run_all.py
(`_assert_wheelhouse_excludes`) via the `quarantine_configs wheelhouse_forbid` config,
so a tampered wheelhouse is caught even if this builder is bypassed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_ARTIFACT_SUFFIXES = (".whl", ".tar.gz", ".zip")


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _dist_of_requirement(line: str) -> str:
    """Best-effort distribution name from a `pip freeze` line."""
    line = line.strip()
    for sep in ("==", ">=", "<=", "~=", "!=", " @ ", "@"):
        if sep in line:
            line = line.split(sep, 1)[0]
            break
    return _norm(line)


def _artifact_matches(filename: str, forbid: list[str]) -> bool:
    low = _norm(filename)
    if not low.endswith(tuple(_norm(s) for s in _ARTIFACT_SUFFIXES)):
        return False
    return any(low.startswith(pref + "-") for pref in forbid)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Wheelhouse output directory")
    ap.add_argument("--forbid", nargs="+", required=True,
                    help="Distribution name(s) to exclude (e.g. scikit-learn scikit_learn sklearn)")
    ap.add_argument("--bootstrap", nargs="*", default=["pip", "setuptools", "wheel", "ninja"],
                    help="Build-bootstrap packages to also download (default: pip setuptools wheel ninja)")
    args = ap.parse_args()

    forbid = [_norm(f) for f in args.forbid]
    os.makedirs(args.out, exist_ok=True)

    # 1. Freeze the clean env and build the allow-list.
    frozen = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                            capture_output=True, text=True, check=True).stdout.splitlines()
    allow = []
    dropped = []
    for line in frozen:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-e ") or s.startswith("git+") or "@ file://" in s:
            dropped.append(s)
            continue
        if _dist_of_requirement(s) in forbid:
            dropped.append(s)  # the repo's own package — never include
            continue
        allow.append(s)

    print(f"[build] allow-list: {len(allow)} pkgs, dropped {len(dropped)} (editable/vcs/forbidden)")
    req_path = os.path.join(args.out, "_allow.txt")
    with open(req_path, "w") as fh:
        fh.write("\n".join(allow) + "\n")

    # 2. Download the closure + bootstrap.
    if allow:
        subprocess.run([sys.executable, "-m", "pip", "download", "-r", req_path, "-d", args.out], check=True)
    if args.bootstrap:
        subprocess.run([sys.executable, "-m", "pip", "download", *args.bootstrap, "-d", args.out], check=True)
    os.remove(req_path)

    # 3. POST-AUDIT — fail closed.
    offending = [n for n in os.listdir(args.out) if _artifact_matches(n, forbid)]
    if offending:
        for n in offending:
            try:
                os.remove(os.path.join(args.out, n))
            except OSError:
                pass
        print(f"[build] FAIL: forbidden artifact(s) reached the wheelhouse and were "
              f"removed: {sorted(offending)}. The closure pulled the repo's own "
              f"package as a transitive dep — investigate before trusting this "
              f"wheelhouse.", file=sys.stderr)
        return 1

    n_art = sum(1 for n in os.listdir(args.out) if _norm(n).endswith(tuple(_norm(s) for s in _ARTIFACT_SUFFIXES)))
    print(f"[build] OK: {n_art} artifacts in {args.out}, none matching {args.forbid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
