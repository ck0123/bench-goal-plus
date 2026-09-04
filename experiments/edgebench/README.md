# EdgeBench campaign controller

这个目录把 EdgeBench 纳入 `bench-goal-plus` 控制面，但不复制 SForge 已经做好的
容器、hidden judge、auto-eval、replica 和 final archive。控制面跟踪 source
branch、固定 data revision，生成实验单元，启动/监控进程，并把 SForge raw
artifact 汇总成同口径表；campaign manifest 记录实际 source commit。

## 代码边界

用户和仓内调度器继续调用 `experiments/edgebench/experiment.py`；它只是兼容入口，
负责转发历史公开符号和 CLI，不再承载实现。实际代码位于 `controller/`：

| 模块 | 单一职责 |
| --- | --- |
| `context.py` | 仓库、上游、task、run 等路径上下文；测试通过显式 context 隔离 |
| `profiles.py` | profile 加载、协议归一化及官方协议 diff |
| `environment.py` | host/API/Docker 检查、资源探测、provision 与 doctor |
| `preparation.py` | 把已解析 profile 物化为不可变 campaign/cell manifest |
| `runtime.py` | SForge 命令、Judge 生命周期、cell 队列、启动/停止/状态 |
| `evidence.py` | raw run、Judge、token、Goal Plus 完成证据的读取与归一化 |
| `reporting.py` | native finalize、comparison JSON、Markdown 与 XLSX |
| `cli.py` | EdgeBench 子命令解析和分发 |

跨 benchmark 的选择、setup/start/status/stop/finish 仍只放在
`scripts/bench.py`；EdgeBench 的 hidden Judge、容器和原生归档仍归 SForge。
新增 benchmark-specific 行为时放入上述所属模块，不把业务逻辑放回兼容入口，也不把
EdgeBench 特例加入通用 dispatcher。

## 方法与 K 的映射

| 方法 | SForge outer run | live concurrency `K` | 含义 |
| --- | ---: | ---: | --- |
| Plain Codex | `K` replicas | `replica-concurrency=K` | K 条互相独立的 trajectory，等价于 independent parallel baseline |
| Goal Plus + Codex | 1 replica | `parallel-num=K` | 一个共享搜索状态中的 K 个 candidate workers |
| Plain Pi | `K` replicas | `replica-concurrency=K` | K 条互相独立的 Pi trajectory |
| Goal Plus + Pi | 1 replica | `parallel-num=K` | Pi 主会话监管一个共享搜索状态中的 K 个 candidate workers |
| Goal Plus + Pi provider API | 1 replica | `parallel-num=K` | 与上一行拓扑相同，但 outer/worker 使用显式 `PROVIDER/MODEL`，不使用 `openai-codex` OAuth |
| Plain Claude | `K` replicas | `replica-concurrency=K` | Claude Code 原生 agent；可使用 Anthropic-compatible provider |

两者固定同一 task definition、hidden judge、model、reasoning effort 和 wall budget
`T`。它们不强行匹配 evaluator calls 或 round；这些是运行后报告的行为量。

Goal Plus profile 可用 `global_evidence_mode` 固定 candidate 间的 Evidence 策略：
`manual`（默认，共享且由 candidate 主动读取）、`auto`（共享并在 verifier 后注入）
或 `independent`（仅可读取本 candidate Evidence，不注入）。解析后的值会写入
profile、campaign 和 cell manifest，并作为 `GOAL_PLUS_GLOBAL_EVIDENCE_MODE`
传入 Goal Plus 冻结 spec。

Goal Plus profile 还把 `shared_dir_enabled` 和
`supplemental_evaluation_enabled` 作为显式布尔实验轴；两者默认均为 `false`。
解析后的值会写入 profile、campaign 和 Goal Plus cell manifest，并通过
`SFORGE_AGENT_EXTRA_ENV` 传入 Work container。控制面总是从冻结 profile 写入这些值，
不会让宿主机遗留的同名环境变量改变实际行为；Pi Goal Plus adapter 会把前者冻结到
SearchSpec，Goal Plus runtime 直接读取后者。

## 从新机器开始

```bash
python3 scripts/bench.py setup \
  --benchmark edgebench --profile vliw-smoke
```

`provision` 只下载 profile 需要的 task definitions，并只 pull 这些题的 work/judge
images。`doctor` 检查：

- EdgeBench 与 Goal Plus tracked branch、clean state 和当次 resolved commit；
- SForge entrypoint；
- Codex auth 文件存在性，但不读取或记录其内容；
- Docker daemon 与 `linux/amd64`；
- 官方 Codex YAML 的 source SHA256、51-task coverage，以及 profile 中每题的
  effective protocol；
- 用一个自动回收的 Work container 实测官方 CPU/memory limit 已由 Docker 接受并
  写入 HostConfig；
- profile 含 `internet=false` 任务时，调用 SForge 自身的 preflight 验证
  passwordless `sudo iptables` 权限；
- HuggingFace dataset revision；
- profile 中每题的精确 work/judge image tag。
- profile 含 Rust 任务时，启动实际 Work/Judge image，用非登录 shell 核对
  `cargo`/`rustc 1.88.0`；镜像不完整时要求宿主机固定 SHA256 的 Rust 缓存可用。

`bootstrap --only edgebench` 会按 rsproxy、SJTU mirror、官方源的顺序预下载
Rust distribution 到 `~/.cache/sforge/rust/`，最终统一核对官方 SHA256。正常发布
镜像已包含相同版本，因此运行时只做快速探测；
缺失或版本漂移时，SForge 才把缓存注入 Work/Judge。Rust compiler 和 crate
依赖都不会在任务容器内联网下载。

任务数据在 `third_party/edgebench/tasks/`，并写入该 checkout 本机
`.git/info/exclude`；它不会污染 fork，也不会让 managed checkout 误报 dirty。

## 准备和启动

先在本地验证 Codex、Docker、Judge 和报告链路时，使用固定的 VLIW smoke preset：

```bash
python3 scripts/bench.py start \
  --preset edgebench-vliw-codex-local-smoke
```

该 preset 固定 `plain-codex`、`gpt-5.6-sol/medium`、`T=300s`、`K=1`、
`cell_concurrency=1`。由于官方 1800 秒 auto-eval 周期长于 smoke 总预算，它在
profile 中显式改为每 60 秒评测一次。Agent 网络固定为 API-only，只放行 Judge 和
resolved LLM API endpoint；`internet=true` 会在 doctor、prepare 和 launch 三层失败关闭。
评测周期差异有逐字段原因并把结果标成
`official_protocol_with_intentional_overrides`，不能当作官方同口径结果。

2026-07-31 已在模块化 controller 上用 campaign
`edgebench-vliw-codex-gpt-5-6-sol-medium-local-smoke-5m-k1-c1-20260731-1755`
完成端到端验证：4 次 hidden Judge 均通过 9/9，最终 raw score 为 2136 cycles，
controller 状态为 `completed` 且 `completion_evidence_passed=true`，并成功生成
`report.md` 与同名 xlsx。

Pi 的两个最小 profile 通过同一个入口：

```bash
python3 scripts/bench.py plan --preset edgebench-vliw-pi-local-smoke
python3 scripts/bench.py plan --preset edgebench-vliw-goal-plus-pi-local-smoke
```

Goal Plus + Codex 的十五分钟语义 smoke 使用：

```bash
python3 scripts/bench.py plan \
  --preset edgebench-vliw-goal-plus-codex-local-smoke
```

该 preset 固定 `gpt-5.5/high`、`T=900s`、`K=2`、`C=2`、`R=1`。setup 和
launch 会先用当次外部 API 配置与已解析的 Goal Plus source commit 在宿主机完成一次
真实 `goal_plus_monitor_snapshot` MCP 往返；普通 shell tool 往返不能代替该 gate。
API 地址与密钥不进入 preset，Goal Plus 的受管 tracking branch 也不因实验 source override
而改变。

确认 host `openai-codex` 登录和 resolved contract 后，把 `plan` 改成 `start`。
当前只声明 wiring-ready；真实 Pi E2E evidence 生成前不得标为 pass。

Pi built-in Z.AI provider/API 路径使用单独的方法与 preset：

```bash
python3 scripts/bench.py plan \
  --preset edgebench-vliw-goal-plus-pi-zai-glm-5-2-1h
```

该 preset 固定 `zai/glm-5.2/high`、`T=3600,K=2,C=1,R=1`，直接使用 Pi 标准
`ZAI_API_KEY`。`edgebench-vliw-goal-plus-pi-glm-provider-1h` 保留给自定义
`models.json` endpoint；“provider”描述的是 Pi 的 provider/model 与 API credential
路径，不表示模型运行在本机。Pi built-in DeepSeek、OpenAI、Anthropic 等 provider
直接使用真实 `PROVIDER/MODEL` 和各自标准 key env，不需要另造 provider 名称。
macOS/OrbStack 与 Linux/Docker 使用同一 adapter；provider registry 中的 `api`
可以是 Pi 支持的 Anthropic 或 OpenAI wire API，控制面不按 wire API 分叉 method。

自定义 Pi API 配置是 campaign 外部输入。设置 `SFORGE_PI_MODELS_FILE` 指向
`models.json`，并在环境中提供该文件 `apiKey` 所引用的变量。doctor 会先在宿主机用
profile 的精确 `PROVIDER/MODEL`、reasoning 和 Pi 版本完成一次 read tool 调用、tool
result 与 final answer 回环；这一步失败时不会启动 Docker。控制面不拼接
`/responses`、`/chat/completions` 或 models route。宿主机通过后，容器阶段只验证配置中
动态 base URL 的无凭据网络可达性，实际协议仍由复制同一 provider 配置的 Pi 负责。

Goal Plus 源码也按同一规则处理。长期默认仍是 `environment/upstreams.json` 中的
`master`；临时分支使用独立 checkout，并同时设置：

```bash
export SFORGE_GOAL_PLUS_SOURCE_DIR=/path/to/muyuan/plugins/goal-plus
export SFORGE_GOAL_PLUS_EXPECTED_REF=experiment/my-goal-plus-ref
```

doctor 在 Docker 前验证 clean checkout、ref/HEAD commit 与必需 runtime assets；prepare
把 source path/ref/commit 写进 campaign，launch 再核对同一个 commit。临时分支不会写回
upstream registry。

当前 50 个可运行公开任务的 Codex-only 固定协议是通用 dispatcher 中的一个 preset：

```bash
python3 scripts/bench.py start \
  --preset edgebench-codex-2h
```

该 preset 固定 `gpt-5.6-sol/medium`、每题 `T=2h`、题内 `K=1`、跨题
`cell_concurrency=2`，并依次执行 bootstrap、provision、doctor、prepare 和 detached
run。它不会覆盖同名 campaign；加 `--dry-run` 可审查 resolved contract 和完整命令链。

```bash
python3 scripts/bench.py start \
  --benchmark edgebench --profile vliw-smoke \
  --campaign-id vliw-matched-01 \
  --model gpt-5.6-terra \
  --wall-time-seconds 300 \
  --live-search-concurrency 2 \
  --cell-concurrency 1
```

`prepare` 不启动容器、不调用模型，也不预建 Goal Plus state。每个
task × method 都得到一个 cell manifest，完整 campaign 位于：

```text
runs/edgebench/vliw-matched-01/
├── campaign.json
├── controller.json
├── profile.json
├── controller.log
├── comparison.json
├── <campaign-id>.xlsx
└── cells/
    └── <task>--<method>/
        ├── cell.json
        ├── command.json
        ├── controller.log
        ├── summary.json
        └── sforge/runs/...     SForge 原始运行目录
```

每个 cell 从 managed EdgeBench checkout 的官方
`examples/all-tasks-k8s/experiment-codex.yaml` 继承 eval interval、Work/Judge
CPU 与 memory、submission cooldown 和 task override，再从 task JSON 继承
`internet`。官方配置文件路径、SHA256、resolved defaults/override、effective
protocol 和逐字段 diff 都写入 manifest；loader 不保留 YAML 的 `env` 或
`model.api_key`。实验轴只允许显式改变 agent、backend、model、reasoning、
timeout、attempt 数和控制面并发字段；profile 的 `protocol_overrides` 只允许
`eval_interval` 与 `internet`，每个值都必须有非空原因。任何实际差异都会进入
manifest 并令 campaign 失去官方同口径标记。

因此 profile 中的 `protocol_source=edgebench-official-codex` 只是上述官方协议 YAML
的资源来源标签，不是 runtime agent 选择。Pi campaign 仍由 `goal-plus-pi-provider`
和 `PROVIDER/MODEL` 决定，完全不会因为该字段改用 Codex。

主 Agent、Candidate Worker 和 Evidence Annotation 使用不同 Pi 模型时，参考
[`examples/pi-role-models`](examples/pi-role-models/README.md)。该示例包含宿主
provider registry、角色 profile 和 Docker 启动流程，但不注册为正式 preset。

`concurrency=K` 控制同一题内的 Plain Codex replicas 或 Goal Plus workers；
`cell_concurrency` 控制同时运行的不同 task × method cells，默认是 1。为了避免
无意中把两个并发层相乘，需要跨题并行时应在同一个 campaign profile 中显式设置
`cell_concurrency`，不要同时启动多个 controller。

Linux rootless Docker 无法从容器直接访问只监听宿主 loopback 的 API/Judge 时，
controller 会为 campaign 生命周期启动随机高端口的 `systemd-socket-proxyd` 桥，
并在结束或可恢复停止时关闭。API 配置按
`SFORGE_AGENT_* > protocol-native environment` 解析；Codex 使用 `OPENAI_*`，
Claude 使用 `ANTHROPIC_*`。密钥只进入子进程环境，不写入
manifest 或命令记录。`college_english_exam_bank` 的 Judge 使用同一 API bridge，
并按 EdgeBench 官方配置固定 `SFORGE_JUDGE_MODEL=gpt-5.5`。

GLM-5.2 Claude smoke 使用已固定的 profile；`thinking.type=enabled` 写入 profile，
`reasoning_effort=high` 通过 Claude Code `CLAUDE_CODE_EFFORT_LEVEL` 传入 Work
container：

```bash
python3 scripts/bench.py start \
  --benchmark edgebench --profile vliw-glm-5-2-high-20m-k1 \
  --campaign-id <campaign-id>
```

不启用深度思考的同预算版本使用 `vliw-glm-5-2-none-20m-k1`。该 profile 固定
`thinking.type=disabled` 和 `reasoning_effort=none`；控制器不向 Claude CLI 传递其
不支持的 `--effort none`，而是在 Work container 中设置
`MAX_THINKING_TOKENS=0` 并移除所有 force-effort 变量。

复现官方 GLM-5.1 Claude Code adaptive-thinking 配置的两小时版本使用
`vliw-glm-5-1-adaptive-2h-k1`。该 profile 固定 `thinking.type=adaptive`，不设置
`reasoning_effort` 或数值 thinking budget，并按官方配置把所有 Claude 模型层级
路由到请求标识 `glm-5.1`，使用 200K context window 和 80% 自动压缩阈值。
实际服务的模型身份必须以 transcript 响应元数据中的 `message.model` 为准：

```bash
python3 scripts/bench.py start \
  --benchmark edgebench --profile vliw-glm-5-1-adaptive-2h-k1 \
  --campaign-id <campaign-id>
```

当前 50 个可运行公开任务的 Plain Codex 两小时 campaign 使用：

```bash
python3 scripts/bench.py start --preset edgebench-codex-2h
```

该 profile 固定 `concurrency=1`、`cell_concurrency=2`，即每题一个 Codex
trajectory、同时跑两道不同题。它仍继承官方每题 CPU/memory quota、cooldown 和
联网策略；不支持 CFS quota 或无法执行 SForge iptables isolation 的 Docker 主机
会令 `doctor` 失败，不能继续作为协议对齐实验。此时应修复 cgroup delegation 与
passwordless iptables、改用能执行这些限制的 Docker，或使用官方 K8s backend。

官方公开集合仍是 51 题。`order_addition_permutation_optimization` 因 pinned Judge
的 private score-helper SHA 不一致，被标记为 `excluded_from_campaigns`，不进入上述
50 题 profile；只有上游发布新 Judge tag 和引用它的新 dataset revision 后才重新纳入。

## 监控、停止与恢复

```bash
python3 scripts/bench.py status \
  --campaign runs/edgebench/vliw-matched-01

python3 scripts/bench.py stop \
  --campaign runs/edgebench/vliw-matched-01
```

`run --detach` 建立独立 process group，并记录 controller PID/PGID。`stop` 请求
controller 向所有 active cells 转发 `SIGINT`，让每个 SForge run 执行原生
closeout；停止请求后不再启动新 cell。超出等待时间只报告仍在运行，不会自动
hard-kill 或清理容器/目录。

同一运行中的 Work container 内，SForge 可显式恢复自己管理且已确认可重试的 Goal Plus
停止：Pi `--session ID '/goal-plus resume'`；Codex `exec resume ID '$goal-plus resume'`。
固定 ticket 在 Goal Plus 实际入口再次检查 Goal/session/revision/control version；用户
pause/clear、中断、needs_user、原因不明或身份变化均禁止自动恢复。恢复保留原 Main/Search、
累计用量和 deadline，T 截止后的 segment 仅收尾。此路径不等于重新启动已停止 campaign。

外层 campaign/cell 的恢复边界仍然是：

- controller 仍活着：只用 `status` 重新观察；
- SForge 已写 `final_result.json`：运行 `finalize`，不再调用模型；
- cell 被中断且没有 final artifact：保留原 cell，重新 `prepare` 一个新 campaign
  作为独立 attempt；
- 孤儿容器：先按 SForge run ID 人工确认，不自动重复启动同一个 cell。

## 汇总

```bash
python3 scripts/bench.py finish \
  --campaign runs/edgebench/vliw-matched-01
```

`finalize` 遍历每条 replica 的 `final_result.json` 和 `run_history.json`，调用
fork 内的官方 score reporter，保留：

- raw score、metric direction、validity；
- EdgeBench 0–100 与 extended score；
- official reference curve comparison；
- actual runtime、rounds、agent/auto submissions、resume count；
- evaluator calls；
- Codex input/cached/output tokens 与 coverage；
- Goal Plus search runs、candidates、agent sessions 和 worker verifier runs。

Markdown 汇总还会加载带 source tar/TeX SHA256 的论文参考快照，逐题显示
Codex + GPT-5.5 的 12 小时、3 次独立 run 的 `mean +/- sample stddev`、本次差值，
并用 `REVIEW` 标记绝对差至少 20 个百分点的排查项。该列只用于诊断；开发 campaign
与论文在模型、时间和 run 数上不同，不构成 leaderboard 同口径比较。2026-07-24
完成的 51-task campaign 还使用了旧的全局联网/无配额/无 cooldown 配置；它保持为
development evidence，不会因后续修复而被回写为官方可比结果。

native finalize 完成后，生成面向交付的 Markdown 和 Excel：

```bash
python3 scripts/benchmark_report.py --campaign runs/edgebench/<campaign-id>
```

默认输出 `report.md` 和 `<campaign-id>.xlsx`；缺失 telemetry 保持为空并附 coverage，
不被转换成 0。

Fork 让 Codex 以 JSONL 输出，并在容器 closeout 时只归档
`~/.codex/sessions/`，不复制 auth/config。若旧 run 没有这些 artifacts，
`usage.coverage` 会明确写成 `agent_output_only` 或 unavailable，不能把零 token
解释为零成本。

## 从长运行批量提取中间时间点

普通任务的 SForge `run_history.json` 会保留定时 auto-eval 边界，因此一个两小时
campaign 完成后，可以一次性离线生成 0.5、1、1.5 和 2 小时的逐题数据，不需要
重新采样、调用模型或重跑 verifier：

```bash
.bench-env/venv/bin/python experiments/edgebench/timecurve.py extract \
  --campaign <campaign-id> \
  --checkpoint-hours 0.5 1 1.5 2
```

默认输出写到 `runs/edgebench/<campaign-id>/timecurve/`：

- `timecurve.json`：逐题记录、覆盖率、各 checkpoint 的 0–100 均分和状态计数；
- `timecurve.csv`：便于表格分析的逐题平面数据。

小于总预算 `T` 的普通 checkpoint 必须与 `eval_interval_seconds` 对齐。例如当前
30 分钟 auto-eval 配置中，`auto-1/2/3` 分别是 0.5/1/1.5 小时；边界是 inclusive，
并按 `submission_id` 合并稍后才完成的 Judge report。恰好等于 `T` 的 checkpoint
使用已完成 cell 的原生 closeout 历史，不要求可能与超时竞态的最后一次 auto-eval。
每行的 `strict_checkpoint`、`status` 和 `reason` 用于区分严格边界、未到达边界和
缺失 artifact；聚合只纳入 `valid=true` 且有合法 `score_0_100` 的行。

三个文字冒险任务使用原生 game mode，没有 auto-eval 历史。要得到它们的精确
中间点，必须在目标时间到达前启动只读 watcher，通常紧接 `run --detach` 执行：

```bash
.bench-env/venv/bin/python experiments/edgebench/timecurve.py watch \
  --campaign <campaign-id> \
  --checkpoint-hours 1 \
  --poll-seconds 5 \
  --detach
```

watcher 的 PID、剩余 checkpoint 和日志分别记录在 `timecurve/watcher.json` 与
`timecurve/watcher.log`；连接断开后仍会继续。它只快照 Judge 已写的
`steps.jsonl`/`game_result.json`，不会影响 campaign。若历史运行没有提前启动
watcher，精确的 game-mode 中间分无法事后恢复，`extract` 会明确记录
`missing_game_snapshot`，不会用 0 分代替。watcher 完成后再执行上面的 `extract`，
即可把普通任务和 game-mode 快照统一写入 JSON/CSV。

这些小于 2 小时的曲线是本地开发 checkpoint，不自动声称可与公开 reference
curve 比较；正式对比仍需先对齐 task revision、环境、`T` 和 `K`。

若要把多批半小时和一小时 checkpoint 汇成可复用 fast-test reference，必须
显式列出输入，不能隐式扫描本机 `runs/`。收集器只接受 `strict_checkpoint=true`、
`valid=true` 且有合法 0–100 分数的行；同一任务优先使用带协议证据的 campaign，
再在同一证据等级内取最高分。边界是 inclusive，分别表示 `<=0.5h` 和 `<=1h`；
边界之后产生的 submission 不纳入，但已在边界内提交的候选允许稍后完成 Judge：

```bash
.bench-env/venv/bin/python experiments/edgebench/timecurve.py \
  collect-fast-reference \
  --timecurve runs/edgebench/<campaign-a>/timecurve/timecurve.json \
  --timecurve runs/edgebench/<campaign-b>/timecurve/timecurve.json \
  --checkpoint-hours 0.5 1 \
  --model gpt-5.6-sol \
  --reasoning-effort medium \
  --output runs/edgebench/local-fast-reference/reference.json
```

下面是 controller maintainer 的参考曲线再生成接口，不是第二套公开 lifecycle。
它将 reference 显式加入最终 XLSX；`comparison.json` 会保存完整 reference 和来源，
主表只显示 checkpoint、local best 与当前结果差值，详细证据位于 `Local Fast`
sheet：

```bash
.bench-env/venv/bin/python experiments/edgebench/experiment.py finalize \
  --campaign <campaign-id> \
  --local-fast-reference runs/edgebench/local-fast-reference/reference.json
```

## Profile 扩展

`profiles/vliw-smoke.json` 是一题接线 profile。扩到正式 8–12 题时复制一个新
profile 并固定：

- `dataset_revision`；
- `task_ids`；
- `methods`；
- model/reasoning；
- `wall_time_seconds=T`、题内 `concurrency=K` 与跨题 `cell_concurrency`；
- judge concurrency、work/judge CPU；
- 每 worker 首次 lease。

不要修改 SForge task JSON 的 hidden judge 或 rescale；profile 只选择任务和资源。

真实 lifecycle smoke 与解释边界见
[`evidence/runs/2026-07-23-edgebench-codex-goal-plus-smokes.md`](../../evidence/runs/2026-07-23-edgebench-codex-goal-plus-smokes.md)。
