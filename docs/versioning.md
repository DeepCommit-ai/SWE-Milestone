# Versioning

One axiom: **any change that could move a score bumps the benchmark version;
anything else doesn't.** A version number is a comparability contract —
same tasks, same environments, same grading.

## Two axes

| Axis | Format | Covers |
|---|---|---|
| **Benchmark data version** | `vX.Y` — the image tag (`SWE_MILESTONE_IMAGE_TAG`; default = **`manifests/BENCHMARK_VERSION`**, the single source of truth) | tasks, tests, image environments, **grading semantics** |
| **Harness version** | git commits / tags | everything score-neutral (refactors, logging, agent integrations) |

Images have no version identity of their own — their tag *is* the benchmark
version. The binding layer is `manifests/digests-<v>.tsv` — the single
per-version manifest: it enumerates the version's images AND freezes their
content digests (drives pull/push plans; diff two versions to prove exactly
which images changed). Digests and commit SHAs are identity; tags are labels.

## Data version

The workspace data (SWE-Milestone-data git clone) is the other score-moving
input, so it carries the **same `vX.Y` tags** as the images. One knob pins
both: `SWE_MILESTONE_IMAGE_TAG` (default = `manifests/BENCHMARK_VERSION`;
bumping a release = edit that one file + commit the new digest manifest —
code, scripts, and CI all read it).

At launch (`scripts/run_all.py` and `harness/e2e/run_e2e.py`),
`harness/e2e/data_version.py` verifies — by read-only git fact check, never a
declaration file — that the data checkout's HEAD is the commit the version
tag points at, and `run_e2e` persists the verdict in `trial_metadata.json`:

```json
"benchmark_version": "v1.0",
"data_version": {"state": "match", "commit": "<sha>", "expected_tag": "v1.0",
                 "explicit_pin": false, "checked": true}
```

Enforcement (hardened 2026-07-17): a mismatch / missing tag / non-git data
root **refuses the launch under the default pin exactly as under an explicit
`SWE_MILESTONE_IMAGE_TAG`** — score comparability is the benchmark's core
contract, so an unverified data checkout never runs silently.
`SWE_MILESTONE_DATA_VERSION_CHECK=off` is the deliberate escape hatch for
development (recorded as `checked: false` in trial metadata); a digest-pinned
image ref (`@sha256:…`) similarly overrides the image-tag gate deliberately.
Align a stale checkout with `./scripts/pull_data.sh` (aligns by default;
`--report-only` to inspect without changing anything).

## Bump rules

| Change | Bump |
|---|---|
| Task / test / image-environment change | benchmark |
| Grading logic change — even with zero image changes | benchmark |
| Single-task fix | benchmark patch (`v1.0.1`) |
| Anything score-neutral | harness only |

There is no "backward compatible" benchmark change — only *comparable* and
*not comparable*; cross-version scores must be labeled.

## Immutability

1. Published version tags are read-only: never overwrite, never delete.
2. **Retag, never rebuild.** Builds are not reproducible; unchanged images
   keep their old digest under the new tag (a free pointer op — pushing a
   retag uploads 0 bytes).
3. `:latest` / `:staging` are floating build tags, never a published basis;
   an explicit `SWE_MILESTONE_IMAGE_TAG` never falls back.
4. Containers launch with `--pull=never`: a missing local image fails loud,
   never a silent registry fetch.
5. Results are append-only. Pre-v1.0 trials recorded old-format image names;
   the single legacy branch in `parse_local_ref` keeps their resume working —
   so don't `docker rmi` old-format local images while such trials remain.

## Naming (v1.0)

```
hub:    <org>/swe-milestone__<repo_full>__<milestone>:<version>
local:  swe-milestone/<repo_full>__<milestone>:<version>
```

Mechanical conversion (`/` ↔ `__`), no lookup table. `base`/`base-offline`
are ordinary milestones; components never contain `__` (validated at load).
Authority: `harness/e2e/image_version.py`; usage: [setup.md](setup.md).
Pre-v1.0 hub images (`hyd2apse/<short>:<mid>-v0.9`) are frozen in place;
no tooling reads them.

## Release runbook (vX.Y)

Run on the machine holding the source images. Example values: v0.9 → v1.0.

> The digest manifest (`manifests/digests-<version>.tsv`) is the single
> per-version manifest: it enumerates the images AND freezes their content
> digests. When cutting a NEW version its manifest does not exist yet — point
> the plan commands at the previous version's file with
> `--manifest manifests/digests-<prev>.tsv` (the name set carries over);
> step 4 then writes the new file.

```bash
# 1. Inventory check — every manifest row has a local source (expect no output)
python3 -m harness.e2e.image_version retag-plan --version v1.0 \
    --from-version v0.9 --base-offline-from latest |
while IFS=$'\t' read -r old new; do
    docker image inspect "$old" >/dev/null 2>&1 || echo "MISSING $old"
done

# 2. Retag old -> new (pointer op; keep the old tags, see Immutability #5)
python3 -m harness.e2e.image_version retag-plan --version v1.0 \
    --from-version v0.9 --base-offline-from latest |
while IFS=$'\t' read -r old new; do docker tag "$old" "$new" || exit 1; done

# 3. Push everything (docker login first; non-zero exit on any failure)
./scripts/push_images.sh

# 4. Freeze digests — match the NEW hub repo (RepoDigests[0] can be a stale
#    entry from an old-name pull); commit the resulting file
python3 -m harness.e2e.image_version push-plan --version v1.0 |
while IFS=$'\t' read -r local hub; do
    repo="${hub%%:*}"
    digest=$(docker image inspect \
        --format '{{range .RepoDigests}}{{println .}}{{end}}' "$hub" 2>/dev/null |
        grep "^${repo}@" | head -1)
    printf '%s\t%s\n' "$local" "${digest:-PUSH-FIRST}"
done > manifests/digests-v1.0.tsv

# 5. Verify: local + Hub against the frozen manifest, then smoke one eval.
#    --local BEFORE pushing images catches "rebuilt locally, manifest stale";
#    --hub AFTER pushing confirms the Hub tags point at the frozen bytes.
#    (CI reruns the --hub check on every manifests/ change and daily:
#    .github/workflows/verify-image-digests.yml)
python3 scripts/verify_image_digests.py --local --version v1.0
python3 scripts/verify_image_digests.py --hub --version v1.0
./scripts/pull_images.sh --dry-run
python3 scripts/verify_quarantine.py --repo <short>

# 6. Tag the data repo with the SAME version (from the release data checkout)
git -C <SWE-Milestone-data> tag v1.0 && git -C <SWE-Milestone-data> push origin v1.0
```

After release: bump `manifests/BENCHMARK_VERSION` (the single source of truth
read by code, scripts, and CI); other machines align via
`./scripts/pull_images.sh` (layer dedup makes it near-free) or step 2 above.
