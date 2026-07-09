# Releasing benchmark images v1.0 (operator runbook)

Policy (spec 2026-07-08): published version tags are immutable (never
overwrite, never delete); unchanged content is RETAGGED, never rebuilt
(builds are not reproducible — a rebuild silently changes the arena).

Run on the machine that holds the v0.9 / :latest local images.

## 1. Inventory check — every manifest row has a local source image

    cd EvoClaw
    python3 -m harness.e2e.image_version retag-plan --version v1.0 \
        --from-version v0.9 --base-offline-from latest |
    while IFS=$'\t' read -r old new; do
        docker image inspect "$old" >/dev/null 2>&1 || echo "MISSING  $old"
    done

Expected: no output. Any MISSING line must be resolved first
(re-pull v0.9 via the OLD script from git history, or rebuild base-offline
via scripts/build_offline_closure.py — the ONLY images that may be built).

## 2. Retag (old scheme, old version) -> (new scheme, v1.0)

Content unchanged => digests unchanged. Pure pointer operation.

    python3 -m harness.e2e.image_version retag-plan --version v1.0 \
        --from-version v0.9 --base-offline-from latest |
    while IFS=$'\t' read -r old new; do docker tag "$old" "$new" || exit 1; done

Do NOT `docker rmi` the old-format tags yet: stopped pre-v1.0 trials
resume with the recorded old names (spec §8).

## 3. Push everything (115 images)

    docker login
    ./scripts/push_images.sh            # exits non-zero if anything failed

## 4. Freeze digests into the manifest (binding layer)

    python3 -m harness.e2e.image_version push-plan --version v1.0 |
    while IFS=$'\t' read -r local hub; do
        printf '%s\t%s\n' "$local" \
            "$(docker image inspect --format '{{index .RepoDigests 0}}' "$hub" 2>/dev/null || echo PUSH-FIRST)"
    done > manifests/digests-v1.0.tsv

Commit `manifests/digests-v1.0.tsv`. A future v1.x diffs against this file
to prove which images actually changed.

## 5. Verify from a consumer's seat

    ./scripts/pull_images.sh --dry-run                  # plan sanity
    ./scripts/pull_images.sh --repo navidrome           # real pull, 1 repo
    python3 scripts/verify_quarantine.py --repo go-zero # quarantine smoke
    # then one real milestone eval via scripts/run_all.py on a spare machine

## 6. After release

- v0.9 hub images stay frozen under the old naming. No compat code reads them.
- DEFAULT_IMAGE_TAG is already v1.0 in code; machines still on v0.9-only
  local images will fail fast (explicit pin) or loudly fall back (default) —
  both intended. Run step 2 on each eval machine, or pull fresh.
