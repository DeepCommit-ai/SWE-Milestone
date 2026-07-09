#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DATA_ROOT="${SWE_MILESTONE_DATA_ROOT:-$PROJECT_ROOT/EvoClaw-data}"
LOG_ROOT="${SWE_MILESTONE_LOG_ROOT:-$PROJECT_ROOT/EvoClaw-log}"

usage() {
    cat <<'EOF'
Usage:
  Single repo (migrate from EvoClaw-data to EvoClaw-log):
    migrate_trial.sh <src_dir> <new_name> [trial_type]

  All repos (batch migrate):
    migrate_trial.sh --all <old_name> <new_name> [trial_type]

  Rename in place (no copy, just rename folder + update references):
    migrate_trial.sh --rename <trial_dir> <new_name>

Arguments:
  src_dir      Source trial directory in EvoClaw-data
  trial_dir    Existing trial directory to rename in place
  old_name     Trial folder name to find across all repos (used with --all)
  new_name     New trial name
  trial_type   e2e_trial or mstone_trial (default: e2e_trial)

Examples:
  # Single repo
  ./migrate_trial.sh \
    /data2/gangda/EvoClaw-data/nushell_.../e2e_trial/claude-code_glm-5_001 \
    _claude-code_glm-5_run_001 e2e_trial

  # All repos
  ./migrate_trial.sh --all claude-code_glm-5_001 _claude-code_glm-5_run_001 e2e_trial

  # Rename in place
  ./migrate_trial.sh --rename \
    /data2/gangda/EvoClaw-log/nushell_.../e2e_trial/_old_name \
    _new_name
EOF
    exit 1
}

# ── Escape string for use in sed/grep as a literal ───────────────────────────
escape_regex() {
    printf '%s' "$1" | sed 's/[.[\(*^$+?{|\\]/\\&/g'
}

# ── Scan a directory for likely API key patterns ─────────────────────────────
# Returns 0 if clean, 1 if any pattern matched. Prints offending files to stderr.
check_no_api_keys() {
    local DIR="$1"
    local matches
    matches=$(grep -rEln \
        --include='*.json' --include='*.jsonl' --include='*.yaml' --include='*.yml' \
        --include='*.csv' --include='*.log' --include='*.md' --include='*.txt' \
        -e 'sk-[a-zA-Z0-9]{20,}' \
        -e 'sk-ant-[a-zA-Z0-9_-]{20,}' \
        -e 'AIzaSy[a-zA-Z0-9_-]{20,}' \
        -e 'hf_[a-zA-Z0-9]{30,}' \
        -e '[Bb]earer[[:space:]]+[a-zA-Z0-9_.-]{20,}' \
        -e 'x-api-key["[:space:]:=]+[a-zA-Z0-9_-]{20,}' \
        -e '(ANTHROPIC|OPENAI|GOOGLE|GEMINI|HF|HUGGINGFACE|MISTRAL|GROQ|OPENROUTER|DEEPSEEK|MOONSHOT|ZHIPU|GLM)_API_KEY["[:space:]:=]+[a-zA-Z0-9_-]{10,}' \
        "$DIR" 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
        echo "  ABORT: Potential API key patterns detected in:" >&2
        echo "$matches" | sed 's|^|    |' >&2
        return 1
    fi
    return 0
}

# ── Rename text references inside a directory ────────────────────────────────
rename_refs() {
    local DIR="$1"
    local OLD="$2"
    local NEW="$3"
    local OLD_ESC
    OLD_ESC="$(escape_regex "$OLD")"

    find "$DIR" -type f \( \
        -name '*.json' -o -name '*.yaml' -o -name '*.yml' \
        -o -name '*.csv' -o -name '*.log' -o -name '*.md' \
        -o -name '*.txt' -o -name '*.jsonl' \
    \) -exec grep -Fl "$OLD" {} + 2>/dev/null | while read -r f; do
        sed -i "s|$OLD_ESC|$NEW|g" "$f"
        echo "  Updated: ${f#"$DIR"/}"
    done
}

# ── Core migration function ──────────────────────────────────────────────────
migrate_one() {
    local SRC_DIR="${1%/}"
    local NEW_NAME="$2"
    local TRIAL_TYPE="$3"

    if [[ ! -d "$SRC_DIR" ]]; then
        echo "SKIP: Source does not exist: $SRC_DIR"
        return 1
    fi

    local OLD_NAME
    OLD_NAME="$(basename "$SRC_DIR")"

    # Extract repo name from path: .../<repo_name>/<trial_type>/<trial_name>
    local SRC_PARENT SRC_GRANDPARENT REPO_NAME
    SRC_PARENT="$(dirname "$SRC_DIR")"
    SRC_GRANDPARENT="$(dirname "$SRC_PARENT")"
    REPO_NAME="$(basename "$SRC_GRANDPARENT")"

    if [[ ! -d "$LOG_ROOT/$REPO_NAME" ]]; then
        echo "SKIP: Repo '$REPO_NAME' not found in $LOG_ROOT"
        return 1
    fi

    local DST_DIR="$LOG_ROOT/$REPO_NAME/$TRIAL_TYPE/$NEW_NAME"

    if [[ -d "$DST_DIR" ]]; then
        echo "SKIP: Destination already exists: $DST_DIR"
        return 1
    fi

    echo "--- $REPO_NAME ---"
    echo "  Source: $SRC_DIR"
    echo "  Dest:   $DST_DIR"

    # Step 1: rsync
    mkdir -p "$DST_DIR"
    rsync -a \
        --exclude='testbed/' \
        --exclude='.trial.lock' \
        --exclude='resume_retry_state.json' \
        "$SRC_DIR/" "$DST_DIR/"
    echo "  Copied."

    # Step 1.5: Safety scan — refuse to keep destination if API keys leaked in
    if ! check_no_api_keys "$DST_DIR"; then
        echo "  Cleaning up: rm -rf $DST_DIR" >&2
        rm -rf "$DST_DIR"
        return 1
    fi

    # Step 2: Rename in text files
    if [[ "$OLD_NAME" != "$NEW_NAME" ]]; then
        rename_refs "$DST_DIR" "$OLD_NAME" "$NEW_NAME"
    fi

    echo "  Done."
    echo ""
}

# ── Argument parsing ─────────────────────────────────────────────────────────
[[ $# -lt 2 ]] && usage

if [[ "$1" == "--rename" ]]; then
    # Rename mode: rename folder + update references in place
    [[ $# -lt 3 ]] && usage
    TRIAL_DIR="${2%/}"
    NEW_NAME="$3"

    if [[ ! -d "$TRIAL_DIR" ]]; then
        echo "ERROR: Directory does not exist: $TRIAL_DIR"
        exit 1
    fi

    OLD_NAME="$(basename "$TRIAL_DIR")"
    PARENT_DIR="$(dirname "$TRIAL_DIR")"
    NEW_DIR="$PARENT_DIR/$NEW_NAME"

    if [[ "$OLD_NAME" == "$NEW_NAME" ]]; then
        echo "ERROR: Old and new names are identical: $OLD_NAME"
        exit 1
    fi

    if [[ -d "$NEW_DIR" ]]; then
        echo "ERROR: Destination already exists: $NEW_DIR"
        exit 1
    fi

    echo "=== Rename In Place ==="
    echo "  Directory: $TRIAL_DIR"
    echo "  Old name:  $OLD_NAME"
    echo "  New name:  $NEW_NAME"
    echo "  New path:  $NEW_DIR"
    echo ""

    # Step 0: Safety scan — refuse to rename if API keys are present
    if ! check_no_api_keys "$TRIAL_DIR"; then
        echo "ERROR: Refusing to rename due to potential API keys above." >&2
        exit 1
    fi

    # Step 1: Rename folder
    mv "$TRIAL_DIR" "$NEW_DIR"
    echo "  Folder renamed."

    # Step 2: Update references in text files
    rename_refs "$NEW_DIR" "$OLD_NAME" "$NEW_NAME"

    echo ""
    echo "=== Rename Complete ==="
    echo "  $NEW_DIR"

elif [[ "$1" == "--all" ]]; then
    # Batch mode: migrate across all repos
    [[ $# -lt 3 ]] && usage
    OLD_NAME="$2"
    NEW_NAME="$3"
    TRIAL_TYPE="${4:-e2e_trial}"

    if [[ "$TRIAL_TYPE" != "e2e_trial" && "$TRIAL_TYPE" != "mstone_trial" ]]; then
        echo "ERROR: trial_type must be 'e2e_trial' or 'mstone_trial', got '$TRIAL_TYPE'"
        exit 1
    fi

    echo "=== Batch Migration ==="
    echo "  Old name:   $OLD_NAME"
    echo "  New name:   $NEW_NAME"
    echo "  Trial type: $TRIAL_TYPE"
    echo ""

    success=0
    skipped=0
    for repo_dir in "$DATA_ROOT"/*/; do
        repo_name="$(basename "$repo_dir")"
        [[ "$repo_name" == "assets" || "$repo_name" == "config" ]] && continue

        src="${repo_dir%/}/$TRIAL_TYPE/$OLD_NAME"
        if migrate_one "$src" "$NEW_NAME" "$TRIAL_TYPE"; then
            ((success++)) || true
        else
            ((skipped++)) || true
        fi
    done

    echo "=== Batch Complete: $success migrated, $skipped skipped ==="
else
    # Single mode
    SRC_DIR="$1"
    NEW_NAME="$2"
    TRIAL_TYPE="${3:-e2e_trial}"

    if [[ "$TRIAL_TYPE" != "e2e_trial" && "$TRIAL_TYPE" != "mstone_trial" ]]; then
        echo "ERROR: trial_type must be 'e2e_trial' or 'mstone_trial', got '$TRIAL_TYPE'"
        exit 1
    fi

    echo "=== Single Migration ==="
    migrate_one "$SRC_DIR" "$NEW_NAME" "$TRIAL_TYPE"
    echo "=== Migration Complete ==="
fi
