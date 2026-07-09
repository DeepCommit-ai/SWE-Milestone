# Adding a New Model to an Existing Agent

This guide covers running a **new model** on one of the four agents EvoClaw
already supports (`claude-code`, `codex`, `gemini-cli`, `openhands`).

> Adding a new **agent framework** (a new adapter) is a different, larger task —
> see the "Bring Your Own Agent" note in the [README](../README.md). This doc is
> only about pointing an existing agent at a different model/endpoint.

In the common case this is **just a trial config + an env var or two** — no code.
You touch code only to (a) whitelist a brand-new endpoint domain or (b) add a
pricing row. Here's the whole process.

## TL;DR

```yaml
# trial_configs/<agent>_<model>.yaml
data_root: /path/to/EvoClaw-data
trial_name: <agent>_<model>
agent: claude-code            # claude-code | codex | gemini-cli | openhands
model: <the endpoint's exact model id>
timeout: 18000
# reasoning_effort: high      # optional: low | medium | high | xhigh | max
# auto_compact_window: 200000 # claude-code only: compact at 200K (omit = no compaction, ~1M); see note
# default_haiku_model: <model># claude-code only: pin all model slots (see Step 5)
```

```bash
export UNIFIED_API_KEY=sk-...                      # the model provider's key
export UNIFIED_BASE_URL=https://api.provider.com   # only if not the agent's default vendor
python scripts/run_all.py --config trial_configs/<agent>_<model>.yaml
```

That's it for any endpoint that speaks the agent's native protocol. The sections
below are the details and the two exceptions (new domain, pricing).

> **`auto_compact_window` (claude-code only).** Sets the native
> `CLAUDE_CODE_AUTO_COMPACT_WINDOW` so claude-code compacts its own context
> instead of running to the endpoint's limit (native agent behaviour, keeps
> parity). It's capped at the model's context window, which is **200K** for a
> third-party id claude-code can't pattern-match (e.g. `glm-5.2`). So only two
> settings are supported:
>
> - **unset** → no compaction (context runs to the endpoint's ~1M ceiling).
> - **`200000`** → compact at 200K. The monitor header's `context=` label
>   reflects the effective window (`context=200K` vs `context=1M`).
>
> Any value above 200K is capped to 200K, so don't bother — `200000` is the max.

---

## Step 1 — Pick the agent and the model id

- **agent** must be one of the four already implemented.
- **model** is the **exact** id the endpoint expects, used verbatim as `--model`
  and in env vars. Examples that ship in `trial_configs/`:
  - `claude-opus-4-8` (Anthropic / Vertex)
  - `accounts/fireworks/models/deepseek-v4-pro` (Fireworks)
  - `kimi-k2.6` (Moonshot coding endpoint)
  - `gemini-3.5-flash` (Vertex)

## Step 2 — Choose how it authenticates

The harness uses **two unified env vars** that each agent maps to its own native
vars. Pick one of three routes:

### Route A — API key + base URL (most third-party endpoints)

```bash
export UNIFIED_API_KEY=sk-...
export UNIFIED_BASE_URL=https://api.fireworks.ai/inference   # the provider's endpoint
```

Use this whenever the endpoint speaks the **agent's native protocol** — e.g.
Fireworks / Kimi / OpenRouter / Z.AI all expose an Anthropic-compatible
`/v1/messages` for `claude-code`. Each agent maps the unified vars like so:

| agent | `UNIFIED_API_KEY` → | `UNIFIED_BASE_URL` → | native protocol |
|---|---|---|---|
| `claude-code` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` | Anthropic `/v1/messages` |
| `codex` | `CODEX_API_KEY` | `OPENAI_BASE_URL` | OpenAI |
| `gemini-cli` | `GEMINI_API_KEY` | `GOOGLE_GEMINI_BASE_URL` | Gemini |
| `openhands` | `LLM_API_KEY` | `LLM_BASE_URL` | LiteLLM (many) |

Omit `UNIFIED_BASE_URL` to use the agent's **default vendor** (e.g. `claude-code`
→ `api.anthropic.com`).

### Route B — Vertex AI via ADC (no key)

For Google Vertex AI, auth is **ADC** (Application Default Credentials), not a
key. Set `vertex_ai: true` and run `run_all.py` normally — do **not** set
`UNIFIED_API_KEY`/`UNIFIED_BASE_URL`. Supported on:

- **`gemini-cli`** → Gemini models on Vertex
- **`claude-code`** → Claude models on Vertex (`CLAUDE_CODE_USE_VERTEX`)

See **[docs/vertex-ai.md](vertex-ai.md)** for the full credential setup and the
worked Opus-4.8 example. (`codex` / `openhands` have no Vertex path here.)

### Route C — the agent's own vendor

If you're running the model the agent natively ships with (e.g. `claude-code`
against a real Anthropic model with an Anthropic key), just set `UNIFIED_API_KEY`
and skip the base URL.

## Step 3 — Whitelist the endpoint domain (only if new)

Agent containers enforce an **iptables outbound whitelist** — every other egress
is blocked (code-hosting sites included, to prevent data leakage). If your
`UNIFIED_BASE_URL` host isn't already in `WHITELISTED_DOMAINS`
(`harness/e2e/container_setup.py`), add it:

```python
WHITELISTED_DOMAINS = [
    ...
    "api.your-provider.com",   # ← add the bare host (no scheme, no path)
]
```

Already present: `api.anthropic.com`, `api.openai.com`, `api.fireworks.ai`,
`api.kimi.com`, `api.moonshot.ai`, `open.bigmodel.cn`, and the Vertex hosts
(`aiplatform.googleapis.com`, `oauth2.googleapis.com`). Package registries
(npm/pip/cargo/maven/go) are already allowed.

## Step 4 — Add pricing (for cost reporting)

`harness/e2e/pricing.py` is the single source of truth for cost. Add a row to
`MODEL_PRICING` (USD per 1M tokens):

```python
"your-model": {"input": 1.50, "output": 9.00, "cache_read": 0.15},
```

- If the model id is **versioned/aliased** (e.g. `your-model-2026-05-01`), also
  add a substring → key row to `_MATCH_ORDER` so the dated id resolves.
- You can **skip** this only if a family match already covers it — e.g. any id
  containing `opus` already resolves to the `claude-opus` row, `sonnet` →
  `claude-sonnet`, etc. (see `_MATCH_ORDER`).
- **Without a match, cost silently falls back to claude-sonnet rates** and is
  wrong. When in doubt, add the row.

> Vertex bills Claude/Gemini at the provider's list price, so the same row works
> for the Vertex and direct-API forms of a model.

## Step 5 — (claude-code only) pin the class-based model slots

Claude Code picks a model **by class** at five decision points
(HAIKU / SONNET / OPUS / SUBAGENT / global default) for background tasks,
fallbacks, and subagent spawns. Left unset, those hit `api.anthropic.com` with
hard-coded defaults (e.g. `claude-haiku-4-5`) — bypassing your endpoint and
billing a separate account. Point all five at your model:

```yaml
default_haiku_model: your-model
```

(Despite the name, this drives all five slots.) **Vertex mode sets this
automatically** to the trial `model`, so background/subagent calls stay on the
one model you enabled on Vertex.

## Step 6 — reasoning_effort (optional)

`reasoning_effort: low | medium | high | xhigh | max`. Support and mapping are
**per agent**:

| agent | accepts | default if unset | notes |
|---|---|---|---|
| `claude-code` | low–max → `--effort` (+ `CLAUDE_CODE_EFFORT_LEVEL`) | model's built-in (Opus: xhigh) | effort rides in `output_config.effort`, verified faithful + honored on 2.1.158 (`max` ≈ 2.6× `low`'s thinking). The old bug **#48051** (high/max → medium) was the pre-`output_config` builds — gone in current claude-code. Unset = the model's built-in default. |
| `codex` | low–max → `model_reasoning_effort` | `xhigh` | |
| `gemini-cli` | normalized to `high` | `high` | only thinking on/off; HIGH is the ceiling |
| `openhands` | passed to LiteLLM; `none`/`off` disables | model default | |

## Step 7 — Create the config and run

```bash
cp trial_config.example.yaml trial_configs/<agent>_<model>.yaml
# edit the fields, then:
python scripts/run_all.py --config trial_configs/<agent>_<model>.yaml
./scripts/monitor.sh <agent>_<model>
```

See [docs/running-trials.md](running-trials.md) for launch/monitor/resume.

---

## Worked example — Opus 4.8 on Vertex AI

Goal: `claude-code` + `claude-opus-4-8`, served from **Google Vertex AI**, max
thinking. This needs **Route B** (Vertex/ADC) — no key.

**1. Confirm the model is reachable on your Vertex project** (it must be enabled
in Model Garden first, and Claude models are region-specific):

```bash
PROJECT=<your-gcp-project>; LOC=global
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$LOC/publishers/anthropic/models/claude-opus-4-8:rawPredict" \
  -d '{"anthropic_version":"vertex-2023-10-16","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
# 200 = reachable · 404 = not enabled / wrong region · 429 = enabled but 0 quota
```

> **Region gotcha:** a freshly-enabled Claude base model often has **0
> per-minute-token quota** in regional endpoints (→ instant 429, "submit a quota
> increase request"), while the **`global`** endpoint works immediately. Start on
> `global`; switch `vertex_location` to a region once its quota is granted.

**2. Config** (`trial_configs/claude-code_opus-4.8.yaml`):

```yaml
data_root: /path/to/EvoClaw-data
trial_name: claude-code_opus-4.8
agent: claude-code
model: claude-opus-4-8
vertex_ai: true
vertex_location: global
default_haiku_model: claude-opus-4-8
reasoning_effort: max        # ⚠️ see #48051 above; unset = built-in xhigh
timeout: 18000
```

**3. Launch** (ADC already set up per docs/vertex-ai.md):

```bash
python scripts/run_all.py --config trial_configs/claude-code_opus-4.8.yaml
```

`run_all.py` sets `EVOCLAW_VERTEX*`; `claude_code.py` then emits
`CLAUDE_CODE_USE_VERTEX=1` + `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION`,
mounts the host ADC read-only, and copies it into the agent user's home — Claude
Code talks to Vertex directly.

---

## Checklist

- [ ] agent is one of the four supported
- [ ] `model` is the endpoint's exact id
- [ ] auth chosen: key + base URL **or** `vertex_ai: true` + ADC
- [ ] endpoint domain in `WHITELISTED_DOMAINS` (if new)
- [ ] pricing row in `pricing.py` (or a family match already covers it)
- [ ] (claude-code) `default_haiku_model` set
- [ ] `reasoning_effort` decided (mind claude-code #48051)
- [ ] config created, launched, monitored

---

**After the trials finish:** surfacing results on the `:5000` dashboard is a
separate pipeline in the sibling `analysis/` repo — see
`analysis/docs/dashboard_migration.md` (migrate → register → refresh → build,
plus the PYTHONPATH pitfall that silently under-reports cost).
