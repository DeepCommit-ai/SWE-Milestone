# Re-evaluation (v1.0 maintenance)

How to re-score existing trials after a data-side repair (ENV-PATCH image
layer, `filter_list`, classification amendment) **without re-running any
agent**. Current benchmark version is **1.0**; every artifact below just
records "1.0" plus the data-repo commit it was produced from.

## Rules

1. **Agents are never re-run.** Inputs are the frozen agent artifacts under
   `EvoClaw-log/<range>/e2e_trial/<arm>/` — that directory is the primary
   record and is **never written to** by re-evaluation.
2. **Declare the expectation first.** Before re-running, write down which
   (milestone × arm) pairs may change and in which direction; everything
   else must come out identical. An undeclared change is a bug, not a result.
3. **Compare mechanically, then decide.** A script diffs original vs re-eval
   per test id. Promotion of re-eval results into the primary record is a
   separate, explicit, human-approved step — not part of this procedure and
   currently not enabled.
4. **An output file is not proof of completion.** A replay cell is valid only
   when the evaluator exits `0` or `1`, `patch_successfully_applied` is true,
   and neither `infra_invalid` nor `infrastructure_failure` is set. Exit `2`
   and `eval_status: infra-invalid` retain diagnostic evidence, but must be
   retried or refined; they are never promotable ordinary zeroes.

## Where things live

```
EvoClaw-log/reeval/                      # gitignored scratch area, safe to delete
└── <range>/e2e_trial/<arm>/evaluation/<milestone>/
    ├── evaluation_result.json           # evaluator output (same schema as primary)
    └── ...                              # evaluator artifacts/logs
└── <range>/EXPECTATION.md               # declared scope & direction, data commit,
                                         # patched image tags, date
```

The tree **mirrors the primary layout** under `EvoClaw-log/<range>/…`, so any
comparison is "same relative path, two roots". Patched images get a distinct
local tag (recorded in `EXPECTATION.md`); published tags are never
overwritten.

**Evaluation-time repo hooks** live in the data workspace, wired via the repo
config key `evaluation_post_snapshot_script` (e.g. dubbo's Maven closure,
go-zero's go.mod backfill). The evaluator runs the hook after every snapshot
application (END base and START fallback alike), passes context via env —
`SWE_MILESTONE_ID`, `SWE_MILESTONE_BASE_TAG`,
`SWE_MILESTONE_LEGACY_SNAPSHOT` (`1` when running under
`--allow-legacy-snapshot`) — and echoes the script's stdout into the eval log
as its audit trail. Hook identity (path + sha256 + applied flag) is persisted
in `evaluation_result.json`.

## How to re-evaluate one (arm × milestone)

Offline evaluator CLI against the patched image, feeding the frozen agent
snapshot/patch from the primary record:

```
python -m harness.e2e.evaluator \
  --milestone-id <MID> \
  --patch-file  EvoClaw-log/<range>/e2e_trial/<arm>/e2e_workspace/<MID>/... \
  --baseline-classification EvoClaw-data/<range>/test_results/<MID>/<MID>_classification.json \
  --output EvoClaw-log/reeval/<range>/e2e_trial/<arm>/evaluation/<MID>/evaluation_result.json
```

(Exact flags per `harness/e2e/evaluator.py --help`; `filter_list` is applied
automatically when present.)

Batch drivers must apply the validity gate above when deciding whether to
skip an existing cell and again before reporting the campaign complete. The
formal orchestrator retries this condition automatically; a direct CLI loop
must do the same explicitly. After retries are exhausted, leave the evidence
in the re-evaluation tree and mark the campaign incomplete rather than
substituting a score.

## How to compare

Per test id, between primary and reeval `evaluation_result.json`:

- pairs **outside** the declared scope: must be byte-equal on outcomes;
- pairs **inside** the declared scope: deltas must match the declared
  direction; list every delta in the comparison output.

Store the comparison output next to `EXPECTATION.md`. Any undeclared delta →
stop and investigate; nothing is promoted.

## Previewing score impact with monitor.sh

`scripts/monitor.sh` wraps `harness.e2e.collect_results --multi-repo`; because
`reeval/` mirrors the primary layout, it can read either root directly:

```
./scripts/monitor.sh <arm> --data-root <EvoClaw-log>        --detail <repo>   # current scores
./scripts/monitor.sh <arm> --data-root <EvoClaw-log>/reeval --detail <repo>   # re-eval scores
```

Read the two `--detail` tables side by side for per-milestone deltas
(status / F2P / N2P / P2P / score). Caveats:

- In the reeval root only re-evaluated cells have data; every other milestone
  shows "Not run", so **repo-level aggregates are meaningless there** — only
  per-milestone rows are comparable before promotion.
- Score columns: `score_1000` = V2 (`(F2P+N2P)_ach/req × max(0, 1 −
  P2P_missed/min(1000, P2P_req))`), `score_full` = V1 (ratio × P2P ratio),
  `score_reliable` = PR-F1 where broken = P2P failed+missing. Repo score =
  mean over graded milestones × 100. `resolve_pct` counts the `resolved` bit.
- `collect_results` prefers `evaluation_result_filtered.json` over
  `evaluation_result.json` per cell (`--non-filter` disables).

## Preflight & silent-failure catalog

Re-evaluation's characteristic failure is a config/environment gap that makes
the evaluator emit a **plausible but wrong score with no error raised** — it
survives direction-only comparison. The operational checklist (image pin,
`config/<repo>.yaml` reachability so `test_framework` normalization stays on,
anchor probe, concurrency/network budget) and the catalog of incidents that
each cost a full re-run live in **`docs/post_verify/re-evaluation-playbook.md`**.
Run that playbook before launching and before believing any score.

## Promotion procedure (explicit, human-approved; per campaign)

Promotion = replacing evaluator *outputs* in the primary record after the
mechanical comparison passed. Never enabled by default; each campaign is
promoted once, by hand, after user sign-off.

1. **Back up, append-only**: move the cell's current evaluator outputs
   (`evaluation_result.json`, `evaluation_result_filtered.json` if present,
   `artifacts/`, `artifacts.tar.gz`, `feedback_report.md`) to
   `reeval/promotion_backup/<range>/<arm>/<milestone>/`. Never delete.
2. **Copy in the re-eval outputs** from the reeval mirror path. **Never touch
   `source_snapshot.tar`** (frozen agent artifact — input, not output).
3. **Sync `summary.json`**: update `results[<milestone>]` (`eval_status`,
   `test_summary`, keep `attempt`) to match the new evaluation_result.
   Required because `collect_results.load_e2e_results` only replaces a
   summary-sourced `test_summary` when it reads a *filtered* file; with plain
   `evaluation_result.json` it corrects `eval_status` but keeps the stale
   summary counts.
   *Exception — filter_list-only campaigns (e.g. M021):* dropping the new
   `evaluation_result_filtered.json` next to the untouched original is
   sufficient; the filtered-preference path replaces `test_summary` on its
   own, and the original file staying in place is the audit trail.
4. **Record the flip**: run the two monitor commands above before/after,
   store the diff as `SCORE_DELTA_<campaign>.md` next to the campaign's
   EXPECTATION.md, then regenerate any downstream aggregates.

## Current application: D-1 (nushell)

| Item | Value |
|---|---|
| Repaired milestones | `milestone_G01_48bca0a`, `milestone_core_development.4` |
| Repair | ENV-PATCH: mixed-tree conciliation (`apply_patches.sh`, idempotent, no-op on GT overlay) + feature-closure completion |
| Declared expectation | these 2 milestones: `compilation_failure` → measured scores; all other milestones identical |
| Out of scope | `core_development.1` (flaky census lane), `core_development.3` (classification adjudication lane) |
| Acceptance before re-eval | GT empty-overlay self-grade green ×2 **and** mixed-tree probe with a real vintage `source_snapshot.tar` (a standalone-only fix caused this incident) |
