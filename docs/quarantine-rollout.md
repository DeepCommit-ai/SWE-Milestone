# Quarantine rollout to all ecosystems — risk study & plan

**Status:** ✅ **ROLLED OUT (issue #12, 2026-06-10)** — all 5 ecosystems (pip,
cargo, go, maven, npm) are wired and live-smoke-tested for all 7 repos. See
[`docs/quarantine.md`](quarantine.md) → "Status" for the per-repo matrix and the
implemented mechanism. This doc is kept as the **risk study + the long-term SNI
proxy plan**; the per-ecosystem analysis below is what informed the configs.

**What shipped vs. this study:** the network lever (deny domains + CIDR) works
per the analysis. The offline closures do NOT rely on the eval image's pre-baked
cache as first assumed — an audit found that cache holds only the **A-version**
closure, so cutting the network broke legitimate B builds for all 6 non-pip
repos. The fix shipped as a per-repo **`base-offline:latest`** image re-baked with
the full A→B dependency closure (+ bumped toolchains), selected at launch by
`image_for_repo()`; `base:latest` is left untouched. See
[`docs/quarantine.md`](quarantine.md) → "The base-offline image" for the
per-ecosystem build recipe and the self-exclusion guarantee. The go ecosystem was implemented with **`GOPROXY=off`
as the primary defense** (NOT the `/32`-exemption CIDR-deny proposed below):
`proxy.golang.org` shares Vertex's Google range, and rather than risk an anycast
`/32` carve-out that could rotate into Vertex's IP and break the model path,
`verify_network_lockdown` auto-exempts the Google-shared Go domains from the
unreachable assertion and leans on `GOPROXY=off`. That `proxy.golang.org`
residual is the one thing the SNI proxy (below) still needs to close.

**Related:** [`docs/quarantine.md`](quarantine.md) (the design + full implementation)

## Why this doc exists

quarantine blocks the cheat where an agent downloads the **repo-under-test's own
target-version source** from a package registry (confirmed on scikit-learn:
`pip download scikit-learn==1.6.0` → copied the answer). The fix is *default-deny +
offline dependency closure*. pip is done. This doc studies **extending it to the
other 6 benchmark repos' ecosystems** and the risks found.

Benchmark repos by ecosystem:

| ecosystem | repos | registry |
|---|---|---|
| pip | scikit-learn | PyPI |
| cargo | ripgrep, nushell | crates.io |
| go | navidrome, go-zero | proxy.golang.org |
| maven | dubbo | repo1.maven.org |
| npm | element-web | registry.npmjs.org |

## The real mechanism (corrected) — and the one code change needed

> An earlier draft of this doc claimed go/maven/npm "cannot be CIDR-blocked
> because the registry shares a CDN with the LLM endpoint, so we need an SNI
> proxy." **That was wrong.** The whitelist allows by *resolved /32 IP per
> domain*, so the LLM endpoint can always be kept by **explicitly allowing its
> domain's /32** — even when it sits inside a CDN range we otherwise deny. The
> correct fix is small. Below is the measured reality.

How each registry is currently reachable (measured 2026-05-31):

| ecosystem | registry | CDN | reachable via |
|---|---|---|---|
| pip | pypi / pythonhosted | Fastly | `CDN_CIDR_RANGES` 151.101/16,146.75/16 **+** whitelist domains |
| cargo | crates.io, static/index.crates.io | Fastly | same Fastly /16s **+** whitelist domains |
| go | proxy/sum.golang.org | Google | `CDN_CIDR_RANGES` **142.250.0.0/15** + whitelist domains |
| maven | repo1.maven.org | Cloudflare | `CDN_CIDR_RANGES` **104.16.0.0/13** + whitelist domains |
| npm | registry.npmjs.org | Cloudflare | `CDN_CIDR_RANGES` 104.16.0.0/13 + whitelist domains |

So blocking any registry = (a) remove its domain(s) from the whitelist **and**
(b) `EVOCLAW_DENY_CIDRS` the CDN /range it rides. The catch:

**The LLM endpoint can share that CDN range.** Vertex resolves to `142.251.x`,
which **is inside** `142.250.0.0/15` — the same range go's proxy uses. claude.ai
is on Cloudflare `104.x` — the same range npm/maven use. So naively CIDR-denying
the range would also drop the model API.

**But this is solvable without an SNI proxy** — and it's exactly the property the
whitelist already has: it allows by the **/32 IP resolved from each allowed
domain**. We just need the CIDR-deny to **exempt those explicitly-allowed /32s**.

### Required code change (one function)

Today, `_resolve_whitelisted_ips()` prunes **every** resolved IP that falls in a
denied CIDR (container_setup.py ~942-958). That's fine for pip (no KEEP-service is
on Fastly) but it would drop Vertex when denying the Google range. Fix:

- Designate a set of **protected service domains** (Vertex `aiplatform` / ADC
  `oauth2` / `storage.googleapis.com` / `claude.ai` / `api.anthropic.com` …).
- Resolve their /32s into an `explicit_allow` set.
- CIDR-prune applies to all resolved IPs **except** those in `explicit_allow`.

Verified by simulation (deny `142.250.0.0/15`, protect the Vertex/ADC domains):
`aiplatform → ACCEPT(/32) 142.251.32.170`, `oauth2 → ACCEPT(/32)`,
`proxy.golang.org → DROP`, `sum.golang.org → DROP`. Clean separation, no proxy.

With this one change, the **same** EVOCLAW_DENY_DOMAINS + EVOCLAW_DENY_CIDRS lever
works for **all** ecosystems — including Google (go) and Cloudflare (maven/npm).

### Why pip needed CIDR-deny in the first place (the SNI sub-hole)

Even after removing pypi/pythonhosted from the whitelist domains, other allowed
Fastly domains (`docs.python.org`, `security.debian.org`, …) **resolve to the same
anycast /32s as PyPI** (`151.101.x.223`). Because Fastly routes by SNI, the agent
could `curl --resolve files.pythonhosted.org:443:<that allowed /32>` and still reach
PyPI. CIDR-denying the Fastly /16 closes that. The same SNI sub-hole exists on
Google/Cloudflare, so the CIDR-deny (with the /32 exemption above) is needed there
too — which is why "just remove the registry domain" is **not** sufficient alone.

## Per-ecosystem plan

The unifying principle stays: **provide the repo's third-party dependency closure
as a local offline source, force the package manager at it, and block the live
registry — by whatever mechanism is safe for that ecosystem.** The repo's own
package/sub-packages are `path`/`vendor`/`workspace` deps → excluded by construction.

### pip (DONE — reference implementation)
- Closure: `pip download` in the clean base image → wheelhouse (78 wheels).
- Force-offline: `PIP_NO_INDEX=1`, `PIP_FIND_LINKS=/wheelhouse`.
- Block: `EVOCLAW_DENY_DOMAINS=pypi.org,files.pythonhosted.org` (drop from resolved
  IPs) **+** `EVOCLAW_DENY_CIDRS=151.101.0.0/16,146.75.0.0/16` (drop Fastly, incl.
  IPs admitted via other Fastly-fronted domains).
- Cost: apt breaks (deb.debian.org is Fastly). OK — toolchain baked into image.
- **Verified**: github/pythonhosted/pypi/sdist-curl all CONNFAIL; deps install from
  wheelhouse; Vertex reachable.

### cargo (ripgrep, nushell) — LOW risk, mirrors pip
- crates.io is **also Fastly** and the LLM endpoint isn't → the pip CIDR lever works.
- Closure: `cargo vendor` in the clean image → a vendor dir; the repo's own
  workspace crates (`grep-*`, `globset`, `ignore`; `nu-*`) are path deps, excluded.
- Force-offline: `.cargo/config.toml` →
  `[source.crates-io] replace-with = "vendored-sources"` + `[net] offline = true`.
- Block: same `EVOCLAW_DENY_DOMAINS=crates.io,static.crates.io,index.crates.io`
  **+** reuse `EVOCLAW_DENY_CIDRS` Fastly ranges (already covers crates.io).
- ⚠ Note: ripgrep's cheat used the **sub-crate** `grep-searcher@0.1.16` from
  crates.io — `cargo vendor` correctly excludes it (path dep), so the closure is
  self-exclusive. Verify the vendor dir contains **no** `grep-*`/`nu-*` at the
  target version.

### go (navidrome, go-zero) — HIGH risk (Google-CDN collision)
- **Cannot** CIDR-block: proxy.golang.org is Google, same as Vertex.
- Closure: `go mod vendor` → `/vendor`; the repo's own module is never vendored.
- Force-offline — two equivalent options, both **avoid IP blocking**:
  1. `GOFLAGS=-mod=vendor` + `GOPROXY=off` + `GONOSUMDB=*`/`GONOSUMCHECK` so the
     build uses only `/vendor` and never contacts the proxy. **No firewall change.**
  2. Or `GOPROXY=file:///go-localproxy` pointing at a pre-filled module cache.
- Block: rely on `GOPROXY=off` (manager-level) rather than the firewall, since the
  firewall can't separate proxy.golang.org from Vertex. **Risk:** a determined
  agent could still `git clone`-style fetch — but github is already blocked, and Go
  modules can't be fetched direct-from-VCS when GOPROXY=off and the module isn't in
  vendor. **Open question:** is `GOPROXY=off` alone tamper-proof if the agent
  resets the env? (pip's `PIP_NO_INDEX` had the same theoretical hole, which is why
  pip *also* firewalls — go can't. Mitigation below.)
- **Extra mitigation needed for go/maven/npm** (see "Residual risk" below).

### maven (dubbo) — HIGH risk (Cloudflare collision)
- **Cannot** CIDR-block: repo1.maven.org is Cloudflare, same as claude.ai.
- Closure: `mvn dependency:go-offline` populating a local `~/.m2/repository`; the
  repo's own `org.apache.dubbo:*` artifacts are built locally, not downloaded.
- Force-offline: `mvn -o` (offline) and/or `settings.xml` with a `<mirror>` pointing
  at the local repo + `<offline>true</offline>`.
- Block: manager-level offline only (no safe CIDR). Same residual-risk caveat.
- ⚠ The dubbo cheat vector is `*-3.3.6-sources.jar` — go-offline pulls binary
  artifacts, and the source jars of dubbo's OWN modules are never a dependency, so
  the closure excludes them. Verify `~/.m2` has no `org/apache/dubbo/**/*3.3.6*`.

### npm (element-web) — HIGH risk (Cloudflare collision)
- **Cannot** CIDR-block: registry.npmjs.org is Cloudflare.
- Closure: `npm ci` (or a local verdaccio / offline cache) from the clean image.
- Force-offline: `npm ci --offline` + `.npmrc` `registry=file://...` or a local
  registry; `--prefer-offline`/`--offline`.
- Block: manager-level offline only. Same caveat.
- Note: element-web's app source isn't on npm (lowest cheat risk of all 7), but the
  closely-coupled `matrix-js-sdk`/`matrix-react-sdk` at exact target versions are —
  those ARE legit deps, so they'll be in the closure; that's fine (they're upstream
  libraries, not element-web's answer source). The thing to prevent is reaching
  github for element-web@v1.11.97 — already covered by the existing github block.

## Residual risk: manager-offline without a firewall backstop

For pip/cargo the firewall is a hard second layer (even if the agent unsets
`PIP_NO_INDEX`, the registry IP is unreachable). For **go/maven/npm we lose that
backstop** because we can't IP-block the registry without killing the LLM. So the
block is "only" at the package-manager config layer, which a determined agent could
theoretically override by resetting env / editing config.

Options to restore a hard backstop for go/maven/npm (pick per appetite):

1. **L7 / SNI-filtering egress proxy** (the proper fix): route all container egress
   through a forward proxy that allows by **hostname (SNI)**, not IP. Deny
   `registry.npmjs.org` / `repo1.maven.org` / `proxy.golang.org` while allowing
   `aiplatform.googleapis.com` / `claude.ai` — even when they share an IP. This is
   the only mechanism that cleanly separates same-CDN hosts. Cost: stand up mitmproxy
   / tinyproxy in the sandbox + trust its cert; ~moderate build.
2. **/etc/hosts pinning**: point the registry hostnames at `127.0.0.1` inside the
   container (we already poison code-hosting hosts this way). Cheap, and it survives
   as long as the agent doesn't `curl --resolve` / edit `/etc/hosts` (it's root-owned
   after lockdown). Weaker than (1) but near-free and reuses existing machinery.
3. **Accept manager-offline as sufficient** for the first rollout, and rely on
   *post-hoc trace audit* (the same subagent sweep we ran) to catch any registry
   re-fetch. Cheapest; detection not prevention.

Recommendation: **(2) /etc/hosts pinning as the default backstop** for go/maven/npm
(cheap, reuses the code-hosting-poison path), with **(1) the SNI proxy** as the
eventual proper solution if we want prevention-grade guarantees across the board.

## Verification protocol (per ecosystem, before trusting a secure run)

Generalize the pip battery. In the locked container, ALL must hold:
- `curl github.com` → CONNFAIL (baseline)
- registry host(s) → **CONNFAIL or 127.0.0.1** (depending on mechanism)
- `<pm> install <self>@<target>` → fails (no distribution / offline)
- `<pm> install <a-real-dependency>` → succeeds from local closure
- LLM endpoint (`aiplatform.googleapis.com` / `claude.ai`) → reachable
- end-to-end: resolve the self-package's target artifact URL and fetch it → must fail

Per-ecosystem "install" commands: pip `pip download`, cargo `cargo fetch`,
go `go mod download`, maven `mvn dependency:get`, npm `npm install`.

## Rollout order (risk-ranked)

1. **cargo** (ripgrep, nushell) — low risk, reuses the pip Fastly lever almost verbatim.
2. **npm** (element-web) — Cloudflare collision, but lowest cheat exposure; good first
   test of the /etc/hosts-pin backstop.
3. **maven** (dubbo) — Cloudflare collision; sources.jar vector.
4. **go** (navidrome, go-zero) — Google collision (highest stakes: same CDN as Vertex);
   do last, with the most testing of the no-firewall path.

## Open questions for the team

- Do we want **prevention** (SNI proxy, more infra) or is **manager-offline +
  /etc/hosts-pin + post-hoc audit** acceptable for the leaderboard?
- Should quarantine be **default-on** for all official runs, or opt-in per trial?
  (It changes the environment, so re-running historical results for parity is a
  separate cost.)
- The wheelhouse/vendor closures are built from the **clean base image** — do we
  snapshot & version them so a secure run is reproducible?
- The 03:00 incident (a cron-like process zeroed all 7 repo-root `milestones.csv`)
  is unrelated but shows shared-machine data fragility — worth a separate hardening note.
