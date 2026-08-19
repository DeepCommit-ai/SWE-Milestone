# Image Patching (ENV-PATCH) — standard procedure

How to repair a published evaluation image, verify the repair, use it in a
re-evaluation campaign, and promote it as a benchmark **patch release**
(`vX.Y.Z`). This is the missing middle between `docs/versioning.md` (version
identity, release mechanics) and `docs/re-evaluation.md` (re-scoring frozen
agent artifacts). Detection of what needs patching: `docs/env-deps-verification.md`
+ the `env-deps-audit` skill.

One sentence: **patch = overlay on the published image, evaluated under an
rc tag, christened as `vX.Y.Z` only after the campaign's gates pass — the
released image is byte-identical (same digest) to what the campaign
evaluated.**

## 0. Principles (inherited + two new ones)

1. All of `docs/versioning.md` Immutability applies: published version tags
   are read-only; **retag, never rebuild** unchanged images; `:latest` is
   never a published basis; results are append-only; old local images are
   kept while any resumable trial exists.
2. **Eval-side vs rollout-side (new, load-bearing):** an ENV-PATCH may touch
   only what the *evaluator* sees (milestone images, eval-time closure
   overlays, baselines). It must NOT change the rollout world (`base`,
   `base-offline`, quarantine policy, prompts) — agents already acted under
   that world, so re-evaluating frozen artifacts stays legitimate. A repair
   that changes the rollout world invalidates re-evaluation entirely: it
   requires re-RUNS and at least a minor version, not a patch. Check this
   boundary first; it decides everything downstream.
3. **Evaluate-then-christen (new):** campaigns run on `-rc` tags; promotion
   is a pointer operation onto the exact bytes the campaign scored. Never
   rebuild between campaign and release — a rebuild is a different image
   and voids the campaign's evidence.

## 1. Artifacts and naming

| Artifact | Convention | Precedent |
|---|---|---|
| Overlay Dockerfile | `dockerfiles/<mid>/Dockerfile.<target-version>` in the **data repo** (e.g. `Dockerfile.v1.0.1`), committed on a patch branch `envfix-<target-version>` | `feature_enhancements/Dockerfile.v1.0`, `maintenance_ui_ux/Dockerfile.v1.0` |
| Overlay contract | `ARG SOURCE_IMAGE` (defaults to the previous published version's image) + `ARG CLOSURE_IMAGE` (pinned base-offline) + `RUN --mount=type=bind,from=closure,…,readonly` offline install steps | `milestone_seed_599112e_1/Dockerfile` |
| RC image tag (local only) | `swe-milestone/<repo_full>__<mid>:<target-version>-rc<N>` (e.g. `:v1.0.1-rc1`) | new |
| Campaign record | `PATCHED_IMAGES.txt` (one milestone id per line) + rc tags **and their image IDs/digests** in the campaign's `EXPECTATION.md` | `docs/re-evaluation.md` |
| Released tag | `…:<target-version>` per `harness/e2e/image_version.py` naming (local + hub forms) | versioning.md |

RC rules: rc tags are **campaign-scoped and floating** — never pushed to the
Hub, never referenced by `SWE_MILESTONE_IMAGE_TAG`, never used to launch
trials (`run_all` version gates reject them by design); they are consumed
only via the offline evaluator's explicit image argument. Bump `-rc<N>` on
every rebuild; stale rc tags may be deleted after release.

Overlay hygiene: the overlay must be **offline and reproducible-in-spirit**
— `docker build --pull=false --network=none`, packages exclusively from the
pinned closure image's caches (verify the cache HAS the needed artifact
before building), no `latest` refs anywhere. If a patch chain grows
(v1.0.2 on top of v1.0.1), the new overlay's `SOURCE_IMAGE` default points
at the previous *published* version — chains stay explicit in the ARG line.

## 2. Build

```bash
# per milestone in PATCHED_IMAGES.txt (data repo checkout on branch envfix-v1.0.1)
docker build --pull=false --network=none \
  -f dockerfiles/<mid>/Dockerfile.v1.0.1 \
  -t swe-milestone/<repo_full>__<mid>:v1.0.1-rc1 \
  dockerfiles/<mid>/
```

The evaluator's derived cache-overlay (`ensure_offline_evaluation_image`)
needs no manual handling: it is content-addressed and re-derives
automatically from the rc image.

## 3. Verification gates (per patched image, before any campaign use)

All three recorded in the campaign dir; any failure blocks the campaign.

1. **Golden-baseline invariance.** GT self-grade on the rc image must be
   byte-identical to the primary baseline — except deltas the patch
   *declares* (e.g. previously collection-broken suites now collecting).
   A declared baseline delta makes this a **baseline-changing patch** (§5).
2. **Environment verification.** The repair target is present and resolvable
   in the rc image (env-deps probe catalog from the `env-deps-audit` skill /
   `docs/env-deps-verification.md`), and `scripts/verify_env_deps.py
   --static` is clean for the repo.
3. **No collateral drift.** `docker diff`-level sanity: the overlay touched
   only the intended stores (node_modules/site-packages/.m2/…), not
   `/testbed` source, not test files, not tool binaries.

## 4. Use in re-evaluation

Per `docs/re-evaluation.md`, with one addition: the campaign's
`EXPECTATION.md` must pin, per patched milestone, the rc tag **and its
image ID** — the validity of every replay cell is conditional on having run
against exactly those bytes (the eval artifacts record
`offline_cache_effective_image_id`, so this is auditable after the fact).

## 5. Baseline-changing patches (collection repairs)

When a repair legitimately changes what the golden baseline collects
(suites that were error-classified now run), the patch also ships data:

1. Regenerate the affected milestones' classification via the standard
   building pipeline **against the rc image**; stage outputs in the campaign
   dir (never write `test_results/` in place).
2. The declared expectation must enumerate: which classification files
   change, the new graded-id counts, and the direction rules for every
   affected arm. **All arms' affected cells re-evaluate under the new
   baseline — never a subset** (uniform denominators are the point).
3. Staged classifications enter the data repo in the same patch branch as
   the overlays, and land on main only at release (§6).

## 6. Promotion runbook (patch release `vX.Y.Z`)

Preconditions: campaign complete; mechanical comparison passed; user
approved the deltas. Then, on the machine holding the images:

```bash
V_OLD=v1.0  V_NEW=v1.0.1

# 1. Carry over EVERY image of the current version (pointer op, 0 bytes):
grep -v '^#' manifests/digests-${V_OLD}.tsv | cut -f1 | while read local; do
  [ -n "$local" ] && docker tag "$local" "${local%:*}:${V_NEW}"
done

# 2. Overwrite the patched set's new tags with the campaign's rc bytes:
while read mid; do
  docker tag "swe-milestone/<repo_full>__${mid}:${V_NEW}-rc1" \
             "swe-milestone/<repo_full>__${mid}:${V_NEW}"
done < <campaign>/PATCHED_IMAGES.txt

# 3-5. Push, freeze digests, verify — identical to versioning.md steps 3-5,
#      with --version ${V_NEW} and --manifest manifests/digests-${V_OLD}.tsv
#      wherever a plan command needs the carried-over name set.
#      Unchanged images upload 0 bytes; only the patched set uploads.

# 6. Data repo: merge branch envfix-${V_NEW} to main (overlay Dockerfiles +
#    staged classifications), then tag:
git -C <data> tag ${V_NEW} && git -C <data> push origin main ${V_NEW}

# 7. Single source of truth:
echo ${V_NEW} > manifests/BENCHMARK_VERSION   # + commit (harness repo)
```

**The proof obligation:** `diff manifests/digests-${V_OLD}.tsv
digests-${V_NEW}.tsv` must show changed digests for exactly the
`PATCHED_IMAGES.txt` set and nothing else. Paste that diff into the release
notes. CI's daily `verify_image_digests --hub` then monitors the new
manifest like any other.

After image promotion: **score promotion** for the campaign's cells follows
`docs/re-evaluation.md` §Promotion (separate, human-approved, per campaign)
— scores were produced on rc bytes that are now, by digest identity, the
released `vX.Y.Z` images, so promoted cells are labeled `vX.Y.Z` honestly.

## 7. Ordering (the whole pipeline)

```
detect (env-deps-audit)                    # what is broken, which cells
→ patch branch: overlay Dockerfiles       # data repo, envfix-vX.Y.Z
→ build rc images (offline)               # §2
→ gates: golden / env-probe / no-drift    # §3  — fail ⇒ fix overlay, rc+1
→ re-eval campaign on rc tags             # §4, docs/re-evaluation.md
→ USER reviews deltas                     # stop point
→ image promotion = patch release          # §6 (christening the rc bytes)
→ score promotion                          # docs/re-evaluation.md §Promotion
→ publication                              # §9 — three independent chains
```

Abort at any gate leaves published state untouched: rc tags and the patch
branch are the only artifacts, both disposable.

## 8. Checklist

- [ ] Eval-side only (rollout world untouched) — else STOP: re-runs, not a patch
- [ ] Overlay `Dockerfile.<vX.Y.Z>` per milestone, on branch `envfix-<vX.Y.Z>`, offline build args pinned
- [ ] Closure cache verified to contain every artifact the overlay installs
- [ ] rc images built `--network=none`, tagged `:<vX.Y.Z>-rc<N>`, never pushed
- [ ] Golden invariance recorded (declared deltas enumerated if baseline-changing)
- [ ] Env probe + `verify_env_deps.py --static` clean
- [ ] `docker diff` collateral check recorded
- [ ] EXPECTATION.md pins rc tags + image IDs + PATCHED_IMAGES.txt
- [ ] Campaign + mechanical comparison + user approval
- [ ] Carry-over retag → patched overwrite → push → freeze `digests-<vX.Y.Z>.tsv`
- [ ] Manifest diff == patched set exactly; pasted into release notes
- [ ] `verify_image_digests --local` and `--hub` green; data repo merged + tagged; `BENCHMARK_VERSION` bumped
- [ ] Score promotion per re-evaluation.md; cross-version labels on any published comparison
- [ ] Publication: all three chains of §9 run and verified (none is triggered by the others)

## 9. Publication — three independent chains

Promoting scores does **not** publish them. Three separate chains carry a
release outward, and **none of them triggers the others** — this is the step
most often left half-done, so run and verify each explicitly.

| # | Chain | Command | Failure mode if skipped |
|---|---|---|---|
| 1 | **Dashboard** | `python3 analysis/refresh_data.py` (needs `PYTHONPATH` + an explicit corpus root), then **`npm run build`** in `analysis/visualization/dashboard` | Data is compiled into the Vite bundle. Refreshing without rebuilding changes nothing a viewer sees, and the served page keeps the old numbers while the files on disk look updated. |
| 2 | **Leaderboard page** | `SWE-Milestone-website/versions/<v>/build.py` over the website CSV → `index.html`; Flask serves it directly | `refresh_data.py` does **not** touch this chain. The dashboard can be fully current while `/leaderboard` still shows pre-release numbers. |
| 3 | **HF corpora** (`SWE-Milestone-data`, `SWE-Milestone-log`) | `analysis/scripts/sync_log_corpus.py` (incremental align + `.hf_revision` fail-closed gate), then upload | Renames and deletions do not propagate by pattern alone — after any rename, reconcile the remote by hand and confirm no residue. |

Rules that apply to all three:

- **Label the version wherever scores are shown.** Rows that were not
  re-evaluated keep the version that produced them; do not silently re-label
  them as the new release.
- **Verify by looking at the served artifact**, not at the build log — open the
  page (or fetch it) and confirm a number you know changed.
- **Publication is user-gated like promotion.** These chains are outward-facing;
  run them only under the same explicit approval that authorised the release.
