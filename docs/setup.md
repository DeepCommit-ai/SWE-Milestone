# Setup

## Prerequisites

- Python >= 3.10
- Docker
- Model API access via environment variables: `UNIFIED_API_KEY` and `UNIFIED_BASE_URL`

## Installation

```bash
git clone https://github.com/Hydrapse/EvoClaw.git
cd EvoClaw
uv sync
```

## Workspace Data

Workspace data (metadata, SRS documents, test classifications) is hosted on HuggingFace:

```bash
git lfs install
git clone https://huggingface.co/datasets/hyd2apse/EvoClaw-data
```

The dataset contains one directory per repository:

```
EvoClaw-data/
├── navidrome_navidrome_v0.57.0_v0.58.0/
├── apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/
├── BurntSushi_ripgrep_14.1.1_15.0.0/
├── zeromicro_go-zero_v1.6.0_v1.9.3/
├── nushell_nushell_0.106.0_0.108.0/
├── element-hq_element-web_v1.11.95_v1.11.97/
└── scikit-learn_scikit-learn_1.5.2_1.6.0/
```

Each repository workspace directory contains:

```
<repo_name>/
├── metadata.json                      # Repo metadata (src_dirs, test_dirs, patterns)
├── dependencies.csv                   # Milestone dependency DAG
├── milestones.csv                     # Milestone catalog
├── selected_milestone_ids.txt         # (optional) Subset of milestones to evaluate
├── additional_dependencies.csv        # (optional) Extra DAG edges
├── non-graded_milestone_ids.txt       # (optional) Milestones excluded from scoring
├── srs/{milestone_id}/SRS.md          # Requirements specification per milestone
└── test_results/{milestone_id}/       # Baseline test classifications
    └── {milestone_id}_classification.json
```

The "Milestones" column in the main README counts graded milestones only. Some repositories include additional non-graded milestones (listed in `non-graded_milestone_ids.txt`) that the agent must still implement as part of the DAG but are excluded from scoring.

## Docker Images

Pre-built Docker images are hosted on DockerHub under the `hyd2apse` namespace. There are two types of images per repository:

- **Base image** (`base` / `base-offline`) -- the agent runs inside this container (passed via `--image`)
- **Milestone images** -- used by the evaluator to run tests for each milestone

Naming is mechanical on both sides (single authority:
`harness/e2e/image_version.py`; inventory: `manifests/images-<version>.tsv`):

```
hub:    <org>/swe-milestone__<repo_full>__<milestone>:<version>
local:  swe-milestone/<repo_full>__<milestone>:<version>
```

Example (org `hyd2apse`, version `v1.0`):

```
hub:    hyd2apse/swe-milestone__navidrome_navidrome_v0.57.0_v0.58.0__milestone_006:v1.0
local:  swe-milestone/navidrome_navidrome_v0.57.0_v0.58.0__milestone_006:v1.0
```

`base` and `base-offline` are ordinary milestones (same pattern). The payload
`<repo_full>__<milestone>` is byte-identical on both sides; `repo_full` and
`milestone` never contain `__`, so parsing is unambiguous.

### Pulling images

**`docker login` first** -- anonymous DockerHub pulls are rate-limited and a
full `./scripts/pull_images.sh` run fetches ~115 images.

```bash
docker login

# Pull and retag all images for a specific repo
./scripts/pull_images.sh --repo navidrome

# Pull and retag everything in the manifest
./scripts/pull_images.sh

# Dry run (print the pull plan, do nothing)
./scripts/pull_images.sh --repo navidrome --dry-run
```

The script executes the plan emitted by
`python3 -m harness.e2e.image_version pull-plan` (one `<hub>\t<local>` pair
per line); retagging hub -> local is a pure pointer operation.

### Version pinning

The benchmark data version is pinned via `EVOCLAW_IMAGE_TAG` (default `v1.0`,
defined once in `harness/e2e/image_version.py`). Images for a published
version are immutable: never re-pushed, never deleted. Pre-v1.0 images remain
on the hub under the old `hyd2apse/<short>:<milestone>-v0.9` scheme, frozen;
the current tooling intentionally does not read them (use the old script from
git history if you ever need them).
