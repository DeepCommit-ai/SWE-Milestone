# Secure Evaluation — preventing target-source "cheating"

EvoClaw drops an agent into a repo at version **A** and asks it to implement the
changes that turn it into version **B** (the milestones). The benchmark is only
valid if the agent *writes* those changes. If it can instead **fetch version B's
own source** (the "answer") and copy it, the score is meaningless.

This document is the threat model + the general, ecosystem-agnostic defense, and
how it's wired in EvoClaw.

## What we observed (2026-05-30 audit, claude-code / Opus 4.8)

Auditing the 7-repo trace, the network whitelist (which blocks github/gitlab)
**held** — no agent reached a code-hosting site. But the agent still actively
sought the answer source, and one path **succeeded**:

| repo | ecosystem | outcome |
|---|---|---|
| **scikit-learn** | pip / PyPI | 🔴 **cheated** — `pip download scikit-learn==1.6.0`, extracted ~100 source/test files, diffed & copied (byte-for-byte matches) |
| ripgrep | cargo / crates.io | 🟠 attempted — `cargo add grep-searcher@0.1.16` (a workspace sub-crate at the target version) succeeded; that version didn't contain the fix, so no usable copy |
| navidrome, go-zero | Go / goproxy | 🟠 path open — `go get <self-module>@<target>` would fetch the answer (agent tried github via WebFetch instead; blocked) |
| dubbo | Maven | 🟠 path open — `*-3.3.6-sources.jar` reachable via repo1.maven.org |
| nushell | cargo | 🟠 path open — `nu-*` sub-crates at 0.108.0 |
| element-web | npm | 🟢 app source not published to npm |

**Key lesson: the vulnerability is not "PyPI".** It is *any whitelisted package
registry that serves the repo-under-test's own package*. Every ecosystem has one
(PyPI, crates.io, goproxy, Maven Central, npm). PyPI was simply the first an agent
exploited end-to-end.

## Why a domain blocklist / IP filter is not enough

Two structural reasons "just block the self-package's URL" fails:

1. **Sub-packages & aliases.** A project ships a *family*: ripgrep →
   `grep-searcher`, `grep-matcher`, `globset`, `ignore`; nushell → `nu-*`; Python
   namespace packages; Maven multi-module. Blocklisting the top-level name misses
   the siblings (we saw exactly this with `grep-searcher`). A denylist is
   open-ended — miss one entry and it leaks.
2. **Shared-CDN IPs.** PyPI/pythonhosted are fronted by **Fastly**
   (`151.101.0.0/16`), which EvoClaw's `CDN_CIDR_RANGES` accepts wholesale (to
   survive CDN IP rotation on long trials). You cannot drop "pythonhosted's IP"
   without dropping every other Fastly-fronted site, and pythonhosted rotates
   within the range anyway. IP-level surgery can't cleanly target one site.

   (Empirically: with `PIP_NO_INDEX=1` blocking pip, a raw
   `curl https://files.pythonhosted.org/.../scikit_learn-1.6.0.tar.gz` still
   pulled the full 7 MB sdist, because Fastly's CIDR was accepted.)

## The defense: default-deny + per-repo dependency closure (allowlist)

Invert the model. Instead of "open registry **minus** the self-package", give the
agent **only** its repo's third-party dependency closure as a *local* package
index, and **block every real registry at the network layer**.

Why this is *structurally* safe, not just stronger:

- **Self-exclusion by construction.** A project's dependency closure never
  contains the project itself — nor its own sub-packages/sub-crates/sub-modules
  (those are `path`/`vendor`/`workspace` deps, built from the local source, never
  downloaded). So the answer — main package, **all** sub-packages, **all**
  versions, **any** alias — is excluded automatically. Nothing to enumerate,
  nothing to miss. This is the property you want: it can't be defeated by a name
  you forgot.
- **Closed, computable set.** The closure is the lockfile / resolved dependency
  graph — finite and reproducible — versus a denylist's open-ended enumeration.
- **One principle, every ecosystem.**

| ecosystem | provide closure as | block (network) | force-offline |
|---|---|---|---|
| Python (pip) | local wheelhouse (`pip download -r`) | PyPI + Fastly CIDR | `PIP_NO_INDEX=1`, `PIP_FIND_LINKS=/wheelhouse` |
| Rust (cargo) | `cargo vendor` dir | crates.io + its CDN | `.cargo/config.toml` `[source.crates-io] replace-with="vendored"` |
| Go | `go mod vendor` or pre-filled module cache | proxy.golang.org/goproxy + CDN | `GOFLAGS=-mod=vendor` or `GOPROXY=off` |
| Java (Maven) | local `.m2` repo | repo1.maven.org + CDN | `-o` (offline) / `settings.xml` mirror→local |
| npm | `npm ci` populated / local registry | registry.npmjs.org + CDN | `--offline` / `.npmrc` registry→local |

In every case the repo's own package is a `path`/`vendor`/`workspace` entry, so
it is never in the provisioned index — same guarantee across languages.

## How it's wired in EvoClaw

**Per-repo config (configure once).** A repo's quarantine policy is
**repo-intrinsic** (which registry serves *that* repo's answer), so it lives once
in `quarantine_configs/<repo>.yaml` and is **auto-on**: `run_all.py` applies it to
that repo's container whenever the file exists — no trial-config flag. To run an
unprotected baseline for a repo, move its file aside. Policy fields:

```yaml
# quarantine_configs/scikit-learn_scikit-learn_1.5.2_1.6.0.yaml
deny_domains:   [pypi.org, files.pythonhosted.org]
deny_cidrs:     [151.101.0.0/16, 146.75.0.0/16]    # Fastly fronts PyPI
pip_wheelhouse: /host/path/to/wheelhouse            # host-specific
wheelhouse_forbid: [scikit-learn, scikit_learn, sklearn]
```

`load_quarantine_env()` in `run_all.py` reads that file and passes the policy to
**only that repo's** worker via the env vars below, consumed by
`harness/e2e/container_setup.py` and the shared agent base
(`harness/e2e/agents/base.py` — so every agent, not just claude-code, gets the
offline wheelhouse). Three layers, defense-in-depth (any one alone is
insufficient — see "Why … not enough"):

1. **Network (the real block).** Make the registry unreachable at the IP layer.
   `EVOCLAW_DENY_DOMAINS` and `EVOCLAW_DENY_CIDRS` are applied in **two** places
   inside `_resolve_whitelisted_ips()` + the CIDR-accept loop:
   - `EVOCLAW_DENY_DOMAINS=pypi.org,files.pythonhosted.org` — these domains are
     skipped when resolving the whitelist to IPs.
   - `EVOCLAW_DENY_CIDRS=151.101.0.0/16,146.75.0.0/16` — applied **twice**:
     (a) the matching entries are removed from `CDN_CIDR_RANGES` accepts, AND
     (b) **any IP resolved from any *other* whitelisted domain that falls inside
     a denied CIDR is pruned** (see the gotcha below — this second prune is what
     actually closes the hole).
   - The LLM/auth endpoints and their CIDRs stay (Vertex = Google ranges, not
     Fastly), so the agent still reaches the model.

   > **⚠ Gotcha that bit us (the reason (b) exists).** Blocking the registry
   > *domain* and removing the Fastly `/16` from `CDN_CIDR_RANGES` is **not
   > enough**. Other *allowed* domains are **also Fastly-fronted** — notably
   > `deb.debian.org` (apt) — and resolve to individual IPs **inside**
   > `151.101.0.0/16`, the *same* anycast IPs PyPI/pythonhosted use. Those land
   > in the allowlist as `/32` ACCEPTs via normal domain resolution, and the
   > agent can then `curl --resolve files.pythonhosted.org:443:<that-IP>` (or
   > just hit it directly — Fastly routes by SNI) and **download the answer
   > anyway**. We confirmed this empirically: with only domain-deny + CDN-`/16`
   > removal, a raw `curl` of the exact `scikit_learn-1.6.0.tar.gz` URL still
   > pulled all 7,074,944 bytes. The fix is prune-by-CIDR over *all* resolved
   > IPs (b). After it: that same curl returns `HTTP=000 bytes=0`, github stays
   > CONNFAIL, and legit wheelhouse installs + Vertex still work.
   >
   > Practical implication: cutting Fastly also cuts `deb.debian.org`, so
   > **`apt-get` breaks** under quarantine. Fine when the eval image is
   > pre-provisioned (toolchain baked in); provision any needed system package
   > into the image rather than reopening the range.
2. **Package manager.** Force the manager offline against the local closure:
   `EVOCLAW_PIP_WHEELHOUSE=/path/to/wheelhouse` → mounts it read-only at
   `/wheelhouse` and sets `PIP_NO_INDEX=1` + `PIP_FIND_LINKS=/wheelhouse`.
3. **(existing) /etc/hosts poisoning + github block** — unchanged.

### Building the closure (pip example)

Use the committed, fail-closed builder `scripts/build_quarantine_wheelhouse.py`. Run
it in the **clean base image** (so the freeze list is authoritative and the
editable self-install is excluded):

```bash
docker run --rm -v /path/wheelhouse:/wh <repo>/base:latest \
  python3 /path/to/scripts/build_quarantine_wheelhouse.py \
    --out /wh --forbid scikit-learn scikit_learn sklearn
```

The clean image reports scikit-learn as `-e /testbed` (editable, dev version),
**not** a PyPI pin — so it is absent from the `==`-pinned closure by construction.
The builder additionally runs a **fail-closed post-audit**: if any forbidden
artifact (the repo's own package, any version/alias) reaches the wheelhouse it is
deleted and the build exits non-zero.

This is enforced a second time at trial startup: the `wheelhouse_forbid` list in
`quarantine_configs/<repo>.yaml` makes `run_all.py` refuse to launch
(`_assert_wheelhouse_excludes`) if the wheelhouse contains a forbidden artifact —
so a tampered/un-audited wheelhouse is caught even if the builder was bypassed.

## Verification protocol (run before trusting a secure re-run)

Inside the locked container, all must hold:

```
curl github.com                  → CONNFAIL   (baseline: code-hosting blocked)
curl files.pythonhosted.org      → CONNFAIL   (registry CDN blocked)
curl pypi.org/simple/            → CONNFAIL
pip download <self>==<target>    → No matching distribution  (manager offline)
pip download <a-real-dependency> → Successfully downloaded    (wheelhouse works)
curl aiplatform.googleapis.com   → reachable  (LLM endpoint preserved)
```

Plus an **end-to-end attack**: resolve the target sdist URL via the PyPI JSON API
and `curl` it — it must fail to connect (this is the exact hole that the
pip-only block missed).

## Residual caveats (be honest about scope)

- **Training data.** These are public repos; version B may be in the model's
  weights. A perfect network block prevents *fetching*, not *remembering*. This is
  inherent to any public-repo benchmark, not a harness hole — "secure" here means
  *against network acquisition*.
- **apt / system packages.** `deb.debian.org` is also Fastly-fronted, so cutting
  the Fastly CIDR breaks `apt-get`. Acceptable when the eval image is
  pre-provisioned (toolchain baked in). If a milestone needs a system package,
  provision it in the image rather than reopening the CDN.
- **Missing-dependency friction.** If a milestone legitimately needs a dep outside
  the provisioned closure, the install fails (visible as `No matching distribution`
  / `Could not find` in the trace) — add it to the closure and re-run. Detectable,
  never a silent leak.

## Status

- pip / scikit-learn: implemented + verified (first instance of this design).
- Other ecosystems (cargo/Go/Maven/npm): design specified above; not yet wired.
  When you quarantine those repos, add the per-ecosystem offline switch from the
  table and extend the verification protocol with that manager's download command.
