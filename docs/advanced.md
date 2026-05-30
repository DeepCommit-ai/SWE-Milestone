# Advanced Usage

Deeper dives — single-repo / single-milestone debugging, result collection,
config tuning, output layout, and the `flock` ownership model.

For day-to-day "launch a trial and monitor it" flows, see
[`running-trials.md`](./running-trials.md).

---

## Single-Repo `run_e2e`

`run_all.py` is just a thin launcher around `run_e2e`. For one-off debugging
you can call `run_e2e` directly. Same `flock` + `--force` semantics apply.

```bash
# Fresh start (or wipe-and-restart with --force)
python -m harness.e2e.run_e2e \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --image navidrome_navidrome_v0.57.0_v0.58.0/base:latest \
  --srs-root /path/to/EvoClaw-data/navidrome_.../srs \
  --workspace-root /path/to/EvoClaw-data/navidrome_... \
  --agent claude-code --model claude-sonnet-4-6 \
  --timeout 18000 --trial-name my_experiment_001 \
  --force

# Resume an existing trial directly (container must exist)
python -m harness.e2e.run_e2e \
  --resume-trial /path/to/EvoClaw-data/.../e2e_trial/my_experiment_001
```

### CLI Reference

| Argument | Description |
|----------|-------------|
| `--repo-name` | Repository identifier (e.g., `navidrome_navidrome_v0.57.0_v0.58.0`) |
| `--image` | Base Docker image for the agent container |
| `--srs-root` | Path to SRS directory (contains `{milestone_id}/SRS.md` files) |
| `--workspace-root` | Path to workspace with metadata, DAG, and test data |
| `--agent` | Agent framework: `claude-code`, `codex`, `gemini-cli`, `openhands` |
| `--model` | Model identifier (e.g., `claude-sonnet-4-6`) |
| `--timeout` | Max agent runtime in seconds |
| `--reasoning-effort` | Reasoning level: `low`, `medium`, `high`, `xhigh`, `max` |
| `--prompt-version` | Prompt template version (`v1`, `v2`) |
| `--trial-name` | Trial name. Ending in `_NNN` is used as-is; bare names auto-increment. |
| `--force` | Wipe trial dir + remove container + take over the per-trial lock (SIGTERM stale owner, then SIGKILL after 10s) |
| `--remove-container` | Remove container after trial completes (default: keep running) |
| `--skip-testbed-copy` | Skip copying `/testbed` from container after trial |
| `--resume-trial PATH` | Resume from existing trial directory (container must exist) |
| `--no-resume-session` | In resume mode, start a new agent session instead of resuming the previous one |

To clean up all containers from a specific trial:
```bash
docker rm -f $(docker ps -q --filter "name=my_experiment_001")
```

---

## Run a Single Milestone

For testing or debugging a single milestone in isolation:

```bash
python -m harness.e2e.run_milestone \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --srs-path /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0/srs/milestone_001/SRS.md \
  --agent claude-code \
  --model claude-sonnet-4-6
```

---

## Collect Results

### Single repo

```bash
python -m harness.e2e.collect_results \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --trials my_experiment_001 \
  --trial-type e2e
```

### Re-evaluate a snapshot

Re-run evaluation against a previously captured `source_snapshot.tar`:

```bash
python -m harness.e2e.evaluator \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --patch-file /path/to/trial/evaluation/milestone_001/source_snapshot.tar \
  --baseline-classification /path/to/test_results/milestone_001/milestone_001_classification.json \
  --output /path/to/output/evaluation_result.json
```

---

## E2E Config (`e2e_config.yaml`)

The `e2e_config.yaml` (at `harness/e2e/e2e_config.yaml`) controls evaluation
behavior. The default is the EvoClaw Benchmark configuration:

```yaml
dag_unlock:
  early_unblock: true          # Unlock milestones immediately on submission
  ignore_weak_dependencies: true
  strict_threshold:
    fail_to_pass: 1.0          # 100% of fail_to_pass tests must pass
    pass_to_pass: 1.0          # No regressions allowed
    none_to_pass: 1.0

retry_and_timing:
  debounce_seconds: 120        # Wait for tag hash to stabilize
  max_retries: 2               # Re-evaluate if tag changes
  max_no_progress_attempts: 3  # Max recovery attempts without progress
```

See the full [`e2e_config.yaml`](../harness/e2e/e2e_config.yaml) for all
available options.

---

## Trial Output Structure

Every trial produces:

```
{data_root}/{repo_name}/e2e_trial/{trial_name}/
├── trial_metadata.json        # Run configuration
├── orchestrator.log           # Detailed orchestration log
├── agent_stats.json           # Agent statistics (cost, tokens, turns)
├── log/
│   ├── agent_prompt.txt       # Initial prompt sent to agent
│   ├── agent_stdout.txt       # Agent stdout
│   └── agent_stderr.txt       # Agent stderr
└── evaluation/
    ├── summary.json           # Aggregated results across milestones
    └── {milestone_id}/
        ├── source_snapshot.tar
        └── evaluation_result.json
```

Locks live alongside trial dirs but in a sibling hidden subdir:

```
{data_root}/{repo_name}/e2e_trial/.locks/
├── {trial_name}.lock          # fcntl.flock target (size 0)
└── {trial_name}.info          # JSON sidecar with owner pid/started_at/cmdline/host
```

---

## Lock Internals

Each `(workspace_root, trial_name)` pair has an exclusive `fcntl.flock` on
`<workspace>/e2e_trial/.locks/<trial_name>.lock`. Implemented in
[`harness/e2e/trial_lock.py`](../harness/e2e/trial_lock.py).

Properties:

- **Race-free acquire**: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` is atomic.
- **No stale-cleanup needed**: kernel releases the flock the instant the
  holder's fd closes — process exit (any reason: clean, SIGKILL, OOM, segfault)
  releases automatically. No `atexit` hook required.
- **Lock file outside trial dir**: `--force` rmtrees `trial_root` without
  racing the lock.
- **Diagnostic sidecar**: `<trial_name>.info` is a JSON dump of
  `pid / started_at / cmdline / host`, written atomically (tempfile + rename)
  after acquire. Used to format the "owned by …" refusal message.

`--force` flow:

1. Try `LOCK_EX | LOCK_NB` → `BlockingIOError`.
2. Read sidecar → extract `pid`.
3. `SIGTERM`, poll up to 10s for graceful exit (lets the watcher reap docker
   subprocesses for in-flight evaluations).
4. If still alive: `SIGKILL`.
5. Retry `LOCK_NB` (kernel may need a tick after the dead process is reaped).
6. Write fresh sidecar with our own info.

Without `--force`, a busy lock prints the sidecar contents to stderr and
exits 1. No silent overwrite, no race.

---

## Resume Constraint

Container mounts (verified via `docker inspect`):

```
~/.claude/.credentials.json  → /tmp/host-claude-credentials/.credentials.json
~/.local/share/claude        → /tmp/host-claude-share
<trial>/e2e_workspace        → /e2e_workspace
```

`/testbed` (the repo work tree) lives **only** in the container's writable
layer. Consequence: `docker rm` on a trial container destroys all
in-container git history, uncommitted code, and the agent's session cache.

What survives on the host across `docker rm`:

- `evaluation/{milestone_id}/source_snapshot.tar` — per-submitted milestone
- `orchestrator.log`, `agent_stats.json`, `agent_*.txt`
- DAG state, trial metadata

Implications:

- `--resume-trial` exits with a container-not-found error if the container is
  gone (`harness/e2e/resume.py:280-282`); use `--force` (full restart from the
  initial commit) instead.
- For a soft "continue from where we were", keep the container around — even
  stopped is fine (`verify_container_for_resume` auto-starts stopped
  containers).
