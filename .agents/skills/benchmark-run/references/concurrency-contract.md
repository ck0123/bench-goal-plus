# Benchmark 并发契约

## 四个相互独立的维度

| 符号 | 含义 | Manifest 字段 | 比较规则 |
|---|---|---|---|
| `T` | 一个 trajectory/search 的墙钟预算 | `wall_time_seconds` | 对比方法之间保持一致 |
| `K` | Plain 的隔离 outer trajectory 数，或 Goal Plus 的 internal subagent 数 | `live_search_concurrency` / `concurrency` | 运行后按方法拓扑核对实际 Agent 数量 |
| `C` | 同时运行的不同 cell/task 数量 | `cell_concurrency` | 单独配置并明确报告 |
| `R` | 独立 attempt/seed 数量 | attempt/seed matrix | 不得用 `C` 代替 |

Plain Codex、Plain Claude 和 Plain Pi 把 `K` 映射为相互隔离的 outer trajectories。
Goal Plus 把 `K` 映射为共享一份 Search 状态的内部 subagent。其他方法只有在 runner
明确声明并验证其 K 拓扑后才能使用 `K>1`。
必须保留每种方法的原生控制流，并在运行后报告实际 Agent/subagent 数、evaluator call、
iteration、token、cost coverage 和实际墙钟时间。
Goal Plus 的新 SearchSpec 只用 `budget.max_parallel` 承载 `K`；
`budget.max_candidates` 已弃用，不能与 `K` 分开配置。
新 cell 的精确 `$goal-plus`/`/goal-plus` 命令还必须显式包含 `max_parallel=K`；runtime
把它持久化为 `command_config` 并机械约束 ready SearchSpec。只在目标正文写
`budget.max_parallel=K` 不构成 K 配置证据。

自然语言中的“并发”“并行”“同时跑几个”没有默认归属。真实 launch 前必须把 `K` 和 `C`
拆开显示，说明对应拓扑和 `K × C` 同时运行规模，并取得用户明确确认；不能根据单个数字猜测。

## 新 benchmark 的迁移标准

1. 识别 native scheduler，以及拥有可变环境的工作单元。
2. 除非共享正是被测试的机制，否则每个 worker/lane 都必须拥有相互隔离、可重置的 workspace。
3. `K` 必须进入 typed host command，并由 controller/runtime 实际执行；不能只写在
   prompt prose 中。
4. `C` 只能加在 task cell 之上。按 `K × C` 的任务拓扑计算主机容量，并保留 native
   CPU/memory quota。
5. 一个 campaign 只使用一个 controller；不得通过重复启动 controller 伪造 `C`。
6. Plain 与 Goal Plus 都先测试 `K=1`，再测试一个低成本的 `K=2` wiring task；分别核对
   outer trajectory 或 internal subagent 数量等于 manifest 中的 `K`。
7. 无法证明实际数量等于 `K` 时，只能声明 `K=1` 已支持，并把缺失机制标记为 `partial`。

## 预设示例

`edgebench-codex-2h` 恰好表示 `T=7200`、`K=1`、`C=2`、`R=1`。它只是一个预设
示例，不是其他 benchmark 的并发默认值。common matrix controller 当前只声明
`C=1`；在 task 隔离和实际并发数量完成测试之前，不得开放跨 cell 并发。
