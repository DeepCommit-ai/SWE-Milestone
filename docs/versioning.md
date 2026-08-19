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
| Anything that could touch a score — tasks, tests, image environments, grading logic, spec/SRS text | **minor: increment the last digit** (`v1.0` → `v1.0.1` → `v1.0.2`) |
| Anything score-neutral — refactors, logging, agent integrations, monitoring, docs | harness only, no benchmark bump |
| A larger jump (`v1.1`, `v2.0`) | **only when the maintainer says so** |

### The rule: minor by default, by decree

The version is set **by decision, not by inference**:

> **Every score-touching release is a minor bump — the last digit — unless the
> maintainer explicitly calls for a bigger one.** This is a standing rule, not a
> judgement call to be re-derived per change.

A minor release may bundle whatever the maintainer bundles: an environment
repair, a config change, an SRS correction, or all three. There is **no
per-change test that decides the version number** — earlier revisions of this
document tried to derive it from whether re-evaluation could back-apply a
change, and that framing is retired. Deriving version numbers from a mechanism
is a judgement call made per change, under time pressure, by whoever is doing
the work; it drifts and it invites arguments. A standing default does not.

The mechanism question still matters, but only as **analysis**: it tells you
what work a change implies (what re-scoring can recover, what needs re-runs) —
see the impact analysis below. It does not name the release.

Release notes describe what went into a release; they are not a consistency
proof, and a release that bundles unrelated work is normal.

### Impact analysis: required on every score-touching change

A version bump is only half the obligation. The other half is knowing what it
did to results that already exist. Every score-touching change — patch or minor
— carries this procedure, and it ends at the maintainer's desk, not in an
automated update:

1. **Scan `SWE-Milestone-log` for trials that could be affected.** Derive the
   set from evidence, not reasoning: search the published corpus for the failure
   signature the change removes (or the tests/milestones it touches). Two
   completeness rules learned the hard way:
   - **Signature ∪ no-result.** A signature sweep is blind to cells whose
     evaluation failed before producing any test output — there is nothing to
     grep. The scope is *cells carrying the signature* **UNION** *cells with no
     valid evaluation result on the affected milestones*.
   - **Verify the exclusions.** Check the cells you plan to leave out, and say
     what you checked. "We believe the sweep was complete" is not a finding;
     "every excluded cell on the affected milestones was inspected and carries
     no relevant signature" is.
2. **Report what syncing would involve** — which cells, which artifacts, what it
   costs, and crucially what re-evaluation *cannot* recover (spec changes need
   agent re-runs; environment repairs do not).
3. **The maintainer decides whether to update scores.** Do not promote, do not
   refresh the leaderboard, do not "just fix" the affected rows. The analysis is
   a decision document; the decision is human. Published rows that are not
   updated stay labeled with the version that produced them.

Worked example — the nushell frozen-API-contract fix (2026-08-18). Three
milestones' SRS left a public signature under-specified; agents that guessed
wrong had 8-10 of 13 cells score zero on a compile failure. Same models, same
images, same grading: **14.0% before, ~56% after**. Re-evaluation recovers
nothing here — the agent's decision is frozen in the submission. It shipped in
the ordinary minor release `v1.0.1`; recovering those trials would require
re-running the agents, which the impact analysis reports and the maintainer
decides on.

Worked example (the other shape) — the element-web `html-react-parser` env
repair (2026-08-18). Eval images lacked a dependency the SRS declared and the
GT tests required, so correct solutions scored zero. Re-evaluation *can*
back-apply it (agents were never re-run; frozen submissions were re-scored
against repaired images), and the impact analysis found the movement confined
to 4 milestones / 15 cells / 6 trials, +1.70 to +5.97 element-repo points on the
board metric, `resolved` unchanged everywhere. It shipped in `v1.0.1` — an
ordinary minor release, bundled with the dubbo prune-config and nushell SRS
work that landed in the same window. The back-application property made the
*work* cheap; it had no bearing on the version number.

Patch releases (`vX.Y.Z`) — repairing published images, rc-tag campaigns,
and the promotion runbook that reuses steps 3-5 below with carried-over
name sets — are specified in **[image-patching.md](image-patching.md)**.
Their core invariant: the released image is digest-identical to what the
re-evaluation campaign scored ("evaluate-then-christen").

### Displaying the version: minor in public, patch for reproduction

The version has two audiences, and they need different precision. The public
presentation therefore shows the **minor** level only:

> **The website badge reads `v1.0`, never `v1.0.1`.** The patch level is not
> hidden — it lives in the data (trial metadata, digest manifests, release
> notes) where anyone reproducing a number will find it.

This is deliberate, and it follows from what the two levels mean:

- **Minor is the comparability class.** A patch is, by definition, an
  explicitly declared assertion that old and new scores sit on one scale
  (see the rule above — patches exist only by decree, precisely because that
  assertion has to be made consciously). So everything inside `v1.0.x` is one
  comparable set, and `v1.0` is the honest name for that set. A first-time
  reader comparing a `v1.0.1` score against a `v1.0` score is doing something
  the version system supports.
- **Patch is the reproduction coordinate.** Someone re-running a cell needs to
  know exactly which image bytes produced a number — that is what
  `manifests/digests-v1.0.1.tsv`, the data-repo tag, and the release notes are
  for. Surfacing it in a headline badge would add precision the casual reader
  cannot use and does not need, while implying a discontinuity that does not
  exist.

Consequence for tooling: **do not "fix" the badge to render the full version.**
If the badge is ever derived from `manifests/BENCHMARK_VERSION` instead of being
written by hand, it must be truncated to `major.minor` — that keeps the
single-source-of-truth property without leaking the patch level into the public
comparability statement.

### One label, three repos

A bump is never "just tag the data repo". The same `vX.Y` label is applied to
all three, and the launcher resolves them through **one** knob
(`expected_benchmark_version()` — `SWE_MILESTONE_IMAGE_TAG`, defaulting to
`manifests/BENCHMARK_VERSION`):

| Repo | Carries the tag? | Why |
|---|---|---|
| Images | **Yes — every row of the digest manifest** | Images have no identity of their own; their tag *is* the version. Whatever the change was, every repo's image must exist under the new label or that repo cannot launch. |
| `SWE-Milestone-data` | **Yes** | Same knob derives the expected data tag; the launcher fact-checks HEAD against it. |
| `SWE-Milestone` (harness) | **Yes, as a release marker** | `manifests/BENCHMARK_VERSION` lives here, so the bump is a harness commit anyway. The tag records *which harness commit shipped with vX.Y*. |

**A data-only change still retags every image.** Nothing about the images
changed — but `BENCHMARK_VERSION` drives image resolution for all seven repos,
so tagging only the data repo leaves the launcher looking for images that do not
exist. The carry-over is a pointer operation (`docker tag`) and pushes 0 bytes;
see image-patching.md §6 step 1. Its by-product is the cleanest possible
evidence: `diff manifests/digests-<old>.tsv manifests/digests-<new>.tsv` must
show **no digest changes at all**, proving the release touched no environment.

**The harness tag is a marker, not a lockstep.** Score-neutral harness work
(refactors, logging, agent integrations, monitoring) moves freely between
benchmark versions and never bumps one. Only the release commit is tagged. Do
not read "harness has no tag for vX.Y.Z" as a version inconsistency.

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
