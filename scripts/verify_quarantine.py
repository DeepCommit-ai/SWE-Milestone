#!/usr/bin/env python3
"""Live smoke test of a repo's quarantine policy against its real base image.

Spins up <repo>/base:latest with the policy's env applied, runs the minimal
base init (fakeroot — NOT the agent install), applies lock_network() (which
itself runs the fail-closed verification: deny-domain probes, answer-fetch
URL probes, cache forbid-glob audit), then runs positive probes (LLM
endpoints reachable, offline switches visible). Mirrors the verification
protocol in docs/quarantine.md.

Usage:
    python scripts/verify_quarantine.py --repo ripgrep [--keep]
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from harness.e2e.container_setup import ContainerSetup  # noqa: E402
from harness.e2e.quarantine import image_for_repo, load_quarantine_env  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from run_all import _load_dotenv_files  # noqa: E402


def _match_repo(substr: str) -> str:
    confs = sorted(p.stem for p in (PROJECT_ROOT / "quarantine_configs").glob("*.yaml"))
    hits = [c for c in confs if substr.lower() in c.lower()]
    if len(hits) != 1:
        print(f"Error: --repo '{substr}' matched {hits or 'nothing'} in quarantine_configs/", file=sys.stderr)
        sys.exit(1)
    return hits[0]


def _probe(cs: "ContainerSetup", url: str, expect_blocked: bool) -> bool:
    # python3-based probe (curl-independent — the scikit base ships no curl).
    reachable = cs._url_reachable_in_container(url)
    blocked = not reachable
    ok = blocked == expect_blocked
    label = "BLOCKED" if blocked else "reachable"
    print(f"  {'PASS' if ok else 'FAIL'}  {url} -> {label}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="Repo name substring (matched against quarantine_configs/)")
    ap.add_argument("--image", default=None, help="Override image (default <repo_lowercase>/base:latest)")
    ap.add_argument("--keep", action="store_true", help="Keep the container for manual inspection")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _load_dotenv_files()
    repo = _match_repo(args.repo)
    # Default to the SAME image production runs use (base-offline for quarantine
    # repos), so the smoke test audits what trials actually launch (#7).
    image = args.image or image_for_repo(repo, PROJECT_ROOT)
    container = f"quarantine-verify-{repo.lower().replace('/', '_')[:40]}"

    q_env = load_quarantine_env(repo, PROJECT_ROOT)
    if not q_env:
        print(f"Error: no quarantine env derived for {repo}", file=sys.stderr)
        return 1
    os.environ.update(q_env)
    print(f"Repo:      {repo}\nImage:     {image}\nContainer: {container}")
    for k, v in sorted(q_env.items()):
        print(f"  {k}={v}")

    cs = ContainerSetup(container_name=container, image_name=image, repo_name=repo)
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    failures = 0
    try:
        # Manual minimal start: docker run with the quarantine env/mounts but
        # WITHOUT the agent init (no claude install needed for a network test).
        cmd = ["docker", "run", "--pull=never", "-d", "--init", "--cap-add=NET_ADMIN",
               "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
               "--name", container]
        cmd += cs._framework.get_quarantine_env_vars()
        cmd += cs._framework.get_quarantine_mounts()
        cmd += [image, "tail", "-f", "/dev/null"]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        cs._ensure_python3()
        r = subprocess.run(["docker", "exec", container, "python3", "-c",
                            cs._get_base_init_script()],
                           capture_output=True, text=True)
        if "fakeroot" not in r.stdout:
            print(f"Warning: base init output unexpected:\n{r.stdout}\n{r.stderr}")

        # lock_network runs the fail-closed verification suite internally
        # (deny-domain probes, verify_fetch_urls, cache_forbid_globs audit).
        cs.lock_network()
        print("lock_network + built-in quarantine verification: PASS")

        print("Positive probes (must stay reachable):")
        for url in ("https://api.anthropic.com", "https://aiplatform.googleapis.com"):
            if not _probe(cs, url, expect_blocked=False):
                failures += 1

        print("Offline switches visible in container env:")
        want = {
            "EVOCLAW_CARGO_OFFLINE": "CARGO_NET_OFFLINE=true",
            "EVOCLAW_GO_OFFLINE": "GOPROXY=off",
            "EVOCLAW_MAVEN_OFFLINE": "MAVEN_ARGS=-o",
            "EVOCLAW_NPM_OFFLINE": "npm_config_offline=true",
            "EVOCLAW_PIP_OFFLINE": "PIP_NO_INDEX=1",
        }
        r = subprocess.run(["docker", "exec", container, "env"],
                           capture_output=True, text=True)
        for flag, expect in want.items():
            if not q_env.get(flag):
                continue
            key = expect.split("=")[0]
            ok = any(line.startswith(key + "=") and expect in line
                     for line in r.stdout.splitlines())
            print(f"  {'PASS' if ok else 'FAIL'}  {expect}")
            if not ok:
                failures += 1
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        failures += 1
    finally:
        if args.keep:
            print(f"Container kept: docker exec -it {container} bash")
        else:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
