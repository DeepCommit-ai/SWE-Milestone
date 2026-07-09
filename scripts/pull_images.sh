#!/usr/bin/env bash
#
# Pull benchmark images from DockerHub and retag them to the local scheme.
#
#   hub:    <org>/swe-milestone__<repo_full>__<milestone>:<version>
#   local:  swe-milestone/<repo_full>__<milestone>:<version>
#
# ALL naming knowledge lives in harness/e2e/image_version.py (single
# authority); this script only executes the plan it emits. The image
# inventory lives in manifests/images-<version>.tsv.
#
# Failure semantics: per-image WARN + continue; summary at the end;
# non-zero exit if anything failed (fail-closed, but you learn the full
# missing list in one run and keep the successful pulls).
#
# NOTE: docker login first — anonymous DockerHub pulls are rate-limited and
# a full run pulls ~115 images.
#
# Usage:
#   ./scripts/pull_images.sh                      # everything in the manifest
#   ./scripts/pull_images.sh --repo navidrome     # one repo (repeatable)
#   ./scripts/pull_images.sh --dry-run            # print the plan, do nothing
#   ./scripts/pull_images.sh --version v1.0 --org hyd2apse
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
DRY_RUN=false
PLAN_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=true; shift ;;
        --repo)     PLAN_ARGS+=(--repo "$2"); shift 2 ;;
        --org)      PLAN_ARGS+=(--org "$2"); shift 2 ;;
        --version)  PLAN_ARGS+=(--version "$2"); shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--dry-run] [--repo <short>]... [--org <org>] [--version <v>]"
            echo "Names come from harness/e2e/image_version.py + manifests/images-<v>.tsv."
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

PLAN="$(cd "$ROOT" && python3 -m harness.e2e.image_version pull-plan ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"})" || exit 2

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
total=0; ok=0; failed=()

while IFS=$'\t' read -r hub local; do
    [[ -z "$hub" ]] && continue
    total=$((total + 1))
    if $DRY_RUN; then
        echo -e "  ${CYAN}[dry-run]${NC} pull ${GREEN}${hub}${NC} -> ${local}"
        continue
    fi
    echo -e "  ${YELLOW}Pulling${NC} ${hub}"
    if docker pull "$hub" && docker tag "$hub" "$local"; then
        echo -e "  ${GREEN}Done${NC}    ${local}"
        ok=$((ok + 1))
    else
        echo -e "  ${YELLOW}WARN${NC}    failed: ${hub} (continuing)"
        failed+=("$hub")
    fi
done <<< "$PLAN"

echo ""
echo "──────────────────────────────────────────────"
if $DRY_RUN; then
    echo -e "${CYAN}DRY RUN:${NC} ${total} images would be pulled and retagged."
    exit 0
fi
echo -e "${GREEN}Pulled ${ok}/${total}.${NC}"
if [[ ${#failed[@]} -gt 0 ]]; then
    echo -e "${RED}FAILED ${#failed[@]}:${NC}"
    printf '  %s\n' "${failed[@]}"
    exit 1
fi
