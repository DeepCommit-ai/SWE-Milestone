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

## How to compare

Per test id, between primary and reeval `evaluation_result.json`:

- pairs **outside** the declared scope: must be byte-equal on outcomes;
- pairs **inside** the declared scope: deltas must match the declared
  direction; list every delta in the comparison output.

Store the comparison output next to `EXPECTATION.md`. Any undeclared delta →
stop and investigate; nothing is promoted.

## Current application: D-1 (nushell)

| Item | Value |
|---|---|
| Repaired milestones | `milestone_G01_48bca0a`, `milestone_core_development.4` |
| Repair | ENV-PATCH: mixed-tree conciliation (`apply_patches.sh`, idempotent, no-op on GT overlay) + feature-closure completion |
| Declared expectation | these 2 milestones: `compilation_failure` → measured scores; all other milestones identical |
| Out of scope | `core_development.1` (flaky census lane), `core_development.3` (classification adjudication lane) |
| Acceptance before re-eval | GT empty-overlay self-grade green ×2 **and** mixed-tree probe with a real vintage `source_snapshot.tar` (a standalone-only fix caused this incident) |
