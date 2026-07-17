#!/usr/bin/env bash
#
# Align the local SWE-Milestone-data checkout with a benchmark version tag.
#
# The data repo carries the same vX.Y tags as the Docker images
# (docs/versioning.md); harness/e2e/data_version.py verifies the checkout at
# trial launch. This script is the remediation it points at.
#
# Default: fetch tags and ALIGN the checkout to the version tag (detached
# HEAD). Trial state is safe — a dirty tree refuses the checkout, and live
# trial data lives in gitignored paths a checkout never touches. Use
# --report-only to just show where HEAD stands.
#
# Usage:
#   ./scripts/pull_data.sh                              # align to the pinned version
#   ./scripts/pull_data.sh --report-only                # inspect, change nothing
#   ./scripts/pull_data.sh --data-root /path --version v1.0
#   SWE_MILESTONE_DATA_ROOT=... ./scripts/pull_data.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
DATA_ROOT="${SWE_MILESTONE_DATA_ROOT:-}"
VERSION=""
CHECKOUT=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root)   DATA_ROOT="$2"; shift 2 ;;
        --version)     VERSION="$2"; shift 2 ;;
        --report-only) CHECKOUT=false; shift ;;
        --checkout)    CHECKOUT=true; shift ;;  # legacy alias of the default
        --help|-h)
            echo "Usage: $0 [--data-root PATH] [--version vX.Y] [--report-only]"
            echo "Default: align the checkout to the version tag. Default data root:"
            echo "\$SWE_MILESTONE_DATA_ROOT. Default version: SWE_MILESTONE_IMAGE_TAG"
            echo "or manifests/BENCHMARK_VERSION."
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    VERSION="$(cd "$ROOT" && python3 -c '
from harness.e2e.data_version import expected_benchmark_version
print(expected_benchmark_version()[0])')" || exit 2
fi

if [[ -z "$DATA_ROOT" ]]; then
    echo "Error: no data root. Pass --data-root or set SWE_MILESTONE_DATA_ROOT" \
         "(see .env_private / docs/setup.md)." >&2
    exit 2
fi
if ! git -C "$DATA_ROOT" rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: $DATA_ROOT is not a git checkout. Clone it first:" >&2
    echo "  git lfs install && git clone https://huggingface.co/datasets/DeepCommit-ai/SWE-Milestone-data" >&2
    exit 2
fi

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

echo "Fetching tags from origin..."
git -C "$DATA_ROOT" fetch origin --tags || echo -e "${YELLOW}WARN${NC} fetch failed (offline?); checking local refs only"

HEAD_SHA="$(git -C "$DATA_ROOT" rev-parse HEAD)"
TAG_SHA="$(git -C "$DATA_ROOT" rev-parse "refs/tags/${VERSION}^{commit}" 2>/dev/null)" || {
    echo -e "${RED}Tag ${VERSION} does not exist in the data repo.${NC}"
    echo "Publish it from the release checkout: git tag ${VERSION} && git push origin ${VERSION}"
    exit 1
}

if [[ "$HEAD_SHA" == "$TAG_SHA" ]]; then
    echo -e "${GREEN}OK${NC} data checkout is at ${VERSION} (${HEAD_SHA:0:12})"
    exit 0
fi

echo -e "${YELLOW}HEAD ${HEAD_SHA:0:12} != ${VERSION} (${TAG_SHA:0:12})${NC}"
if $CHECKOUT; then
    # Refuse to move a dirty tree — trial state must never be clobbered.
    if [[ -n "$(git -C "$DATA_ROOT" status --porcelain)" ]]; then
        echo -e "${RED}Refusing checkout: the data tree has local changes.${NC}"
        git -C "$DATA_ROOT" status --short | head -20
        exit 1
    fi
    git -C "$DATA_ROOT" checkout --detach "refs/tags/${VERSION}" || exit 1
    echo -e "${GREEN}OK${NC} checked out ${VERSION} (detached HEAD)"
else
    echo "Report-only mode: re-run without --report-only to align to ${VERSION}."
    exit 1
fi
