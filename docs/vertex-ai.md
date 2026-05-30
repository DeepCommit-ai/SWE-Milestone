# Running on Google Vertex AI

EvoClaw can drive **gemini-cli** against Gemini models hosted on **Google
Vertex AI**. Enabled by a single flag in the trial config; `run_all.py` handles
the rest.

> **Only `gemini-cli` is supported for Vertex.** gemini-cli speaks the native
> Gemini protocol and has built-in Vertex support, so it talks to Vertex
> directly using ADC copied into its container. Other agents (e.g. claude-code)
> speak the Anthropic API and **cannot** talk to a Gemini model on Vertex —
> `vertex_ai: true` with any non-gemini-cli agent errors out.

## Why it's different from other endpoints

Vertex AI does **not** use a static API key. It authenticates with **ADC**
(Application Default Credentials — an OAuth token that **expires hourly**).
gemini-cli refreshes that token itself, so EvoClaw just copies the host's ADC
file into the agent container (read-only) and whitelists the two Google
endpoints it needs (`aiplatform.googleapis.com`, `oauth2.googleapis.com`).

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

```yaml
agent: gemini-cli
model: gemini-3.5-flash      # the Vertex publisher model id
vertex_ai: true              # ← activates Vertex mode (gemini-cli only)
vertex_location: global      # optional, default: global
vertex_project: my-project   # optional, default: the ADC quota project
timeout: 18000
```

No key, no base URL. Launch the normal way:

```bash
python scripts/run_all.py --config trial_configs/gemini-cli_gemini-3.5-flash.yaml
```

When `vertex_ai: true`, `run_all.py` sets `EVOCLAW_VERTEX` + project/location in
the env the workers inherit; the gemini-cli framework
(`harness/e2e/agents/gemini.py`) then:
1. mounts the host ADC read-only and copies it into the agent user's home,
2. writes `~/.gemini/settings.json` with `security.auth.selectedType: vertex-ai`,
3. sets `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`,
4. runs `gemini --model <model> --skip-trust ...` against Vertex directly.

## Available models (example project)

Probe what your project can reach (replace PROJECT):

```bash
PROJECT=my-project; LOC=global
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$LOC/publishers/google/models/gemini-3.5-flash:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"ping"}]}]}'
```

A 200 means the model is reachable; 404 means wrong id/location or no access.

## Caveat: context caching is weak on the global endpoint

gemini-cli relies on Gemini's **implicit** caching (it does not create explicit
`CachedContent`). On the **`global`** endpoint, capacity-aware routing spreads
consecutive requests across data centers, so the per-DC implicit KV cache is
reused inconsistently — measured cache-hit rate ~48% (a controlled back-to-back
test saw 0%), vs ~90% for gemini-cli on AI Studio. Pinning to a fixed *regional*
endpoint normally fixes this, but models that are **global-only** (e.g.
gemini-3.5-flash) can't be pinned. This only inflates **cost**, not scores. We
deliberately do **not** add an explicit-caching layer — that would make the
benchmark measure "gemini-cli + our cache layer" instead of gemini-cli as it
ships, breaking parity with the other agents.

## Operational notes

- Cost reporting: ensure the model has an entry in `harness/e2e/pricing.py`
  (otherwise cost falls back to claude-sonnet rates and is wildly overstated).
- gemini-cli on a large repo: the first container init can be slow; the agent
  installs Node + the CLI on first start.
