#!/usr/bin/env bash
#
# Pull Docker images from DockerHub and retag them for the E2E harness.
#
# The evaluator expects images named:
#   {repo_full_name}/{milestone_id}:${VERSION}     (VERSION default: v0.9)
#
# DockerHub images are stored as:
#   DOCKERHUB_ORG/<short_name>:<milestone_id>-${VERSION}
#
# Version is controlled by --version or EVOCLAW_IMAGE_TAG (default v0.9),
# matching the harness default in evaluator.py / run_milestone.py / run_all.py.
#
# This script pulls and retags automatically.
#
# Usage:
#   ./scripts/pull_images.sh                         # pull all repos
#   ./scripts/pull_images.sh --repo navidrome        # pull one repo
#   ./scripts/pull_images.sh --repo navidrome --repo dubbo
#   ./scripts/pull_images.sh --dry-run               # show what would be pulled
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
            echo "Usage: $0 [--dry-run] [--repo <name>]... [--org <dockerhub_org>]"
            echo ""
            echo "  --dry-run       Show what would be pulled without pulling"
            echo "  --repo <name>   Only pull this repo (can repeat). Options:"
            echo "                  navidrome, dubbo, ripgrep, go-zero, nushell, element-web, scikit-learn"
            echo "  --org <org>     DockerHub org (default: hyd2apse)"
            echo "  --version <v>   Benchmark data version tag (default: v0.9; env EVOCLAW_IMAGE_TAG)"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ──────────────────────────────────────────────
# Repository definitions
# ──────────────────────────────────────────────

declare -A REPO_FULL    # Full repo name used in image paths
declare -A REPO_MIDS    # Space-separated milestone IDs

REPO_FULL[navidrome]="navidrome_navidrome_v0.57.0_v0.58.0"
REPO_MIDS[navidrome]="milestone_001 milestone_002 milestone_003_sub-01 milestone_003_sub-02 milestone_003_sub-03 milestone_003_sub-04 milestone_004 milestone_006 milestone_007"

REPO_FULL[dubbo]="apache_dubbo_dubbo-3.3.3_dubbo-3.3.6"
REPO_MIDS[dubbo]="m001.1 m001.2 m003.1 m003.2 m003.3 m004 m006 m011 m016.1 m017 m018 m019 m025"

REPO_FULL[ripgrep]="burntsushi_ripgrep_14.1.1_15.0.0"
REPO_MIDS[ripgrep]="milestone_seed_119407d_1_sub-01 milestone_seed_119407d_1_sub-02 milestone_seed_292bc54_1 milestone_seed_5f5da48_1_sub-01 milestone_seed_5f5da48_1_sub-02 milestone_seed_b610d1c_1 milestone_seed_2924d0c_1 milestone_seed_8c6595c_1 milestone_seed_a6e0be3_1_sub-01 milestone_seed_a6e0be3_1_sub-02 maintenance_style_1 maintenance_fixes_1_sub-01 maintenance_fixes_1_sub-02"

REPO_FULL[go-zero]="zeromicro_go-zero_v1.6.0_v1.9.3"
REPO_MIDS[go-zero]="m001 m003 m004 m005 m007.1 m007.2 m008 m009 m010 m013 m014 m017 m018 m019 m020 m021 m022 m023 m024 m025 m026 m027 m028"

REPO_FULL[nushell]="nushell_nushell_0.106.0_0.108.0"
REPO_MIDS[nushell]="milestone_g01_48bca0a milestone_g02_da9615f milestone_g02_a647707 milestone_g04_1ddae02 milestone_g04_ca0e961 milestone_g05_0b8531e milestone_g05_be6e868 milestone_m02_parser milestone_m08_docs milestone_core_development.1 milestone_core_development.2 milestone_core_development.3 milestone_core_development.4"

REPO_FULL[element-web]="element-hq_element-web_v1.11.95_v1.11.97"
REPO_MIDS[element-web]="milestone_seed_7ff1fd2_1 milestone_seed_e9a3625_1_sub-01 milestone_seed_e9a3625_1_sub-02 milestone_seed_e9a3625_1_sub-03 milestone_seed_be3778b_1 milestone_seed_56c7fc1_1 milestone_seed_fba5938_1 milestone_seed_aa99601_1 milestone_seed_f59af37_1 milestone_seed_8bb4d44_1 milestone_seed_e662c19_1 milestone_seed_3762d40_1 milestone_seed_599112e_1 maintenance_ui_ux feature_enhancements milestone_seed_3f47487_1 maintenance_bug_fixes milestone_seed_05df321_1"

REPO_FULL[scikit-learn]="scikit-learn_scikit-learn_1.5.2_1.6.0"
REPO_MIDS[scikit-learn]="m01 m03 m04 m06 m11 m12.1 m12.2 m12.3 m12.4 m12.5 m13 m17"

ALL_REPOS=(navidrome dubbo ripgrep scikit-learn go-zero element-web nushell)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

pull_and_retag() {
    local hub_image="$1"
    local local_image="$2"

    if $DRY_RUN; then
        echo -e "  ${CYAN}[dry-run]${NC}  pull ${GREEN}${hub_image}${NC}  ->  ${local_image}"
    else
        echo -e "  ${YELLOW}Pulling${NC}  ${hub_image} ..."
        docker pull "$hub_image"
        echo -e "  ${YELLOW}Tagging${NC}  ${hub_image}  ->  ${local_image}"
        docker tag "$hub_image" "$local_image"
        echo -e "  ${GREEN}Done${NC}     ${local_image}"
    fi
}

# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

if [[ ${#SELECTED_REPOS[@]} -gt 0 ]]; then
    REPOS_TO_PROCESS=("${SELECTED_REPOS[@]}")
else
    REPOS_TO_PROCESS=("${ALL_REPOS[@]}")
fi

total_images=0

for repo in "${REPOS_TO_PROCESS[@]}"; do
    if [[ -z "${REPO_FULL[$repo]+x}" ]]; then
        echo -e "${RED}Unknown repo: $repo${NC}"
        echo "Available: ${ALL_REPOS[*]}"
        exit 1
    fi

    repo_full="${REPO_FULL[$repo]}"

    echo ""
    echo -e "${GREEN}=== $repo ===${NC}"
    echo -e "  Local prefix: ${repo_full}/"

    # Base image
    pull_and_retag \
        "${DOCKERHUB_ORG}/${repo}:base-${VERSION}" \
        "${repo_full}/base:${VERSION}"
    total_images=$((total_images + 1))

    # Milestone images
    for mid in ${REPO_MIDS[$repo]}; do
        pull_and_retag \
            "${DOCKERHUB_ORG}/${repo}:${mid}-${VERSION}" \
            "${repo_full}/${mid}:${VERSION}"
        total_images=$((total_images + 1))
    done
done

echo ""
echo "──────────────────────────────────────────────"
if $DRY_RUN; then
    echo -e "${CYAN}DRY RUN complete.${NC} ${total_images} images would be pulled and retagged."
    echo "Run without --dry-run to actually pull."
else
    echo -e "${GREEN}Complete.${NC} ${total_images} images pulled and retagged."
fi
