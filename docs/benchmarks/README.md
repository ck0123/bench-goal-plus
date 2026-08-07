# Benchmark 导读、本机运行能力与完整规模

本页只统计正式 benchmark 和论文实验 substrate，不把 SkyDiscover、EvoX、
OpenEvolve 等搜索 framework/method 当作 benchmark。完整分类见
[Agent 方案、搜索方法与 Benchmark](../experiment-taxonomy.md)。

这里回答两个问题：每套 benchmark 实际在测什么，以及当前 Mac 能否直接跑
一个已接入 case、一个正式子集或整个 track。每篇子文档都按同一顺序展开：
**任务边界 → 一个真实 case → 输入 → agent 动作 → 期望输出 → verifier →
Goal Plus 实验价值**。

“完整运行”分为两种口径：

- **coverage run**：每题至少产生一个候选并经过官方 verifier，用于确认接线和任务覆盖。
- **search campaign**：按 benchmark 默认或论文预算反复生成、验证、保留 best-seen，用于比较 Goal Plus、plain Codex、parallel、EvoX/OpenEvolve。

---

## 本机可直接运行什么

这里的“直接运行”是指已有明确 bootstrap、materialize、evaluator 和
Plain Codex / Goal Plus 入口，而不是“仓库能下载”或“代码看起来可以”。
必须区分 **无需 Docker 的 host 直跑**、**当前 Mac 上需要 Docker 的直跑**
和 **只有 evaluator、还不能做标准 Agent 实验**。

### 无需 Docker：当前已接通的正式 Benchmark case

| Benchmark | 当前可直接跑的 case | 环境 | Plain Codex | Goal Plus + Codex | 全集状态 |
|---|---|---|---|---|---|
| HeuriGym | `operator_scheduling` | pinned Python + 已 bootstrap 数据 | 已完成真实 E2E | 已完成真实 E2E | 其余 8 题待接 |
| Frontier-Engineering v1-lite | `MallocLab` | C 编译器 + `make` | 已完成真实 E2E | 已完成真实 E2E | 其余 9 题 runtime 待冻结 |
| AutoLab | `toy_isa_opt` host adapter | C 编译器 + `make` | 已完成真实 E2E | 已完成真实 E2E | 完整 CPU/Harbor 路径仍含 task containers |

这三题是当前没有 Docker 时最稳妥的正式 benchmark 起点。统一入口和建议
`T/K` 见
[`experiments/benchmark_compare/README.md`](../../experiments/benchmark_compare/README.md)。

### 需要 Docker：当前 Mac 已接通的正式 Benchmark case

| Benchmark | 当前可直接跑的 case | 当前能力 | 全集状态 |
|---|---|---|---|
| ALE-Bench Lite | `ahc027` | official-lite evaluator、Plain Codex、Goal Plus + Codex 已完成真实 E2E | Lite 其余 9 题尚未 campaign-ready |
| Frontier-CS | `problem-0` | pinned judge image、Plain Codex、Goal Plus + Codex 已完成真实 E2E | 其余题尚未 materialize |
| EdgeBench | VLIW Kernel Optimization | SForge work/judge、Plain Codex、Goal Plus lifecycle E2E 已通 | 8–12 个 gradient subset 尚未冻结 |
| SWE-bench Verified | `sympy__sympy-16886`；Plain Codex C2 另含 `sympy__sympy-19346` | 三种方法的 C1 native lifecycle 已验收；Plain Codex 两题 C2 已验收 | 当前只声明 Linux/amd64 smoke，不代表 500 题 split |

这些任务可以在当前有 Docker 的 Mac 上跑，但不是 host-only 路径。启动前必须
确认 `docker info` 成功，并保留镜像、冷启动和 evaluator 时间。

### 只有局部能力，暂不能当作标准本机 Agent 实验

| 对象 | 当前本机能力 | 还缺什么 |
|---|---|---|
| SwarmResearch 15-task substrate | Docker 下 Circle Packing evaluator 已通 | paper-compatible Swarm/Plain/Goal Plus 统一入口；ADRS/ALE worker build context |
| SkyDiscover Math/ADRS task pack | 19 个非 Torch CPU evaluator images 已构建并通过依赖检查 | 逐 task evaluator smoke 和统一 benchmark controller |
| PERFOPT-Bench | 无 | 公开 executable artifact 当前返回不可用，无法取得任务、依赖和 evaluator |

### 可 host 运行但不计入正式 Benchmark 套数

| Task pack | Docker | Docker 空间 | 当前用途 |
|---|---|---:|---|
| OpenEvolve `cpu_portable` 12 题 | 不需要 | `0 GB` | 方法接线、机制诊断和 OpenEvolve/Plain/Goal Plus 四路径 pilot |
| SkyDiscover Circle Packing | 不需要 | `0 GB` | SkyDiscover runtime + EvoX 方法的 compatibility smoke |
| Local VLIW replica | 不需要 | `0 GB` | 从 EdgeBench 镜像提取的 Plain/Goal Plus 本地比较任务；非官方 EdgeBench 分数 |

这些 task pack 是此前最容易混淆的地方：能运行一个 evaluator task，不等于新增了一套
benchmark。OpenEvolve 和 EvoX 属于被比较的搜索方法，SkyDiscover 是承载
EvoX 的 runtime。

---

## Docker 依赖速查

以下按 **bench-goal-plus 当前支持的可评分路径** 标记，而不是只看上游是否
存在 Dockerfile。`混合` 表示已有 host-portable 单题，但完整论文/多题路径
仍依赖容器。空间统一按“镜像逻辑大小 / 共享层实际增量 / 建议预留”解释；
没有实测时明确标为未知。

| Benchmark / substrate | Docker | Docker 空间 | 没有 Docker 时能否跑 |
|---|---|---|---|
| HeuriGym | 不需要 | `0 GB` | 当前 `operator_scheduling` 可完整评分 |
| Frontier-Engineering | 当前 case 不需要 | MallocLab `0 GB` | 当前仅确认 MallocLab；其余 v1-lite runtime 逐题冻结 |
| AutoLab | 混合 | host case `0 GB`；已测 task image `0.277 GB` | `toy_isa_opt` 可 host 跑；完整 paper-compatible 路径使用容器 |
| ALE-Bench Lite | **需要** | C++ 路径 `4.03 GB`；建议 `10 GB` | 可以 materialize/查看，不能走当前 official-lite 评分 |
| SwarmResearch 15-task substrate | **需要** | 当前 `0.196/2.10 GB` 两种 Circle Packing 口径；完整集建议 `10–20 GB` | 可以分析轨迹，不能同口径正式评分 |
| [SkyDiscover Math/ADRS task pack](skydiscover-task-packs.md) | **需要** | 非 Torch 19 tags：逻辑 `8.57 GB`、实际新增约 `2.49 GB`、建议 `10 GB` | Circle Packing、HotPotQA 和 Image Gen 仍有 host 路径 |
| Frontier-CS | **需要** | 共用 judge `1.27 GB`；建议 `2 GB` | 当前 problem-0 评分需要 pinned judge image |
| EdgeBench | **需要** | VLIW work + judge 逻辑 `2.23 GB`；单 case 建议 `5 GB` | SForge 需要 work container 和独立 hidden judge |
| SWE-bench Verified | **需要** | 单个当前 SymPy task image 逻辑约 `2.56 GB`；C2 两个精确镜像合计约 `5.14 GB` | Agent 与官方 harness 都要求每题的精确 task image |
| PERFOPT-Bench | 无法判定 | 未知 | executable artifact 不可访问；不是“不需要 Docker” |

`local_examples/vliw_kernel_optimization` 可在无 Docker 主机运行 public 和
held-out local evaluator，但它没有 SForge 的安全隔离，因此不会改变上表
EdgeBench 的“需要 Docker”结论。

---

## 当前规模与时间

以下估算基于 16 GiB Intel Mac、8.4 GB Docker VM、单 worker 串行执行。没有
Docker 的机器只适用上表“不需要”路径。模型延迟、候选超时和首次依赖下载会
造成较大波动，因此这里给区间而不是伪精确值。

| Benchmark 范围 | 题数 | 当前准备度 | Coverage run | Search campaign |
|---|---:|---|---:|---:|
| ALE-Bench Lite | 10 | 环境、官方 verifier、plain Codex 已通 | 单候选/题约 3–5 小时；只扫 verifier 约 20–40 分钟 | 约 31 candidates/题时，串行约 60–100 小时 |
| HeuriGym 全集 | 9 | 环境和 1 题已通；其余数据待下载 | 单候选/题约 1–2 小时 | 默认 3 iterations，约 3–6 小时 |
| Frontier-Engineering v1-lite | 10 | MallocLab 已通；其余 9 题 runtime 待安装 | 环境安装加单候选/题约 3–8 小时 | 100 iterations/题，约 40–120 小时 |
| AutoLab CPU subset | 25 | `toy_isa_opt` 已通；其余镜像待构建 | 10 分钟/题的 bounded coverage 约 6–10 小时 | 20 题 × 2h + 5 题 × 4h = 60 agent-hours；加 verifier 约 2.5–3 天 |
| SwarmResearch 论文任务集 | 15：Math 5 + ADRS 5 + ALE 5 | Circle Packing 已通；ADRS/ALE worker 布局待修 | evaluator-only 约 2–6 小时 | 公开轨迹任务 wall span 串行合计约 76.9 小时 |
| Frontier-CS Algorithmic | 当前固定版本 188 | problem-0 已通；其余 task 尚未 materialize | reference/verifier 全扫约 1–3 小时 | 单次 agent/题约 10–30 小时；20 calls/题可能 100–300 小时 |
| EdgeBench open-source subset | 51；先选 8–12 gradient cases | VLIW 的环境、Plain Codex、Goal Plus 已通；统一 controller 已接入 | 单候选/题通常 10 分钟–2 小时，取决于任务 | 正式 profile 建议每题 1–2 小时；8–12 题约 16–48 method-hours |
| SWE-bench Verified | 500；当前固定 1 题 smoke | 单题镜像、doctor、Plain Codex/Pi 与 Goal Plus + Pi controller 已接入 | 当前 preset 每个 method 30 分钟 Agent/Search 预算，另加官方测试 | 完整 split 尚未开放 campaign capability |

这些数字不应直接拿来横向比较方法速度：ALE 的一次 candidate 会跑多个 generated cases，AutoLab 的“2 小时”是长时 agent budget，Swarm 的公开 wall span包含并行研究者，而 Frontier-CS 的题量远大于其他集合。公平实验最终应以 **evaluator calls + wall time + model calls/tokens** 三组预算同时报告。

Goal Plus 的逐项接入改造、`K/E/Q` 三层并发和 matched-budget baseline 见 [Goal Plus benchmark 接入与并发实验协议](../goal-plus-benchmark-experiment.md)。OpenEvolve 自带的无特殊硬件任务见 [OpenEvolve CPU 示例审计](../openevolve-cpu-examples.md)。

---

## 快速导读

| 文档 | 它真正测什么 | 代表 case |
|---|---|---|
| [ALE-Bench Lite](ale-bench-lite.md) | LLM/agent 能否为未知启发式实例编写高分程序，并利用 public feedback 继续优化 | AHC027 机器人清扫路径 |
| [HeuriGym](heurigym.md) | 能否从约束和目标函数生成通用启发式求解器，而非只回答一个固定答案 | HLS Operator Scheduling |
| [Frontier-Engineering v1-lite](frontier-engineering-v1-lite.md) | 能否在真实工程 artifact 上持续改进连续分数 | MallocLab 动态内存分配器 |
| [AutoLab CPU subset](autolab-cpu.md) | 长时 agent 是否会实验、验证、保留最好实现并抵抗 shortcut | Toy ISA 流水线调度 |
| [SwarmResearch 15](swarmresearch-15.md) | 多 lineage / swarm 搜索是否能在大搜索空间中累积有效发现 | 26 圆装箱 |
| [Frontier-CS Algorithmic](frontier-cs-algorithmic.md) | 面向开放算法研究问题生成可执行程序，并从连续 partial score 改进 | Polyomino Packing |
| [EdgeBench](edgebench.md) | 在真实隔离 artifact + hidden judge 上利用连续 feedback 持续优化 | VLIW Kernel Optimization |
| [SWE-bench Verified](swe-bench-verified.md) | 根据真实 issue 修复代码，并通过官方隐藏回归测试 | `sympy__sympy-16886`；C2 加 `sympy__sympy-19346` |

PERFOPT-Bench 因缺少可执行公开 artifact 继续挂起，不进入本文档集。
SkyDiscover 是 runtime，EvoX/OpenEvolve 是搜索方法，因此不作为 benchmark
单独写 case 文档；其自带任务的
[Docker 与空间说明](skydiscover-task-packs.md)作为环境页维护，分类和当前
接入状态见[实验对象分类](../experiment-taxonomy.md)。

---

## 本地展开顺序

1. **先跑三道无 Docker 已接入 case**：HeuriGym Operator Scheduling、
   Frontier-Engineering MallocLab、AutoLab Toy ISA。
2. **再跑三道 Docker 已接入 case**：ALE AHC027、Frontier-CS problem 0、
   EdgeBench VLIW，验证隔离评分链路。
3. **HeuriGym 9 + ALE Lite 10**：补齐其余 adapter 后再形成 19 题 coverage；
   当前不能把单题 E2E 写成全集 ready。
4. **Frontier-Engineering v1-lite 10**：补齐其余 9 题 runtime 后扩展 coverage。
5. **AutoLab 只选 6–10 个 CPU case**：先验证 persistence，不在 Mac 上消耗完整 60 小时。
6. **SwarmResearch 15**：修好统一 evaluator/worker 后作为最终大实验 substrate。
7. **Frontier-CS 选 10 题**：保留 188 题 track 作为题库，不在本地对所有方法全扫。
8. **SWE-bench Verified 单题 smoke**：先完成 Plain Codex/Pi 与 Goal Plus + Pi 官方 harness 验收，再讨论扩大 panel。
9. **EdgeBench 冻结 8–12 个 gradient cases**：Mac 只做单题接线，正式多方法 campaign 放到 Linux。

空间与 Linux 节点规划见 [Docker 镜像空间计划](../docker-storage-plan.md)，工程门禁以 [`benchmarks/registry.json`](../../benchmarks/registry.json) 为唯一状态源。
