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
# quarantine_configs/scikit-learn_scikit-learn_1.5.2_1.6.0.yaml  (pip)
ecosystem:      [pip]                                # used by the coverage gate
deny_domains:   [pypi.org, files.pythonhosted.org]
deny_cidrs:     [151.101.0.0/16, 146.75.0.0/16]    # Fastly fronts PyPI
pip_wheelhouse: ${EVOCLAW_WHEELHOUSE_DIR}/sk_wheelhouse   # expanded from .env_private
wheelhouse_forbid: [scikit-learn, scikit_learn, sklearn]
verify_fetch_urls: [https://pypi.org/simple/scikit-learn/]   # must fail to connect
```

```yaml
# quarantine_configs/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml  (maven)
ecosystem:        [maven]
deny_domains:     [repo1.maven.org, repo.maven.apache.org, central.sonatype.com]
deny_cidrs:       [104.16.0.0/12]                   # Cloudflare fronts Maven Central
maven_offline:    true                              # → MAVEN_ARGS=-o
maven_repo_local: /root/.m2/repository              # image's pre-baked .m2
cache_forbid_globs: ["/root/.m2/repository/org/apache/dubbo/*/3.3.[4-9]*"]
verify_fetch_urls: [".../dubbo-common-3.3.6-sources.jar"]
```

**Policy fields.** `ecosystem` (required — see the gate below). Network deny:
`deny_domains`, `deny_cidrs`. Offline closure, one per package manager:
`pip_wheelhouse` (+`wheelhouse_forbid`), `cargo_offline`, `go_offline`,
`maven_offline` (+`maven_repo_local`), `npm_offline`. Fail-closed audits run at
container lockdown: `cache_forbid_globs` (the image's pre-baked cache must not
ship the repo's own post-baseline artifacts) and `verify_fetch_urls` (the exact
observed cheat URLs — any successful TCP connect aborts the trial).

### Fail-closed coverage gate (issue #12)

Quarantine is **opt-in by file presence**, which is how 6 repos once ran open. To
make "silently unprotected" impossible, `run_all.py` runs a **coverage gate**
before launching: every repo must have a config that declares its `ecosystem`
and whose `deny_domains` covers **all** of that ecosystem's registries
(`harness/e2e/quarantine.py:ECOSYSTEM_REGISTRIES`). A missing/incomplete policy
**hard-fails the launch** (`Refusing to launch …`); `--unprotected` is the
explicit, logged escape hatch for an intentional open baseline. Use `none` as
the ecosystem for a repo with no answer-serving registry.

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
   - **CIDR-deny matching is by subnet overlap, not string equality.** The
     builtin `CDN_CIDR_RANGES` accepts Cloudflare as `104.16.0.0/13`; a Maven/npm
     policy denies `104.16.0.0/12`. String-equality skipping would leave the
     `/13` ACCEPT in place and the registry reachable — `cidr_overlaps_any()`
     drops any builtin range that overlaps a denied one.
   - **Go's residual.** `proxy.golang.org` / `sum.golang.org` / `golang.org` /
     `go.dev` ride Google ranges **shared with Vertex `aiplatform`**, so they
     cannot be IP-blocked without cutting the model path. Their defense is the
     manager-offline layer (`GOPROXY=off`); `verify_network_lockdown`
     auto-detects this (a denied domain whose resolved IPs all fall in a
     still-ACCEPTed CDN range) and downgrades it to a warning instead of a
     hard-fail. `goproxy.cn` (Qiniu) / `goproxy.io` (Cloudflare) / `pkg.go.dev`
     (GCLB) are IP-blockable and stay hard-asserted. The SNI egress proxy (long
     term, see `docs/quarantine-rollout.md`) eliminates this residual.
2. **Package manager (offline closure).** Force the manager at a local closure
   instead of the live registry. Wired in `harness/e2e/agents/base.py`
   (`get_quarantine_env_vars` / `get_quarantine_mounts`, shared by **all**
   agents). Per manager, set from the policy's offline switch:
   - **pip** — `pip_wheelhouse` mounts the host wheelhouse read-only at
     `/wheelhouse`, sets `PIP_NO_INDEX=1` + `PIP_FIND_LINKS=/wheelhouse`.
   - **cargo** — `cargo_offline: true` → `CARGO_NET_OFFLINE=true`.
   - **go** — `go_offline: true` → `GOPROXY=off` (also written into
     `/etc/environment` + `.bashrc` by `lock_network`, since shell profiles
     override a bare `docker -e`).
   - **maven** — `maven_offline: true` → `MAVEN_ARGS=-o`
     (`+ -Dmaven.repo.local=<maven_repo_local>`, redirecting Maven at the
     image's pre-baked `.m2` since the agent runs as `fakeroot`).
   - **npm** — `npm_offline: true` → `npm_config_offline=true`.

   For cargo/go/maven/npm the closure is the **cache pre-baked into the eval
   image** (verified present: cargo registry, `/go/pkg/mod`, `/root/.m2`,
   `node_modules`); the repo's own package is a path/vendor/workspace dep, never
   in that cache. `cache_forbid_globs` asserts this at lockdown.
3. **(existing) /etc/hosts poisoning + github block** — unchanged.

### Building the closure (pip example)

Use the unified offline-closure builder `scripts/build_offline_closure.py`. It
runs **inside** the repo's networked base image (so the freeze list is
authoritative and the editable self-install is excluded), bakes the result
directly into `base-offline:latest` at `/wheelhouse`, and runs a **fail-closed
post-audit** (`audit_wheelhouse_self_exclusion`): if any forbidden artifact (the
repo's own package, any version/alias) reaches the wheelhouse the build exits
non-zero.

```bash
# Build pip closure into base-offline image (B-aware — union of all milestone envs)
python scripts/build_offline_closure.py --repo scikit-learn_scikit-learn_1.5.2_1.6.0
```

The clean image reports scikit-learn as `-e /testbed` (editable, dev version),
**not** a PyPI pin — so it is absent from the `==`-pinned closure by
construction. Self-exclusion is audited at build time by
`audit_wheelhouse_self_exclusion` (driven by `wheelhouse_forbid` in the repo's
`quarantine_configs/<repo>.yaml`).

## The base-offline image (B-aware closure for cargo/go/maven/npm)

> **The gap this closes.** The eval base images pre-bake only the **A-version**
> dependency cache. Quarantine cuts the network *and* forces the package manager
> offline — so for a non-pip repo, an agent implementing A→B can't fetch the
> **legitimate** new deps B needs (a 2026-06-10 audit found this breaks the build
> for all 6 non-pip repos: ripgrep needs 81 new crates, dubbo 499 new Maven
> artifacts, go-zero 744 modules incl. the go-redis v8→v9 migration, etc.). These
> are normal public-registry deps, **not** cheat artifacts — but offline they're
> unreachable. pip avoided this because scikit's wheelhouse was already built
> from a B-capable env.

**The fix: a per-repo `base-offline:latest` image.** For cargo/go/maven/npm
quarantine repos, `image_for_repo()` (harness/e2e/quarantine.py) launches from
`<repo>/base-offline:latest` instead of `base:latest` — the same image **re-baked
with the A→B dependency closure** plus any bumped toolchain. `base:latest` is
never overwritten (fully reversible: just delete the offline tag). pip stays on
`base:latest` (its closure is the mounted host wheelhouse, not baked in).

**Self-exclusion still holds.** The repo's own package is a path / workspace /
reactor dep — built locally, never pulled from the registry — so it is absent
from the third-party closure by construction. Every offline image was audited to
contain **none** of the repo's own post-baseline artifacts (the same property
`cache_forbid_globs` asserts at runtime). So baking B's deps does **not** leak the
answer: the agent gets `regex`/`netty`/`grpc`/`html-react-parser`, never the
repo's own B source.

### Building a base-offline image (per ecosystem)

Run in a **networked** container started from `base:latest`; collect every
milestone image's lockfile (the union covers any A→B path the agent may take);
pull the closure online; **verify offline**; audit self-exclusion; then
`docker commit` to `base-offline:latest`. Never `mvn install` (it would bake the
repo's own artifact into `.m2`). Per-ecosystem pull + offline-verify commands:

| ecosystem | pull closure (online) | offline verify (gate) | toolchain bump |
|---|---|---|---|
| cargo | `cargo fetch --locked` per milestone lock | `cargo build --offline` exit 0 (agent re-resolves, so `--offline build` is the faithful gate, not `--locked`) | nushell 1.86→**1.88** (`rustup toolchain install`, driven by `rust-toolchain.toml`); ripgrep none |
| go | `GOFLAGS=-mod=mod GOPROXY=… go mod download all` per milestone | `GOPROXY=off go mod download all` + `go build ./...` zero cache misses | go-zero 1.19→**1.21.13**, navidrome 1.24.4→**1.24.5** (replace `/usr/local/go`, SHA256-checked) |
| maven | `mvn dependency:go-offline` (+ `spotless:check`, `dependency:get` for dynamic/classified/test-scope artifacts go-offline misses) | `mvn -o test-compile` after `git clean -xfd` → BUILD SUCCESS, zero "Cannot access … offline" | dubbo none |
| npm | `yarn install` per distinct milestone lock | `rm -rf node_modules && yarn install --offline` clean; full `yarn build` offline | element-web none |

Toolchains aren't in the dependency closure, so a version bump (e.g. nushell's
`rust-toolchain.toml` 1.88, go-zero's `go 1.21` directive) must be **pre-installed**
in the offline image — it can't be fetched offline. The bumped tool lives at the
same path the base PATH already points to, so it's transparent to the harness
(which invokes `go test`/`cargo test`/`mvn test` via `sh -c`, inheriting the image
PATH — not a login shell).

## Verification protocol (run before trusting a secure re-run)

Most of this is now **automated**. `verify_network_lockdown` (run at every
container lockdown) asserts the denied hosts and every `verify_fetch_urls` entry
are unreachable and audits `cache_forbid_globs`. Probes use **python3, not curl**
(`_url_reachable_in_container` — a single IPv4 TCP connect), so they are accurate
even on images that ship no curl (e.g. the scikit base, where the old curl probes
silently passed). `scripts/verify_quarantine.py --repo <name>` spins up a repo's
real base image, applies the policy + lockdown, and additionally checks the LLM
endpoints stay reachable and the offline switches are present:

```
python scripts/verify_quarantine.py --repo dubbo      # → ALL PASS
```

Conceptually, inside the locked container all must hold:

```
github.com / <registry CDN>      → CONNFAIL   (code hosting + registry blocked)
<verify_fetch_urls entries>      → CONNFAIL   (exact observed cheat URLs)
<pm> install <self>@<target>     → fails      (manager offline, e.g. "No matching distribution")
<pm> install <a-real-dependency> → succeeds   (offline closure works)
aiplatform.googleapis.com        → reachable  (LLM endpoint preserved)
```

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

**All 7 benchmark repos covered** (issue #12), each with a
`quarantine_configs/<repo>.yaml` and live-smoke-tested via
`scripts/verify_quarantine.py` (ALL PASS):

| repo | ecosystem | network deny | offline closure | offline image |
|---|---|---|---|---|
| scikit-learn | pip | PyPI + Fastly `/16`s | host wheelhouse | base:latest (wheelhouse mounted) |
| ripgrep | cargo | crates.io + Fastly `/16`s | `CARGO_NET_OFFLINE` + baked closure | base-offline (+81 crates) |
| nushell | cargo | crates.io + Fastly `/16`s | `CARGO_NET_OFFLINE` + baked closure | base-offline (+820 crates, rust 1.88) |
| dubbo | maven | Maven Central + Cloudflare `/12` | `mvn -o` + baked `.m2` | base-offline (+499 artifacts) |
| go-zero | go | goproxy.cn/io + proxy domain-deny | `GOPROXY=off` + baked modcache | base-offline (744 modules, go 1.21.13) |
| navidrome | go + npm | (go set) + npm registry + Cloudflare `/12` | `GOPROXY=off` + `npm_config_offline` | base-offline (+19 modules, go 1.24.5) |
| element-web | npm | npm registry + Cloudflare `/12` | `npm_config_offline` | base-offline (+168 cache entries) |

Implemented since the original pip design: per-manager offline switches
(`harness/e2e/agents/base.py`), subnet-overlap CIDR deny, the Google-shared
auto-exemption, the fail-closed coverage gate + `--unprotected`
(`scripts/run_all.py`), curl-independent verification probes,
`cache_forbid_globs` / `verify_fetch_urls` audits, and — **the part that makes
offline trials actually buildable** — a per-repo **`base-offline:latest`** image
baked with the A→B dependency closure + bumped toolchains (image_for_repo;
`base:latest` untouched). All 6 non-pip images built + offline-verified +
self-exclusion-audited 2026-06-10; scikit uses its host wheelhouse.

**One pip edge note:** rebuilding scikit-learn from scratch with the default
`pip install -e .` (build isolation **on**) wants `patchelf`, absent from the
wheelhouse. Not a blocker — the image ships scikit-learn already editable-built,
and `--no-build-isolation` rebuilds offline fine; only matters if an agent does a
clean isolated rebuild.

**Remaining residual:** `proxy.golang.org` (Google range shared with Vertex) is
defended only at the manager layer (`GOPROXY=off`), not the firewall. The
**SNI-filtering egress proxy** in `docs/quarantine-rollout.md` is the definitive
fix that closes it (and the whole shared-CDN class).
