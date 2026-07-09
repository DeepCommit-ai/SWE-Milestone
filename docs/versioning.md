# Versioning

One axiom: **any change that could move a score bumps the benchmark version;
anything else doesn't.** A version number is a comparability contract —
same tasks, same environments, same grading.

## Two axes

| Axis | Format | Covers |
|---|---|---|
| **Benchmark data version** | `vX.Y` — the image tag (`SWE_MILESTONE_IMAGE_TAG`, default in `harness/e2e/image_version.py`) | tasks, tests, image environments, **grading semantics** |
| **Harness version** | git commits / tags | everything score-neutral (refactors, logging, agent integrations) |

Images have no version identity of their own — their tag *is* the benchmark
version. The binding layer is `manifests/images-<v>.tsv` (inventory; drives
pull/push plans) + `manifests/digests-<v>.tsv` (content freeze — diff two
versions to prove exactly which images changed). Digests and commit SHAs are
identity; tags are labels.

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

# 5. Verify: plan sanity, spot-check registry digests, smoke one eval
./scripts/pull_images.sh --dry-run
docker manifest inspect <hub_ref>        # digest must equal the frozen value
python3 scripts/verify_quarantine.py --repo <short>
```

After release: bump `DEFAULT_IMAGE_TAG` in `image_version.py` (if not already
done with the code change); other machines align via `./scripts/pull_images.sh`
(layer dedup makes it near-free) or step 2 above.
