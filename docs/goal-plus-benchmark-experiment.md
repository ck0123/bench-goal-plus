# Goal Plus benchmark 接入与并发实验协议

## 结论先行

这 7 套 benchmark 与 Goal Plus 的总体方向是匹配的，但目前还不能直接组成公平大实验。真正缺的不是再写一个 prompt，而是把 **artifact 型任务、并发控制、统一 wall deadline、Codex 总控成本和原始分数轨迹** 接入同一控制面。

优先级如下：

| 优先级 | Benchmark | 匹配度 | 原因 |
|---|---|---|---|
| P0 | Frontier-Engineering v1-lite | 很高 | 原生就是固定预算下的连续工程优化，且已内置 OpenEvolve/AB-MCTS adapter |
| P0 | HeuriGym | 很高 | 9 题 CPU 可跑，合法性和 cost 分离，适合看反馈利用与搜索效率 |
| P0 | OpenEvolve CPU examples | 很高 | 可直接固定相同 evaluator，对 Goal Plus 与 OpenEvolve 做方法级比较 |
| P1 | ALE-Bench Lite | 高 | 10 题有连续 raw score 和 public/private 边界，适合验证 generalization |
| P1 | EdgeBench open-source subset | 很高 | native work/judge 隔离、连续 raw score 和正式 reference curves 都适合搜索策略对比 |
| P1 | SwarmResearch 15 | 很高但工程未齐 | 最接近“并发研究是否真的有价值”的最终对手与任务集 |
| P2 | AutoLab CPU subset | 高但昂贵 | 最能验证长时 persistence，但预算应按 agent-hours 而非 iteration 对齐 |
| P2 | Frontier-CS Algorithmic | 中高但昂贵 | partial score 有搜索梯度，但 188 题和 judge 资源不适合先全量跑 |

核心 claim 不能写成“4 个并发 worker 比 1 个更容易撞到好答案”。那只是 agent 版 Pass@4。应该写成：

> 在相同模型、任务、总 wall budget 与并发槽下，Goal Plus 通过跨 lineage 的 Search Evidence、可修订 Search Schema、去重/准入和同 worker 延续，获得高于独立并发与 OpenEvolve 的 deadline best / best-score AUC；实际 evaluator calls、tokens 与成本同时报告，优势不能只由更多采样解释。

---

## 先把三种“并发”分开

| 维度 | 记号 | 含义 | 是否属于方法能力 |
|---|---:|---|---|
| Agent 并发 | `K` | 同一题同时思考、改 artifact 的 lineage 数 | 是，Goal Plus/Swarm/OpenEvolve 的核心比较维度 |
| Evaluator 并发 | `E` | 同时运行多少个官方 verifier | 通常不是；会影响 CPU、内存和 timing 噪声 |
| Task 并发 | `Q` | 同时跑多少道不同任务 | 不是；只是缩短 campaign wall time |

正式报告必须分别记录 `K/E/Q`。例如 Frontier-Engineering 官方 `v1_lite.yaml` 的 `run.max_parallel=4` 是**四道任务并发**，并不等价于每道题内部有四条搜索 lineage。

对计时型任务使用：

```text
K = 4 agent lanes
E = 1 serialized verifier
Q = 1 task
```

这样 agent 可以并行形成假设和候选，但 MallocLab throughput、AutoLab system task、Background Blur 等 wall-clock fitness 不会被并发评测污染。

---

## Goal Plus 当前已有能力与必须整改项

当前跟踪官方 `yiyanzhi_akane1/muyuan` 的 `master` 分支下 `plugins/goal-plus`；具体实验会把当次 Muyuan root
resolved commit 和 Goal Plus source path 写入 manifest。该分支已有 Codex/Pi
`parallel_loops` 与 `adaptive_search`、独立的实时并发 `max_parallel` 与可空累计候选上限 `max_candidates`、
同 native worker continuation、verifier-backed best、Search Evidence/Schema、
worker min/max runtime 和 usage report；benchmark-specific fixture 已从 runtime 仓迁出。

但用于这批 benchmark 仍缺 6 项：

1. **通用 artifact adapter**：现有 `goal_plus.benchmarking` 只 materialize MCQ/numeric 的 `answer.json`；要扩为 `materialize → prompt → evaluate → parse → archive`，允许 C++、Python、Rust、配置和多文件 artifact。
2. **批处理 Codex controller**：当前通用 benchmark runner 只有 `fixed`/`pi-rpc` backend。Codex 真 E2E 由顶层 `codex exec` 驱动 `spawn_agent/wait_agent/followup_task`，需要把 ST runner 方式产品化并保存总控 transcript。
3. **多次 verifier 闭环**：当前 benchmark runner 每个 candidate 最后只验证一次；这些任务要求同一 lineage 多轮验证、继续和 best-local 回滚。
4. **统一 wall controller 与 evaluator ledger**：正式系统对比由外层 controller 固定 `T/K`，每次 verifier 仍原子记账但不拒绝调用。只有明确标为 mechanism ablation 时才启用 hard ticket cap。
5. **预算强制与成本覆盖**：不为了模仿 OpenEvolve round 改 Goal Plus core。正式实验以 outer wall deadline 做硬边界，同时记录 worker + Codex 总控的 tokens/cost；缺字段必须标记 coverage，不可当作 0。
6. **统一轨迹导出**：每次 evaluation 保存 `candidate/parent/artifact hash/raw metric/direction/validity/call index/wall time/model usage`，才能计算 AUC、重复率和跨 lineage transfer。

这些改动优先放在本仓。只有 task 自身 evaluator 或 runner 必须变更时，才改对应 fork。

---

## Codex 接入整改总表

先区分两种接法：**Plain Codex** 是一次 `codex exec` 直接在题目 workspace 中改 artifact；**Goal Plus + Codex** 是 Goal Plus 管理多条长期 lane、共享 Search Evidence/Schema，并由 Codex worker 反复修改和验证 artifact。前者只证明 Codex 能做题，后者才是本项目要比较的搜索方法。

| Benchmark / baseline | Plain Codex 当前证据 | Goal Plus + Codex 当前状态 | benchmark / fork 要改什么 | `bench-goal-plus` 要新增什么 | `goal-plus` core 要改什么 | 最小完成门槛 |
|---|---|---|---|---|---|---|
| ALE-Bench Lite | **已通 1 题**：AHC027 可由 `scripts/run_codex.py` 运行并通过官方 public verifier | 未接通 | 不改上游；保持 public feedback / private final 边界 | SearchSpec、每 lane 独立 workspace、public raw-score parser、全局 call gate、最终 private-lite 一次评测 | 无 benchmark-specific 改动 | `K≥2`、同一题多轮 public evaluate、可继续同 worker、产出完整 usage/trajectory |
| HeuriGym | 未跑 Codex smoke | 未接通 | 原则上不改上游；仅在固定 fork 保留必要的 macOS/数据下载兼容补丁 | 9 题 materializer、数据 digest、solver-only 编辑约束、统一 `verify()+evaluate()` wrapper、minimize parser | 无 benchmark-specific 改动 | 先通 1 题三轮，再通 9 题；每次记录 valid/cost/runtime |
| Frontier-Engineering v1-lite | 只验证过 MallocLab evaluator，不等于 Codex 做题已通 | 未接通 | 在固定 fork 新增 `frontier_eval.algorithms.goal_plus` 插件；不改 task/evaluator | Goal Plus controller、history/usage exporter，复用 `metrics.json` 与 `artifacts.json` | 无 benchmark-specific 改动 | MallocLab 20-call 闭环；随后 lite 10 题能与原生 OpenEvolve/AB-MCTS 共用同一 evaluator |
| AutoLab CPU subset | 官方 verifier smoke 已验证；Codex agent 尚未跑通 | 未接通 | 通常不改任务；若 Harbor agent discovery 要求注册，只加薄 agent shim | Harbor workspace/container bridge、允许文件白名单、reward parser、CPU/内存/硬件指纹采集 | 无 benchmark-specific 改动 | 先通 1 个 puzzle/challenge 的 `K=2,E=1` 长时 run，并能恢复/保留 best artifact |
| SwarmResearch 15 | 只验证过 circle-packing evaluator；Codex 全链未通 | 未接通 | **需要修固定 fork**：bootstrap/import 与 ADRS/ALE worker build context；不改评分语义 | 15 题 `task-eval → native metric` adapter、session/commit/call/cost 轨迹转换、长期 lane controller | 无 benchmark-specific 改动 | 先通 1 题，再做 5-task `K=4/8` pilot；能与公开 Swarm 轨迹按 calls/cost 对齐 |
| Frontier-CS Algorithmic | 只验证过 problem 0 judge；Codex 全链未通 | 未接通 | 不改上游 judge | 10 题 materializer、controller-owned 容器池、partial-score parser、串行 evaluator gate | 无 benchmark-specific 改动 | 单题 20-call 闭环能接受“合法 partial score 但 `passed=false`”，再扩到冻结 10 题 |
| EdgeBench open-source subset | **已通 1 题**：VLIW 的 Plain Codex、hidden judge、final archive 和完整 session usage 可用 | **已通 1 题**：旧长 run 已验证 K 个 internal workers/promotion；新 controller 已完成同 T/K/model lifecycle E2E | 跟踪 fork branch；其中已增加 K/worker lease 参数、Codex JSONL usage、session archive、managed Goal Plus source 与 CLI path 修复；不改 task/judge 语义 | 已有 `provision/doctor/prepare/run/status/stop/finalize` campaign controller；待冻结 8–12 题 profile | 无 benchmark-specific 改动 | 用 `T>=300s,K>=2` 补一轮实际 dispatch worker 的 matched pilot；随后 8–12 gradient cases 可在 Linux 批量调度 |
| OpenEvolve CPU examples（任务包 + 原生基线） | **已通 1 题**：Function Minimization 由通用 adapter materialize，Plain Codex 将 raw score 提升 23.46%，4 public + 1 final calls | runner 已实现、真实模型 run 待验收 | 不改 OpenEvolve controller/provider；原生 OpenEvolve 保持自己的搜索入口 | 已有 task catalog、materializer、原生 evaluator wrapper，以及 `T/K` 外层控制的 native/Plain/Goal Plus 三入口；待补真实三方法证据与 usage coverage | 不需要 | 在同一 Function Minimization wall budget 与并发下补原生 OpenEvolve 与 Goal Plus；随后扩到 Background Blur、Circle Packing 和两道 JAX 数学题 |

### 表格结论

- **不需要把每个 benchmark 都改造成“支持 Codex API”**。Codex 本身通过 CLI 在隔离 workspace 中读题、改文件、跑测试；benchmark 侧只需稳定的 materializer 和 evaluator adapter。
- **Goal Plus 已经能把 Codex 当 native worker 使用**；当前缺口是把已在 ST 中验证过的 Codex 总控方式产品化为批量实验 runner，而不是再做一套模型 API client。
- 绝大多数整改应落在 `bench-goal-plus`。需要改固定 fork 的固有接口主要是 Frontier-Engineering 的 algorithm plugin 和 Swarm 的复现基础设施问题；OpenEvolve examples 只抽取 task/evaluator contract，不修改其 controller 或 provider。
- `goal-plus` core 不需要为六套 benchmark 各写逻辑，也不需要加入 OpenEvolve round/call 模拟器。外层 wall controller、evaluator ledger、总控 usage 与统一轨迹都留在本仓。
- 因此当前真实状态是：**ALE、HeuriGym、AutoLab、Frontier-Engineering、
  Frontier-CS 与 EdgeBench 已各有至少一题的 Plain / Goal Plus 真实路径证据；
  OpenEvolve CPU task 包已有批量 materialize/evaluator 能力；SwarmResearch 仍是
  最主要的完整 agent 路径缺口。正式大实验尚缺 matched multi-seed 数据与冻结
  subset，不能把这些接线 smoke 当作方法排名。**

---

## 不是 Pass@4 的实验设计

### 必跑方法

| 方法 | 并发 | 共享信息 | 作用 |
|---|---:|---|---|
| Single AutoResearch | 1 | 自己的历史 | 长链 baseline |
| Independent Parallel | `K` | 无 | 精确控制“只是多抽 K 次”的收益 |
| Shared Raw History | `K` | 最近原始日志 | 判断结构化 Evidence/Schema 是否有额外价值 |
| OpenEvolve | `K` 个 process workers | population、islands、migration、artifacts | 主要 evolutionary baseline |
| Goal Plus | `K` 条长期 lane | Evidence、Schema、coverage/reservation、continuation | 完整方法 |

SwarmResearch 15 上再加入论文原生 Swarm；Frontier-Engineering 上再加入仓库原生 AB-MCTS。消融可以后补，但 Independent Parallel 和 OpenEvolve 不能省。

### 主预算与机制消融

不可能同时严格匹配 wall time、tokens 和 evaluator calls，因此同一 run 输出两套视图：

1. **系统级主结果**：相同 wall deadline、`K/E/Q` 和硬件；比较 time-to-target、deadline best 和 wall-time AUC，并把实际 calls/tokens/cost 作为效率与解释变量。
2. **机制消融（可选）**：相同 `B` 个 evaluator tickets、相同模型和 `K`；只在需要隔离 feedback 数量时比较 final best 与 call-index AUC。它不能要求改坏 Goal Plus 的原生调度。

主结论以系统级 `T/K` 对比为准，同时用 actual tokens/cost 和 evaluator calls 排除“只是用了更多 compute”的替代解释。`K=4` 与 `K=1` 的 scaling slice 必须明确 agent compute 不同，不能直接写成算法效率提升。

### 主要指标

- task-native final best，保留原始方向；
- normalized gain 与 best-score AUC（分别按 evaluator calls、wall time、known cost）；
- time/calls/cost-to-threshold；
- valid candidate rate、编译/运行/约束失败分解；
- declared/realized overlap、active collision、重复 diff/机制比例；
- post-stagnation improvement、escape rate；
- cross-lineage transfer：一个 lane 采用其他 lane 的 verified evidence 后产生的增量。

`pass@K` 只在本来就是二元任务时作为辅助指标。对于连续优化题，主要结果不能退化成“是否有任一候选超过阈值”。

### Goal Plus 必须满足的 go/no-go 条件

要声称并发协调有价值，至少要同时看到：

1. 相同 `T/K/model/host` 下，Goal Plus 对 Independent Parallel 的 paired deadline best 或 wall-time AUC 差值为正；
2. 对 OpenEvolve 的 final best 或 AUC 有稳定优势，而不是只赢一题；
3. 重复/碰撞下降，或 post-stagnation escape / cross-lineage transfer 上升，给出机制证据；
4. 把总控 agent 的 tokens/cost 算入后，单位成本收益仍成立；
5. 至少 3 seeds，并报告 task-level win/tie/loss 与 paired bootstrap CI。

如果只满足“最终 best 略高”，但 calls、总控成本或重复率没对齐，应归类为更多采样或方差，不是 Goal Plus 的结构性收益。

---

## 逐 benchmark 整改与对标

| Benchmark | Pilot `K/E/Q` | Work-matched 主预算 | 原生/论文坐标 | 最终主结果 |
|---|---:|---:|---|---|
| ALE Lite | `4/1/1` | 20 public calls/题 | iterative public refine；private final | private raw score + public-call AUC |
| HeuriGym | `4/1/1` | 12 calls/题 | 原生 3 iterations | valid + native cost/AUC |
| Frontier-Eng lite | `4/1/1` | 20 pilot、100 formal | OpenEvolve/AB-MCTS；100 iterations | raw score + Medal + AUC |
| AutoLab CPU | Mac `2/1/1`；Linux `4/1/1` | equal agent-hours；另报 equal wall | 单 agent 1–12h、anchored reward | native metric + reward + time/cost curve |
| SwarmResearch 15 | `4–8/1/1` pilot；最终对齐公开轨迹的 peak live agents | calls + known cost；另报 3h slice | 50 美元 cutoff、约 100 美元轨迹、50-agent 总规模参考 | native score/AUC + collision/transfer |
| Frontier-CS 10 | `4/1/1` | 20 calls/题 | high/low Pass@1 分层 | checker raw partial score + AUC |

### ALE-Bench Lite

**整改**：复用现有 `adapters/ale`，补 SearchSpec 生成器、官方 public-eval process verifier、lower-is-better raw parser、每题全局 call gate，以及 final private-lite 一次性提交。每条 lane 必须从相同 starter 独立 materialize，private data 不进入 workspace。

**建议并发/预算**：pilot 用 `K=4, E=1, Q=1, B=20 public calls/题`；evaluator 内部可用 4 case workers。10 题筛选后只对 frozen best 做一次 private-lite。

**与官方工作对应**：官方示例是 initial generation + public feedback iterative refinement，private 最终一次；因此 Goal Plus 的每个 verifier ticket 对应一次 `public_eval`，不能拿 Codex turn 当 iteration。官方 rank/performance 和现有模型结果只做外部坐标，主因果比较需重跑相同 Codex 模型。

**是否匹配**：匹配。它能区分“4 个独立程序”与“4 条 lane 利用共享路线/失败证据后继续优化”，而且 raw score 提供比 Pass@4 更强的梯度。

### HeuriGym

**整改**：为 9 题做统一 materializer，固定 train/demo/eval 数据边界；solver 为唯一可编辑面；包装每题 `verify()` + `evaluate()`，输出 `valid/cost/runtime`，并保留最小化方向。下载和固定 Hugging Face 数据 digest。

**建议并发/预算**：smoke 使用官方 `3 iterations` 口径；主实验用 `K=4, E=1, B=12 calls/题`。官方执行默认 8 cores、10 秒 timeout；当前 Mac 不应并行跑 4 个 8-core evaluator。

**与官方工作对应**：先复现原生 LLM Solver Agent 的 3 轮结果，再跑相同模型的 independent/OpenEvolve/Goal Plus。跨题主指标保留 native cost，并附 expert-relative quality；不要只比较 valid/pass rate。

**是否匹配**：高度匹配，且最适合做第一套完整 9 题实验。失败类型明确、反馈可归因，Goal Plus 是否避免重复无效 solver 很容易观测。

### Frontier-Engineering v1-lite

**整改**：最少的 benchmark-specific 工作。直接在 `frontier_eval.algorithms` 新增 `goal_plus` Algorithm，复用 unified task 的 `initial_program/eval_command/metrics.json/artifacts.json`；不要为 10 题各写 judge。增加 Goal Plus history exporter，与 OpenEvolve 的 `history/index.jsonl` 对齐。

**建议并发/预算**：pilot `20 calls/题`，正式与 v1-lite 对齐为 `100 calls/题`。方法并发用 `K=4`；计时型 evaluator 设 `E=1`。batch 的 `run.max_parallel` 固定为 `Q=1` 做方法学实验，campaign 吞吐测试可另设 `Q=4`。

**与官方工作对应**：上游已经提供 OpenEvolve、AB-MCTS、ShinkaEvolve 和 frozen v1/v1-lite leaderboard。Goal Plus 应成为同一 `frontier_eval` algorithm entry，使用相同 task runtime 和 raw `combined_score`；final 同时报 raw、Medal Score 和 100-call curve。

**是否匹配**：当前最强的 head-to-head substrate。它原生强调 fixed interaction budget 与 best design，并且 v1-lite 专门挑了随预算渐进提升的题。

### AutoLab CPU subset

**整改**：写 Harbor agent adapter，把 Goal Plus 控制状态放在容器外；每条 lane 拥有独立 task filesystem，只有 task.toml 允许文件可编辑；`tests/test.sh` 的 reward/正确性作为 process verifier。增加 container CPU/memory、硬件 fingerprint 和反 shortcut 证据。

**建议并发/预算**：Mac 先 `K=2, E=1`，只跑 puzzle/challenge；Linux 64 GB 再用 `K=4`。正式同时做：`1×2h` 对 `4×30m` 的 equal-agent-hours，以及 `4×2h` 的 equal-wall-time scaling。不能把后者直接说成效率提升。

**与官方工作对应**：官方任务预算是 1–12 小时，CPU system leaderboard 推荐 Ryzen 9 9950X/64 GB。Mac 可做同机方法比较，但 system task 的绝对 speedup 不与 leaderboard 混用；最先选 Toy ISA、VLIW、stack machine、sorting network 等硬件不敏感题。

**是否匹配**：适合验证 persistence 和“并发宽度 vs 单链深度”，但不是第一套全量实验。OpenEvolve 对比需按实际 evaluator calls/model calls 和 agent-hours重算，不能按 iteration 名称对齐。

### SwarmResearch 15

**整改**：先修复复现仓 bootstrap/import 和 ADRS/ALE 共享 worker build context；建立 15 题统一 `task-eval → native metric` adapter。Goal Plus 必须记录 orchestrator + workers 全部 usage，并允许固定 lane 长期 continuation；Swarm 轨迹则统一还原 agent session、commit、evaluator calls 和 50 美元 cutoff。

**建议并发/预算**：5-task pilot 用 `K=4/8`；最终实验先从公开轨迹重建 peak live agents，再匹配实际 `K`，同时向 README 提到的 50-agent **累计规模**扩展。不能把“50 agent run”未经证据解释为 50 个同时在线。work-matched 优先按 evaluator calls + known cost，另给 3 小时 wall slice。Goal Plus 的 `max_parallel` 是固定 lane 数，不应与 Swarm 累计 spawn 的 agent budget 直接画等号。

**与论文工作对应**：公开任务轨迹约 100 美元/题，论文报告 50 美元 cutoff；README 另给出 50 agents、约 3 小时 Codex 运行的成本参考。外部论文分数只能做 reference；要归因于方法，必须用同一 Codex 版本重跑原生 Swarm 与 Goal Plus。

**是否匹配**：最适合最终大实验。这里 Goal Plus 必须证明它比 Swarm 的 population steering 和 OpenEvolve/SkyDiscover 更早发现高价值、低重叠方向，而不是仅仅多开 agent。

### Frontier-CS Algorithmic

**整改**：materialize 冻结的 10 题子集；每题生成通用程序 workspace；judge 运行在 controller-owned container。parser 必须接受“合法 partial score 但 upstream `passed=false`”的结果，并保存 checker-native score、合法性和错误消息。

**建议并发/预算**：`K=4, E=1, Q=1, B=20 calls/题`。当前 Mac 的 8.4 GB Docker VM 不能并发启动多个 4 GiB shm judge；agent 可以并行，judge 必须排队。正式 10 题 campaign 放到 32 GB+ Linux。

**与官方工作对应**：用公开 Pass@1 分布只做 5 high/5 low 的分层抽样；最后比较 partial raw score/AUC，而不是再次报告 pass@4。188 题只用于 verifier sweep 和后续扩展。

**是否匹配**：方法上匹配、资源上偏重。必须先证明 10 题有连续 gradient，剔除只有 0/满分的硬门槛题。

### EdgeBench open-source subset

**整改**：保留 SForge 对 work container、hidden judge、auto-eval 和 final archive
的所有权；`bench-goal-plus` 只固定 fork branch/data revision、生成方法 cell、管理
PID/PGID 和输出统一 summary。Plain Codex 的 `K` 映射为 K 个独立 SForge
replicas，Goal Plus 的 `K` 映射为一个 outer run 内的 K 个 workers。

**建议并发/预算**：Mac 用 VLIW 做 `T=5–15min,K=1/2` 接线；Linux 正式 profile
用 8–12 个 gradient cases、`K=4,E=1,Q` 按节点资源排程、每题 1–2h。cell 默认
串行，避免 replicas、Goal Plus workers 和 task concurrency 三层相乘。

**与官方工作对应**：保留 raw score 与 task direction，同时用 fork 的官方
curve reporter 生成 EdgeBench 0–100 和 matched/nearest checkpoint reference。
论文/reference curve 只作外部定位；Goal Plus 归因仍需同模型、同 `T/K` 的
Plain Codex / Independent Parallel 重跑。

**是否匹配**：很匹配。它同时提供真实 artifact、连续梯度和较大搜索空间，是
证明 Goal Plus 不只是 Pass@K 的核心候选；最脆弱点是不同任务的 runtime 和
资源跨度很大，因此 subset/profile 必须先冻结。

---

## 推荐实施顺序

1. 完成通用 artifact contract、evaluator ticket gate、轨迹 schema 和 Codex 总控 usage。
2. 在 Frontier-Engineering MallocLab 同时接通 Independent/OpenEvolve/Goal Plus。
3. 完成 HeuriGym 9 题，验证跨题汇总和失败分类。
4. 接入 OpenEvolve CPU 四题严格包（另加 Function Minimization smoke），做 Goal Plus vs OpenEvolve 的第一版方法结论。
5. 扩展 ALE Lite 10 与 Frontier-Engineering v1-lite 10。
6. Linux 上完成 AutoLab hard subset、Frontier-CS 10 和 Swarm 5-task pilot。
7. 最后冻结 SwarmResearch 15-task 大实验。

OpenEvolve 自带 CPU 示例的具体筛选见 [OpenEvolve CPU 示例审计](openevolve-cpu-examples.md)。
