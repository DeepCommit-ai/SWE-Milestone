<!-- Committed 2026-08-22 from the local planning notes (docs/superpowers/specs/residue-prune-spec.md), verbatim. -->
<!-- Status: design record (2026-07-10) for the residue prune that harness/e2e/residue_prune.py implements; the -->
<!-- operational side (enabling it per dataset, auditing prune facts) is post_verify/prune-config-authoring.md. -->

# Overlay 残留剪枝(Residue Prune)Spec — 定稿

> 状态:**设计定稿(2026-07-10),决策门 1/2/3 全部通过,待派生本仓库实现计划**。
> 证据链:三路机制/静态/实证取证 + 四项静态预测与假伤害审计,全部零 agent 成本实测;数字见 §8 附录。
> 背景:病灶③的法医分析见 DeepCommit 仓库 `docs/backlog/benchmark-refine-plan.md`;本文取代其中 P1"换基底"条目的实施方案,随评测器代码在本仓库维护。

**Goal:** 修正评测树的组装语义为"**源码区归 agent、测试区归 GT、环境区归镜像**",消除 overlay 残留(病灶③)的实证伤害;边界毛刺用内置 keep-list 点名解决。

**Architecture:** 不换基底。保留"checkout GT END + 叠加 agent tar"流程,在解包后增加一个删除 pass(v2 谓词 + START 溯源守卫 + keep-list),复用快照侧 `SrcFileFilter` 的子谓词。

---

## 1. 问题(一段话 + 指针)

评测树 = 整棵 GT END checkout + 纯加性 `tar -xf`(`evaluator.py:896`),零删除语义。后果(全部双向实证):**A** GT 新增文件存活(navidrome 迁移重声明,1 arm×5 milestone 全灭)、**B** agent 删除被复活(`wasm_base_plugin.go` 重构复活,23/30 arm)、**C** 连续模式累积传染(go-zero `range.go`,48 单元)。另:healing 方向(m002 双峰)根子是病灶①(GT 树自身坏),由自洽门负责,不在本 spec 范围;静默 START 回退由本 spec 的 fail-loud 字段终结。

关键设计事实:隐藏测试的注入机制 = END checkout 残留本身,故修法必须区分"该留的残留(测试/环境)"与"不该留的(源码)",而非笼统清除。

## 2. 剪枝规则(v2,最终版)

设 `S` = agent snapshot tar 路径集合;`E` = 基底树(END,回退时 START)在 src_dirs 内的路径集合;子谓词复用快照侧 `SrcFileFilter`(同一个类、同一份 metadata.json)。

**删除 p 当且仅当全部成立:**

```
p ∈ E
AND p ∉ S
AND is_src_file(p)                    # 天然排除测试文件与 exclude_patterns
AND NOT is_generated_file(p)          # 生成文件 = 环境供给
AND NOT is_modifiable_test_file(p)    # 可改测试 = GT 考卷
AND ext(p) ∈ 代码源扩展白名单          # .go .rs .java .py .ts .tsx .js .jsx .pyx .pxd .c .h …
AND p ∉ KEEP_LIST                     # 边界毛刺点名保护(§3)
AND p ∈ START 树                      # 溯源守卫(阶段一;阶段二撤除,见 §5)
```

> 不得整体复用 `should_include_in_snapshot` 作剪枝判据:该谓词把可改测试(`src_filter.py:254-255`,无 is_test 守卫)与生成文件(:249-251)也判 True,且 `is_src_file`(:158-185)无扩展名概念——按它删会撕考卷、删 go:embed 资产(证据 §8.3)。

权威表:

| 文件类别 | 权威方 | 行为 |
|---|---|---|
| 纯代码源文件(非测试/exclude/生成/可改,代码扩展名) | **agent** | tar 有→agent 版;tar 无→删(经守卫与 keep-list) |
| 普通测试文件 | **GT** | 保留(隐藏测试注入不变) |
| 可改测试(仅 navidrome 定义) | GT 考卷(agent 可覆盖) | tar 有→agent 版;tar 无→保留 |
| 生成文件 / src 内非代码资产 / exclude | 环境/GT | 永不删 |
| src_dirs 之外 | 环境/GT | 不碰 |

推论:剪枝后 src_dirs 内纯代码源文件集合 ≡ agent tar 同类内容——案例 A/B/C 由同一条规则消灭,连续模式累积传染终止(每次评测相对当次基底重剪)。

## 3. Keep-list 机制(边界毛刺的点名保护)

**定位**:config(src_dirs/test_dirs glob)按路径分区,覆盖 165 个 GT 新增文件中的 164 个;毛刺 = 路径与角色背离的文件(测试道具住在源码区)。用**专用名单**点名,不塞 test_dirs(按模式它们不是测试文件,glob 一次性豁免既脆弱又扰乱语义)。

**三分放置(2026-07-10 定案)**:**事实**存 SWE-Milestone-data 各 range `metadata.json` 新字段 `prune_keep_list`(精确 repo 相对路径列表,per-range,不用 glob——点名语义;缺省空列表,老数据零影响);**机制**在本仓库评测器(读取 + `p ∉ KEEP_LIST` + V3b 断言);**生成流程**(判别式重算)归 DeepCommit pipeline,追踪于 [DeepCommit-Env#28](https://github.com/DeepCommit-ai/DeepCommit-Env/issues/28)。

**派生判别式**(自动重算名单用;在 165 文件上恰好只圈出第 1 条):
仅被测试文件引用 **∧** SRS 未提及 **∧** 具夹具本质(形参 `testing.T` / 命名 mock|fake|stub|fixture / 嵌入 `tests.*` / 注释 "for testing")。

**初始名单**:

| 文件 | range | 理由 | 影响 |
|---|---|---|---|
| `core/mock_library_service.go` | navidrome | 唯一确证毛刺:src 区测试 mock,SRS 0 提及,零非测试引用者 | 不保护则 nativeapi 测试包连坐,~20 P2P 冤枉分;**START 守卫覆盖不了下游 milestone(它进入 sub-03+ 的 START 树而 agent tar 恒缺)→ 必须进阶段一** |
| `core/stores/dbtest/sql.go` | go-zero | 宽限候选:SRS FR14 明令路径(属契约,技术上公平)但漏一个 trivial move,畸重(~~152 测试~~ **2026-07-11 实算修正:`RunTest` = 66 唯一 N2P 测试函数/127 raw id,全 N2P 零 P2P**) | 阶段一由 START 守卫在 M028 引入点自然覆盖;**4b 队列 Q3 裁定:补 SRS 符号契约后不进 keep-list**(见 plans/2026-07-11-4b-contract-queue.md) |

维护:每次数据集 refine 后用判别式重算;keep-list 命中数进 `pruned_files` 审计字段;V3b 断言保护(见 §7)。

## 4. 契约补全:实验设置,非风险(2026-07-10 用户裁定)

隐藏测试绑定的标识符/签名/路径,**就是基准的验收契约**——这是实验设置本身。凡契约未写进 SRS 的,属 SRS 不完整,修法是**补 SRS(修法 4b,既定方法论)**,与剪枝无涉。

剪枝对此的作用是把缺口从"随机被残留掩盖"(有时垫着通过=leakage、有时撞名=级联)变成**确定的信号**。实测信号清单(§8.2):22/98 milestone、≈30 个 backing 文件、逐符号可枚举;其中 CONTRACT-GAP(agent 做了等价实现仅命名/路径不同)7–8 个 milestone,≈30 条 SRS 契约附录(0.5–1 人日)——**这份清单即 4b 的工作队列**。阶段一的 START 守卫只是把契约补全与剪枝发布在时序上解耦(守卫期间 GT 新增文件不剪,缺口维持今日现状),不是回避。

## 5. 实施范围(保守分阶段)

| 阶段 | 内容 | 预期 |
|---|---|---|
| **1a(先行,纯记录零风险)** | ① fail-loud 字段:`base_tag` / `fallback_triggered` / `pruned_files_count`(+ `pruned_files` 明细);② **快照完整性告警**(评测侧:tar vs START 应含集合;采集侧对偶检查:agent tag 全树经过滤 vs tar——抓 src_dirs 外丢失与未提交丢失);触发告警的格子**跳过剪枝**并标记 | 终结静默回退;揭穿 deepseek 类残缺快照(§8.4) |
| **1b(核心止血)** | v2 谓词 + START 守卫 + keep-list,**只上 navidrome + go-zero**(全部实证大额伤害所在;Go 包级连坐 = 伤害与收益同源) | 修复 ~69/76 实证受害单元;受益 arm 粗估 go-zero +5~15、navidrome 案例 A arm +15~30;**预期近单向**(↓ 通道经 §8.4 抽样:量级小且 ~17% 属"真回归该破"的公平改进) |
| **阶段二(4b 契约批次就位后)** | 撤 START 守卫 + 全语言 + ≈30 条 SRS 契约附录同批;keep-list 复审(dbtest 宽限决策) | 关闭 GT 新增文件 leakage、引入点单点碰撞(~7 单元)、ripgrep E0761(2 arm) |
| **本阶段明确不做** | TS/Java/Py/Rust 剪枝(实证伤害≈0)、leakage、引入点碰撞、隔离计分/真换基底 | 记录在案,不阻塞 |

## 6. 实现锚点(本仓库)

- 位置:`harness/e2e/evaluator.py` `_apply_tar_to_container()`(874-940),`tar -xf`(896)之后、`apply_patches.sh` 重放(905-916)**之前**;rust_test_filter(919-929)照旧在后。
- 两条基底路径都剪:`_apply_tar_with_fallback()` END 路径(977)与 START 回退路径(999,以 START 树为 `E`)。
- 删除清单:`tar -tf`(容器外)+ `git ls-tree -r <base-tag>`(容器内)差集 → v2 谓词 + 守卫 + keep-list 过滤 → 容器内批量 `rm -f`。路径归一化:剥 `./`、跳目录条目、symlink 按路径。
- 记录字段挂 `EvaluationResult.to_dict()`(195-228)。
- 规模:剪枝 ~40 行 + 字段 ~10 行 + 完整性检查 ~15 行 + 单测;不改快照格式、不改镜像、不需 agent 侧配合。

## 7. 验证(全零 agent 成本)

1. **V2 离线干跑**:scratch clone 模拟三案例集群(navidrome sub-01 家族、go-zero mathx、m004),断言:撞名消失,新增编译失败 ⊆ §8.2 预测清单;
2. **V3 GT-as-agent 不变量**:GT END 全树快照过完整管线,剪枝后与 END 树逐字节相等,resolved=true(自评方法论同步改为 GT-as-agent——空 overlay 自评在剪枝语义下失效);
3. **V3b 永不删断言**(V3 的盲区补丁——GT tar 什么都有,V3 对假伤害不敏感):单测覆盖五类永不删 + keep-list;运行期 assert:待删路径命中测试模式/可改测试/资产类/keep-list → abort 而非删除;
4. **V4 A/B 重评**(两个 Go range,存量 tar,剪枝 ON/OFF):每个分差归因三桶——撞名平反(↑)/ 真回归曝光(↓,公平)/ 其他(→ 停下来查)。阶段一预期近单向,任何无法归桶的 ↓ = bug。

## 8. 证据附录(2026-07-10 实测,数字不再展开,过程见各审计报告)

**8.1 V1-R(Rust inline 通道)**:inline 占计分测试 ripgrep 70.5% / nushell 23.1%,但与剪枝几乎不相交——12 arm × 全 24 milestone 穷举:**LIVE(新破坏在跑计分测试)= 0**,DEAD-TODAY 24(模块未被 agent 声明链触达,今天就是死代码),AMBIG 18(今天已 E0761,剪枝反修复)。→ Rust 无需豁免(但阶段一仍不上 Rust,因实证伤害为零)。

**8.2 V1-G(符号引用面)**:22/98 milestone 非零(navidrome 3 / go-zero 8 / element 9 / dubbo 1 / scikit 1),≈30 backing 文件,上界 796 目标测试(+381 同包放大);per-arm realized 中位 53/104/39/0/0;成因:Go 算法类 OMISSION 为主(诚实失败),CONTRACT-GAP 7–8 个(element barrel/路径、go-zero M028+M007.1、dubbo M003.3)→ 4b 工作队列。scikit doctest 通道存在但本次 GT 新增文件计分 doctest=0(**2026-07-11 复核:该表述须收窄——doctest 在 scikit range 其他位置活跃计分,M12.4 有 80 F2P doctest,仅 GT 新增文件上为 0**)。
> **2026-07-11 重建实算**(原明细未落盘,5 range 并行重算,锚点全部复现;JSON 归档 plans/v1g-2026-07-11/):非零 23(边缘 M003 方法级剔除后 22)、backing 宽口径 57、CONTRACT-GAP 20 文件/9 milestone;4b 工作队列定稿于 plans/2026-07-11-4b-contract-queue.md(P0+P1 ≈27 条)。

**8.3 假伤害审计(谓词侧)**:原谓词两漏洞——可改测试(navidrome `agents_plugin_test.go`,M007 载 9 条计分 n2p,284/284 tar 实测都带、零触发但原理不安全)、非代码资产(element 550/550 tar 各删 ~5 个 GT 新增 .pcss,jest mock 故分数不翻;dubbo 6/398 删 SPI/mustache,**被测试引用,真实翻分风险**;navidrome go:embed .sql = 潜在 go build 灾难,零触发)。→ v2 合取后全部归零。

**8.4 假伤害审计(脚手架 + P2P 遮蔽)**:165 个 GT 新增源文件 = MAIN 145 / UNREFERENCED 15 / TEST-ONLY 5,其中真毛刺仅 1(mock_library_service.go)+ 畸重契约 1(dbtest);P2P 复活遮蔽抽样 12 例:~75% 合法重构(剪除安全;profiling 案例示警符号迁移≠API 一致)/ ~17% 真回归被遮蔽(mathx 68、profiling 8,剪除=公平改进)/ ~8% 即毛刺。**附带发现**:deepseek-v4-pro 快照病态残缺(整包缺失 13–96 文件/milestone,靠复活续命)→ 1a 完整性告警的直接动因,历史分数标记存疑。

## 9. 决策记录

| 日期 | 决策 |
|---|---|
| 2026-07-10 | 门 1:Rust 不豁免,全语言统一 v2(V1-R LIVE=0);门 2:破坏面可枚举可控(V1-G) |
| 2026-07-10 | **用户裁定**:契约缺口属实验设置而非剪枝风险,SRS 补全(4b)是既定方法论;剪枝产出的 CONTRACT-GAP 清单 = 4b 工作队列 |
| 2026-07-10 | **用户认可**:边界毛刺用内置 keep-list 点名解决;保守分阶段(1a 记录先行 → 1b Go 两 range 止血 → 阶段二全量+4b) |
| 2026-07-10 | **放置裁定**:本 spec 随评测器代码在本仓库维护(docs/residue-prune-spec.md);法医分析与数据 refine 方法论留在 DeepCommit docs/backlog |
| 2026-07-10 | **keep-list 三分放置**:事实 = SWE-Milestone-data metadata.json `prune_keep_list`;机制 = 本仓库评测器;生成流程 = DeepCommit([#28](https://github.com/DeepCommit-ai/DeepCommit-Env/issues/28)),实现经验回填后落地 |
| 2026-07-10 | **用户批准开始实现**:1a + 1b 两阶段;阶段二仍待 4b 契约批次 |
| 2026-07-11 | **1a+1b 实现落地**(`106a9b0`),`harness/e2e/residue_prune.py` 纯逻辑 + evaluator/orchestrator/run_milestone 接线;A/B run1(12 cell)全部归因 |
| 2026-07-11 | **对抗审查一轮**(claude 双 subagent 正确性/红队 + 我自查):F1 完整性门 fail-open(→改 fail-closed)、F2 过滤器漂移、F3 语言范围泄漏 + 6 小项,`c04907a` 修复 |
| 2026-07-11 | **codex 独立复查**发现 claude 三方都漏的 **Critical**:orchestrator `_run_evaluation_once` 用阈值重算覆盖 fail-closed verdict(CLI 路径安全、真实 trial 路径失效)。连同 codex F2–F6(capture 侧 symlink 绕过、config-omission fail-open、witness 不完整、sidecar 未绑定、扩展未归一化)一并修复 → **`EvaluationResult.scoring_untrusted` 属性,所有 resolved 重算点必须 AND 之** |
| 2026-07-11 | **§11 越界改动逐条裁决**(用户):11.1 A/B/C 全部保留,B 补加固(丢弃日志 debug→warning + 存量 tar 只读扫描);11.2 D/E 保留(re-evaluation 流程 = 现实漂移场景,witness 非未来假想);11.3 F/G 均追认;11.4-H 按双信号重写(porcelain 未提交 + prev-tag diff 范围外;采集容器无 START tag——setup 反泄漏删光,以 prev agent-impl tag 为最近基线)。spec 按用户裁定留在 process artifacts 区,不搬回 docs/ |
| 2026-07-11 | **通用化(用户指示"扩展到所有 repo 所有语言,任何数据可复用")**:默认开语义(`resolve_prune_enablement`:有 src/test 分区即剪,legacy 无分区不剪不锁分)+ 全语言默认扩展;验证 run4(5 新 range:V3 GT-as-agent 5/5 全绿、V4 ON/OFF 10/10 逐 test CLEAN)+ run5(navidrome/go-zero 全语言 B/C 对照 16/16 SAME,案例 B 的 TS/JSX 通道修复实证,.lua 边界正确);config 生成 skill 落 `docs/post_verify/prune-config-authoring.md`(agent 智能生成 metadata 事实/代码只管谓词的分工)。**生效推迟**至 glm-5.2 批次结束(pending 清单:代码 commit + 数据 repo revert a383f21)。**附带发现**:跨镜像同名 tag 树不一致 → DeepCommit-Env#29,离线分析必须用 milestone 自己的镜像 |
| 待定 | dbtest 宽限(倾向阶段一不做特判,记入 4b 批次);阶段二启动时间(随 4b 契约批次);sidecar tar-digest 硬绑定(codex F5 深化,follow-up);GT 自洽普查扩展(nushell core_dev.1 census + go-zero M004 自评失败,数据侧) |

## 10. 无安全门 + fail-closed 架构(对抗审查后确立,2026-07-11 用户裁定去门)

**核心原则:tar 缺席永远意味着 agent 删除,永远剪枝。没有"快照可疑就跳过剪枝"的安全门。** 近空 tar → 删光 base 源码 → 老实编译失败得 0,而不是被门"保护"进而白嫖 GT 解。

- **为什么去门**:原设计有一道完整性门(缺失>阈值→跳过剪枝),本意保护"采集管线 bug 弄坏快照"的诚实 agent(deepseek);但这道门**fail-open**——攻击者投近空 tar 主动触发它、绕过剪枝、复活 GT 全解。用户裁定:去掉门,残缺快照按 agent 删除如实判低分,采集 bug 去修采集管线(完整性仍记录在 stdout 日志 + `snapshot_integrity_ok` 字段供排查,但**绝不影响评分**)。实测:近空 tar 打 M028 → 剪 343 文件 → resolved=False、F2P 0/0(攻击死)。
- **fail-closed 仅剩"机制失败"**:`EvaluationResult.scoring_untrusted` = `residue_prune_skipped_reason ∈ {ls-tree-failed, tar-unreadable, config-invalid}`(`FAIL_CLOSED_SKIP_REASONS`,**已移除 snapshot-integrity-failed**)。这些是"剪枝器根本没法跑"(不是启发式门):ls-tree/tar 读不出 → 硬错误(eval 失败);config 缺失 → scoring_untrusted。强制点:`compare_results` + `orchestrator._run_evaluation_once`(阈值重算 AND `not scoring_untrusted`);CLI/run_milestone 直接用 `resolved`。**新增任何 resolved 重算点都必须 AND `scoring_untrusted`**。
- **witness(F2/F4)**:snapshot 完整性 sidecar 记录采集期 filter 配置(`capture_filter`),eval 侧重建该 filter、对 START 树求"被采集期判为测试/排除"的集合 = drift-proof witness(覆盖 agent 删掉的 GT 测试)。sidecar tag 不匹配 milestone 即忽略(F5)。存量 tar 无 sidecar → witness=None,仅在"采集后 metadata 未漂移"时安全(本阶段两 range 已核实无漂移)。
- **capture 侧硬化**:非常规 tar 成员(symlink/hardlink/device)一律丢弃(F2 codex,防 symlink 冒充 GT 测试);过滤异常不再退化为未过滤 tar,改为 raise。

**下一步**:阶段二(撤 START 守卫 + 全语言 + 4b 契约批次)。§11 越界改动已全部裁决关闭(2026-07-11)。

## 11. 越界改动复核 — 已裁决(2026-07-11,全部关闭)

> 背景:实现过程中为剪枝的安全/健壮改了一些**用户未明确要求**、且部分**触及全部 7 range 都走的采集公共路径**的地方。2026-07-11 用户逐条裁决,结果如下。

### 11.1 公共路径改动(A/B/C)→ **全部保留,B 补加固(已落地)**

- **A 过滤异常 `raise`**:保留。原"降级用未过滤 tar"= 把 GT 隐藏测试整包泄入快照归档,是数据完整性洞,不只是剪枝问题;触发条件极罕见(tarfile IO 失败)。
- **B 丢弃非常规 tar 成员**:保留 + 两项加固:① 丢弃日志 debug→**warning(聚合,含样本)**,不再静默;② 存量 4190 tar 只读扫描,全部命中仅两类,均零伤害:**element-web `src/customisations/README.md`(上游 repo 自带 symlink,729 tar)**——非代码扩展名永不剪(v2 谓词),eval 树由 GT checkout 提供,今后每次 element 采集有一条预期内 warning;**1 个 navidrome tar 的 `ui/node_modules/.bin/*`(52 个 symlink,agent 误提交依赖目录,老管线产物)**——本就不该进快照,丢弃即清理。附带发现:2 个 sonnet-4.5 element 空 tar(历史采集残缺,另案,佐证 1a 完整性检查动因)。
- **C ls-tree `-z` + `quotePath`**:保留,无争议。

### 11.2 witness 机械(D/E)→ **保留**

裁决依据:`docs/re-evaluation.md` 的重评流程(旧 tar × data-side repair 后的 metadata)**就是**"采集后漂移"的现实场景——witness 保证重评时不把"采集期被过滤的测试文件"误判为 agent 删除。非未来假想,保留 D(capture_filter sidecar + eval 侧重建)与 E(tag 绑定)。

### 11.3 设计判断(F/G)→ **均追认**

- **F**:ls-tree/tar/config 三种机制失败锁 `scoring_untrusted`。降为 warning = codex F1 critical(剪枝没跑的格子按加性 overlay 评分)原样重开。追认。
- **G**:`SWE_MILESTONE_RESIDUE_PRUNE` 开关是 re-eval 流程刚需;`residue_prune_enabled` 已留痕结果 JSON。追认。

### 11.4-H 采集丢包告警 → **已按双信号重写(2026-07-11 落地)**

原实现盲区确认属实(tag 树对比抓不到未提交文件)。**采集容器内不存在 milestone START tag**(container_setup 反泄漏删光全部 tag),故"START 树 vs tar"在采集侧不可得,等价替代为三通道:

1. **管线丢包**(保留):agent tag 应含集合 vs tar;
2. **未提交丢失**(新):采集时刻容器内 `git status --porcelain -z`,经 test/exclude 降噪 + 采集范围分桶(in-scope = 本应进 tar 的工作;out-of-scope = 放错位置永远进不了 tar)。注:tag 检测异步,可能混入 agent 已开始的下个 milestone 在制品 → 定位为启发式 warning;
3. **范围外提交**(新):`git diff --diff-filter=d prev..tag` 中不被 archive pathspec 覆盖的路径;prev = 上一个 agent-impl tag(trial 首个 milestone 无基线,记 `outside_diff_base: null` 跳过)。

三通道均 warning + sidecar 字段(`uncommitted_lost_*` / `uncommitted_outside_*` / `committed_outside_*`),绝不中断采集。纯逻辑(porcelain 解析 / 范围判定 / 分桶降噪)入 `residue_prune.py` 并单测覆盖。
