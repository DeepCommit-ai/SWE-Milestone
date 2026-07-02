# Migrating Trial Results onto the Dashboard

How to take finished trial results and surface them on the `:5000` dashboard —
the step **after** [`adding-a-model.md`](adding-a-model.md) / [`running-trials.md`](running-trials.md).

This touches **two repos**:

| Repo | Role |
|------|------|
| `EvoClaw/` (this repo) | Harness, `migrate_trial.sh`, `pricing.py`, `harness/e2e/log_parser/` (produces `agent_stats.json`) |
| `analysis/` (sibling repo) | Dashboard: `plotters/common.py`, `visualization/dashboard/` (theme, `build_data.py`, Vite bundle), `refresh_data.py` |

Trial data itself lives outside both, symlinked in:
`EvoClaw-data/` → raw trials (from a run), `EvoClaw-log/` → curated/published trials (what the dashboard reads).

---

## The pipeline (4 steps)

```bash
# ── Step 1: migrate the trial(s) from EvoClaw-data → EvoClaw-log ──────────────
#   rsync copy (source is NOT mutated); excludes testbed/, .trial.lock;
#   scans for leaked API keys; SKIPs if destination already exists.
bash EvoClaw/scripts/migrate_trial.sh --all \
     <old_trial_folder_name> \
     _<agent>_<model>_run_<NNN> \
     e2e_trial

# ── Step 2: register the model in the dashboard (analysis repo) ───────────────
#   Only if the (agent, model) pair is new. See "Naming" below.
#   - analysis/plotters/common.py : AGENT_MODEL_DISPLAY + AGENT_MODEL_ORDER
#   - analysis/visualization/dashboard/src/theme.ts :
#       MODEL_COLOR_OVERRIDES + AGENT_MODEL_ORDER + the name→{agent,model} map

# ── Step 3: regenerate the data ──────────────────────────────────────────────
#   ⚠️ PYTHONPATH MUST point at THIS workspace's EvoClaw (see Pitfall #1).
PYTHONPATH=/ABS/PATH/TO/EvoClaw-Bench/EvoClaw \
  python analysis/refresh_data.py \
    --skip evoclaw_pull \
    --log-root /data2/gangda/EvoClaw-log \
    --data-root /data2/gangda/EvoClaw-data

# ── Step 4: rebuild the bundle (data is baked into the Vite bundle) ───────────
cd analysis/visualization/dashboard && npm run build
```

Flask serves the built `static/dashboard_dist/` at `:5000`; a browser refresh
(hard-refresh, `Ctrl+Shift+R`) picks up the new bundle hash. `refresh_data.py`
internally runs `extract_e2e_csv → … → build_data.py`; the `evoclaw_pull` step
is skipped so it does not `git pull` the sibling EvoClaw repo.

---

## Naming conventions

- **Log folder:** `_<agent>_<model>_run_<NNN>` — leading `_`, trailing `_run_NNN`
  (e.g. `_claude-code_glm-5.2_run_002`). `migrate_trial.sh` rewrites the trial
  name references inside the copied files but does **not** touch the `model`
  field (see Pitfall #4).
- **Display name:** resolved by `agent_model_display(agent, model)` in
  `common.py`. Unlisted pairs fall back to a prettified slug (`glm-5.2` →
  `CC GLM-5.2`), but any suffixed variant (`glm-5.2-1m` → `CC GLM-5.2-1m`, note
  the missing space) needs an explicit `AGENT_MODEL_DISPLAY` entry to render
  cleanly (`CC GLM-5.2 1M`).
- **The dashboard keys off `agent_stats.json`'s top-level `model` +
  `agent_framework`** — NOT `trial_metadata.json`. `extract_e2e_csv.py:407`
  reads `agent_stats["model"]`.

### 200K vs 1M (compaction) variants — how to show two of the same model

To surface two variants of one model as distinct dashboard series, give one
variant's copy a suffixed top-level `model` in `agent_stats.json` (e.g.
`glm-5.2` → `glm-5.2-1m`) — but **keep the `modelUsage` key as the original**
(`glm-5.2`), because cost recomputation looks pricing up by that key
(`recalculate_cost_from_model_usage`); changing it makes pricing lookup miss.
`sed '0,/"model": "glm-5.2"/s//"model": "glm-5.2-1m"/'` edits only the first
(top-level) occurrence — it appears at line ~4, before `modelUsage` /
`milestone_stats`.

---

## Cost semantics (two different numbers)

| Where | Number | Meaning |
|-------|--------|---------|
| Dashboard `trials[].total_cost_usd` | `recalculate_cost_from_model_usage(modelUsage)` — full, real spend | **This is the authoritative cost.** For `_claude-code_*` trials the agent's self-reported price is recomputed at canonical pricing (`build_data.py:95-98`). |
| `milestone_results.csv` `m_cost_usd` sum | per-milestone attribution | Lower — omits seed/overhead not attributed to a scored milestone. An analysis convenience, **not** the real spend. Do not quote it as total cost. |

For a healthy trial `recalc(modelUsage) == summary.total_cost_usd`.

---

## Pitfalls (learned the hard way)

### 1. `PYTHONPATH` must point at THIS workspace's `EvoClaw` — else cost is silently under-reported

`refresh_data.py` / `build_data.py` do `from harness.e2e.collect_results import
recalculate_cost_from_model_usage`. If you run with `PYTHONPATH=.` (the
`EvoClaw-Bench` root) instead of `PYTHONPATH=.../EvoClaw`, the import resolves
against a **different** `EvoClaw-Bench/harness` (symlinked to an *older* copy
under `/data2/.../agent-workspace/…`), whose `pricing.py` is stale.

The failure is **silent**: the import "succeeds", `recalc` returns a value —
just the *wrong* value (old prices). One bad refresh rewrites the whole
`evoclaw-data.json`, so **every `claude-code` model's cost gets under-reported**
at once. Observed: GLM-5.2 200K showed ~$19.04/repo avg instead of the correct
~$25.12 (ripgrep $7.76 vs $10.23).

**Diagnose:** compare `recalculate_cost_from_model_usage.__module__`'s
`sys.modules[...].__file__` under both PYTHONPATHs — it should point inside your
current workspace's `EvoClaw`, not `/data2/.../agent-workspace/`.

**Fix:** always
`PYTHONPATH=/ABS/PATH/TO/EvoClaw-Bench/EvoClaw python analysis/refresh_data.py …`.

Note: `build_data.py`'s `_LOG_ROOT = ANALYSIS_ROOT / "EvoClaw-log"` is a
hard-coded symlink (it does NOT read `EVOCLAW_LOG_ROOT`), so the *log* is always
found — only the *harness/pricing* source is affected by PYTHONPATH.

### 2. `total_turns = 0` when the container was already gone at extraction time

Symptom: a trial shows `total_turns = 0` and empty `milestone_stats`, yet
`duration_ms`, `total_cost_usd`, and `all_tool_calls` are all populated
(observed on `deepseek-v4-pro` and `qwen3.6-27b`, all 7 repos).

Root cause: `total_turns` is summed from per-milestone turn counts, and
milestone assignment needs `get_milestone_times()` — a `docker exec … git
for-each-ref refs/tags/agent-impl-*` against the **live** container
(`base.py`). If the container is already stopped at stats-extraction time it
returns `{}`, so no usage unit is attributed, `milestone_stats` is empty, and
the old `total_turns = … if milestone_stats else 0` yielded **0** — even though
`usage_units` (the real turns) were parsed and stored. `total_turns ==
len(usage_units)` for healthy trials (verified: glm-5.2 1359 == 1359).

**Fixed in two places:**
- `harness/e2e/log_parser/base.py` — `total_turns` now falls back to
  `len(native_usage_units)` when `milestone_stats` is empty (prevents this at
  the source for future runs).
- `analysis/.../build_data.py` `_trial_summary_from_log` — `turns` falls back
  to `len(stats["usage_units"])` when `summary.total_turns` is 0 (recovers
  already-extracted trials without re-running the harness; `milestone_stats`
  itself cannot be rebuilt exactly, as it needs container-only timestamps, but
  the turns total does not depend on milestone boundaries).

### 3. Don't confuse the two cost numbers

See "Cost semantics". The dashboard number is `recalc(modelUsage)`; the CSV
per-milestone sum is lower and is not the real spend.

### 4. `migrate_trial.sh` does not change `model`

It rsyncs and rewrites the trial-name references, nothing else. Suffix edits for
variants (Pitfall-adjacent: 200K/1M) must be applied to the migrated copy's
`agent_stats.json` **after** migration.

---

## Pre-publish verification checklist

Before `npm run build`, sanity-check the regenerated `evoclaw-data.json`:

- [ ] New `(agent, model)` renders with the intended display name (not a raw slug).
- [ ] `total_cost_usd` per repo ≈ each trial's `recalculate_cost_from_model_usage(modelUsage)` (spot-check one).
- [ ] `total_turns > 0` **and** `milestone_stats` non-empty for every migrated trial — if turns is 0 but `tool_calls`/`cost` are fine, you hit Pitfall #2.
- [ ] For variant pairs, both series appear with distinct `model` ids and the `modelUsage` key is unchanged (cost still correct).
- [ ] The refresh was run with `PYTHONPATH=.../EvoClaw` (Pitfall #1).
