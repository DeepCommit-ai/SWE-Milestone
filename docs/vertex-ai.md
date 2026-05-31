# Running on Google Vertex AI

EvoClaw can drive **gemini-cli** (Gemini models) and **claude-code** (Claude
models) against **Google Vertex AI**. Enabled by a single flag in the trial
config; `run_all.py` handles the rest.

> **Supported agents: `gemini-cli` and `claude-code`.** Each uses its own
> built-in Vertex support and talks to Vertex directly using ADC copied into its
> container — gemini-cli via the native Gemini protocol, claude-code via
> `CLAUDE_CODE_USE_VERTEX`. The model must match the agent's family (a Gemini id
> for gemini-cli, a Claude id for claude-code); you can't reach a Gemini model
> from claude-code or vice-versa. `vertex_ai: true` with `codex` / `openhands`
> errors out.

## Why it's different from other endpoints

Vertex AI does **not** use a static API key. It authenticates with **ADC**
(Application Default Credentials — an OAuth token that **expires hourly**).
The agent refreshes that token itself (gemini-cli's google-genai SDK /
claude-code's google-auth), so EvoClaw just copies the host's ADC file into the
agent container (read-only) and whitelists the two Google endpoints it needs
(`aiplatform.googleapis.com`, `oauth2.googleapis.com`).

You therefore **do not set `UNIFIED_API_KEY` or `UNIFIED_BASE_URL`** for Vertex
mode — there is no key and no proxy.

> **Security note:** this copies project-level ADC credentials into a `--yolo`
> agent container and opens its egress to Google's API endpoints. It's a
> deliberate, opt-in loosening of the network sandbox; only use it for trusted
> Vertex runs.

## Credentials — there is no key to type

Unlike third-party endpoints (Fireworks, OpenRouter, …) where you `export
UNIFIED_API_KEY=sk-...`, Vertex authenticates with **ADC**, a credential file
created once on the host. EvoClaw reads it automatically.

### One-time setup (per host / per account)

```bash
# Authenticate ADC — opens a browser; sign in with the account that has Vertex
# access. Writes ~/.config/gcloud/application_default_credentials.json.
gcloud auth application-default login

# Point billing/quota at your project (optional if the account has one default)
gcloud auth application-default set-quota-project <YOUR_GCP_PROJECT_ID>
```

After this, every trial reuses it — you do not repeat it per run.

### Verify it's working

```bash
gcloud auth application-default print-access-token   # prints a token → ADC OK
```

### Switching account or project

```bash
gcloud auth application-default login                       # different Google account
gcloud auth application-default set-quota-project <PROJECT>  # different project
```

Or override the project per-trial with `vertex_project:` in the config.

### Headless / CI (no browser): use a service account

```bash
# GCP Console → IAM → Service Accounts → grant "Vertex AI User" → Keys → JSON.
# Place it where ADC looks, or point CLOUDSDK_CONFIG at a dir containing it.
export CLOUDSDK_CONFIG=/path/to/gcloud-config-dir
```

The agent container mounts `${CLOUDSDK_CONFIG:-~/.config/gcloud}` (read-only)
and copies the ADC file to the agent user's home during init.

### Notes

- If your org **disallows API keys** (common), ADC is the only option — exactly
  what this mode is for.
- Newer Gemini models (Gemini 2.5+, 3.x) may be served only in the **`global`**
  location, not regional ones. Check with the curl probe below.

## Trial config

Common to both: `vertex_ai: true`, no key, no base URL. Launch the normal way
(`python scripts/run_all.py --config <file>`).

```yaml
# optional, shared by both agents:
vertex_location: global      # default: global (some models are global-only)
vertex_project: my-project   # default: the ADC quota project
```

### gemini-cli (Gemini models)

```yaml
agent: gemini-cli
model: gemini-3.5-flash      # the Vertex publisher model id
vertex_ai: true
vertex_location: global
timeout: 18000
```

### claude-code (Claude models)

```yaml
agent: claude-code
model: claude-opus-4-8       # the Vertex publisher model id (no @version needed)
vertex_ai: true
vertex_location: global      # Claude models are region-specific; see the quota note below
default_haiku_model: claude-opus-4-8   # keep background/subagent calls on Vertex too
reasoning_effort: max        # honored in current claude-code (verified 2.1.158); see docs/adding-a-model.md
timeout: 18000
```

> **Claude region/quota gotcha:** a freshly-enabled Claude base model often has
> **0 per-minute-token quota** in regional endpoints (a single request → 429,
> "submit a quota increase request"), while the **`global`** endpoint works
> immediately. Start on `global`; switch `vertex_location` to a region once its
> quota is granted. (Gemini models like `gemini-3.5-flash` are `global`-only.)

## How it works

When `vertex_ai: true`, `run_all.py` sets `EVOCLAW_VERTEX` + project/location in
the env the workers inherit, then the agent framework wires its native Vertex
mode:

**gemini-cli** (`harness/e2e/agents/gemini.py`):
1. mounts the host ADC read-only and copies it into the agent user's home,
2. writes `~/.gemini/settings.json` with `security.auth.selectedType: vertex-ai`,
3. sets `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`,
4. runs `gemini --model <model> --skip-trust ...` against Vertex directly.

**claude-code** (`harness/e2e/agents/claude_code.py`):
1. mounts the host ADC read-only and copies it into the agent user's home,
2. sets `CLAUDE_CODE_USE_VERTEX=1` + `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION`
   (+ `GOOGLE_APPLICATION_CREDENTIALS` pointing at the copied ADC),
3. routes all five class-based model slots to the trial `model` (so background /
   subagent calls stay on the one model you enabled on Vertex),
4. runs `claude --model <model> --effort <effort> ...` against Vertex directly.

## Probe what your project can reach

Replace `PROJECT`. The host is `aiplatform.googleapis.com` for `global`, else
`${LOC}-aiplatform.googleapis.com`.

**Gemini** (publisher `google`, `:generateContent`):

```bash
PROJECT=my-project; LOC=global
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$LOC/publishers/google/models/gemini-3.5-flash:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"ping"}]}]}'
```

**Claude** (publisher `anthropic`, `:rawPredict`, Anthropic-format body):

```bash
PROJECT=my-project; LOC=global
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$LOC/publishers/anthropic/models/claude-opus-4-8:rawPredict" \
  -d '{"anthropic_version":"vertex-2023-10-16","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
```

- **200** — reachable.
- **404** — wrong id/location, or the model isn't enabled in Model Garden.
- **429** — enabled but **0 (or insufficient) per-minute quota** in that region;
  use `global` or file a quota-increase request.
- **400 "not servable in region"** — that region doesn't serve the model.

## Cost (gemini-cli): driven by context volume, not caching

gemini-cli relies on Gemini's **implicit** caching (no explicit `CachedContent`),
and it works well on the `global` endpoint for real multi-turn runs — measured
cache hit ~**90%** across a full 7-repo trial (`cached/input`; note Gemini's
`cached` is a subset of `input`). So caching is **not** the cost lever.

What drives cost is **context length per call**: gemini-cli lets the conversation
grow to the ~700k-token window cap and rarely compacts, so each of ~95 turns per
milestone re-sends a ~290k-token prompt; ~96% of spend is prompt tokens (even at
~90% cache). The lever for cheaper runs is context **compaction/summarization**,
not the cache rate. This affects **cost only, not scores**, and we deliberately
do not add a custom caching/optimization layer (it would break benchmark parity
with the other agents, which run with only their own native behavior).

## Operational notes

- Cost reporting: ensure the model has an entry in `harness/e2e/pricing.py`
  (otherwise cost falls back to claude-sonnet rates and is wildly overstated).
  Any id containing `opus` / `sonnet` / `haiku` already resolves via a family
  match, so most Claude ids are covered without a new row.
- First container init can be slow: gemini-cli installs Node + the CLI; claude-code
  installs the standalone `claude` binary. Subsequent resumes reuse the container.
- claude-code on Vertex: watch for **429** during a real trial — the `global`
  endpoint has finite per-minute token quota too. A 429 makes the worker exit
  with no submission (see the resume/rate-limit note in the README), so request a
  quota bump if it recurs rather than just resuming into the same limit.
