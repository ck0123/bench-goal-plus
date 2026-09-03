---
name: benchmark-run
description: 准备、启动、监控、停止、恢复或汇总 bench-goal-plus benchmark campaign。用户要求选择 native/common runner、按 registry 调度任意已登记 benchmark、固定 T/K/C/R、执行某个 campaign preset，或留下可复现 evidence 时使用。
---

# Benchmark 运行

先读 [runner-map.md](references/runner-map.md)，根据 resolved runner/target 进入对应
benchmark reference；再读
[concurrency-contract.md](references/concurrency-contract.md) 和
[runner-contract.md](references/runner-contract.md)。不能只凭本页的统一流程运行一个
benchmark。环境未通过 `$benchmark-setup` 的 doctor 时不得启动正式 run。

## 统一入口

```bash
python3 scripts/bench.py catalog
python3 scripts/bench.py plan \
  --benchmark <registered-id> ...
```

先用 `catalog` 选择 target；用 `plan` 审查 resolved contract 和完整命令链。
dispatcher 默认执行 bootstrap 和 doctor；native profile 还执行 provision。可以显式跳过
bootstrap/provision，但不得跳过 doctor。长运行是否 detach 由登记的 runner capability 决定，
不得自行拼后台 shell。

具体 campaign 只作为 preset/example。EdgeBench 51 题可读
[EdgeBench runner reference](references/benchmarks/edgebench.md) 和
[edgebench-codex-2h.md](examples/edgebench-codex-2h.md)；不得据此推断其他
benchmark 使用相同 lifecycle 或支持相同并发。

## 通用流程

1. 冻结 task/evaluator、model、reasoning、`T/K/C/R`、seed、method 和 resolved commit。
   方法会把外部或受管运行时源码复制进执行环境时，还必须冻结并展示该源码的
   source kind、ref/branch 和完整 commit SHA；tracking branch 不能代替实际 commit。
2. 在 `benchmarks/runners.json` 解析 target/runner；使用 native controller、common matrix 或 OpenEvolve batch 的 `prepare`，确认 prepare 不调用模型且不预建 Goal Plus state。
   Common matrix 的普通方法运行使用 `--method plain-codex` 或 `--method goal-plus-codex`；只有做 B0-B4 消融实验时才使用 `--condition`。两者不能混用。
3. 对 Agent 容器执行网络门禁：doctor 必须证明 effective `internet=false`、每个模型调用角色的
   API endpoint 完整、Judge + LLM API 精确 allowlist 可实施，prepare 生成的命令必须包含
   `--disable-internet`。不得允许任务公网、包仓库或公共代理；provider 路由失败时停止并报告，
   不得静默切到 `--enable-internet`。
4. 完成下面的 K/C 启动确认门禁；未确认前只能执行只读的 `catalog`、`doctor` 和 `plan`，
   不得执行 `launch` 或 `e2e`。
5. 用 `launch` 启动 runner。长运行使用已有 detach/controller，不自行拼后台 shell。
6. 用统一 `status --campaign <path>` 读取 `agent-run.json` 和 native manifest。不要因为终端断开就重建 campaign。后台 campaign 的进展查询还必须按下文的“进展查询与终态归档”处理。
7. 只在 capability 允许时调用 `stop` 或 `resume`。EdgeBench stop 后归档 partial，不伪称原 trajectory 可恢复；common/OpenEvolve batch 只补跑未完成 cell。
8. native final artifact 存在后再 `finalize`/`summarize`，再用 `$benchmark-report` 导出。后台 campaign 在进展查询中到达终态时，不得停在“可以归档”的提示；满足条件就完成归档。

## 进展查询与终态归档

用户询问“当前进展”“跑完了吗”“状态如何”时，按一次完整的后台 campaign 检查点处理：

1. 先执行 `status --campaign <path>`，区分 runner/controller 是否仍在运行、是否已到终态，以及是否 `can_finalize`。
2. 尚未到终态时只报告当前执行进展，不生成最终报告。
3. 已到终态、`can_finalize=true` 且尚未归档时，在同一轮主动调用 `$benchmark-report` 的统一 `finish` 流程；不要只告诉用户“还没执行 finish”。用户明确要求“只看状态”“不要归档”时除外。
4. 已归档时不要重复执行 `finish`。终态但不满足归档条件时，报告具体缺失证据或失败原因，不把它描述成仍在运行。
5. `finish` 后重新读取状态并验证 source JSON、`report.md` 和 workbook，再返回最终结果与绝对路径。

所有进展答复必须分别写清：

- **执行状态**：运行中，或已结束及其终态（completed/succeeded/partial/failed/stopped）；
- **归档状态**：未到归档阶段、已归档、归档失败，或因何无法归档。

禁止单独使用“还没执行 finish”“还没完成”来概括一个已经到终态的 campaign。应写成例如：
“benchmark 执行已成功结束；检测到尚未归档，现已自动执行 finish 并生成报告。”

## K/C 启动确认门禁

自然语言里的“并发 2”“并行 2”“同时跑 2 个”不能自动映射到配置，因为它们既可能表示
单个 task cell 内的 `K`，也可能表示跨 task cell 的 `C`。遇到这类说法必须分别询问：

- `K` 是 Plain 的隔离 outer trajectories，还是 Goal Plus 的 internal subagents；
- `C` 是否表示同一个 campaign 同时运行的 task cell 数。

Plain Codex、Plain Claude 和 Plain Pi 的 `K` 表示一个 task cell 中相互隔离的 outer
trajectories；Goal Plus 的 `K` 表示共享同一 Search 状态的 internal subagents。其他没有
明确拓扑契约的方法在 `K>1` 时必须 fail closed。

执行真实 `launch` 或 `e2e` 前，必须向用户展示 `plan` 解析后的确认块，并得到明确确认。
不能只列出 `T/K/C/R` 字母和值；每一项都必须同时用自然语言说明本次运行中的含义：

```text
T=<秒数>：每条 task trajectory 或 search 的墙钟探索预算
K=<数量>：Plain 的隔离 outer trajectories 数，或 Goal Plus 的 internal subagents 数
C=<数量>：campaign 同时运行的不同 task cell 数
R=<数量>：每个 task 的独立重复/seed 数
method=<方法及其 K 拓扑>
方法运行时源码=<source kind；ref/branch；完整 commit SHA；不使用外部源码时写 not applicable>
单个 cell 拓扑=<Plain 的 K 条隔离 outer trajectories，或 Goal Plus 的 1 个 outer session + K 个 internal subagents>
同时运行规模=<按该方法解释的 K × C>
总 cells=<task 数 × method 数 × R>
```

所有 Goal Plus 方法的“方法运行时源码”必须指向实际复制或挂载进任务环境的 Goal Plus
checkout。使用外部实验 checkout 时，必须显示 external、显式 expected ref 和完整 HEAD SHA，
并另行说明 registry 的受管 tracking branch 未被改写；使用受管 checkout 时也必须显示实际
branch 和完整 SHA。只写版本号、目录名、tracking branch、短 SHA，或只在 doctor/prepare
日志中出现而没有进入确认块，都不能通过启动确认门禁。

即使 preset 已冻结 K/C，也必须展示其解析值。混合方法 campaign 必须分别说明 Plain 的 K
条 outer trajectories 与 Goal Plus 的 1 个 outer session + K 个 internal subagents。
用户只确认了其中一个维度、缺少方法运行时源码版本、使用了未标注的“并发/并行”数字，
或确认内容与 `plan`/doctor 冻结的源码不一致时，
不得启动；先重新 `plan` 并再次确认。`resume` 已有 campaign 不重复询问，但不能借 resume
修改原有 K/C。

## Goal Plus typed command gate

每个新 Goal Plus cell 必须由一条精确宿主命令启动，并在目标正文之前写入连续的 typed
配置 token：

<!-- markdownlint-disable MD013 -->

```text
$goal-plus mode=autonomous max_parallel=K workspace_backend=git_worktree promotion_mode=MODE strategy=STRATEGY workers=MODEL*K annotator=MODEL <目标>
/goal-plus mode=autonomous max_parallel=K workspace_backend=git_worktree promotion_mode=MODE strategy=STRATEGY workers=MODEL*K annotator=MODEL <目标>
```

<!-- markdownlint-enable MD013 -->

不要使用额外分隔符。`max_parallel=K`、workspace、promotion、strategy 和固定 worker
model 以及 profile 明确指定的 annotator model 必须进入 host command 的
`command_config`，并以同一结构写入 campaign manifest；
不能只在后续 prompt prose 中要求 Agent 设置这些字段。Goal Plus runtime 会机械校验
ready
SearchSpec 与显式配置一致。worker budget、verifier、edit surface、evaluation mode 和
benchmark-native Judge bridge 等尚未进入通用 command schema 的字段，继续留在任务
正文。
profile 明确关闭 annotator 时省略 `annotator=`，不能写占位值。

Common/OpenEvolve 和 SWE-bench Verified 使用 run-local source，配置
`promotion_mode=apply`。EdgeBench 由 `sforge-goal-plus-submit` 拥有回写和 Judge，配置
`promotion_mode=artifact_only`，避免 runtime apply 与 benchmark bridge 双重回写。具体
strategy 仍由对应 runner reference 冻结。Skill、MCP 或普通自然语言都不能代替精确宿主
命令创建 Goal Plus 记录。需要重新提交宿主命令来恢复记录时，只能使用无附加文本的
`$goal-plus resume` 或 `/goal-plus resume`。同一 Pi 进程内继续当前 session 时可以普通
follow-up；EdgeBench 通过 `pi -c` 启动新进程恢复 native session 时，extension 会重新加载，
因此必须把无附加文本的 `/goal-plus resume` 作为该进程唯一的新用户输入。两种情况都不得
提交一个新的 `/goal-plus` goal。

## 交付

返回 campaign id/path、profile、实际 `T/K/C/R`、controller PID/状态、监控命令、停止命令和报告命令。凭据只从继承环境或 Codex auth store 读取。

## Gotchas

- 只有 runner capability 中 `cell_concurrency=true` 且已有测试证据时才能接受 `C>1`。
- Plain 方法的 `K` 是隔离 outer trajectories；Goal Plus 的 `K` 是共享状态 internal workers。
- Completion evidence、stop/resume 语义和 report source 由选中的 benchmark reference
  定义；不得把 EdgeBench 的 SForge/Goal Plus 规则套到 common 或 OpenEvolve runner。
- 不要启动多个 controller 来伪造 `C`；总并发必须由一个 campaign manifest 记录。
- 重新运行 interrupted cell 会产生新 attempt；不得覆盖或伪装成原 trajectory 的 resume。
