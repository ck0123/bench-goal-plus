# EdgeBench runner

EdgeBench 保留 native SForge lifecycle。控制面负责选择 profile/preset、部署依赖、启动和
监控 campaign；SForge 继续拥有 Work container、hidden Judge、任务隔离和最终归档。

## 执行前

1. 阅读
   [Host 与鉴权矩阵](../../../benchmark-setup/references/host-auth.md)。
2. 用 `catalog` 确认 `edgebench-native` 的 method 和 capability。
3. 用 preset 或 profile 冻结 task、method、model、reasoning 和 `T/K/C/R`。
4. 运行 `plan`，检查 native `provision → doctor → prepare → run --detach` 命令链。
5. 检查 doctor 中 `network:api-only-policy` 和 `network:offline-task-isolation` 均通过，
   endpoint 清单覆盖 main、Goal Plus worker 和 evidence annotation；检查 prepare 产物只有
   `--disable-internet`。任一缺失时不得 launch，也不得用开放公网 smoke 替代。
6. Goal Plus 方法在启动确认块中必须展示 doctor 已验证、prepare 将复制进 Work container
   的 `source_kind + expected_ref/branch + 完整 commit SHA`。外部 source 还必须明确说明
   `environment/upstreams.json` 中的受管 Goal Plus tracking branch 没有变化。确认块缺少这组
   版本信息时不得 launch。

## 已登记方法

<!-- markdownlint-disable MD013 -->

| Method | SForge agent | `K` 的含义 |
| --- | --- | --- |
| `plain-codex` | `codex` | 固定 `K=1`，一条 outer trajectory |
| `goal-plus-codex` | `codex-goal-plus` | 一个 outer run 内 `K` 个 Goal Plus workers |
| `plain-claude` | `claude-code` | 固定 `K=1`，一条 outer trajectory |
| `plain-pi` | `pi` | 固定 `K=1`，一条 outer trajectory |
| `plain-pi-provider` | `pi-provider` | 固定 `K=1`，一条使用显式 `PROVIDER/MODEL` 的 outer trajectory |
| `goal-plus-pi` | `pi-goal-plus` | 一个 outer run 内 `K` 个 Goal Plus workers |
| `goal-plus-pi-provider` | `pi-goal-plus-provider` | 与上一行拓扑相同，但 outer/worker 都使用显式 `PROVIDER/MODEL` API 路径 |

<!-- markdownlint-enable MD013 -->

不要使用未登记的别名。method 必须在 plan 阶段通过 runner
`supported_methods` 校验。`goal-plus-pi` 专指 `openai-codex` OAuth；Z.AI 或
自定义 Anthropic/OpenAI-compatible endpoint 使用 `goal-plus-pi-provider`，且 model
必须写成精确的 `PROVIDER/MODEL`。
provider 的 wire API 由 Pi registry 决定：`anthropic-messages` 和
`openai-completions`/`openai-responses` 使用同一个 method。macOS 与 Linux
也使用同一 adapter；host 只提供 registry/credential，实际 agent 始终运行在
EdgeBench Linux Work container 中。

一个 Goal Plus cell 可为 main、worker、annotation 选择不同 provider。controller 必须把
所有角色 base URL 传给 SForge；SForge 只将这些 URL 与 Judge 解析成精确 `IP:port`
allowlist。缺失 built-in endpoint、custom `baseUrl`、loopback bridge 或 iptables 权限时失败
关闭；不得因为 provider 多样而设置 `internet=true`。

`goal-plus-codex` 在创建 Docker 资源前还必须使用 campaign 将采用的精确 Codex 版本、
model/reasoning、外部 API 配置和已解析的 Goal Plus source commit 完成一次原生
`goal_plus_monitor_snapshot` MCP tool call。只有普通 shell tool roundtrip、只列出 MCP
registration/resource，或通过手写 stdio client 调 server 都不能通过该 gate。端点更换后
每次重新运行 setup/launch 都会重做语义探针；endpoint 和 credential 不写入 preset。

这个 MCP 探针只验证工具连接，不能创建 Goal Plus。真实 cell 必须由 exact
`$goal-plus ...` UserPromptSubmit 触发 project-local hook，hook 建立宿主授权后 Skill 才能解释
工作流；`--disable plugins` 防止个人 plugin 改写入口。新 cell 的命令必须显式包含
`mode=autonomous`、`max_parallel=K`、`workspace_backend=git_worktree`、
`promotion_mode=artifact_only`、`strategy=agent_guided`、`workers=MODEL*K`，以及 profile
显式配置时的 `annotator=MODEL`。其中
`artifact_only` 表示 benchmark-native `sforge-goal-plus-submit` 独占回写和 Judge，不能再由
Goal Plus runtime apply 一次。Codex 使用 `codex exec resume --last`，Pi 使用独立
session 目录中的稳定 `--session ID`；两者均传普通 continuation prompt，不存在
Goal Plus resume 子命令。只有 unfinished 且 attached 的原 session 才允许 harness
自动继续；paused、needs-user、detached、stale、缺失身份或终态均失败关闭。
resume 前的 promotion/Judge bridge 输出独立持久化，不拼进用户输入。

当前十五分钟 VLIW preset：

```bash
python3 scripts/bench.py plan \
  --preset edgebench-vliw-goal-plus-codex-local-smoke
```

它固定 `gpt-5.5/high`、`T=900,K=2,C=2,R=1`，不固定 API 地址或 Goal Plus 分支。

`goal-plus-pi-provider` 在 Docker 前必须完成 provider runtime gate。controller 把外部
Pi registry 的所选 provider/model 原样复制到仓库本地的隔离
`PI_CODING_AGENT_DIR`，并使用 campaign 将采用的精确 Pi 版本运行一次真实 JSON session；
事件必须报告精确 `PROVIDER/MODEL`，并包含 tool call、tool result 和 final answer。
reasoning model 还必须出现 thinking event/content 或非零 reasoning usage。只读取配置、
只做 controller HTTP 请求，或只看到普通 assistant 文本都不够。

协议和 API route 由外部 Pi registry 的 `api` 与 `baseUrl` 决定。控制面不得追加
`/responses`、`/chat/completions` 或其他协议路径，不得探测后改写 registry，也不得静默
回退到另一 wire API/provider/model。宿主 gate 让 Pi 自己按这份配置完成语义验证；诊断
Work container 只对 registry 给出的 base URL 做无凭据连通性检查，随后 SForge 再用复制
进去的同一份 registry 执行 `pi --list-models <provider>` 并核对精确模型。更换 API 地址、
协议或模型时只需更新外部 registry 并重新经过 prepare/doctor，benchmark 代码和默认
upstream 分支都不随之修改。

EdgeBench Work container 不再精确锁死旧 Pi：默认
`SFORGE_PI_PACKAGE_VERSION=latest`，安装输出必须记录解析后的 `pi --version`。短 capability
smoke 可以跟随 latest；一小时等正式 campaign 必须在 profile 的
`pi_package_version` 字段冻结刚通过 smoke 的精确版本，避免同一 campaign 中 npm tag
漂移。

一小时 VLIW Z.AI built-in provider preset：

```bash
python3 scripts/bench.py plan \
  --preset edgebench-vliw-goal-plus-pi-zai-glm-5-2-1h
```

它固定 `T=3600,K=2,C=1,R=1`，使用 Pi built-in `zai/glm-5.2`，只要求
`ZAI_API_KEY`。`edgebench-vliw-goal-plus-pi-glm-provider-1h` 保留为自定义
`models.json` endpoint 路径；实际 launch 前仍需按 K/C 门禁展示并确认解析结果。

profile 中的 `protocol_source=edgebench-official-codex` 只表示资源、网络、评测周期等
协议默认值来自 EdgeBench 官方 `experiment-codex.yaml`。实际 agent/provider 仍由
method 和 model 决定；该字段不把 Pi campaign 变成 Codex campaign。

## Judge 资产完整性

profiled `check` 会把精确 task revision、Work/Judge tag、image ID 与
`experiments/edgebench/references/known-asset-issues.json` 核对。命中
blocking issue 时，即使镜像存在也必须失败关闭，不得 launch、不得把失败的
harness pass rate 当作 0–100 分，也不得把修补后的镜像重新标成原 tag。

`order_addition_permutation_optimization` 的 Judge tag `f6f385925889` 已确认存在发布时的
score-helper SHA 自检不一致。恢复正式测评需要上游发布新的 Judge tag，并由新的 task dataset
revision 引用它；只修改本地 test 常量最多是诊断验证，不构成 official evaluator 修复。当前两个
已知坏 dataset revision 已将该题标为 `excluded_from_campaigns`，profile 加载阶段即拒绝调度。

## 可运行公开集 Codex campaign

```bash
python3 scripts/bench.py plan --preset edgebench-codex-2h
python3 scripts/bench.py launch --preset edgebench-codex-2h
```

该 preset 固定 50 个当前可运行的公开任务（官方集合仍为 51 题）、Plain Codex、`gpt-5.6-sol`、`medium`、
`T=7200,K=1,C=2,R=1`。`C=2` 表示两个 task cells 并发，不是两个 candidate。

## Controller 日志边界

对所有 detached EdgeBench cell，在 `run` 子命令前传入 SForge 全局参数
`--silent`。把 cell 的 `controller.log` 只作为 SForge 子进程的启动、结束和错误控制台日志；
完整 agent trajectory 以 SForge 的 `agent_output.txt` 为准。

单 task、单 outer replica 在没有 `--silent` 时会触发 SForge verbose 模式，把容器内 agent
stdout 原样复制到 `controller.log`。Pi JSON delta 尤其会绕过 `agent_output.txt` 的兼容过滤，
重新产生数百 MiB 的重复快照。不得用压缩、轮转或另一层 JSON 过滤掩盖这条重复落盘路径；
检查生成的 `command.json`，确认 `--silent` 位于 `run` 之前。

## 监控和停止

```bash
python3 scripts/bench.py status --campaign runs/edgebench/<campaign-id>
python3 scripts/bench.py stop --campaign runs/edgebench/<campaign-id>
```

status 必须保留 native campaign/cell/PID/trajectory 状态。Goal Plus cell 还应展示
candidate、worker session/handle、verifier ledger、剩余时间和最新 Judge submission。
stop 是保留 partial evidence 的 controller closeout；partial trajectory 不能被删除，
也不能被伪装成原 trajectory 的无损 resume。

仅做 provider/thinking smoke 时，观察到所需思考证据后立即 stop，并在同一轮执行
`finish` 归档 partial evidence。报告必须标注这是 wiring smoke，不得作为 EdgeBench
score 或完整 T 预算结果。

## Goal Plus completion evidence

Goal Plus + Codex 的 session allocation 本身不是 worker launch：

- 至少记录 `K` 个不同的 spawned worker thread，或 `K` 个不同的 Codex host handle；
- 至少 `K` 个 candidate-bound verifier records；
- 必须有 promotion 和 official Judge trajectory。

Codex worker 的 `strategy.worker_budget` 由每次
`search_start_agent_session` 返回的 `launch.budget_control` 交给父会话强制执行。全部 `K`
个 worker 绑定后，父会话必须立即进入 orchestration-only watchdog 阶段：只在非空的真实
receiver thread ID 集合上执行 `wait_agent`，到各自 initial wait deadline 后发送一次配置的
closeout message，再等待 final window，并在仍未终止时执行配置的 interrupt。worker 存活时
不得用主工作区分析或独立优化填充等待时间。`worker_budget` 只进入 frozen spec、没有对应
Codex host 操作证据，或父会话一直工作到外层 cutoff，都不算预算已执行。

Codex `0.150.1` 的内置 collaboration schema 可能只暴露 `wait` 和 `close_agent`，而不暴露
`send_message`/`interrupt_agent`。此时父会话仍须完成 initial wait 和 final wait，并只在 hard
deadline 使用一次 `close_agent` 作为 hard-stop fallback。worker drain 后必须先读取
`goal_plus_monitor_snapshot`；其中的 durable `verifier_ledger` 和
`verifier_candidate_ids` 是 selection gate 的权威来源。`goal_plus_status` 不展示
Search verifier ledger，不能单独用于判定缺少 worker evidence。coverage 达到
`K/K` 后应立即执行 `search_select` 和 `search_promote`，期间不得进入主
workspace 自行分析或优化。

`T` 截止必须是 SForge 的真实 agent segment boundary，不能把同一个 Codex 进程直接允许运行
到 `T + finalization_grace`。若 Goal Plus 在 `T` 时仍未终态，SForge 终止探索 segment，并用
原生 session resume 加普通 continuation prompt 启动 finalization-only segment；该 segment
的第一个原生工具调用必须是
`goal_plus_monitor_snapshot`，且只能恢复 evidence、选优、提升、Judge、结果记录和终态报告。

Goal Plus + Pi 不使用 Codex collaboration events，必须持久化至少 `K` 个 candidate-bound
Pi sessions 和 verifier records，并同样保留 promotion 与 official trajectory。
缺失任何 required evidence 时 cell/campaign 为 `partial`。

Goal Plus completion evidence 中的源码版本必须与确认块和 campaign manifest 的
`goal_plus_source.commit` 相同；branch 名相同但 commit 漂移也必须在 launch 前失败关闭。

## Finalize

```bash
python3 scripts/bench.py finish --campaign runs/edgebench/<campaign-id>
```

native finalizer 生成 `comparison.json` 和 native workbook；统一 report exporter 再生成
`report.md` 与 `<campaign-id>.xlsx`。不要直接修改 native artifacts 来改变结论。

只有调试 EdgeBench controller 本身时才直接运行
`experiments/edgebench/experiment.py --help`；正常用户流程始终使用 `scripts/bench.py`。
