# Running Trials

Operational runbook for `run_all.py` + `monitor.sh`. Covers everyday flows:
launching a trial across all repos, tracking progress, recovering when a single
repo stalls, and managing trial IDs (`_NNN` suffixes).

For single-milestone debugging, raw `run_e2e` invocation, result collection,
`e2e_config.yaml`, trial output layout, and lock internals, see
[`advanced.md`](./advanced.md).

---

## Quick Start

```bash
# 1. Host paths — set once, auto-loaded every run (persists across shells)
cp .env .env_private                     # then set SWE_MILESTONE_DATA_ROOT in .env_private

# 2. Trial config
cp trial_config.example.yaml trial_config.yaml
# Edit: trial_name, agent, model  (data_root defaults to ${SWE_MILESTONE_DATA_ROOT})

# 3. API credentials  (skip for Vertex/ADC; can also live in .env_private)
export UNIFIED_API_KEY="sk-..."
export UNIFIED_BASE_URL="https://..."   # optional, for proxy / custom endpoint

# 3. Launch — fire-and-forget (returns immediately)
python scripts/run_all.py --config trial_config.yaml

# 4. Monitor
./scripts/monitor.sh my_experiment
```

`run_all.py` spawns one detached `run_e2e` per repo (each in its own session via
`setsid()`) and exits. **No `nohup` needed.** Each worker writes to
`.evoclaw/<repo>.log`.

> **Resource note:** All 7 repos in parallel = up to **35 Docker containers**
> (1 agent + up to 4 concurrent eval per repo). Use `--repos a b c` to narrow
> the set if you need to stay under that ceiling.

---

## Trial Config

Required fields: `data_root`, `trial_name`. Everything else has defaults. See
[`trial_config.example.yaml`](../trial_config.example.yaml) for the annotated
template.

Two forms for `trial_name`:

| `trial_name:` value | Behavior |
|---|---|
| `my_experiment` (bare) | `run_all.py` resolves to latest `_NNN` (or `_001`); the `--force` / `--new` flags steer this — see matrix below |
| `my_experiment_002` (fixed `_NNN`) | Used as-is; `--force` / `--new` only affect lifecycle, not the name |

> **Third-party endpoints:** When Claude Code talks to a non-Anthropic endpoint
> (Z.AI, DeepSeek, all-hands proxy, …), set `default_haiku_model` to your main
> model name. Claude Code has *five* class-based model slots (HAIKU / SONNET
> / OPUS / SUBAGENT / global default) that each fall back to a hard-coded
> Anthropic default (e.g., `claude-haiku-4-5`) when unset — those requests
> hit `api.anthropic.com` directly, bypassing your `UNIFIED_BASE_URL` and
> billing a separate Anthropic account. `default_haiku_model` (despite the
> name) points all five slots at the same model, keeping every request on
> your proxy.

> **Context compaction (`auto_compact_window`, claude-code only):** by default
> claude-code runs with **no compaction** — context grows to the endpoint's
> ceiling (~1M). Set `auto_compact_window: 200000` in the trial config to make
> it compact at 200K instead (its native `CLAUDE_CODE_AUTO_COMPACT_WINDOW`;
> values above 200K are capped, so `200000` is the only useful setting). The
> monitor header's `context=` label reflects the effective window. Details:
> [`adding-a-model.md`](./adding-a-model.md).

> **Google Vertex AI (gemini-cli / claude-code):** Vertex doesn't use an API
> key — it uses ADC. Set `vertex_ai: true` with `agent: gemini-cli` (Gemini
> ids) or `agent: claude-code` (Claude ids); the ADC is copied into the
> container and the agent talks to Vertex directly (no proxy). You set
> neither `UNIFIED_API_KEY` nor `UNIFIED_BASE_URL`. See [`vertex-ai.md`](./vertex-ai.md).

---

## Resume / Force / New

Same `run_all.py --config <yaml>` invocation handles all three lifecycles. The
behavior depends on three things:

1. Whether `trial_name` ends with `_NNN`
2. Which flag is passed (`--force`, `--new`, or neither)
3. Whether a worker is already alive on the resolved trial (per-repo `flock`)

### Behavior matrix

When `trial_name` is **bare** (e.g., `my_experiment`):

| Flag | Resolved trial | If no worker alive | If worker alive |
|------|----------------|--------------------|-----------------|
| (none) | latest existing `_NNN`, or `_001` if none | resume that one | refuse with `Trial X is owned by PID Y` |
| `--force` | latest existing `_NNN`, or `_001` if none | wipe trial dir + container, restart fresh | SIGTERM old worker, then wipe + restart |
| `--new` | next `_NNN` (`max+1`) | start fresh | start fresh (different `_NNN`, no conflict) |

When `trial_name` is **fixed** (e.g., `my_experiment_002`), it's used as-is;
only the worker-alive logic applies.

### Common commands

```bash
# Initial launch — fresh _001
python scripts/run_all.py --config trial.yaml

# Re-launch — resume _001 if no worker is alive,
# otherwise refuses with full owner info (PID, started_at, cmdline, host).
python scripts/run_all.py --config trial.yaml

# Force takeover of _001 — SIGTERM running worker (SIGKILL after 10s),
# wipe trial dir + container, restart. Same _001 suffix preserved.
python scripts/run_all.py --config trial.yaml --force

# Start a new experiment as _002 (alongside whatever's already running)
python scripts/run_all.py --config trial.yaml --new

# Force-restart only one repo (others untouched)
python scripts/run_all.py --config trial.yaml --force --repos ripgrep

# Resume only specific repos
python scripts/run_all.py --config trial.yaml --repos ripgrep navidrome
```

---

## Monitor Progress

`monitor.sh` has three modes — pick by terminal width / detail you need.

### Compact overview (default)

Fits in 80 cols; shows per-repo Total / Submit / Eval / Score / Resolve / Status.

```bash
./scripts/monitor.sh                                # auto-detect trial
./scripts/monitor.sh my_experiment_001              # specific trial
./scripts/monitor.sh exp_001 exp_002                # compare trials side-by-side
./scripts/monitor.sh exp_001 --repos ripgrep dubbo  # filter
```

Sample:
```
🏃 my_experiment_001 | 6 running, 1 done
  claude-code | claude-opus-4-7 | effort=xhigh | context=1M

  Repo              Total Submit Eval   Score        Resolve  Status
  ──────────────────────────────────────────────────────────────────
  ripgrep              11      8    8   54.9%     18% (2/11)  ● running
  navidrome             9      9    9   33.2%     22% (2/9)   ✓ done
  ...
```

- **Total**: graded milestones (non-graded excluded)
- **Submit**: tagged by agent
- **Eval**: scored by harness
- **Resolve**: milestones meeting strict pass thresholds

### Per-milestone detail

Drill into a repo with F2P / N2P / P2P / Precision / Recall:

```bash
./scripts/monitor.sh --detail ripgrep    # specific repo (substring)
./scripts/monitor.sh --detail            # every repo that has started
```

### Full table

Wide table with Cost / Time / Turns / OutTok columns. Best piped to a file:

```bash
./scripts/monitor.sh --full > results.txt
```

---

## When a Repo Looks Stuck

A repo is "stuck" if `monitor.sh` shows no new submissions for several
consecutive ticks (typically 60+ minutes). The cause varies — agent crashed,
context overflow, API rate-limit loop, TCP wedge (claude-code has no
application-level HTTP timeout), or just a slow milestone — but **the
recovery action is the same for almost all of them**: diagnose, then kill +
resume. Only the diagnostic distinguishes "really wedged" from "still
working slowly".

### Diagnose

```bash
TRIAL=my_experiment_001
REPO=BurntSushi_ripgrep_14.1.1_15.0.0      # full repo dir name
CONT="${REPO}-${TRIAL}"
TRIAL_DIR=/path/to/SWE-Milestone-data/$REPO/e2e_trial/$TRIAL

# 1. Worker alive?  (substring-match the trial; --resume-trial workers don't
#    have --repo-name in cmdline, so grep the trial name not the repo flag)
pgrep -af "harness\.e2e\.run_e2e" | grep "$TRIAL" | grep "$REPO"

# 2. Most reliable signal — when did claude last write to the session jsonl?
#    Every agent action (tool_use, tool_result, assistant turn) writes here,
#    so its mtime is the closest thing to an agent-alive heartbeat.
docker exec "$CONT" sh -c \
  'ls -la /home/fakeroot/.claude/projects/-testbed/*.jsonl' | head

# 3. agent process CPU time (low CPU + long elapsed = idle / wedged)
docker exec "$CONT" sh -c 'ps -ef | grep "\--model" | grep -v grep'

# 4. Orchestrator main thread is silent during long agent runs by design
#    (it blocks on subprocess.run); look for auth / fatal errors here.
tail -30 "$TRIAL_DIR/orchestrator.log"

# 5. (Optional) Peek at what the agent is actually DOING — tail the session
#    jsonl (same path as step 2). Recent records carry assistant thinking,
#    tool_use calls, and tool_results. Reading the last ~15 entries tells
#    you whether "no new submit for 30 min" is (a) productive slow work —
#    agent is reasoning through real code, editing files, running tests,
#    making non-trivial progress — or (b) a semantic loop — agent re-reads
#    the same file, re-states the same plan, declares itself done without
#    creating tags. (a) should be left alone; (b) is effectively stuck and
#    will either self-terminate at 3/3 no-progress or warrants intervention.
```

A jsonl mtime > 30 min while the agent process is alive with barely any CPU
time is the smoking gun for a mechanical wedge. Step 5 catches the
complementary case — mtime and CPU look fine but the agent is spinning on
reasoning without submitting. Both are worth distinguishing before you
decide to kill/resume vs wait.

### Recover (kill + resume)

Two patterns, picked by what's actually wedged. Both preserve the container,
`/testbed` git state, evaluations, and the agent's session jsonl, so
`claude --resume <id>` picks up where it left off.

**Approach B — kill the in-container `claude` only** (default, simpler):

```bash
TRIAL=my_experiment_001
REPO=BurntSushi_ripgrep_14.1.1_15.0.0
docker exec "${REPO}-${TRIAL}" pkill -KILL -f 'claude --model'
```

Use when the host worker is alive (look at `pgrep -af "harness\.e2e\.run_e2e"`)
but `claude` itself is wedged on a hung API call. The host worker's
`subprocess.run(claude --resume ...)` returns non-zero, the harness then
**automatically retries the same session** up to `resume_subprocess_retry_limit`
times (default 3, with `recovery_wait_seconds`=60s between retries) before
falling back to a fresh session. No worker restart needed; flock unaffected;
DAG state preserved in worker memory.

**Approach A — kill worker + claude + relaunch** (full restart):

```bash
TRIAL=my_experiment_001
REPO=BurntSushi_ripgrep_14.1.1_15.0.0
CONT="${REPO}-${TRIAL}"

# 1. Kill host run_e2e first (else its recovery loop will fire a new claude
#    before you can step in)
pkill -KILL -f "harness.e2e.run_e2e.*${REPO}.*${TRIAL}"

# 2. Kill orphan claude inside container (docker exec does NOT propagate
#    signals by default — step 1 leaves the in-container claude alive and
#    still holding the session jsonl)
docker exec "$CONT" pkill -KILL -f 'claude --model'

# 3. Wait for kernel to release the flock and for cleanup to settle
sleep 5

# 4. Re-launch — run_all.py auto-detects metadata.json and uses --resume-trial
python scripts/run_all.py --config trial.yaml --repos <substring>
```

Use when the host worker itself is broken (crashed, deadlocked, or you want a
fresh DAG-state load from disk). Worker restart loses anything cached in
worker memory, but trial state on disk is preserved.

### Fresh-session fallback isn't a failure

If all `resume_subprocess_retry_limit` retries fail, the harness clears the
persistent session id and starts a brand-new agent session against the same
trial dir / container. **This is a recovery, not a loss.** What survives:

- All `git tag`s the agent created → already-submitted milestones still count
- `/testbed` work tree (commits, in-progress files)
- `evaluation/<milestone_id>/` — past evaluation results and snapshots
- DAG state, `trial_metadata.json`

What's lost: claude's conversation history (thinking, tool history) and the
proxy-side prompt cache. The new session reads `TASK_QUEUE.md` from scratch,
sees which milestones are tagged, and continues with what's left. In some
cases the fresh session is actually *better* — if the wedge was triggered by
a specific accumulated context (e.g., a long debugging chain), starting
fresh side-steps the pathological state.

> **Don't reach for `--force` here.** `--force` wipes the trial dir and removes
> the container, throwing away every `git tag` the agent created and the entire
> `/testbed` work tree (since `/testbed` is not bind-mounted to the host —
> see [`advanced.md`](./advanced.md#resume-constraint)). Reserve `--force` for
> "the trial itself is wrong" (bad config / wrong model / corrupted state),
> not for "the agent's network call hung".

> **SWE-Milestone benchmark protocol**: trials are resumed until all milestones are
> submitted and evaluated, unless three consecutive resumes yield no new
> submissions. Reproducibility studies should follow the same setting.

---

## Babysitting Routine (for AI assistants)

When the user asks you to watch a long trial, schedule a recurring monitor
every 30 minutes. Each tick is self-contained — don't keep state across
ticks.

**Each tick**:

1. `./scripts/monitor.sh <trial_name>` and diff per-repo Δsubmit vs prior tick.
2. `pgrep -af "harness\.e2e\.run_e2e" | grep "<trial>" | wc -l` should match
   monitor's "running" count. A worker that vanished without "✓ done" = died.
   Note: `--resume-trial` workers don't carry `--repo-name` in cmdline; grep
   the trial name (which both fresh and resume cmdlines contain).
3. If everything is moving, report deltas in ≤10 lines and stop. If anything
   looks off (repo idle, worker count mismatch, unexpected score drop), dig —
   it's cheap. Catching a wedge at tick 3 saves hours over tick 8.

**Diagnostic**: use the commands in *Diagnose* above. Session jsonl mtime is
the primary signal — every agent action writes to it. (Default paths there
are claude-code's; other agents have analogous session files — see
[`advanced.md`](./advanced.md) for per-agent details.) Recent mtime = agent
is doing something, leave it alone. Otherwise judge the whole picture, not a
checklist.

When mtime is recent but the repo hasn't submitted for a long time, peek
at the jsonl content (*Diagnose* step 5) to tell productive slow work from
a semantic loop. Don't kill a repo whose agent is actively reading new
files, editing, and running tests — that's just slow progress. Kill or let
auto-stop a repo whose agent is re-reading the same spots and re-declaring
itself done.

### Acting

**Default: kill + resume autonomously.** Agent bugs, TCP wedges, session
corruption, naturally-stopped trials — routine, the user can't help,
asking just adds latency. The babysitter exists so the user can ignore
the trial. Two recovery patterns, picked by how the repo failed:

**(a) Wedged** — session jsonl 30+ min stale **and** agent process alive
with CPU barely moving.

Use **Approach B**: `docker exec "$CONT" pkill -KILL -f 'claude --model'`.
The harness retries up to 3 times before falling back to a fresh session,
so a single transient stall recovers automatically and the worker stays up.
Use Approach A (worker restart) only if the host worker itself is sick.

**(b) Stopped** — monitor shows `■ stopped`; orchestrator.log tail reads
`E2E Trial INCOMPLETE · Stopped after 3 consecutive attempts without
progress`. The agent 3× in a row declared itself done (`terminal_reason:
completed`) while the DAG still has unsubmitted milestones.

Auto-resume it: `python scripts/run_all.py --config <yaml> --repos <substring>`.
No `--force` — it's a clean resume (`run_all.py` detects the existing
`trial_metadata.json` and uses `--resume-trial`). The no-progress counter
resets on resume, so the agent gets a fresh 3-attempt budget. Repeat on
every tick the repo is `■ stopped`.

Report what you did in the next tick either way.

**Escalate only when the user's input is actually needed**:

- Auth / 401 in stderr → credentials may need refresh.
- Container died or won't restart → may need `--force` (data loss).
- Same repo wedges again shortly after a resume → pattern, not a one-off.
- Same repo hits `■ stopped` 3+ times in the same session despite
  resumes → model is genuinely incapable; needs a different model /
  prompt decision, not more resumes.
- Infrastructure problems (disk full, docker daemon).
- Novel symptoms you can't reason through.

**Don't**: `--force` for wedges or for stopped trials (it wipes
`/testbed` + git tags — reserve for trial-level config errors); restart
healthy repos because a neighbor wedged; touch trials the user didn't ask
about.

> The benchmark-protocol note below describes the *reproducibility-study*
> rule (stop at 3-consec no-progress). Operational babysitting overrides
> that — keep pushing a trial to completion unless the user says otherwise.

---

## Environment Variables

Unified API:

| Variable | Description |
|---|---|
| `UNIFIED_API_KEY` | API key (required). Mapped to agent-native env var inside the container. |
| `UNIFIED_BASE_URL` | Base URL (optional). For proxies / custom endpoints. |

> **Custom proxy domain:** if `UNIFIED_BASE_URL` points outside the default
> whitelist, add it to `WHITELISTED_DOMAINS` in
> `harness/e2e/container_setup.py` — agent containers block all other
> outbound traffic.

Host paths (set once in `.env_private`, auto-loaded by `run_all.py`; a real
shell-exported var still wins):

| Variable | Description |
|---|---|
| `SWE_MILESTONE_DATA_ROOT` | Where you downloaded SWE-Milestone-data. Trial configs use `data_root: ${SWE_MILESTONE_DATA_ROOT}`. |

> **Quarantine (anti-cheat) is zero-touch.** It auto-applies to any repo that has
> a `quarantine_configs/<repo>.yaml` policy — nothing to pass per run (a `🔒` marker
> shows at launch), and a coverage gate refuses to launch a repo whose policy is
> missing or incomplete. To run an **unprotected baseline**, pass `--unprotected`
> (the only escape hatch — moving the policy file aside trips the gate). Full
> design + how to add a repo: [`quarantine.md`](./quarantine.md).
