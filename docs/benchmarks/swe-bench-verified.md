# SWE-bench Verified

## 30 秒理解

SWE-bench Verified 测试 Agent 能否根据真实 GitHub issue 修改一个真实代码仓库，并让
官方隐藏回归测试通过。当前接入面是一个 Linux/amd64 单题 smoke，用来验证容器隔离、
patch 导出和官方 harness 接线，不代表 500 题完整 campaign 已 ready。

当前固定 case 是 `sympy__sympy-16886`。数据集 revision、仓库 base commit 和官方任务镜像
均写入 profile；最终原始指标是官方 `resolved` 布尔值，方向为 maximize。

## 代表 case：`sympy__sympy-16886`

### 输入是什么

Agent 只看到 issue problem statement、仓库名、base commit 和任务镜像标识。完整数据行只
保存在 host campaign 的 evaluator 目录；gold patch、test patch、`FAIL_TO_PASS` 和
`PASS_TO_PASS` 不会进入 Agent task 文件或 prompt。

### Agent 要做什么

Plain Codex 或 Plain Pi 在精确的 SWE-bench task image 内运行一条隔离 outer trajectory。
Goal Plus 运行一个 outer 主会话，并由它启动共享同一 Search 状态的内部 worker。标准对比
固定 `K=1,C=1,R=1`；另有一个 Astropy Goal Plus + Codex 专用机制实验使用
`K=2,C=1,R=1`，不与 `K=1` 合并为 matched 主结果。

### 期待输出是什么

controller 在 Agent 结束后导出唯一的 `git diff --binary --full-index`。默认情况下 Agent
容器随后被确认删除；debug 模式则要求它被确认停止并保留。完成任一隔离状态后，patch 才能
交给独立的官方 evaluator。Agent 不直接给分，也不接触 evaluator 数据文件。

### Verifier 如何评分

官方 SWE-bench harness 在单独容器中应用 model patch，并执行该实例的官方测试脚本。
controller 保留 `resolved`、`patch_successfully_applied`、原始 `report.json` 和 evaluator
调用次数。同一 campaign 最多尝试一次官方 evaluator；未解决但报告完整仍是有效分数 0，
缺报告则是 partial/failed，不会静默写成 0。

## Docker 与空间

Docker 空间当前按本地精确 task image 的逻辑大小约 `2.56 GB` 记录；还要为 Agent 临时层、
官方 evaluator 容器和测试日志预留空间。无 Docker 环境只能读取 task/manifest，不能运行
Agent 容器，也不能产生官方 `resolved` 分数。

task image 始终保留：controller 固定使用官方 harness 的 `cache_level=instance`、
`clean=false` 和 `force_rebuild=false`，不会调用 `docker rmi`。需要检查 Agent 修改后的
`/testbed` 时，在 `plan` 和 `launch` 中同时增加 `--retain-containers`。runner 会停止而不是
删除 Agent 容器，并将 name/ID 写入 status 和最终报告；`finish` 不会自动清理它。当前开关
只保留 Agent 容器；官方 harness 仍清理独立 evaluator 容器，但其报告和日志会完整保留。

## 实验怎么用

当前提供 Plain、Pi、Goal Plus + Pi，以及 Goal Plus + Codex 冻结 preset：

| Preset | Method | Model | T/K/C/R |
| --- | --- | --- | --- |
| `swe-bench-verified-sympy-16886-codex-smoke` | Plain Codex | `gpt-5.6-sol`, medium | `1800/1/1/1` |
| `swe-bench-verified-sympy-16886-pi-smoke` | Plain Pi | `zai/glm-5.2`, medium | `1800/1/1/1` |
| `swe-bench-verified-sympy-16886-goal-plus-pi-smoke` | Goal Plus + Pi | `zai/glm-5.2`, medium | `1800/1/1/1` |
| `swe-bench-verified-sympy-16886-goal-plus-pi-luna-high-smoke` | Goal Plus + Pi | `bench-openai/gpt-5.6-luna`, high | `1800/1/1/1` |
| `swe-bench-verified-sympy-16886-goal-plus-codex-acceptance-off-smoke` | Goal Plus + Codex，开放补充评价 OFF（旧 ID） | `gpt-5.6-luna`, high | `1800/1/1/1` |
| `swe-bench-verified-sympy-16886-goal-plus-codex-acceptance-on-smoke` | Goal Plus + Codex，开放补充评价 ON（旧 ID） | `gpt-5.6-luna`, high | `1800/1/1/1` |
| `swe-bench-verified-astropy-13033-goal-plus-codex-luna-high-k2-peer-smoke` | Goal Plus + Codex，动态 peer comparison 机制实验 | `gpt-5.6-luna`, high | `1800/2/1/1` |

campaign 顺序运行。runner 暂不支持 provision、detach、stop、resume、通用 `K>1` 或 `C>1`；
只有上表冻结 preset 接受 `K=2`；应使用 preset 启动，使 task/model/T/K/C 漂移在确认块生成前
被拒绝，而不是直接调用底层 profile。
真实 launch 前仍必须展示并确认解析后的 T/K/C/R。以下 `K=1,C=1` 路径均已通过归档的
真实官方 harness smoke：

- [Plain Codex](../../evidence/runs/2026-08-02-swe-bench-verified-plain-codex-sol/summary.json)
- [Plain Pi](../../evidence/runs/2026-08-02-swe-bench-verified-plain-pi-glm/summary.json)
- [Goal Plus + Pi，Luna/high](../../evidence/runs/2026-08-03-swe-bench-verified-goal-plus-pi-luna/summary.json)

这些 pass 不扩展到 `K>1`、其他实例或完整 500 题 split。两个 Plain development smoke
保留了 prepare 时工作树非 clean、随后由 `904cae6` 收录实现的 provenance；不会把它改写成
clean run，完整 campaign readiness 仍为 partial。

专用 `K=2` profile 要求 MainAgent 同批创建两个不同的公开证据假设，每个候选绑定一个
Codex worker。机制验收还要求 ViewAgent 的动态比较引用另一个候选真实已结算的 commit，且
两个 candidate-bound worker 的运行 lease 有真实重叠，并由 Global Evidence 留下某个 worker
在下一次 verifier 前读取该 peer View 的持久化记录。仅出现 `budget.max_parallel=2`、只有两个
候选，或只在 closeout 生成比较，都不算动态 peer comparison 跑通。该 profile 改变了单题
计算量，因此只报告为机制实验。

Codex preset 另外冻结 `auth_mode=openai-compatible`、`OPENAI_BASE_URL`、
`OPENAI_API_KEY` 和 Responses wire API。Linux 上的 loopback endpoint 使用与 EdgeBench
相同的 `systemd-socket-proxyd` bridge；doctor 会分别验证 host 和实际 task container 的
`POST /responses`。该路径不读取 OAuth auth file；日志里出现 `chatgpt.com` 应视为路由错误。

Astropy 13033 的四个 Codex ON/OFF profile 还冻结
`agent_network_policy=public-egress-blocked`。controller 为每个 campaign 创建独立 Docker
internal network，把模型 API bridge 绑定到该 network 的 host gateway，并在 Agent 启动前
同时验证 Docker network mode 和公网 IP 连接失败。模型调用仍可用，但 Agent 没有公网路由，
不能通过网页搜索题目；验证与 network 清理状态进入最终报告。
固定 Goal Plus runtime 安装发生在 Agent/模型进程启动前；仅这个 setup 阶段临时连接 Docker
默认 bridge。安装后 controller 必须先断开 setup bridge，并验证 internal network 是唯一剩余
网络，才允许模型探针和 Agent 启动。
计时 Agent 还会获得指向容器本地拒绝端口的 HTTP/HTTPS proxy，并将模型 gateway 放入
`NO_PROXY`，让常见网页与 Git 查询快速失败；Docker internal network 仍是最终隔离边界。
每个 native campaign 只接受一个正整数 attempt seed，并在 Search strategy、manifest 和报告中
保持一致。

Luna Goal Plus + Pi preset 复用同一组 OpenAI-compatible Responses 环境变量，但通过
campaign-local Pi provider registry 选择 `bench-openai/gpt-5.6-luna`。该 registry 只保存
环境变量引用；loopback endpoint 同样经过 Linux bridge，doctor 会验证 host、task container
和 Pi 模型列表三层接线。

## 可复用对比数据

报告保留 task、method、model、reasoning、dataset revision、SWE-bench commit、base commit、
image、raw metric/direction、Agent 与 evaluator 墙钟时间、finalization grace、token coverage、
evaluator calls 和 patch apply 状态。缺失 token 数据保持 unavailable，不补零。

Goal Plus + Pi 还保留 frozen spec、candidate、绑定 Pi worker session、worker verifier、pool
终态和 promotion patch。`.gp` 在 Agent 容器停止/删除前导出；这些 completion evidence
缺失时保留官方 raw score，但 cell/campaign 标记为 partial。

单题 smoke 可以证明方法接线与官方评分边界，但不能用于声称整个 Verified split 的通过率，
也不能与不同 T/K/C/R 的结果做 matched comparison。

## 代码与证据

- Runner/target/preset：[`benchmarks/runners.json`](../../benchmarks/runners.json)
- Dataset revision：[`benchmarks/datasets.json`](../../benchmarks/datasets.json)
- Native controller：[`experiments/swe_bench_verified/README.md`](../../experiments/swe_bench_verified/README.md)
- Readiness：[`benchmarks/registry.json`](../../benchmarks/registry.json)

`finish` 的 `reported` 表示 campaign-local JSON、Markdown 和 XLSX 已生成，不会自动修改
Git registry。适配验收必须把已审计的最小证据投影到 `evidence/runs/`，通过
`stage_evidence` 绑定到具体 method，并在同一变更中提升该 method；validator 会拒绝没有
method-specific evidence 的 `pass`。

下载源只允许加速传输。国内 PyPI 或 Hugging Face mirror 不得替换锁定 revision、精确 Docker
tag、image ID 或官方 evaluator；目标镜像已存在时不会主动 pull。
