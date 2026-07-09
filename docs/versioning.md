# Versioning policy

A benchmark version number is a **comparability contract**: two scores may
share a leaderboard only if they were produced against the same tasks, the
same environments, and the same grading semantics. Everything below follows
from one axiom:

> Any change that could move a score bumps the benchmark version.
> Any change that cannot, does not.

## Two version axes + a binding layer

| Axis | Format | Covers | Lives in |
|---|---|---|---|
| **Benchmark data version** | `vX.Y` (e.g. `v1.0`) | tasks, tests, Docker image environments, **grading semantics** | image tags; `EVOCLAW_IMAGE_TAG` (default in `harness/e2e/image_version.py`) |
| **Harness version** | git tags / commits (3-part `vX.Y.Z` if tagged, to stay visually distinct) | everything score-neutral: refactors, logging, monitoring, agent integrations | git history |

Docker images have **no version identity of their own** — they are the frozen
form of the benchmark data, so their tag *is* the benchmark data version.

The **binding layer** is the per-version manifest pair:

- `manifests/images-<version>.tsv` — the image inventory (`short`,
  `repo_full`, `milestone`; ~115 rows for v1.0). The ONLY place the inventory
  is defined; pull/push/retag plans are generated from it
  (`python3 -m harness.e2e.image_version {pull,push,retag}-plan`).
- `manifests/digests-<version>.tsv` — the content freeze: each image's
  registry manifest digest, captured after the release push. Diffing two
  versions' digest files proves exactly which images changed.

Anchors vs labels: the **digest** (and the git **commit SHA**) are content
identity; tags are human-readable pointers. Provenance records should carry
the anchor, not just the label.

## Bump rules

| Change | Bump |
|---|---|
| Task/test edits, image environment changes (e.g. quarantine hardening) | benchmark version |
| **Grading/judging logic change** — even with zero image changes | benchmark version (digest file notes "images identical to previous") |
| Single-task fix | benchmark patch version (e.g. `v1.0.1`); only that image's digest changes |
| Harness refactor / logging / new agent support / infra fixes | harness only |

There is no "backward compatible" benchmark change — only *comparable* and
*not comparable*. Scores across benchmark versions must be labeled as such.

## Immutability discipline

1. **Published version tags are read-only.** Never overwrite, never delete —
   deleting breaks reproduction of every historical score on that version.
2. **Retag, never rebuild.** Docker builds are not reproducible (a rebuild
   silently drifts the arena). For a new version, unchanged images get the
   OLD digest retagged (`docker tag` — a free pointer operation; pushing a
   retag uploads 0 bytes, layers dedupe); only changed images are rebuilt.
3. **`:latest` and `:staging` are floating build tags** — pipeline-internal
   only, never the basis of a published version, never trusted by an
   explicitly pinned run (`resolve_image` never falls back when
   `EVOCLAW_IMAGE_TAG` is set).
4. **Containers launch with `--pull=never`** — a missing local image is a
   loud failure, never a silent registry fetch mid-eval.
5. **Results are append-only and stamped**: benchmark version, image ref (and
   ideally digest), model/agent — recorded per trial; old records are facts
   and are never rewritten. Pre-v1.0 trials recorded old-format image names;
   the single legacy parse branch (`parse_local_ref`) exists solely so
   resuming them keeps working.

## Naming (v1.0 scheme)

```
hub:    <org>/swe-milestone__<repo_full>__<milestone>:<version>
local:  swe-milestone/<repo_full>__<milestone>:<version>
```

The payload is byte-identical on both sides; conversion is mechanical
(`/` ↔ `__` under the fixed `swe-milestone` prefix) with no lookup table.
`base` and `base-offline` are ordinary milestones. `repo_full` and
`milestone` never contain `__` (validated at load). Single authority:
`harness/e2e/image_version.py`. See `docs/setup.md` for usage and
`docs/release-v1.0-images.md` for the release runbook.

Pre-v1.0 hub images (`hyd2apse/<short>:<milestone>-v0.9`) are frozen in
place; no tooling reads them (fetch via the old script from git history if
ever needed).
