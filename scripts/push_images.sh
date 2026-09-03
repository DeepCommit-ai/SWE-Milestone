#!/usr/bin/env bash
#
# Push locally-built benchmark images to DockerHub (inverse of pull_images.sh).
# Scope: EVERYTHING in the manifest (base-offline is no longer special).
#
#   local:  swe-milestone/<repo_full>__<milestone>:<version>
#   hub:    <org>/swe-milestone__<repo_full>__<milestone>:<version>
#
# Naming authority: harness/e2e/image_version.py; inventory:
# manifests/images-<version>.tsv. Requires `docker login` first.
# Missing local image => WARN + skip; summary; non-zero exit on any skip/fail.
#
# Usage:
#   ./scripts/push_images.sh [--dry-run] [--repo <short>]... [--org O] [--version V]
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
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

PLAN="$(cd "$ROOT" && python3 -m harness.e2e.image_version push-plan ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"})" || exit 2

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
total=0; ok=0; failed=()

while IFS=$'\t' read -r local hub; do
    [[ -z "$local" ]] && continue
    total=$((total + 1))
    if $DRY_RUN; then
        echo -e "  ${CYAN}[dry-run]${NC} push ${local} -> ${GREEN}${hub}${NC}"
        continue
    fi
    if ! docker image inspect "$local" >/dev/null 2>&1; then
        echo -e "  ${YELLOW}WARN${NC}    missing local ${local} (skipping)"
        failed+=("$local (missing)")
        continue
    fi
    echo -e "  ${YELLOW}Pushing${NC} ${hub}"
    if docker tag "$local" "$hub" && docker push "$hub"; then
        echo -e "  ${GREEN}Done${NC}    ${hub}"
        ok=$((ok + 1))
    else
        echo -e "  ${YELLOW}WARN${NC}    push failed: ${hub} (logged in?)"
        failed+=("$hub (push failed)")
    fi
done <<< "$PLAN"

echo ""
echo "──────────────────────────────────────────────"
if $DRY_RUN; then
    echo -e "${CYAN}DRY RUN:${NC} ${total} images would be pushed."
    exit 0
fi
echo -e "${GREEN}Pushed ${ok}/${total}.${NC}"
if [[ ${#failed[@]} -gt 0 ]]; then
    echo -e "${RED}FAILED/SKIPPED ${#failed[@]}:${NC}"
    printf '  %s\n' "${failed[@]}"
    exit 1
fi
