#!/usr/bin/env bash
#
# Push locally-built base-offline images to DockerHub (the inverse of
# pull_images.sh). The offline-closure images are produced by
# scripts/build_offline_closure.py as:
#   {repo_full_name}/base-offline:latest
#
# and are published to DockerHub so other machines can reproduce the
# quarantine (anti-cheat) runs without rebuilding the ~1-4GB closure locally:
#   DOCKERHUB_ORG/<short_name>:base-offline-${VERSION}
#
# This matches the tag pull_images.sh expects (hub_offline=
# "${DOCKERHUB_ORG}/${repo}:base-offline-${VERSION}"), closing the
# build → push → pull → run loop.
#
# Version is controlled by --version or EVOCLAW_IMAGE_TAG (default v0.9),
# matching the harness default and pull_images.sh.
#
# Usage:
#   ./scripts/push_images.sh                          # push all 7 repos' base-offline
#   ./scripts/push_images.sh --repo navidrome         # push one repo
#   ./scripts/push_images.sh --repo ripgrep --repo dubbo
#   ./scripts/push_images.sh --dry-run                # show what would be pushed
#
# Requires `docker login` to DOCKERHUB_ORG first.
#
set -euo pipefail

DOCKERHUB_ORG="${DOCKERHUB_ORG:-hyd2apse}"
VERSION="${EVOCLAW_IMAGE_TAG:-v0.9}"
DRY_RUN=false
SELECTED_REPOS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=true; shift ;;
        --repo)     SELECTED_REPOS+=("$2"); shift 2 ;;
        --org)      DOCKERHUB_ORG="$2"; shift 2 ;;
        --version)  VERSION="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--dry-run] [--repo <name>]... [--org <dockerhub_org>] [--version <v>]"
            echo ""
            echo "  Push locally-built <repo_full>/base-offline:latest images to"
            echo "  DOCKERHUB_ORG/<short>:base-offline-<VERSION> (what pull_images.sh expects)."
            echo ""
            echo "  --dry-run       Show what would be pushed without pushing"
            echo "  --repo <name>   Only push this repo (can repeat). Options:"
            echo "                  navidrome, dubbo, ripgrep, go-zero, nushell, element-web, scikit-learn"
            echo "  --org <org>     DockerHub org (default: hyd2apse)"
            echo "  --version <v>   Image tag (default: v0.9; env EVOCLAW_IMAGE_TAG)"
            echo ""
            echo "  Requires 'docker login' first."
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ──────────────────────────────────────────────
# Repository definitions  (short_name -> repo_full, mirrors pull_images.sh)
# ──────────────────────────────────────────────

declare -A REPO_FULL
REPO_FULL[navidrome]="navidrome_navidrome_v0.57.0_v0.58.0"
REPO_FULL[dubbo]="apache_dubbo_dubbo-3.3.3_dubbo-3.3.6"
REPO_FULL[ripgrep]="burntsushi_ripgrep_14.1.1_15.0.0"
REPO_FULL[go-zero]="zeromicro_go-zero_v1.6.0_v1.9.3"
REPO_FULL[nushell]="nushell_nushell_0.106.0_0.108.0"
REPO_FULL[element-web]="element-hq_element-web_v1.11.95_v1.11.97"
REPO_FULL[scikit-learn]="scikit-learn_scikit-learn_1.5.2_1.6.0"

ALL_REPOS=(navidrome dubbo ripgrep scikit-learn go-zero element-web nushell)

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# ──────────────────────────────────────────────
# Resolve repo set
# ──────────────────────────────────────────────

if [[ ${#SELECTED_REPOS[@]} -gt 0 ]]; then
    REPOS=("${SELECTED_REPOS[@]}")
else
    REPOS=("${ALL_REPOS[@]}")
fi

total=0
pushed=0
skipped=0

for repo in "${REPOS[@]}"; do
    repo_full="${REPO_FULL[$repo]:-}"
    if [[ -z "$repo_full" ]]; then
        echo -e "${RED}Unknown repo: $repo${NC} (valid: ${ALL_REPOS[*]})" >&2
        exit 1
    fi
    total=$((total + 1))

    local_latest="${repo_full}/base-offline:latest"
    hub_offline="${DOCKERHUB_ORG}/${repo}:base-offline-${VERSION}"

    echo -e "${GREEN}=== $repo ===${NC}"

    # The local closure image must exist (built by build_offline_closure.py).
    if ! docker image inspect "$local_latest" >/dev/null 2>&1; then
        echo -e "  ${YELLOW}WARN${NC}     local ${local_latest} not found — build it first:"
        echo -e "           python scripts/build_offline_closure.py --repo ${repo_full}"
        skipped=$((skipped + 1))
        continue
    fi

    if $DRY_RUN; then
        echo -e "  ${CYAN}[dry-run]${NC}  tag  ${local_latest}  ->  ${hub_offline}"
        echo -e "  ${CYAN}[dry-run]${NC}  push ${GREEN}${hub_offline}${NC}"
        continue
    fi

    echo -e "  ${YELLOW}Tagging${NC}  ${local_latest}  ->  ${hub_offline}"
    docker tag "$local_latest" "$hub_offline"
    echo -e "  ${YELLOW}Pushing${NC}  ${hub_offline} ..."
    if docker push "$hub_offline"; then
        echo -e "  ${GREEN}Done${NC}     ${hub_offline}"
        pushed=$((pushed + 1))
    else
        echo -e "  ${RED}FAIL${NC}     docker push ${hub_offline} (logged in to ${DOCKERHUB_ORG}?)" >&2
        skipped=$((skipped + 1))
    fi
done

echo ""
if $DRY_RUN; then
    echo -e "${CYAN}[dry-run]${NC} ${total} repo(s) would be pushed to ${DOCKERHUB_ORG} as base-offline-${VERSION}"
else
    echo -e "${GREEN}Pushed ${pushed}/${total}${NC} base-offline image(s) to ${DOCKERHUB_ORG} (tag base-offline-${VERSION}); ${skipped} skipped."
fi
