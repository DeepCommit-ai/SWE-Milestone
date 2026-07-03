<p align="center">
  <img src="assets/banner.png" width="720" alt="EvoClaw Banner" />
</p>

<p align="center">
  <b>A Continuous Task Evaluation Playground for AI Harness</b>
</p>

<p align="center">
  <a href="https://evo-claw.com"><img src="https://img.shields.io/badge/Website-evo--claw.com-blue.svg" alt="Website" /></a>
  <a href="https://arxiv.org/abs/2603.13428"><img src="https://img.shields.io/badge/arXiv-2603.13428-b31b1b.svg" alt="arXiv" /></a>
  <a href="https://huggingface.co/datasets/EvoClaw-Bench/EvoClaw-data"><img src="https://img.shields.io/badge/%F0%9F%A4%97-Dataset-orange.svg" alt="HuggingFace Dataset" /></a>
  <a href="https://github.com/DeepCommit-ai/DeepCommit"><img src="https://img.shields.io/badge/Data%20Pipeline-DeepCommit-blue?logo=github&logoColor=white" alt="DeepCommit Data Pipeline" /></a>
  <a href="https://hub.docker.com/u/hyd2apse"><img src="https://img.shields.io/badge/Docker-hyd2apse-2496ED?logo=docker&logoColor=white" alt="DockerHub" /></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue.svg" alt="Python 3.10+" /></a>
</p>

---

> [!NOTE]
> 🆕 **[DeepCommit](https://github.com/DeepCommit-ai/DeepCommit)**, the agentic data pipeline that builds the EvoClaw benchmark from real-world commit histories, is now open-source!

Most existing benchmarks evaluate agents on **isolated, one-shot tasks**. But real-world workflows are not a bag of independent missions, they are continuous processes where tasks build on each other, dependencies interleave, and context accumulates over a long session.

<p align="center">
  <img src="assets/evoclaw_concept.png" width="560" alt="Independent Coding Task vs. Continuous Software Evolution" />
</p>

**EvoClaw** is a general-purpose evaluation harness for **continuous tasks**. It drops an AI agent into a working environment and challenges it to complete an ordered sequence of milestones. As the agent works, EvoClaw silently extracts checkpoints, evaluates each milestone, and asynchronously unlocks downstream tasks, enabling fine-grained, per-milestone analysis without interrupting the agent's session. 

<p align="center">
  <img src="assets/evoclaw_illustration.png" width="820" alt="How EvoClaw works: Continuous Task Evaluation with DAG, Agent Loop, and Test-Based Grading" />
</p>

Currently focused on **software evolution**, EvoClaw's architecture is designed to extend to other domains.

## ✨ Key Features

- **Test Your Model**: Out of the box, EvoClaw ships with the [EvoClaw Benchmark](https://arxiv.org/abs/2603.13428) (long-horizon software evolution itineraries from 7 real-world repos) and 4 pre-configured agent frameworks ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://openai.com/index/codex/), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [OpenHands](https://github.com/All-Hands-AI/OpenHands)). Provide a model API key and start evaluating.

- **Bring Your Own Agent**: The agent layer is decoupled from the evaluation engine (see below). Plug in your own agent by implementing a lightweight adapter. EvoClaw also provides a per-milestone analysis framework for detailed performance breakdowns.

- **Bring Your Own Data**: Supply your own task descriptions, test environments (Docker), test list for scoring, and task dependencies. EvoClaw handles orchestration, checkpoint-based evaluation, and reporting, enabling continuous task evaluation beyond coding.

## 👋 Overview

<p align="center">
  <img src="assets/evoclaw_arch.png" width="820" alt="EvoClaw Architecture: Orchestrator, Agent Container, DAG Manager, Evaluation Cycle, and Analytics" />
</p>

Each evaluation trial works as follows:

1. An agent is dropped into a persistent Docker container with a workspace at a given starting state.
2. It receives a sequence of **task specifications** describing tasks to achieve.
3. Tasks are ordered by a **dependency DAG**---downstream tasks unlock as upstream ones are completed.
4. The agent signals completion by creating git tags (e.g., `agent-impl-milestone_001`).
5. A **watcher thread** silently detects tags, extracts artifact snapshots, and runs pre-defined automated validation in a separate, one-time task evaluation container.
6. Results, logs, and outcomes are automatically collected and analyzed per task.

## 🔧 Setup

**0. Prerequisites**

- Python >= 3.10
- Docker
- Model API access via environment variables: `UNIFIED_API_KEY` and `UNIFIED_BASE_URL`

**1. Installation**

```bash
git clone https://github.com/Hydrapse/EvoClaw.git
cd EvoClaw
uv sync
```

**2. Data & Docker Images**

Workspace data is hosted on [HuggingFace](https://huggingface.co/datasets/EvoClaw-Bench/EvoClaw-data). Docker images are hosted on [DockerHub](https://hub.docker.com/u/hyd2apse).

```bash
# Download workspace data
git lfs install
git clone https://huggingface.co/datasets/EvoClaw-Bench/EvoClaw-data

# Pull all repos at once
./scripts/pull_images.sh
```

> See [docs/setup.md](docs/setup.md) for the full data layout, Docker image naming conventions, and manual retag instructions.

**3. Host paths (configure once)**

EvoClaw reads host-specific paths from `.env_private` (gitignored), auto-loaded by `run_all.py` on every launch — set them **once** and they persist across shells, no re-exporting. Copy the template and edit the one path that matters (where you downloaded the data):

```bash
cp .env .env_private
# edit .env_private →  EVOCLAW_DATA_ROOT=/abs/path/to/EvoClaw-data
```

Trial configs then use `data_root: ${EVOCLAW_DATA_ROOT}` — no host path to repeat. (Anti-cheat *quarantine* is auto-on per repo and needs no extra host paths; see [docs/quarantine.md](docs/quarantine.md).)

## 🚀 Usage

> Hand [`docs/running-trials.md`](docs/running-trials.md) to your agent — it has everything needed to launch trials, monitor progress, recover stuck repos autonomously, and manage trial IDs.

**1. Configure** — copy the template and edit:

```bash
cp trial_config.example.yaml trial_config.yaml
```

```yaml
# modify trial_config.yaml 
data_root: ${EVOCLAW_DATA_ROOT}        # set once in .env_private (or hardcode a path)
trial_name: my_experiment              # name for this evaluation run
agent: claude-code                     # agent: claude-code | codex | gemini-cli | openhands
model: claude-opus-4-7                 # model identifier (use claude-opus-4-7[1m] for 1M context)
timeout: 18000                         # optional: max agent runtime per repo (seconds)
# reasoning_effort: high               # optional: low | medium | high | xhigh | max 
# repos: [navidrome, ripgrep]          # optional: run only these repos (default: all)
```

**2. Run** — evaluate across all repos:

```bash
export UNIFIED_API_KEY=sk-...
export UNIFIED_BASE_URL=https://...   # optional, for proxy or custom endpoints
# NOTE: if UNIFIED_BASE_URL is a custom domain, add it to WHITELISTED_DOMAINS
# in harness/e2e/container_setup.py — agent containers block all other outbound traffic.
python scripts/run_all.py --config trial_config.yaml
```

**3. Monitor** — check progress in another terminal:

```bash
./scripts/monitor.sh                              # auto-detects trial, compact view
./scripts/monitor.sh my_experiment --detail        # per-milestone breakdown
./scripts/monitor.sh my_experiment --full          # full table with all columns
```

> **Tip:** `run_all.py` is fire-and-forget — it spawns one detached `run_e2e` per repo and exits immediately (no `nohup` needed). Re-running the same command is the resume operation: each worker holds an `flock` on its trial dir, so the second invocation either takes over only-if-the-first-one-died, or refuses with a clear "owned by PID …" message. Add `--force` to wipe & restart the latest matching `_NNN`, or `--new` to start the next `_NNN` fresh. See [docs/running-trials.md](docs/running-trials.md) for the full behavior matrix.

> See [docs/running-trials.md](docs/running-trials.md) for the day-to-day operational runbook (launch, monitor, recover from stuck repos), and [docs/advanced.md](docs/advanced.md) for single-repo / single-milestone debugging, result collection, `e2e_config.yaml`, and lock internals.

> **Running on Google Vertex AI?** Vertex uses ADC (no API key to paste); set `vertex_ai: true` with `agent: gemini-cli` (Gemini models) or `agent: claude-code` (Claude models) and run `run_all.py` as normal. See [docs/vertex-ai.md](docs/vertex-ai.md) for the credential setup and how it works.

> **Adding a new model to an existing agent?** See [docs/adding-a-model.md](docs/adding-a-model.md) — the config fields, auth routes (API key vs Vertex/ADC), domain whitelist, and pricing, with a worked example.

## 🔍 Troubleshooting

Below are common issues you may encounter when running evaluations, along with solutions.

**1. Network access blocked inside containers**

Agent containers enforce an iptables-based outbound whitelist — only domains needed for API access and package management are allowed (e.g., `api.anthropic.com`, `registry.npmjs.org`, `pypi.org`). Code hosting sites (GitHub, GitLab, etc.) are explicitly blocked to prevent data leakage. If your setup routes API requests through a custom proxy, make sure the proxy domain is included in `WHITELISTED_DOMAINS` in `harness/e2e/container_setup.py`.

> **Port 80 outbound blocked?** Some hosts/datacenters block plain HTTP. The harness automatically rewrites Debian/Ubuntu apt sources to HTTPS so `apt-get update` reaches mirrors via 443 — no action needed.

**2. Agent stops before all milestones are completed**

Directly re-run the same `run_all.py` command to resume and continues from where the previous worker left off:

```bash
python scripts/run_all.py --config trial_config.yaml
```

> **Evaluation protocol**: The reported EvoClaw benchmark results follow this protocol: trials are resumed until all milestones are submitted and evaluated. Each resume reuses the same agent session by default to preserve the model's memory of prior work; after three consecutive resumes without new submissions, EvoClaw automatically rotates in a fresh session on the next resume so no repo blocks the trial indefinitely. We encourage reproducibility studies to follow the same setting.

> ⚠️ **Not every "no progress" is the agent's fault.** Rate-limit (HTTP 429), quota exhaustion, or auth (HTTP 401/403) errors also cause workers to exit with no submissions. Rotating the agent session won't help — the next request hits the same error. Check the session jsonl for `api_error_status: 429` / `"reached your usage limit"` / `401`; if present, fix the API key (top up or rotate) **before** resuming.

> **Resume requires the container to still exist.** All of the agent's in-progress work — code changes, git history, and Claude's conversation memory — lives inside the container, with no copy on the host. If the container is deleted, the whole trial's working state is lost and only `--force` (restart from scratch) is available. Source snapshots of already-evaluated milestones are preserved on the host under `evaluation/`, so prior scores survive, and the full agent logs are copied to the host directory when the trial finishes.

## 🤝 Contributing

We welcome contributions! Whether it's adding support for new agents, new task domains, new datasets, bug fixes, or documentation improvements.

## ✍️ Citation

Welcome to cite our paper if you find EvoClaw useful!

```bibtex
@misc{deng2026evoclawevaluatingaiagents,
      title={EvoClaw: Evaluating AI Agents on Continuous Software Evolution},
      author={Gangda Deng and Zhaoling Chen and Zhongming Yu and Haoyang Fan and Yuhong Liu and Yuxin Yang and Dhruv Parikh and Rajgopal Kannan and Le Cong and Mengdi Wang and Qian Zhang and Viktor Prasanna and Xiangru Tang and Xingyao Wang},
      year={2026},
      eprint={2603.13428},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2603.13428},
}
```

## 📄 License

This project is licensed under the [MIT](LICENSE) License.
