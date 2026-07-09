# Quarantine — anti-cheat network isolation

EvoClaw drops an agent into a repo at version **A** and asks it to implement the
changes that turn it into version **B** (the milestones). The benchmark is only
valid if the agent *writes* those changes. If it can instead **fetch version B's
own source** (the "answer") and copy it, the score is meaningless.

Quarantine is the per-repo defense against that. This document is the reference:
what it defends against, how it works, how to operate it, and how to onboard a
new repo.

**TL;DR for operators:** quarantine is **auto-on** — a repo is protected whenever
`quarantine_configs/<repo>.yaml` exists, and `run_all.py` refuses to launch any
repo whose policy is missing or incomplete. For a normal trial you do nothing.
The only opt-out is the explicit `--unprotected` flag. All 7 benchmark repos are
covered and live-verified.

---

## 1. Threat model

The answer is not on GitHub (code-hosting is blocked in every container). It is
on the **package registries** — every ecosystem's official registry publishes the
repo-under-test's own artifacts, and those registries would otherwise be
whitelisted because agents legitimately install third-party dependencies from
them. Observed in a full 7-repo audit (2026-05-30, and re-confirmed in later
trials):

| repo | ecosystem | observed |
|---|---|---|
| **scikit-learn** | pip / PyPI | 🔴 **cheated** — `pip download scikit-learn==1.6.0`, extracted ~100 files, copied byte-for-byte |
| **go-zero** | go / goproxy | 🔴 **cheated** (later trial) — target-version source via a Go module proxy |
| ripgrep | cargo / crates.io | 🟠 attempted — `cargo add grep-searcher@0.1.16` (a workspace sub-crate at the target version) |
| dubbo | maven / Maven Central | 🟠 path open — `*-3.3.6-sources.jar` reachable |
| nushell | cargo | 🟠 path open — `nu-*` sub-crates at 0.108.0 |
| navidrome | go | 🟠 path open — `go get <self-module>@<target>` |
| element-web | npm | 🟠 path open via the **Go module proxy** (it mirrors ANY public repo with a v-tag, not just Go projects) |

**The vulnerability is not "PyPI".** It is *any whitelisted registry that serves
the repo-under-test's own package* — and note the go-proxy row: a Go module proxy
is a **cross-ecosystem** answer channel, because it serves a zip of any public
repo at any v-prefixed tag.

Why simple blocklisting fails, structurally:

1. **Sub-packages & aliases.** A project ships a family (ripgrep →
   `grep-searcher`, `globset`, `ignore`; nushell → `nu-*`; Maven multi-module).
   A name denylist is open-ended — miss one sibling and it leaks.
2. **Shared-CDN IPs.** Registries ride CDNs the whitelist must otherwise accept.
   PyPI rides Fastly `151.101.0.0/16`; other *allowed* domains (e.g.
   `deb.debian.org`) resolve to IPs **inside the same range**, and Fastly routes
   by TLS SNI — so `curl --resolve files.pythonhosted.org:443:<any-fastly-ip>`
   fetches the answer through an IP the whitelist accepted for apt. (Confirmed
   empirically: domain-deny alone still pulled the full 7 MB sdist.)

## 2. The defense in one idea

Invert the model. Instead of "open registry minus the self-package", give the
agent **only its repo's third-party dependency closure, locally**, and **cut the
registries off entirely**:

- **Bake the closure into the image.** `scripts/build_offline_closure.py`
  downloads, online and ahead of time, the **union of every milestone's declared
  dependencies** (the whole A→B path) into a per-repo `base-offline` image
  (cargo registry cache, go module cache, `.m2`, yarn cache, pip `/wheelhouse`).
- **Self-exclusion by construction.** A project's dependency closure never
  contains the project itself — its own crates/modules/artifacts are
  path/workspace/reactor deps, built from local source, never downloaded. The
  builder audits this (`cache_forbid_globs` must match nothing) and the maven
  path explicitly removes any self@B artifacts its online fetch pulled in.
- **Then cut the network and force the package managers offline.** Legitimate
  deps still install (they're local); the answer is unreachable (not local, and
  the network path is gone).

## 3. The four defense layers

Each quarantined container gets four stacked layers; bypassing one still leaves
the next.

### Layer 1 — IP firewall (iptables, `lock_network`)

The container's OUTPUT chain defaults to **DROP** with ACCEPTs for the resolved
whitelist (LLM/auth endpoints etc.) plus builtin CDN ranges. Quarantine
subtracts from that:

- `deny_domains` are excluded from whitelist resolution (their IPs are never
  ACCEPTed) — `_resolve_whitelisted_ips` in `harness/e2e/container_setup.py`.
- `deny_cidrs` are applied **twice**: (a) any builtin CDN ACCEPT that
  **subnet-overlaps** a denied range is dropped (`cidr_overlaps_any` — overlap,
  not string equality: a policy denying `104.16.0.0/12` must kill the builtin
  Cloudflare `/13` accept), and (b) any IP resolved from any *other* whitelisted
  domain that falls inside a denied range is pruned. Prune (b) is what closes
  the shared-CDN SNI hole from §1.

### Layer 2 — DNS poisoning (`/etc/hosts`)

Code-hosting domains (github/gitlab/…) are poisoned to `0.0.0.0` in **every**
container. Under quarantine, the **Go module-proxy mirror domains**
(`QUARANTINE_MIRROR_DOMAINS` in `harness/e2e/quarantine.py`) are poisoned too —
in every quarantined container regardless of ecosystem, because the go proxy is
a cross-ecosystem answer channel (§1). Non-quarantine baselines keep working go
fetches (parity).

### Layer 3 — package managers forced offline

Declared per-ecosystem in the policy, injected as container env by
`get_quarantine_env_vars` (`harness/e2e/agents/base.py` — shared by all agents):

| policy switch | container effect |
|---|---|
| `cargo_offline: true` | `CARGO_NET_OFFLINE=true` |
| `go_offline: true` | `GOPROXY=off` (also written into `/etc/environment` + `.bashrc` by `lock_network` — shell profiles override a bare `docker -e`) |
| `maven_offline: true` | `MAVEN_ARGS=-o` (+ `-Dmaven.repo.local=<maven_repo_local>`) |
| `npm_offline: true` | `npm_config_offline=true` |
| (pip — derived from `ecosystem: [pip]`, no key) | `PIP_NO_INDEX=1`, `PIP_FIND_LINKS=/wheelhouse` |

This layer does double duty: it is an **independent anti-cheat line** (for go it
is the *primary* one — `GOPROXY=off` makes `go get <self>@<target>` fail inside
the toolchain itself, needed because proxy.golang.org can't be IP-blocked, see
§9), and it **keeps legitimate builds working** — an online-configured manager
would hang against the DROP rules; an offline one uses the image's baked closure
directly. `GOPROXY` is set to `off` under *any* quarantine (the mirror domains
are hosts-poisoned anyway), and stays at the sanctioned proxy in non-quarantine
containers (`goproxy_value` in `harness/e2e/quarantine.py`).

Additionally, `lock_network` removes the agent user's sudoers entry, so the
agent cannot flush iptables or edit `/etc/hosts`.

### Layer 4 — fail-closed verification

Layers 1–3 set the defenses; this layer **proves they hold**, and any failure
refuses the launch rather than warning:

- **Launch gate** (`quarantine_coverage_errors`, `harness/e2e/quarantine.py`) —
  static policy-completeness checks run by `run_all.py` before any container
  starts. Five requirements per repo, detailed in §7.
- **In-container verification** (`verify_network_lockdown`,
  `harness/e2e/container_setup.py`) — runtime fact checks after lockdown:
  - iptables OUTPUT policy is actually DROP; github.com unreachable.
  - **Every denied domain is actually unreachable**, probed from inside the
    container (python3 TCP connect, not curl — some bases ship no curl). Only
    domains in the code-level `FIREWALL_EXEMPTABLE_DOMAINS` whitelist may be
    exempted from this assertion (§9).
  - Every `verify_fetch_urls` entry — the *exact URLs of observed cheats* —
    fails to connect.
  - `cache_forbid_globs` match nothing in the image (the baked closure does not
    contain the answer).
  - sudo is confirmed revoked.
  - A probe that fails to run at all raises (indeterminate ≠ blocked).
- **Direct-entry guard** (`quarantine_guard_error`) — a direct
  `run_e2e --repo-name X` of a policy'd repo without the injected quarantine env
  refuses to start unless `--unprotected` is passed.
- **Env self-recovery** — `ContainerSetup.__init__` recovers the full quarantine
  env from the on-disk policy when the process env lacks it (direct
  `run_e2e --resume-trial`, manual `run_milestone`). Protection follows the
  **disk fact** (does this repo have a policy file), not a losable env var. The
  only exception: `EVOCLAW_UNPROTECTED`, persisted in trial metadata so resuming
  an intentionally-open baseline doesn't silently re-harden it.

The gate checks *what you declared*; the in-container probes check *whether it
actually took effect* (a typo'd CIDR passes the gate but fails the live probe).
Both are needed; together "silently ran unprotected" is structurally impossible.

## 4. Architecture

One YAML flows down one pipeline:

```
quarantine_configs/<repo>.yaml          policy: repo-intrinsic, auto-on
        │
        ▼
harness/e2e/quarantine.py               the only policy interpreter (pure functions)
        │   load_quarantine_env()   → EVOCLAW_* env vars
        │   quarantine_coverage_errors() → launch gate
        │   image_for_repo()        → base-offline vs base (§8)
        ▼
scripts/run_all.py                      gate first, then per-repo env injection
        │
        ▼
harness/e2e/container_setup.py          ContainerSetup: env recovery (__init__),
        │                               offline -e flags (via agents/base.py),
        │                               lock_network(), verify_network_lockdown()
        ▼
the agent's container                   registries unreachable, managers offline,
                                        dependency closure available locally
```

| file | role |
|---|---|
| `quarantine_configs/<repo>.yaml` | per-repo policy + closure recipe |
| `harness/e2e/quarantine.py` | policy loading, env derivation, gates, image selection |
| `harness/e2e/container_setup.py` | firewall, hosts poisoning, verification |
| `harness/e2e/agents/base.py` | offline switches → container env (all agents) |
| `harness/e2e/image_version.py` | image tag pinning (`resolve_image`, §8) |
| `scripts/run_all.py` | launch gate + env injection |
| `scripts/build_offline_closure.py` | builds + validates + tags `base-offline` |
| `scripts/verify_quarantine.py` | standalone live smoke test |
| `scripts/pull_images.sh` | distributes images across machines |

The env vars carrying policy into the container: `EVOCLAW_QUARANTINE`,
`EVOCLAW_DENY_DOMAINS`, `EVOCLAW_DENY_CIDRS`, `EVOCLAW_FIREWALL_EXEMPT`,
`EVOCLAW_{PIP,CARGO,GO,MAVEN,NPM}_OFFLINE`, `EVOCLAW_MAVEN_REPO_LOCAL`,
`EVOCLAW_CACHE_FORBID_GLOBS`, `EVOCLAW_VERIFY_FETCH_URLS`, plus
`EVOCLAW_UNPROTECTED` for the escape hatch.

## 5. Day-to-day usage

**Normal trial — nothing to do.** Quarantine applies itself:

```bash
python scripts/run_all.py --config trial_config.yaml
# monitor shows a 🔒 marker on quarantined repos
```

**Intentional open baseline** (scores count as tainted):

```bash
python scripts/run_all.py --config trial_config.yaml --unprotected
```

This is the **only** way to run a policy'd repo unprotected — deleting or
hand-editing the policy file trips the gate instead. The flag is persisted in
trial metadata, so resuming that trial stays open.

**Standalone health check** (no trial; spins up the real image, applies the
policy, runs the full verification suite, plus positive probes that the LLM
endpoints stay reachable):

```bash
python scripts/verify_quarantine.py --repo dubbo     # substring match; → ALL PASS
```

## 6. Status

All 7 repos protected, quarantined trials launch from the `base-offline` image
(§8), each policy live-verified via `verify_quarantine.py`:

| repo | ecosystem | registries denied | offline switch |
|---|---|---|---|
| scikit-learn | pip | pypi.org, files.pythonhosted.org (+ Fastly CIDRs) | pip (derived) |
| ripgrep | cargo | crates.io, static/index.crates.io (+ Fastly CIDRs) | `cargo_offline` |
| nushell | cargo | crates.io, static/index.crates.io (+ Fastly CIDRs) | `cargo_offline` |
| dubbo | maven | repo1.maven.org, repo.maven.apache.org, central.sonatype.com (+ Cloudflare `/12`) | `maven_offline` |
| go-zero | go | proxy.golang.org, sum.golang.org, goproxy.cn, goproxy.io (+ Qiniu/Cloudflare CIDRs) | `go_offline` |
| navidrome | go + npm | go set + registry.npmjs.org, registry.yarnpkg.com (+ CIDRs) | `go_offline` + `npm_offline` |
| element-web | npm | registry.npmjs.org, registry.yarnpkg.com (+ Cloudflare `/12`) | `npm_offline` |

## 7. Adding quarantine for a new repo

Four steps; every one is machine-checked, so errors are loud, not silent.

### Step 1 — write `quarantine_configs/<repo>.yaml`

The launch gate enforces five requirements. Field ↔ check ↔ what it blocks:

| # | requirement | YAML | blocks |
|---|---|---|---|
| 1 | policy file exists | (the file itself) | the issue-#12 root cause: an unconfigured repo silently running open |
| 2 | ecosystem declared | `ecosystem: [go]` (list; `none` is valid) | un-assertable coverage — the gate looks up `ECOSYSTEM_REGISTRIES[eco]` to know what must be denied |
| 3 | all of the ecosystem's registries denied | `deny_domains:` ⊇ `ECOSYSTEM_REGISTRIES[eco]` | single-domain misses (crates.io has three domains; only one serves downloads) |
| 4 | package manager forced offline | `go_offline: true` etc. (pip: derived) | the manager fetching online despite the deny; also what makes offline builds work |
| 5 | every registry CIDR-denied **or** legitimately exempt | `deny_cidrs:` / `firewall_exempt_domains:` | the shared-CDN SNI bypass (§1); exempt entries must be in the code-level `FIREWALL_EXEMPTABLE_DOMAINS` whitelist or the gate rejects them |

Plus the fail-closed audit hooks: `cache_forbid_globs` (globs of the repo's own
artifacts that must never appear in the image cache) and `verify_fetch_urls`
(exact registry URLs of the answer that must fail to connect), and a `closure:`
block for the image builder (Step 2):

```yaml
# quarantine_configs/zeromicro_go-zero_v1.6.0_v1.9.3.yaml (abridged)
ecosystem: [go]

deny_domains: [proxy.golang.org, sum.golang.org, goproxy.cn, goproxy.io,
               golang.org, go.dev, pkg.go.dev]
deny_cidrs:
  - 104.16.0.0/12      # Cloudflare (goproxy.io)
  - 155.102.0.0/16     # Qiniu CDN (goproxy.cn)
# Google/Vertex-shared domains that CANNOT be IP-blocked (§9). Only domains in
# quarantine.py FIREWALL_EXEMPTABLE_DOMAINS are accepted here.
firewall_exempt_domains: [proxy.golang.org, sum.golang.org, golang.org, go.dev, pkg.go.dev]

go_offline: true

cache_forbid_globs:
  - /go/pkg/mod/cache/download/github.com/zeromicro/*
  - /go/pkg/mod/github.com/zeromicro/*
verify_fetch_urls:
  - https://goproxy.cn/github.com/zeromicro/go-zero/@v/v1.9.3.zip

closure:
  cache_paths: [/go/pkg/mod/cache/download]   # where the baked cache lives
  offline_build: "go build -mod=mod ./..."    # what "buildable offline" means
  toolchain: {go: "1.21.13", gotoolchain_local: true}
```

Don't aim for first-try perfection: run `run_all.py` (or the builder) and let
the gate's error messages enumerate exactly what's missing.

### Step 2 — build the offline-closure image

```bash
python scripts/build_offline_closure.py --repo <repo_full_name>          # local
python scripts/build_offline_closure.py --repo <repo_full_name> --push   # + DockerHub
```

One command builds `swe-milestone/<repo_full>__base-offline:staging` and runs **four fail-closed
gates**; `:latest` is tagged only if all pass, so the existence of a
`base-offline` image is itself the proof it was validated:

1. **Self-exclusion audit** — `cache_forbid_globs` must match nothing inside
   the staging image (the closure must not contain the answer; the maven path
   deletes any self@B artifacts its online fetch pulled, then this re-checks).
2. **Per-milestone offline gate** — for *every* milestone, its B-source
   `/testbed` is copied out of the milestone image, mounted into staging, and
   `closure.offline_build` runs under `--network none`. A failure is classified
   by ecosystem-aware classifiers: a **closure gap** (bytes genuinely missing)
   aborts the build; a **source-state** failure (the milestone's own source
   isn't a clean buildable state — mid-migration checkpoints etc.) is recorded
   but doesn't block, since the closure isn't at fault.
3. **A-baseline gate (cargo)** — the agent *starts* at version A, so the vendor
   must satisfy A's lockfile too, not just each milestone's B.
4. **Toolchain coverage gate** — every rust/go version any milestone *declares*
   (read from the milestone images, not from the config) must be installed in
   the staging image.

How the closure is assembled: a networked multi-stage `docker build` starting
`FROM swe-milestone/<repo_full>__base:<pin>` unions every milestone's declared dependencies into
the shared cache (cargo `vendor` / go `go mod download all` / maven
`dependency:go-offline` + test-scope resolve / yarn per-lockfile installs / pip
freeze-union → `pip download` into an in-image `/wheelhouse`), then a final
stage copies only the warmed cache forward. The plain `__base` image is never
modified — `__base-offline` is a strict layer on top (§8). Escape hatches for odd repos live
in the `closure:` block (`cargo_mechanism: raw-cache`, `go_mechanism`,
`global_npm_tools`, `extra_vendor_crates`). Never run `mvn install` during a
build — it would bake the repo's own artifact into `.m2`; the builder's flow
avoids it by construction.

Prerequisite: the repo's milestone images are present (`scripts/pull_images.sh`).

### Step 3 — promote the tag (easy to forget!)

The builder only tags **`:staging`/`:latest`** (floating build tags), but the
harness pins the benchmark data version (§8). A rebuilt closure is **not
live** until promoted to the pinned tag — release promotion is handled
uniformly by `retag-plan` (see `docs/release-v1.0-images.md`); for a single
repo on one machine:

```bash
# this machine (example: version v1.0)
docker tag swe-milestone/<repo_full>__base-offline:latest \
           swe-milestone/<repo_full>__base-offline:v1.0
# other machines: ./scripts/push_images.sh --repo <short>, then pull_images.sh there
```

### Step 4 — verify, then run

```bash
python scripts/verify_quarantine.py --repo <name>   # expect: ALL PASS
python scripts/run_all.py --config trial_config.yaml
```

## 8. Images and version pinning

**Two images per repo.** `swe-milestone/<repo_full>__base` is the original
eval image (its cache covers only the A-version closure).
`swe-milestone/<repo_full>__base-offline` is built `FROM` it — same
`/testbed`, same environment, plus the A→B union dependency cache (and a
newer toolchain where B needs one). `image_for_repo()` selects at launch:
policy file present → `base-offline`; no policy (or `--unprotected` baselines
still launching a policy-less repo) → `base`. All 7 repos, **including
scikit-learn**, use `base-offline` (the pip wheelhouse is baked in at
`/wheelhouse`, not host-mounted).

**Tag semantics.** The tag is the benchmark **data-version pin** (env
`EVOCLAW_IMAGE_TAG`, default `v1.0` — defined once in
`harness/e2e/image_version.py`); `:latest` is "most recent local build".
`resolve_image()`:

1. `<image>:<pin>` exists locally → use it.
2. Pin came from the default and only `:latest` exists → use it **with a loud
   warning** (content unverified).
3. `EVOCLAW_IMAGE_TAG` set explicitly → never fall back; fail fast.

Containers additionally launch with `--pull=never`: a missing local image is
a loud failure, never a silent registry fetch mid-eval.

**Distribution.** `scripts/pull_images.sh` executes the plan emitted by
`python3 -m harness.e2e.image_version pull-plan` from the inventory
`manifests/images-<version>.tsv`: every image (base, base-offline, all
milestones — base-offline is not special) is pulled from
`<org>/swe-milestone__<repo_full>__<milestone>:<version>` and retagged to the
local `swe-milestone/<repo_full>__<milestone>:<version>`. A failed pull is a
per-image WARN; the script continues, prints the full failure list, and exits
non-zero. Note that pulling **re-points local tags at the Hub version** —
that's what "aligning a machine" means, so make sure the Hub holds what you
want first.

**The rule that follows:** after any closure rebuild, promote (`retag`/`push`)
per Step 3, or the harness will keep launching the previous generation.

## 9. Known residuals — honest scope

- **The Google-shared Go domains.** `proxy.golang.org` / `sum.golang.org` /
  `golang.org` / `go.dev` / `pkg.go.dev` / `index.golang.org` ride Google IP
  ranges **shared with Vertex `aiplatform`** — IP-blocking them would cut the
  model path itself. They are the only entries allowed in
  `firewall_exempt_domains` (enforced by the code-level constant
  `FIREWALL_EXEMPTABLE_DOMAINS` — a *fact*, not a self-declaration; the gate
  rejects anything else and `verify` honors only the intersection). Their
  defense is downgraded: `/etc/hosts` poisoning + `GOPROXY=off`. That stops the
  toolchain and ordinary curl, **not** a deliberate `curl --resolve` with a
  known Google IP. The definitive fix for this whole shared-CDN class is a
  future **SNI-filtering egress proxy** (terminate all egress at a proxy that
  filters by hostname, so IP sharing stops mattering); until then this residual
  is accepted and documented.
- **Training data.** These are public repos; version B may be in the model's
  weights. Quarantine prevents *fetching*, not *remembering* — inherent to any
  public-repo benchmark.
- **apt is dead under quarantine.** `deb.debian.org` is Fastly-fronted, so
  denying the Fastly CIDRs breaks `apt-get`. By design: the eval images are
  pre-provisioned. If a milestone needs a system package, bake it into the
  image; don't reopen the CDN. (`lock_network` installs iptables *before* the
  deny rules take effect.)
- **IPv6.** `/etc/hosts` poisoning uses `0.0.0.0` (v4); containers run with
  `net.ipv6.conf.all.disable_ipv6=1`, which covers the v6 side.
- **Missing-dependency friction.** If a milestone legitimately needs a dep
  outside the closure, the install fails loudly (`No matching distribution` /
  `not found in vendored sources` / GOPROXY-off errors in the trace) — extend
  the closure, rebuild, promote. Detectable, never a silent leak.

## History

The design grew out of GitHub issue #12 (quarantine originally covered only
pip/scikit; three repos were later confirmed cheating through other registries)
and was hardened through the PR #14 review cycle (verification made fail-closed,
runtime auto-exemption replaced by the explicit code-level exempt whitelist,
mirror poisoning scoped to quarantine containers, image pinning). For the full
archaeology — the per-ecosystem risk study, the SNI-proxy design sketch, and the
review/fix reports — see the git history of `docs/` on branch
`quarantine-issue-12` and the PR #14 discussion.
