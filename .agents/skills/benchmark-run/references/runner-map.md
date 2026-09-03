# Benchmark runner map

可执行映射以 `benchmarks/runners.json` 为准。本页只负责把已解析的 runner/target
路由到必须阅读的 reference。

## Runner family

| Runner | 何时使用 | 必读 reference |
| --- | --- | --- |
| `edgebench-native` | EdgeBench 的 SForge、Work/Judge container、native campaign | [EdgeBench](benchmarks/edgebench.md) |
| `swe-bench-native` | SWE-bench Verified task image、patch 导出与官方 harness | [SWE-bench Verified](benchmarks/swe-bench-verified.md) |
| `frontier-engineering-native` | Frontier-Engineering v1-lite UnifiedTask evaluator；默认 9 题 CPU subset，完整 10 题需显式 CUDA opt-in | [Frontier-Engineering](benchmarks/frontier-engineering.md) |
| `zsoft-detect-native` | ZSoft Detect 自带的 Linux Bubblewrap + pinned SWE-agent + metered proxy + official scorer | [ZSoft Detect SWE-agent](benchmarks/zsoft-detect-swe-agent.md) |
| `aibench-coding-native` | aibench coding 可见测试、Linux Bubblewrap 隐藏集隔离和 controller-only 原生评分 | [aibench coding](../../../../experiments/aibench-coding/README.md) |
| `common-matrix` | 单 artifact + evaluator 的普通 benchmark adapter | [Common matrix](benchmarks/common-matrix.md) |
| `openevolve-batch` | OpenEvolve `cpu_portable` task set 和原生 OpenEvolve 对比 | [OpenEvolve](benchmarks/openevolve.md) |

不要根据统一 CLI 猜测 runner capability。`detach`、`stop`、`resume`、`C>1`、
official evaluator 和 report source 必须读取 catalog/registry。

## Target-specific context

Common runner 统一 campaign lifecycle，但 task、artifact、evaluator、Docker 和 readiness
仍由 benchmark 决定：

| Target | Benchmark reference |
| --- | --- |
| ALE-Bench Lite | [docs/benchmarks/ale-bench-lite.md](../../../../docs/benchmarks/ale-bench-lite.md) |
| HeuriGym | [docs/benchmarks/heurigym.md](../../../../docs/benchmarks/heurigym.md) |
| Frontier Engineering | [docs/benchmarks/frontier-engineering-v1-lite.md](../../../../docs/benchmarks/frontier-engineering-v1-lite.md) |
| AutoLab | [docs/benchmarks/autolab-cpu.md](../../../../docs/benchmarks/autolab-cpu.md) |
| SwarmResearch | [docs/benchmarks/swarmresearch-15.md](../../../../docs/benchmarks/swarmresearch-15.md) |
| Frontier-CS | [docs/benchmarks/frontier-cs-algorithmic.md](../../../../docs/benchmarks/frontier-cs-algorithmic.md) |
| SWE-bench Verified | [docs/benchmarks/swe-bench-verified.md](../../../../docs/benchmarks/swe-bench-verified.md) |
| SkyDiscover task packs | [docs/benchmarks/skydiscover-task-packs.md](../../../../docs/benchmarks/skydiscover-task-packs.md) |
| TorchBench | [adapters/torchbench/README.md](../../../../adapters/torchbench/README.md) |
| ZSoft Detect common/native SWE-agent | [docs/benchmarks/zsoft.md](../../../../docs/benchmarks/zsoft.md) |

存在 benchmark reference 只说明契约和当前状态被记录；是否可运行仍以 catalog、doctor、
plan 和实际 evidence 为准。
