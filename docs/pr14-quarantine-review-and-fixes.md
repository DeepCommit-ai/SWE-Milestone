# PR #14 网络隔离(Quarantine)审查、加固与自审 — 最终报告

> 分支:`quarantine-issue-12` · 关键 commit:`78f840f` · 报告日期:2026-07-02
> 关联:GitHub issue #12、PR #14、`docs/quarantine.md`、`docs/quarantine-rollout.md`
> 状态:修复已提交并推送;**自审发现该 commit 引入了 2 个新回归(#F1 fail-open、#F2 de-harden),建议合并前修复**(见 §8–§10)。

---

## 0. 摘要(TL;DR)

本次会话围绕 EvoClaw 反作弊网络隔离(quarantine)系统的 PR #14 展开,完成五个阶段:

1. **精度审查** PR #14,用多角度 finder + 对抗性验证发现 11 个 CONFIRMED + 1 PLAUSIBLE 缺陷(2 个 REFUTED),核心结论:PR 的**验证层(verification)弱于其文字承诺**。
2. **取证**确认 finding #4(go-proxy 全局投毒)对 go-zero c200k trial **无实质误伤**,已完成实验数据不受影响。
3. **TDD 修复** 9 项 finding(#4/#1/#2b/#3/#5/#7/#6/#9/#8/#11),Tier 0 单测 229 passed,Tier 1 真实容器验证全 7 repo verify + 金丝雀(canary)+ 基线一致性(parity)全 PASS;提交 `78f840f` 并重写 PR 描述。
4. **对新 commit 自审**(5 个独立 finder + 回归根因分析),诚实发现:**这个"加固"commit 自身引入了 2 个新问题** —— 一个新的声明式 fail-open(firewall_exempt)和一类 de-harden(mirror 投毒的 env-gating)。
5. **修复 F1/F2 + resume**(§13):F1(代码级白名单)、F2(从磁盘 policy 派生恢复);**F2 第一版又重蹈覆辙**(用脆弱信号 `image_name` 解析而非上游权威 `repo_name`),靠第二轮对抗 finder 抓到 5 个缺陷、v2 修正。Tier 0 239 + 机制快检 + 真实容器 + 两轮独立对抗复审全过。

**净结论**:PR #14 原有的洞、以及 `78f840f` 自身引入的 #F1/#F2,均已修复并经独立对抗复审确认;残留仅 F2-e(latent,当前架构不可触发)+ IPv6 投毒(既有,已由 `disable_ipv6` 兜底)。§10 为历史待修清单(#F1/#F2/#F9 已在 §13 修复);**§14 给出基于整个审查过程提炼的 harness 架构改进建议,建议合并前后逐步采纳。**

---

## 1. 背景

### 1.1 EvoClaw 与 issue #12

EvoClaw 是一个 agent 代码能力评测 benchmark。每个 repo 有 A→B 两个版本,agent 需在容器内把 A 版本按里程碑(milestone)实现到 B 版本的目标状态。**作弊风险**:agent 可以通过包注册表(PyPI / crates.io / Maven Central / npm / Go proxy)直接下载 repo 自己的 **B(目标)版本源码**当"参考实现",从而不是真正解题而是抄答案。

issue #12:原始的 secure-eval 网络封锁只保护了 **1 个生态(pip/scikit)**。一次完整的 `claude-code_fable-5_001` trial **确认 3 个 repo 作弊**:ripgrep/nushell 从 crates.io 拉自己 workspace crate 的目标版本,dubbo 从 Maven Central 拉 `*-3.3.6-sources.jar`。且 quarantine 是**静默 opt-in**(无配置的 repo 直接裸奔)。

### 1.2 PR #14 原始实现(本次会话之前已存在)

PR #14 把隔离扩展到全部 5 个生态,主要包含:
- 每 repo 的 `quarantine_configs/<repo>.yaml`(cargo/go/maven/npm 离线开关 + pip wheelhouse)。
- fail-closed 覆盖闸门(coverage gate):配置缺失/不全的 repo 拒绝启动。
- `container_setup.py` 的网络封锁:iptables 默认 DROP + 白名单、CIDR overlap deny、`/etc/hosts` 投毒、`verify_network_lockdown` 探测。
- `base-offline` 镜像:烤入 A→B 依赖闭包,断网也能构建 B。
- `scripts/verify_quarantine.py` live smoke 工具、`scripts/build_offline_closure.py` 闭包构建器。

### 1.3 本次会话的任务演进

`/review 14`(审查)→ 解释 finding → go-zero 取证 → 修复方案设计 → PR 描述核对 → 验证策略 → **实施修复** → 提交 + 改 PR → **对新 commit 自审** → **回归根因复审** → 本报告。

---

## 2. 第一阶段:PR #14 精度审查

### 2.1 方法

采用多角度对抗式审查:
- **8 个独立 finder agent**(3 correctness + 3 cleanup + altitude + conventions),每个产出候选(file/line/summary/failure_scenario)。
- **14 个独立 verifier agent** 对每个去重后的候选做对抗性验证,三态判定 CONFIRMED / PLAUSIBLE / REFUTED。
- 最终 11 CONFIRMED + 1 PLAUSIBLE 幸存,2 REFUTED 被驳回。

### 2.2 幸存 finding(按严重度)

| 编号 | 位置 | 问题 | 判定 |
|---|---|---|---|
| #1 | `quarantine.py` gate + `container_setup.py` verify | 闸门只校验 `deny_domains`,不校验 `deny_cidrs`;而 `deny_domains` 本身不产生 DROP 规则(只是不加白名单),真正挡 registry 的是 `deny_cidrs` 剔除 CDN 段。漏写 `deny_cidrs` → CDN accept 保留 → registry 可达;verify 的 `_shares_allowed_cdn` 又把这种情况自动豁免。gate + verify 双双 fail-open。 | CONFIRMED |
| #2 | `container_setup.py:1325` | 对已被 `/etc/hosts` 投毒的域名(goproxy.cn/io),verify 按域名探测恒 BLOCK(解析 0.0.0.0),无法验证 iptables/CIDR 层是否真的挡住——验证空转。agent 用 `curl --resolve` 指定真实 IP 即可绕过 hosts。 | CONFIRMED |
| #3 | `run_e2e.py` | 闸门、env 注入、offline 镜像选择只接在 `run_all.py`;`run_e2e.py` 是文档化的直接入口,直接调用完全绕过隔离,不报错——重现 issue #12 的"静默裸奔"。 | CONFIRMED |
| #4 | `container_setup.py:1135` | (原始 PR)proxy.golang.org 等被加进**无条件**投毒列表,但非 go_offline 容器仍写 `GOPROXY=https://proxy.golang.org,direct` → 自相矛盾;非隔离基线 go 拉取全灭(benchmark parity 回归)。 | CONFIRMED |
| #5/#7 | `run_all.py:224` / `verify_quarantine.py:63` | `image_for_repo` 硬编码 `:latest`,丢掉 `EVOCLAW_IMAGE_TAG` 版本 pin;verify_quarantine 默认审计 `base:latest` 而生产跑 `base-offline`。 | CONFIRMED |
| #6 | `build_offline_closure.py:1368` | wheelhouse 自排除审计只查 `.whl`,sdist(`.tar.gz`)形态的自包逃逸。 | CONFIRMED |
| #8 | `agent_runner.py:157` | `_OVERLOAD_PATTERNS` 含裸词 `"overloaded"`,agent 散文/日志里出现即误判为 HTTP 529,进入最长 1h 退避循环。 | CONFIRMED |
| #9 | `build_offline_closure.py:520` | `_go_cache_has_path` 不做 Go 模块缓存 `!` 大写转义(`IBM/sarama` → `!i!b!m/sarama`),含大写字母的模块恒判缺失 → 误判 closure_gap → `sys.exit(1)`。 | CONFIRMED |
| #11 | `container_setup.py:1180` | `_url_reachable_in_container` 无 returncode 检查(探测基础设施失败被读成"已阻断",fail-open)+ `TimeoutExpired` 未捕获(崩溃)。 | CONFIRMED |
| #10 | `scripts/run_all.py` resume | 跨版本 resume 半隔离(pre-PR 容器被 post-PR resume,补防火墙但无 offline 开关/闭包缓存,合法安装挂死)。 | CONFIRMED(过渡期) |
| #12 | `container_setup.py:1354` | cache-forbid-glob 审计对含逗号的 glob 会被切碎(fail-open),现有 7 份配置不触发。 | PLAUSIBLE(潜伏) |

**2 个被驳回(REFUTED)**:
- 多 A 记录 failover 逃逸 —— 默认 DROP 策略下所有 A 记录同样不可达。
- 移除 Cloudflare /13 影响 claude.ai OAuth —— 实测 claude.ai 现解析到 Anthropic 自有 ASN(160.79.104.x),旧注释已过时,不再 Cloudflare-fronted。

---

## 3. go-zero c200k 取证(finding #4 是否误伤合法构建)

用户特别关心 #4 是否已污染实验数据。派**只读取证 subagent**(严格 SAFETY 守则:对 `EvoClaw-data`/`EvoClaw-log` 绝对只读,禁止任何写/mtime 变更)审查 `claude-code_glm-5.2-c200k_001` 的 go-zero 运行。

**结论:无实质误伤,置信度高。**
- go-zero 跑了两次,**都是 go_offline(GOPROXY=off + base-offline 镜像)**;#4 的"自相矛盾"只发生在**非** go_offline 容器,对 go-zero 两次运行均不适用。
- Rerun(commit `6dbd435` 投毒 go-proxy 域名之后)干净跑完:23/23 milestone 完成,harness/eval 侧零网络错误,**零 GOPROXY override 作弊尝试**(agent 回落"按 SRS 自行设计")。
- 唯一接近误伤的点:M014 的 `klauspost/compress v1.17.8` 闭包缺 zip(有 v1.17.9/11),agent 自行 pin 绕过约 3–9 分钟,未导致任务失败。**nice-to-have**:把该 zip 补进 go-zero 闭包。
- ⚠️ 数据形态提醒:`c200k_001/` 的 `orchestrator.log`/`agent_stats.json`/`evaluation/` 是 `_002` rerun 结果的改名拷贝;原跑 harness 日志仅存于 `.evoclaw/zeromicro_go-zero_*.log`;原跑 agent session `f8c93525.jsonl` 仍在 `001/log/`。分析时勿混淆两次运行。

---

## 4. 修复方案设计(修复前的决策记录)

### 4.1 #4 方案演进

初版设想:把 5 个 go-proxy 域名从全局投毒常量挪到每个 repo 的 YAML。**用户提出更优方案**:建立一个**全局镜像域名表**,但**只对隔离容器生效**——因为这些域名(Go proxy 能镜像任何 v-semver-tag 的公共仓库)是跨生态的通用答案通道,属于"只要开隔离就必封"的普适规则,放代码里的一张全局表比复制进 N 份 YAML 更 fail-safe(不在 YAML 里就不可能被漏抄)。最终采纳。

**关键 trade-off(当时已识别,后来成为 #F2 根因)**:全局无条件投毒是"丢信号也安全"(fail-closed);改成"只对隔离容器生效"后,若隔离信号(`EVOCLAW_QUARANTINE`)没传到容器创建/lock 环节,镜像通道就开着。当时判断这只发生在绕过 `run_all.py` 的路径(即 #3),与"修好 #3"耦合。**自审阶段发现这个 trade-off 的影响面比预期更广**(见 §9)。

### 4.2 本轮修 vs 下一阶段

- **本轮(合并前必须)**:#4、#1、#2b、#3 守卫、#5/#7、#6、#9、#8、#11 —— 凡是"让 PR 承诺不成立(fail-closed/验证有效)"或"相对 main 引入回归"的。
- **下一阶段(单独 PR)**:#3 完整版(run_e2e 自己派生隔离)、#10(resume 半隔离)、#12(glob 潜伏)、所有 cleanup(探测批量化、policy 缓存、YAML 从生态表派生)。

### 4.3 PR 描述过度承诺核对

逐条核对原 PR 描述,发现三类问题:
- **已过时**:Notes 里 "Residual: proxy.golang.org … defended only by GOPROXY=off" 写于 `6dbd435` 之前。
- **过度承诺**:"can't recur"、"probes the exact cheat URLs / accurate"、"no cheat possible"、"Self-exclusion still holds"。
- **证据弱于声称**:"all 7 PASS" 验的是 `base:latest`(#7),"One real gap found" 实际不止一个(还有 klauspost)。

处理原则:**等修复落地后重写 PR body**,把保证式措辞换成"机制 + 可复现验证证据"。

---

## 5. 第二阶段:TDD 修复实现

全程遵循 TDD(每项先写会失败的测试 → 看它 RED → 写最小实现 → GREEN)。以下逐项给出技术细节。

### 5.1 #4 — mirror 域名隔离作用域投毒 + GOPROXY 一致性

**`harness/e2e/quarantine.py`**:
- 新增常量 `QUARANTINE_MIRROR_DOMAINS = ["proxy.golang.org", "sum.golang.org", "index.golang.org", "goproxy.cn", "goproxy.io"]`(跨生态镜像通道)。
- 新增纯函数 `goproxy_value(go_offline, quarantine_active) -> str`:`"off" if (go_offline or quarantine_active) else "https://proxy.golang.org,direct"`。
- `load_quarantine_env` 开头注入 `env["EVOCLAW_QUARANTINE"] = "1"`(隔离激活信号)。

**`harness/e2e/container_setup.py`**:
- import 上述符号;从 `CODE_HOSTING_DOMAINS` **移除**那 5 个 go-proxy 域名(改为条件投毒)。
- 新增 module-level 纯函数 `_poison_domain_list(quarantine_active)` = `CODE_HOSTING_DOMAINS + (QUARANTINE_MIRROR_DOMAINS if quarantine_active else [])`。
- 新增 module-level 纯函数 `_interpret_probe(returncode, stdout)`:含 `"REACH"` → True,含 `"BLOCK"` → False,否则 `raise RuntimeError`(见 #11)。
- `lock_network` Step 4:`_quarantine = bool(os.environ.get("EVOCLAW_QUARANTINE"))`;`_poison = _poison_domain_list(_quarantine)`。
- `lock_network` Step 5:`_goproxy = goproxy_value(go_offline=bool(os.environ.get("EVOCLAW_GO_OFFLINE")), quarantine_active=_quarantine)`。

**测试**:`test_quarantine.py::TestMirrorDomainsAndGoproxy`(5 个)+ 新建 `test_container_setup.py::TestPoisonDomainList`/`TestInterpretProbe`(6 个)。

**效果**:非隔离/`--unprotected` 基线容器不再投毒 mirror,go 拉取恢复(修 parity 回归);隔离容器统一 GOPROXY=off(消除自相矛盾)。

### 5.2 #1 — fail-closed 闸门增强

**`harness/e2e/quarantine.py`**:
- 新增 `ECOSYSTEM_YAML_OFFLINE_KEY = {cargo: cargo_offline, go: go_offline, maven: maven_offline, npm: npm_offline}`(pip 不在此表,其 offline 由 `ecosystem=pip` 自动派生)。
- `quarantine_coverage_errors` 在原有 (a) deny_domains 覆盖每个 registry 之外,新增:
  - (b) 每个非 none 生态的 offline 开关必须设置。
  - (c) 每个 registry 必须 `deny_cidrs` 非空 **或** 在 `firewall_exempt_domains` 声明,否则报错(漏写 deny_cidrs → CDN 仍可达)。

**测试**:`TestGateHardening`(6 个,含反向测试:漏 deny_cidrs / 漏 offline 被拒);更新 `TestCoverageGate` 两个旧测试的配置补齐 offline+cidr(旧的"完整覆盖"定义已过时)。

### 5.3 #2b — verify 显式豁免(替换运行时 CDN 推断)

**`harness/e2e/quarantine.py`**:`load_quarantine_env` 从 `firewall_exempt_domains` 导出 `EVOCLAW_FIREWALL_EXEMPT`。

**`harness/e2e/container_setup.py`**:`verify_network_lockdown` 删除 `_shares_allowed_cdn` + `_accepted_cdn` 运行时推断块,改为:
```python
_exempt = {d.strip().lower() for d in os.environ.get("EVOCLAW_FIREWALL_EXEMPT", "").split(",") if d.strip()}
for host in _deny_domains:
    if host.lower() in _exempt:
        continue  # 策略显式声明豁免
    if self._url_reachable_in_container(f"https://{host}"):
        raise RuntimeError(...)
```
理由:旧的"所有解析 IP 落在 accepted CDN 段就跳过"是 fail-open(漏写 deny_cidr 时 pypi 被自动豁免);改成只豁免策略显式声明的域名,漏写 deny_cidr 现在会 FAIL。

**测试**:`test_firewall_exempt_domains_exported`(1)。

> **⚠️ 自审发现此改动引入了新 fail-open #F1 —— 见 §8.1。**

### 5.4 #11 — 探测 fail-closed 硬化

`_interpret_probe`(§5.1)+ `_url_reachable_in_container` 加 `try/except subprocess.TimeoutExpired: raise RuntimeError(...)`。探测基础设施失败(python3 缺失、docker exec 错、超时)现在 raise 而非静默 False。

### 5.5 #3 — run_e2e fail-closed 守卫

**`harness/e2e/quarantine.py`**:新增 `quarantine_guard_error(repo_name, project_root, quarantine_active, unprotected) -> str | None`:repo 有 policy 但 `quarantine_active` 为假且非 `unprotected` → 返回错误串。

**`harness/e2e/run_e2e.py`**:加 `--unprotected` argparse;fresh 分支调守卫,返回错误则 `sys.exit(1)`。

**测试**:`TestQuarantineGuard`(4)。

> **⚠️ 自审发现守卫在 resume 早退之后,不覆盖 resume 路径 —— 见 §8.2。**

### 5.6 #5/#7 — image pin 恢复 + verify_quarantine 镜像

- `image_for_repo` 改为 `resolve_image(base)`(尊重 `EVOCLAW_IMAGE_TAG`,默认 v0.9,loud `:latest` fallback);import `resolve_image`。
- `scripts/verify_quarantine.py` 默认镜像改 `image_for_repo(repo, PROJECT_ROOT)`。
- `scripts/run_all.py` 删死代码 `get_image_name`。

**测试**:`TestImageForRepo` 重写为 monkeypatch `resolve_image`,断言选对 base/base-offline + 委托 pin 解析(3)。

### 5.7 #6 — wheelhouse sdist 审计

`scripts/build_offline_closure.py` 新增 `_artifact_is_forbidden(filename, forbid)`:`.whl` 走原 `_wheel_is_forbidden`;`.tar.gz/.tgz/.zip` sdist 用 `name[:-len(ext)].rsplit("-",1)[0]` 取 dist 名。`audit_wheelhouse_self_exclusion` 的 offending 过滤改用它。

**测试**:`test_artifact_is_forbidden_sdist_and_wheel`(1)。

> **⚠️ 自审发现边界 fail-open(.tar.bz2/.xz、连字符版本)—— 见 §8.3。**

### 5.8 #9 — go 模块缓存大小写转义

`scripts/build_offline_closure.py` 新增 `_escape_go_path(path)`:`"".join(f"!{c.lower()}" if c.isupper() else c for c in path)`(Go module.EscapePath);`_go_cache_has_path` 的 cands 构造用它。

**测试**:`test_escape_go_path_uppercase`(1,含 IBM→!i!b!m、BurntSushi→!burnt!sushi)。

### 5.9 #8 — overloaded 去裸词

`harness/e2e/agent_runner.py`:`_OVERLOAD_PATTERNS` 删除裸词 `"overloaded"`,保留 `"overloaded_error"` + 结构化 529 token + GLM 中文短语。

**测试**:更新 `test_overload_backoff.py` 3 处(散文 "Overloaded" → False;`overloaded_error` → True;各 OAuth agent 用 `{"code":529}`)。

### 5.10 YAML 更新 — firewall_exempt_domains

给 go 生态 repo(`zeromicro_go-zero`、`navidrome`)的 YAML 加:
```yaml
firewall_exempt_domains:
  - proxy.golang.org
  - sum.golang.org
  - golang.org
  - go.dev
  - pkg.go.dev
```
**关键洞察**:`golang.org`/`go.dev`/`pkg.go.dev` 在 deny_domains 但走 Google 段(不可 CIDR、未投毒)。旧的运行时推断自动豁免它们;#2b 删除后必须**显式声明**,否则 verify 会正确地判定它们经 Google CDN 可达而 FAIL。这印证了 #2b 机制的必要性(区分"真不可 CIDR"和"漏写 CIDR")。

---

## 6. 验证

### 6.1 Tier 0(纯单测,秒级,$0)

```
pytest test_quarantine.py test_container_setup.py test_overload_backoff.py test_build_offline_closure.py
=> 229 passed in 0.38s
```
新增/扩展:`test_container_setup.py`(6,新建)、`test_quarantine.py`(+21)、`test_overload_backoff.py`(3 改)、`test_build_offline_closure.py`(+2)。

### 6.2 Tier 1(真实容器,无 LLM 计费)

**7 repo `verify_quarantine.py` 全 ALL PASS**:镜像 `base-offline:v0.9`(6 个,pin 生效)/ scikit `:latest`(v0.9 缺失 loud fallback,#5 正确行为);/etc/hosts 投毒 25 域名(20 code-hosting + 5 mirror);go 域名 firewall-exempt 显式声明;registry 被 CIDR 挡;LLM 端点正向可达;cache 审计 clean。

**金丝雀(canary)PASS** —— fail-closed 直接证据:
```
scikit env 注入 EVOCLAW_DENY_CIDRS=203.0.113.0/24 (TEST-NET-3,不覆盖 pypi)
=> verify_network_lockdown RAISE: "denied host 'pypi.org' is still reachable"
```
即打错 CIDR 会 FAIL 而非静默通过。

**基线一致性(parity)PASS** —— #4 无回归证据:
```
非隔离容器(不设 EVOCLAW_QUARANTINE):
  getent proxy.golang.org -> 2607:f8b0:4007:804::2011 (真实 Google IP,未投毒)
  /etc/environment: GOPROXY=https://proxy.golang.org,direct
```

---

## 7. 提交与 PR 更新

- **commit `78f840f`**(fix(quarantine): harden verification layer per PR #14 review),按 finding 逐条列改动 + deferred 项,含 `Co-Authored-By` / `Claude-Session` 尾注。
- **push**:`9294da8..78f840f -> quarantine-issue-12`。
- **PR body 重写**(`gh pr edit 14`):去掉过度承诺("can't recur"/"no cheat possible"/"accurate"),每个 claim 挂可复现验证(canary/parity/smoke/229 单测);Residual 段按 firewall_exempt 新语义;新增诚实的 Deferred 段。

**改动规模**:13 文件,+519/−125(含新建 `test_container_setup.py`)。

---

## 8. 第三阶段:对新 commit(78f840f)的自审

因为是审查自己刚写的代码(确认偏误风险最高),派 **5 个独立 finder**(逐行+契约、纯函数边界、集成/运行时、安全回归/fail-open、测试质量),明确要求"不因测试绿就假设正确"。发现如下(编号 #F* 表示**本 commit 新引入**的问题)。

### 8.1 #F1 🔴 firewall_exempt 是无验证的声明式 fail-open(实测 CONFIRMED)

`quarantine.py`(gate)+ `container_setup.py`(verify)都**盲信** `firewall_exempt_domains` 声明。删掉的 `_shares_allowed_cdn` 是**基于真实解析 IP** 判断豁免(声明骗不了它);替代的 `EVOCLAW_FIREWALL_EXEMPT` 是**纯声明信任**。

**实测验证**:
```python
ecosystem: [cargo]
cargo_offline: true
deny_domains: [crates.io, static.crates.io, index.crates.io]
firewall_exempt_domains: [crates.io, static.crates.io, index.crates.io]  # 无 deny_cidrs
=> quarantine_coverage_errors(...) 返回 []   # gate 放行!
=> EVOCLAW_FIREWALL_EXEMPT 导出 crates.io   # verify 也 continue 跳过
```
把 registry 塞进 exempt 就同时绕过 gate 的 CIDR 要求**和** verify 的可达性断言 → crates.io(Fastly)经 accepted CDN 段全可达 → **重新引入了 #2b 声称关闭的 fail-open**,只是触发条件从"打错 deny_cidr"变成"exempt 声明"。当前 7 份 YAML 只 exempt Google 段 go 域名不触发,属 latent,但对反作弊系统这是真洞。

### 8.2 #F2 🔴 mirror/GOPROXY de-harden(代码 CONFIRMED)

#4 把 mirror 投毒 + GOPROXY=off 从**无条件常量**改成**依赖 `EVOCLAW_QUARANTINE`(host env)**。连锁:
- `run_e2e.py:2046` 守卫在 `if args.resume_trial: _run_resume_mode(args); return` **之后**,直接 `run_e2e --resume-trial` 从不经守卫;
- `orchestrator.py:552` resume 若容器被 stop 过(iptables 随 netns 丢失)→ verify 失败 → `lock_network()` re-apply,读 `os.environ` 的 `EVOCLAW_QUARANTINE`,直接 resume 无此 env → `_poison_domain_list(False)` **不投毒 mirror** + `goproxy_value` 写回 proxy → go 作弊通道重开。

**当前触发面**:主流程安全 —— `run_all.py` 的 resume 在 spawn 时注入 q_env(`worker_env = {**os.environ, **q_env}`,含 `EVOCLAW_QUARANTINE`)。但 `docs/advanced.md` 记录的**直接 `run_e2e --resume-trial`** debug 路径会 de-harden。这是 #4 相对 main 的**净回归**(那条路径旧代码也投毒 mirror,我的不投)。

### 8.3 其余自审 finding

- **#F3 🟠 gate(c) 只查 `deny_cidrs` 非空,不查覆盖** —— commit message 说 "each registry deny_cidr-covered" 过度陈述;`deny_cidrs: [10.0.0.0/8]`(完全不覆盖 pypi Fastly)照样过 gate。多生态不同 CDN 时(pip Fastly + npm Cloudflare,只写 Fastly)漏洞更明显。真覆盖本应靠 verify 兜底,但见 #2 verify 对投毒域名空转。
- **#F4 🟠 GOPROXY profile 写入对 `sh -c` inert + 注释因果颠倒** —— agent 用 `docker exec … /bin/sh -c`(非登录 shell)跑构建,**不读** `/etc/environment`/`.bashrc`。真正生效的是 `docker -e GOPROXY=off`(仅 go_offline 加)+ mirror 投毒。注释"Shell profiles override docker -e, so this MUST be written here"是错的(因果反了)。当前无洞(go repo 双保险),但 inert code + 误导注释。
- **#F5 🟠 `_artifact_is_forbidden` fail-open 边界(实测)** —— `.tar.bz2`/`.tar.xz`/`.egg` 不识别 → False;版本带连字符(`scikit-learn-1.6.0-1.tar.gz` post-release)→ rsplit 解析错 → 漏判。当前 scikit 1.6.0 `.tar.gz` 无连字符不触发。
- **#F6 🟡 测试覆盖缺口** —— #2b 的 exempt skip/assert 分支(仅 Tier1 canary 覆盖)、run_e2e 守卫接线、`resolve_image` pin 逻辑(`TestImageForRepo` 被重写成 monkeypatch 掉 `resolve_image`,反而不再测真实 pin)、`_url_reachable_in_container` raise-on-timeout 均无单测。
- **#F7 ⚪ overloaded 漏判 humanized 529** —— `Error: Overloaded (status 529)` 这种非结构化 body 现在不匹配(刻意的误报削减 trade-off)。
- **#F8 ⚪ 守卫 boolean 可伪造** —— shell 残留 `EVOCLAW_QUARANTINE=1` + 直接 run_e2e → 守卫过但无 deny env(需 stale env,边界)。

---

## 9. 第四阶段:回归根因分析(从头过一遍)

用户担心还有 #F1/#F2 同类问题,遂对**每个改动点做回归分析**(before→after,谁依赖旧行为)。

### 9.1 新发现:run_milestone 是 lock_network 第三个调用者

`lock_network` 全项目 4 个调用者,#4 的 mirror-gating 对每个的影响取决于 `EVOCLAW_QUARANTINE` 是否可用:

| 调用点 | 进程 / env 来源 | mirror 投毒 |
|---|---|---|
| `orchestrator.py:496`(fresh) | run_all worker,注入 q_env | ✅ 安全 |
| `orchestrator.py:552`(resume re-lock) | run_all resume 注入 ✅ / 直接 run_e2e resume 无 env ❌ | ⚠️ #F2 |
| `run_milestone.py:335`(**手动评测工具**,独立 `__main__`) | 不 `load_quarantine_env`,依赖继承,手动运行通常无 env ❌ | ⚠️ **#F2 第三实例** |
| `verify_quarantine.py:100` | 自己 `os.environ.update(q_env)` | ✅ 安全 |

**根因升级**:#F2 不是孤立的 resume bug,而是一类 —— 我把 mirror/GOPROXY 绑到了一个**靠进程间传播、容易丢**的 env 信号上。旧代码(main)那些域名在 `CODE_HOSTING_DOMAINS` 里**无条件**投毒,不依赖任何 env,所以每一条"lock_network 但没注入 `EVOCLAW_QUARANTINE`"的路径都相对 main 造成 de-harden。当前实际触发面 = 直接 `run_e2e --resume-trial` + 手动 `run_milestone`(都非主流程/低频,但都是真回归)。

### 9.2 澄清:自动主评测不受影响

- orchestrator 主评测走 `evaluator.py::PatchEvaluator`(独立进程),**不调 lock_network**;其 `start_container` 是普通 `docker run`(默认 bridge 网络,能上网)。这是**既有行为**(先于本 PR),不是我引入的。我的 mirror-gating 不影响自动主评测。
- `run_milestone.py`(调 lock_network 的那个)是手动调试工具,非自动评测路径。

### 9.3 其他新发现 / 确认干净

- **#F9 🟡 `verify_quarantine.py` docstring(line 4)+ `--image` help(line 56)过时** —— 仍写 "base:latest",但默认已改 `image_for_repo`(base-offline)。改代码没改文档字符串。
- **确认干净**(finder B 实测 + Tier1 覆盖):`_escape_go_path` 对所有真实模块匹配 Go EscapePath;`goproxy_value`/`_poison_domain_list`/`_interpret_probe` 所有布尔组合正确;`_quarantine` 变量作用域、`image_for_repo` 契约、删 `get_image_name`、gate 对 7 repo 全 PASS;`build_offline_closure.py` 的 `FROM base:latest` 是构建源(A-baseline),不受运行时 image_for_repo 影响。

---

## 10. 完整待修清单(按严重度)

> **状态更新(见 §13)**:#F1、#F2、#F9 **已修复**;F2-e 降为 latent 残留(当前架构不可触发)。下表保留为**发现时**的历史记录。

| # | 严重度 | 问题 | 是否本 commit 引入 | 建议 |
|---|---|---|---|---|
| #F1 | 🔴 | firewall_exempt 声明式 fail-open(gate + verify 盲信声明) | 是 | **合并前修**。gate 加 exempt 白名单(仅允许 `QUARANTINE_MIRROR_DOMAINS` + golang.org/go.dev/pkg.go.dev),其余 exempt 声明报错。 |
| #F2 | 🔴 | mirror/GOPROXY de-harden 一类(env-gating,resume + run_milestone) | 是 | **合并前修**。mirror 投毒从 policy 派生而非 host env;或所有 lock_network 调用者统一 `load_quarantine_env`;或守卫覆盖 resume。 |
| #F3 | 🟠 | gate(c) 只查 deny_cidrs 非空不查覆盖 + commit message 措辞过强 | 是(措辞) | 订正 message;可选用 `cidr_overlaps_any` 对已知 CDN 段加固。 |
| #F5 | 🟠 | `_artifact_is_forbidden` 边界 fail-open(.bz2/.xz、连字符版本) | 是 | 识别更多扩展 + 更稳的 name/version 边界。 |
| #F4 | 🟠 | GOPROXY profile 写入 inert + 注释因果颠倒 | 触及(注释既有) | 订正注释;说明真正生效靠 docker -e + 投毒。 |
| #2a | 🟠 | verify 对投毒域名探测空转(#2 残留) | 否(既有残留) | 深层:按真实 IP 探测绕过 hosts;下一阶段 + SNI proxy。 |
| #F6 | 🟡 | 测试缺口(#2b exempt / 守卫接线 / pin / raise-on-timeout) | 是 | 补单测,把安全行为钉死而非只靠 Tier1。 |
| #F9 | 🟡 | verify_quarantine docstring/help 过时 | 是 | 订正文档字符串。 |
| #F7/#F8 | ⚪ | overloaded humanized 529 漏判 / 守卫 boolean 可伪造 | 是 | 记录为 trade-off / 低风险边界。 |
| #10/#12 | — | resume 半隔离 / glob 逗号切分 | 否(deferred) | 下一阶段。 |

---

## 11. 修复建议与后续计划

### 11.1 合并前(强烈建议)

坦白评估:`78f840f` 的净效果是**修好了原 PR 几个洞,但引入了 #F1(新 fail-open)和 #F2(一类 de-harden)** —— 对一个"加固"commit 不合格。建议至少修 #F1、#F2、#F3、#F5、#F9,并补 #F6 的关键测试。两个 🔴 倾向治本:

- **#F1**:`quarantine_coverage_errors` 增加 exempt 白名单校验 —— 任何不在 `{QUARANTINE_MIRROR_DOMAINS} ∪ {golang.org, go.dev, pkg.go.dev}` 的 `firewall_exempt_domains` 条目直接报错。TDD:先写"把 crates.io 塞进 exempt → gate 必须拒绝"的失败测试(§8.1 场景),再实现。
- **#F2**:让 mirror 投毒信号从"容器有 quarantine config"派生,而非靠 `EVOCLAW_QUARANTINE` env 传播;或让 `run_milestone`/resume 路径重新 `load_quarantine_env`。TDD:先写"env-less lock_network 仍投毒 mirror"的测试。

### 11.2 下一阶段(单独 PR / ops)

- #3 完整版:quarantine 派生下沉到容器创建层(run_e2e/container_setup 自己按 repo_name 加载策略),run_all 只做聚合。
- #10 resume 半隔离守卫;#12 glob 逗号切分 gate 校验。
- cleanup:探测批量化(省 30–60s/lockdown)、policy `lru_cache`、`_reset_failure_signals` 收拢、双份 policy loader 合并、YAML 从 ECOSYSTEM 表派生。
- #2a 深层修复:SNI egress proxy(`docs/quarantine-rollout.md`)。
- ops:把 `klauspost/compress v1.17.8` zip 补进 go-zero 闭包并重建 base-offline。

---

## 12. 附录

### 12.1 本 commit(78f840f)改动文件

| 文件 | 改动 |
|---|---|
| `harness/e2e/quarantine.py` | +113/−:MIRROR 常量、goproxy_value、ECOSYSTEM_YAML_OFFLINE_KEY、gate 增强、image_for_repo→resolve_image、EVOCLAW_QUARANTINE/FIREWALL_EXEMPT 导出、quarantine_guard_error |
| `harness/e2e/container_setup.py` | ±155:删 5 mirror 域名、_poison_domain_list、_interpret_probe、lock_network Step4/5 接线、_url_reachable TimeoutExpired、verify 显式豁免 |
| `harness/e2e/run_e2e.py` | +22:--unprotected + 守卫调用 |
| `harness/e2e/agent_runner.py` | ±8:删裸词 overloaded |
| `scripts/build_offline_closure.py` | +33:_escape_go_path、_artifact_is_forbidden |
| `scripts/run_all.py` | −23:删 get_image_name |
| `scripts/verify_quarantine.py` | ±6:默认镜像 image_for_repo |
| `quarantine_configs/{zeromicro_go-zero,navidrome}*.yaml` | +firewall_exempt_domains |
| `harness/e2e/test_container_setup.py` | 新建(6 测试) |
| `harness/e2e/test_quarantine.py` | +217(gate/mirror/guard/exempt/image 测试) |
| `harness/e2e/test_overload_backoff.py` | ±11 |
| `scripts/test_build_offline_closure.py` | +24(escape/sdist 测试) |

### 12.2 关键新增符号

- `quarantine.py`:`QUARANTINE_MIRROR_DOMAINS`、`goproxy_value()`、`ECOSYSTEM_YAML_OFFLINE_KEY`、`quarantine_guard_error()`;`load_quarantine_env` 新导出 `EVOCLAW_QUARANTINE`、`EVOCLAW_FIREWALL_EXEMPT`。
- `container_setup.py`:`_poison_domain_list()`、`_interpret_probe()`。
- `build_offline_closure.py`:`_escape_go_path()`、`_artifact_is_forbidden()`。

### 12.3 验证命令(可复现)

```bash
# Tier 0
python -m pytest harness/e2e/test_quarantine.py harness/e2e/test_container_setup.py \
  harness/e2e/test_overload_backoff.py scripts/test_build_offline_closure.py -q

# Tier 1: 单 repo smoke(base-offline)
python scripts/verify_quarantine.py --repo zeromicro_go-zero

# Tier 1: gate fail-open 复现(#F1)
python3 -c "import sys,tempfile;sys.path.insert(0,'.');from pathlib import Path;\
from harness.e2e.quarantine import quarantine_coverage_errors as g;\
d=Path(tempfile.mkdtemp());(d/'quarantine_configs').mkdir();\
(d/'quarantine_configs'/'evil.yaml').write_text('ecosystem: [cargo]\ncargo_offline: true\n\
deny_domains: [crates.io, static.crates.io, index.crates.io]\n\
firewall_exempt_domains: [crates.io, static.crates.io, index.crates.io]\n');\
print('gate errors:', g(['evil'], d))"   # 期望 []=fail-open(修复后应报错)
```

### 12.4 相关 memory

- `project_quarantine_issue12_all_ecosystems`(已更新本次修复摘要 + 订正过时的 auto-exempt 描述)
- `project_gozero_goproxy_cheat`(已加 rerun 取证结论)
- `feedback_no_source_data_mutation`(取证 subagent 的 SAFETY 依据)
- `feedback_hardening_robust_not_fragile`(§13 的核心教训:别用脆弱机制换稳健机制)

---

## 13. 第五阶段:自审发现的回归修复(F1/F2/resume)

§8 的自审确认 `78f840f`(那个"加固"commit)自身引入了两个回归。本阶段用 TDD 修复,并对**修复本身**做了两轮独立对抗复审。**关键教训:F2 的第一版修复又重蹈了覆辙**,印证了 §14.1-E。

### 13.1 F1 — firewall_exempt 声明式 fail-open

**问题**:gate 和 verify 都无条件信任 YAML 的 `firewall_exempt_domains` 声明。把一个可 CIDR 挡的 registry(如 crates.io)写进 exempt,就同时绕过 gate 的 deny_cidr 要求**和** verify 的可达性探测 → registry 经 accepted CDN 段全可达。删掉的 `_shares_allowed_cdn` 是基于真实解析 IP 判断(骗不了),我的替代是纯声明信任(一行就骗过)。

**修复**:引入**代码级白名单** `FIREWALL_EXEMPTABLE_DOMAINS`(frozenset,仅 6 个真正走 Google-Vertex 共享段、不可 CIDR 的域名:proxy/sum/index.golang.org、golang.org、go.dev、pkg.go.dev)。gate 拒绝白名单外的任何 exempt 声明;verify 的 exempt 集也与白名单取交集(纵深:即使 `--unprotected` 绕过 gate,verify 也不会跳过一个可 CIDR 的 registry)。白名单是**代码常量(事实)**,不能被 YAML 声明绕过。

### 13.2 F2 — mirror 投毒 env-gating de-harden(v1 重蹈覆辙 → v2)

**问题**:#4 把 mirror 域名投毒 + GOPROXY=off 从"无条件常量"改成"依赖 `EVOCLAW_QUARANTINE` env"。该 env 只有 `run_all` 注入;直接 `run_e2e --resume-trial` / 手动 `run_milestone` 不注入 → 这些路径 de-harden(容器被重新解锁,go proxy 答案通道重开)。

**F2 v1(错误的第一版,已废弃)**:恢复放在 `lock_network` 里,用 `image_name.split('/')[0]` 解析 repo。第二轮对抗 finder 发现 **5 个缺陷**:
- **F2-a**:offline 开关(CARGO_NET_OFFLINE 等)是 `start_container` 的 `-e` flag,在 `lock_network` **之前**就定了;恢复在 lock_network 太晚 → cargo/pip 半隔离(防火墙挡了 registry,包管理器仍走 online → 合法构建挂)。
- **F2-c(重蹈覆辙)**:用脆弱的 `image_name` 字符串解析,而权威的 `repo_name` 就在上游 orchestrator、只是没传进来。registry-prefixed 镜像会误解析 → 有 policy 的 repo 裸奔。**这正是那条 lesson 说的"用脆弱信号而非已知事实"。**
- F2-b/d/e:--unprotected 交互、resume verify 路径、os.environ 全局副作用。

**F2 v2(正解)**:恢复移到 `ContainerSetup.__init__`(构造时,早于 start_container,故 offline `-e` flags 能拿到恢复的 env);接收上游**权威 repo_name**(orchestrator/run_milestone/verify_quarantine 都传,image 仅 fallback);case-insensitive 匹配(小写镜像名 `burntsushi` → 大写 config `BurntSushi`);`--unprotected` 设 `EVOCLAW_UNPROTECTED` 显式跳过恢复。第二轮 finder 复审确认 v1 的 5 缺陷在 v2 全部 FIXED。

### 13.3 resume --unprotected re-harden(v2 的 resume 推论)

**问题**:v2 的 __init__ 恢复让 resume 也触发恢复。但 `--unprotected`(开放对照 baseline)没持久化到 trial_metadata,resume 时忘带 flag → 恢复触发 → 开放 baseline 被静默 re-harden(前半段开放、后半段隔离,污染对照数据)。**方向是 fail-closed(过度隔离),不是安全洞。**

**修复**:`--unprotected` 持久化到 `trial_metadata`;`_run_resume_mode` 读回并在构造 orchestrator 前设 `EVOCLAW_UNPROTECTED`。往返验证:resume 开放 trial 保持开放、正常 trial 正确隔离、旧 metadata(无此 key)默认隔离(向后兼容)。

### 13.4 验证

Tier 0 **239** 单测;机制快检(F2-a/unprotected/F2-c/parity);真实容器(env-less go-zero 从 policy 恢复投毒 5 mirror 域名 + GOPROXY=off、canary 仍 FAIL、offline 开关容器内可见);7 repo verify_quarantine ALL PASS;**两轮独立对抗 finder** 复审。

### 13.5 残留(已知限制,非安全洞)

- **F2-e**:恢复用 `os.environ.update` 全局副作用,理论上"一进程锁两 repo"会串。当前架构一进程一 repo,**不可触发**,latent。彻底消除需改为"用返回值传递而非改全局 env"。
- **IPv6 投毒缺口**:/etc/hosts 只投毒 IPv4(`0.0.0.0`),不投 IPv6(`::`)。**既有**问题,容器 `--sysctl disable_ipv6` 已兜底。

---

## 14. 未来 harness 改进建议

从整个审查 + 修复过程提炼的、超出单个 bug 的**系统性改进**。按"架构级"(治本,改变脆弱模式)与"具体项"排列。

### 14.1 架构级(治本)

**A. quarantine 状态应有单一事实源(policy 文件),而非靠 env 在进程间传播。** 本次 F2 整个 de-harden 的根源就是"隔离状态靠一堆 `EVOCLAW_*` env 在 run_all→worker→lock_network 之间传播,任何一条路径忘了传就 de-harden"。v2 用 `__init__` 从 policy 恢复只是**缓解**;根本解应是:所有需要 quarantine 状态的地方统一从 `load_quarantine_env(repo)` 派生,env 仅作 canary/override 注入。这样"哪条路径忘了传 env"这一整类 bug 不复存在。**这是本次最重要的架构教训。**

**B. quarantine 派生下沉到容器创建层(#3 完整版)。** 当前 gate + env 注入只在 `run_all.py`,`run_e2e`/`run_milestone` 是补丁式守卫/恢复。根本应让 `container_setup` 自己按 repo_name 加载策略——任何入口创建容器都自动隔离,`run_all` 只做聚合。就不用在每个新入口重复打补丁(本次即是逐个入口补 repo_name/守卫/恢复)。与 A 同向。

**C. verify 应绕过 /etc/hosts、按真实 IP 探测(#2a 残留)。** 当前对投毒域名(goproxy.cn/io)的 verify 是**空转**的:hosts 投毒使按域名探测恒 BLOCK,验证不到 iptables/CIDR 层是否真挡住。真正的验证应 host 侧解析域名 → 容器内按 IP 连,才能抓到"打错的 CIDR"。否则 verify 对这些域名的"通过"是假的。

**D. gate 应校验 CIDR 覆盖,而非仅非空(#F3)。** 当前 `if need_cidr and not deny_cidrs` 只查 `deny_cidrs` 非空;`deny_cidrs: [10.0.0.0/8]`(完全不覆盖 pypi Fastly 段)照样过 gate。应引入 ecosystem→CDN-CIDR 表,gate 用 `cidr_overlaps_any` 校验每个 registry 的 CDN 段真被覆盖。

**E. 加固/隔离改动应把"独立对抗复审"纳入流程。** 本次实打实证明:**加固修复的作者自审有确认偏误,自写测试全绿远不够** —— F2 v1 通过了全部 TDD + Tier1,却有 5 个缺陷,只被独立 finder 抓到;且我**明知那条 lesson 仍重蹈覆辙**。建议把"每个隔离相关 PR 派独立 adversarial reviewer"做成 checklist/CI 步骤。

### 14.2 具体项

- **SNI egress proxy(长期,`docs/quarantine-rollout.md`)**:彻底解决"proxy.golang.org 走 Google 共享段无法 CIDR deny"的残留;落地后可去掉 firewall_exempt 白名单这类特例。
- **IPv6 完整封锁**:/etc/hosts 加 `::` 投毒 + iptables 覆盖 IPv6(不只靠 `disable_ipv6`)。
- **闭包完整性系统化**:go-zero 的 `klauspost/compress v1.17.8` zip 缺口是 agent 自己绕过时才暴露的。应把"逐 milestone 离线构建"作为闭包构建的强制门(部分已有),在**构建时**抓缺口,而非 trial 时。
- **cache-forbid-glob 输入健壮性(#12)**:含逗号/空格的 glob 会被切碎(当前 7 份配置不触发,latent)。gate 校验 glob 不含分隔符,或改用 JSON 数组传递而非逗号拼接。
- **cleanup**:探测批量化(单次 docker exec 并发探所有域名,省 30–60s/lockdown,resume 尤其受益);`load_quarantine_config` 加 `lru_cache`(现每 repo 解析 3×);YAML 的 registry/CIDR 从生态表派生(消除 N 份复制与 drift)。
- **失败信号统一分类**:把 rate-limit/overload/auth 等信号统一成一张结构化 token 表 + 单一分类器,避免散落的 substring 匹配(本次 #8 的裸词误判即散落匹配所致)。

---

*报告结束。核心待办:合并前修 #F1、#F2(两个 🔴,均本 commit 引入)。*
