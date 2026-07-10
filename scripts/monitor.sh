#!/usr/bin/env bash
#
# Monitor SWE-Milestone trial progress across all repos.
#
# Usage:
#   ./scripts/monitor.sh <trial_name>                          # monitor a trial
#   ./scripts/monitor.sh <trial_name> --repos navidrome dubbo  # monitor specific repos
#   ./scripts/monitor.sh <trial_name> --data-root /path/to/data
#
# This script auto-generates a config from trial_config.yaml (or --data-root)
# and runs collect_results.py --multi-repo.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PROJECT_ROOT/.swe-milestone"

# ─────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────
TRIAL_NAMES=()
DATA_ROOT=""
REPOS=()
EXTRA_ARGS=()
DETAIL_REPO=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root)  DATA_ROOT="$2"; shift 2 ;;
        --repos)      shift; while [[ $# -gt 0 ]] && [[ "$1" != --* ]]; do REPOS+=("$1"); shift; done ;;
        --detail)
            # --detail with optional repo argument
            if [[ $# -ge 2 ]] && [[ "$2" != --* ]]; then
                DETAIL_REPO="$2"; shift 2
            else
                DETAIL_REPO="__ALL__"; shift
            fi
            ;;
        --full)       EXTRA_ARGS+=("--full"); shift ;;
        --help|-h)
            echo "Usage: $0 [trial_name ...] [OPTIONS]"
            echo ""
            echo "  trial_name          One or more trial names to monitor"
            echo ""
            echo "Display modes:"
            echo "  (default)           Compact overview — progress, score, status (80 cols)"
            echo "  --detail REPO       Per-milestone breakdown for a repo (substring match)"
            echo "  --full              Full wide table with all columns"
            echo ""
            echo "Filters:"
            echo "  --data-root PATH    Path to SWE-Milestone-data (default: from trial_config.yaml)"
            echo "  --repos REPO ...    Only show these repos"
            echo "  -- ...              Extra args passed to collect_results.py"
            exit 0
            ;;
        --)           shift; EXTRA_ARGS=("$@"); break ;;
        --*)          EXTRA_ARGS+=("$1"); shift ;;
        *)            TRIAL_NAMES+=("$1"); shift ;;
    esac
done

# ─────────────────────────────────────────────
# Resolve data_root (needed before trial auto-detect)
# ─────────────────────────────────────────────
SWE_MILESTONE_CONFIG="$PROJECT_ROOT/trial_config.yaml"
if [[ -z "$DATA_ROOT" ]]; then
    # Try reading from trial_config.yaml
    if [[ -f "$SWE_MILESTONE_CONFIG" ]]; then
        DATA_ROOT=$(python3 -c "
import yaml
with open('$SWE_MILESTONE_CONFIG') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('data_root', ''))
" 2>/dev/null)
    fi
    # Fallback: scan trial_configs/ directory for any config with data_root
    if [[ -z "$DATA_ROOT" && -d "$PROJECT_ROOT/trial_configs" ]]; then
        for cfg_file in "$PROJECT_ROOT"/trial_configs/*.yaml; do
            [[ -f "$cfg_file" ]] || continue
            DATA_ROOT=$(python3 -c "
import yaml
with open('$cfg_file') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('data_root', ''))
" 2>/dev/null)
            [[ -n "$DATA_ROOT" ]] && break
        done
    fi
    if [[ -z "$DATA_ROOT" ]]; then
        echo "Error: --data-root not specified and no data_root found in trial_config.yaml or trial_configs/*.yaml"
        exit 1
    fi
fi

# Resolve to absolute path
DATA_ROOT="$(cd "$DATA_ROOT" 2>/dev/null && pwd)" || { echo "Error: data_root not found: $DATA_ROOT"; exit 1; }

# ─────────────────────────────────────────────
# Auto-detect trial name if not provided
# ─────────────────────────────────────────────
if [[ ${#TRIAL_NAMES[@]} -eq 0 ]]; then
    # Auto-detect: find all trial directories across repos
    FOUND_TRIALS=()
    for repo_dir in "$DATA_ROOT"/*/; do
        # Treat dir as a repo if it has metadata.json (SWE-Milestone-data) or trial dirs (SWE-Milestone-log)
        if [[ ! -f "$repo_dir/metadata.json" ]] \
           && [[ ! -d "$repo_dir/e2e_trial" ]] \
           && [[ ! -d "$repo_dir/mstone_trial" ]]; then
            continue
        fi
        trial_base="$repo_dir/e2e_trial"
        [[ ! -d "$trial_base" ]] && continue
        for trial_dir in "$trial_base"/*/; do
            [[ ! -d "$trial_dir" ]] && continue
            t=$(basename "$trial_dir")
            # Deduplicate
            local_found=false
            for existing in "${FOUND_TRIALS[@]:-}"; do
                [[ "$existing" == "$t" ]] && local_found=true && break
            done
            $local_found || FOUND_TRIALS+=("$t")
        done
    done

    if [[ ${#FOUND_TRIALS[@]} -eq 0 ]]; then
        echo "No trials found in $DATA_ROOT"
        echo ""
        echo "Usage: $0 [trial_name ...] [OPTIONS]"
        exit 1
    elif [[ ${#FOUND_TRIALS[@]} -eq 1 ]]; then
        TRIAL_NAMES=("${FOUND_TRIALS[0]}")
    else
        echo "Multiple trials found. Please specify one or more:"
        echo ""
        for t in "${FOUND_TRIALS[@]}"; do
            echo "  $0 $t"
        done
        echo ""
        echo "  $0 ${FOUND_TRIALS[*]}    # show all"
        exit 1
    fi
fi

# Validate that each trial actually exists in at least one repo
for tn in "${TRIAL_NAMES[@]}"; do
    found=false
    for repo_dir in "$DATA_ROOT"/*/; do
        [[ -d "$repo_dir/e2e_trial/$tn" ]] && found=true && break
    done
    if ! $found; then
        echo "Error: trial '$tn' not found in any repo under $DATA_ROOT" >&2
        echo "" >&2
        echo "Available trials:" >&2
        for repo_dir in "$DATA_ROOT"/*/; do
            [[ ! -d "$repo_dir/e2e_trial" ]] && continue
            for td in "$repo_dir/e2e_trial"/*/; do
                [[ -d "$td" ]] && basename "$td"
            done
        done | sort -u | while read t; do echo "  $t" >&2; done
        exit 1
    fi
done

# ─────────────────────────────────────────────
# Auto-generate config
# ─────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"

# Discover repos: has metadata.json (SWE-Milestone-data) or trial dirs (SWE-Milestone-log)
REPO_ENTRIES=""
for repo_dir in "$DATA_ROOT"/*/; do
    if [[ ! -f "$repo_dir/metadata.json" ]] \
       && [[ ! -d "$repo_dir/e2e_trial" ]] \
       && [[ ! -d "$repo_dir/mstone_trial" ]]; then
        continue
    fi
    repo_name=$(basename "$repo_dir")

    # If --repos specified, filter
    if [[ ${#REPOS[@]} -gt 0 ]]; then
        matched=false
        for r in "${REPOS[@]}"; do
            if [[ "$repo_name" == *"$r"* ]]; then
                matched=true
                break
            fi
        done
        $matched || continue
    fi

    REPO_ENTRIES+="    \"$repo_name\": {\"path\": \"$repo_name\"},
"
done

# Ensure each trial name has _NNN suffix (matching run_all.py convention)
RESOLVED_TRIALS=()
for t in "${TRIAL_NAMES[@]}"; do
    if ! [[ "$t" =~ _[0-9]{3}$ ]]; then
        t="${t}_001"
    fi
    RESOLVED_TRIALS+=("$t")
done

# Build config file name from all trial names
CONFIG_LABEL=$(IFS=_; echo "${RESOLVED_TRIALS[*]}")
CONFIG_FILE="$CONFIG_DIR/${CONFIG_LABEL}_collect.py"

# Build Python list of trial names
TRIAL_LIST_PY=""
for t in "${RESOLVED_TRIALS[@]}"; do
    TRIAL_LIST_PY+="\"$t\", "
done

cat > "$CONFIG_FILE" << PYEOF
# Auto-generated by monitor.sh
DATA_ROOT = "$DATA_ROOT"

WORKSPACE_MAPPING = {
$REPO_ENTRIES}

E2E_TRIAL_NAMES = [${TRIAL_LIST_PY}]
PYEOF

# ─────────────────────────────────────────────
# Run collect_results
# ─────────────────────────────────────────────
cd "$PROJECT_ROOT"

COLLECT_ARGS=(
    python3 -m harness.e2e.collect_results
    --multi-repo
    --config "$CONFIG_FILE"
    --trial-type e2e
)

# Pass --detail if specified
if [[ -n "$DETAIL_REPO" ]]; then
    if [[ "$DETAIL_REPO" == "__ALL__" ]]; then
        COLLECT_ARGS+=("--detail" "")
    else
        COLLECT_ARGS+=("--detail" "$DETAIL_REPO")
    fi
fi

# --repos filtering is already applied in the generated WORKSPACE_MAPPING,
# so we don't pass --config-repos (which requires exact key match).

# Append extra args
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    COLLECT_ARGS+=("${EXTRA_ARGS[@]}")
fi

"${COLLECT_ARGS[@]}"

# ─────────────────────────────────────────────
# Session timeline table
# ─────────────────────────────────────────────
export SWE_MILESTONE_DATA_ROOT="$DATA_ROOT"
export SWE_MILESTONE_TRIAL_NAMES="$(IFS=,; echo "${RESOLVED_TRIALS[*]}")"
python3 << 'TIMELINE_EOF'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

data_root = os.environ.get("SWE_MILESTONE_DATA_ROOT", "")
trial_names_str = os.environ.get("SWE_MILESTONE_TRIAL_NAMES", "")
if not data_root or not trial_names_str:
    sys.exit(0)

trial_names = trial_names_str.split(",")
now = datetime.now(timezone.utc)

# ANSI colors for session ID coloring
COLORS = [
    "\033[36m",  # cyan
    "\033[33m",  # yellow
    "\033[35m",  # magenta
    "\033[32m",  # green
    "\033[34m",  # blue
    "\033[91m",  # bright red
    "\033[96m",  # bright cyan
    "\033[93m",  # bright yellow
]
RESET = "\033[0m"
DIM = "\033[90m"
BOLD = "\033[1m"

rows = []  # (repo_short, session_short, start, end, duration_str, is_running, color_idx)

for trial_name in trial_names:
    # Collect session data from all repos
    session_color_map = {}
    color_counter = 0

    for repo_dir in sorted(Path(data_root).iterdir()):
        if not repo_dir.is_dir():
            continue
        trial_dir = repo_dir / "e2e_trial" / trial_name
        history_file = trial_dir / "log" / "session_history.jsonl"
        metadata_file = trial_dir / "trial_metadata.json"

        if not history_file.exists():
            continue

        # Get repo short name: '{owner}_{project}_{v1}_{v2}' → '{project}'
        parts = repo_dir.name.split("_")
        repo_short = parts[1] if len(parts) >= 2 else parts[0]

        # Read start_time from metadata
        trial_start = None
        if metadata_file.exists():
            try:
                with open(metadata_file) as f:
                    meta = json.load(f)
                trial_start = meta.get("start_time", "")
            except Exception:
                pass

        # Parse session history
        events = []
        with open(history_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        # Build session segments: each exec_start → exec_end pair is a row
        segments = []
        for ev in events:
            ts_str = ev.get("ts", "")
            event_type = ev.get("event", "")
            sid = ev.get("session_id", "")

            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if event_type == "agent_exec_start":
                # Auto-close any prior open segment regardless of session_id.
                # Only one agent runs per trial at a time, so a new start
                # implies the previous one ended — covers both killed-process
                # gaps and session rotation (old sid replaced by new sid
                # without a matching exec_end).
                for seg in reversed(segments):
                    if seg["end"] is None:
                        seg["end"] = ts
                        seg["success"] = None  # unknown — implicitly ended
                        break
                segments.append({"session_id": sid, "start": ts, "end": None, "success": None})
            elif event_type == "agent_exec_end" and segments:
                # Match to last open segment
                for seg in reversed(segments):
                    if seg["end"] is None:
                        seg["end"] = ts
                        seg["success"] = ev.get("success", None)
                        break

        for seg in segments:
            sid = seg["session_id"]
            if sid not in session_color_map:
                session_color_map[sid] = color_counter % len(COLORS)
                color_counter += 1

            start = seg["start"]
            end = seg["end"] or now
            is_running = seg["end"] is None
            duration = end - start
            total_sec = int(duration.total_seconds())
            h, rem = divmod(total_sec, 3600)
            m, s = divmod(rem, 60)
            if h > 0:
                dur_str = f"{h}h{m:02d}m{s:02d}s"
            else:
                dur_str = f"{m}m{s:02d}s"

            rows.append((
                repo_short,
                sid[:8],
                start.strftime("%H:%M:%S"),
                end.strftime("%H:%M:%S") if not is_running else "running…",
                dur_str,
                is_running,
                session_color_map[sid],
            ))

if not rows or len(trial_names) > 1:
    sys.exit(0)

# Also compute total trial elapsed
total_elapsed = ""
for trial_name in trial_names:
    for repo_dir in sorted(Path(data_root).iterdir()):
        meta_file = repo_dir / "e2e_trial" / trial_name / "trial_metadata.json"
        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    meta = json.load(f)
                start_str = meta.get("start_time", "")
                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                elapsed = now - start_dt
                total_sec = int(elapsed.total_seconds())
                h, m, s = total_sec // 3600, (total_sec % 3600) // 60, total_sec % 60
                total_elapsed = f"{h}h{m:02d}m{s:02d}s"
            except Exception:
                pass
            break
    if total_elapsed:
        break

# Group rows by repo
from collections import OrderedDict
grouped = OrderedDict()
for row in rows:
    repo = row[0]
    grouped.setdefault(repo, []).append(row)

# Print table
print()
header_suffix = f"  (elapsed: {total_elapsed})" if total_elapsed else ""
print(f"  {BOLD}Session Timeline{RESET}{header_suffix}")
print()
print(f"  {'Repo':<14} {'Session':<10} {'Start':>10}  {'End':>10}  {'Duration':>10}  Status")
print(f"  {'─'*14} {'─'*10} {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}")

repo_list = list(grouped.keys())
for ri, repo in enumerate(repo_list):
    repo_rows = grouped[repo]
    for i, (_, sid, start, end, dur, is_running, cidx) in enumerate(repo_rows):
        color = COLORS[cidx]
        is_last = (i == len(repo_rows) - 1)
        if is_running:
            status = f"\033[32m● running{RESET}"
        elif is_last:
            # Latest segment ended but worker may still be alive (rate-limit
            # sleep, between-session recovery, or fully finished). Distinct
            # from ✓ done which only applies to segments superseded by a
            # later session start.
            status = f"\033[33m↷ idle{RESET}"
        else:
            status = f"{DIM}✓ done{RESET}"
        if i == 0:
            label = f"{repo:<14}"
        else:
            prefix = "└──" if is_last else "├──"
            label = f"{DIM}{prefix}{RESET}{'':11}"
        print(f"  {label} {color}{sid}{RESET}  {start:>10}  {end:>10}  {dur:>10}  {status}")
    if ri < len(repo_list) - 1:
        print()
print()
TIMELINE_EOF
