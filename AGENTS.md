# bench-goal-plus 贡献者规则

本仓库是基准测试运行控制面，负责基准目录、可复现环境、生命周期编排、方法契约、
证据归一化与报告。Goal Plus 是其中一种受支持的方法和一种可观测性证据来源，
不是本仓库的整体架构。

## 智能体标准流程

处理每一个基准测试请求时：

1. 运行 `python3 scripts/bench.py catalog`，解析已登记的目标、预设、运行器、
   受支持方法、Docker 契约和能力标志。
2. 对 catalog 声明 `local_asset_inventory=true` 的 target，在任何 setup、provision、
   fetch、pull 或 build 之前先运行
   `python3 scripts/bench.py check --benchmark <id> --profile <profile>`；使用 preset
   时可改用 `--preset <id>`。独立 task pack 使用
   `python3 scripts/bench.py check --asset-pack <id> --profile <profile>`。检查失败只报告
   缺失项，不得把 provision 当成 inventory。
3. 环境、主机、Docker、上游代码和鉴权工作统一交给 `benchmark-setup`；
   正式 campaign 之前必须执行 `setup`/`doctor`。
4. campaign 配置统一交给 `benchmark-run`；先执行 `plan`，再执行 `launch`，
   记录 `T/K/C/R`，并返回生成的 campaign 路径。
5. 长任务使用 `status` 以及已登记的 `stop`/`resume` 能力。不得替换、删除或
   静默重启一个状态为 `partial` 的 campaign。
6. native campaign 到达终态后，通过 `benchmark-report` 完成最终归档和导出。
7. 新基准或新任务族统一交给 `benchmark-adapt`；在验收路径产生证据之前，
   不得把脚手架或注册表条目称为就绪。

执行平台或基准专属命令之前，必须读取相关技能选定的参考文档。
不得用 macOS 行为推断 Linux，不得用 API 密钥行为推断 OAuth，也不得把一个
基准的生命周期套用到另一个基准。

## 技能路由

| 用户意图 | 技能 | 必须读取的参考文档 |
| --- | --- | --- |
| 端到端请求或尚不明确的基准操作 | `bench-goal-plus` | 先读智能体契约，再路由到下列一个或多个技能 |
| 安装、初始化、Docker、主机兼容性、鉴权 | `benchmark-setup` | `host-auth.md` 和 `benchmark-matrix.md` |
| 规划、启动、监控、停止或恢复 | `benchmark-run` | `runner-map.md`，再读选定基准和运行器的参考文档 |
| 最终归档、指标检查、导出 Markdown/XLSX | `benchmark-report` | `report-contract.md` |
| 增加基准或任务族 | `benchmark-adapt` | `adaptation-checklist.md` |

技能只描述操作流程，并把用户意图路由到 `scripts/bench.py`。注册表和代码仍是
可执行事实来源；主机、鉴权或基准差异不能只隐藏在 Python 实现中。

## 公开契约

- 统一用户入口是 `python3 scripts/bench.py`。
- `catalog`、`setup`、`plan`、`launch`、`status`、`stop`、`resume`、`finish`
  和 `check` 构成公开生命周期词汇。`start` 只是 `launch` 的兼容写法；`e2e`
  是前台运行的便捷路径。
- 不带 `--profile` 的 benchmark target `check` 只检查仓库契约，不是镜像 inventory。
  `check --environment` 是显式的全环境复合检查：先按 registry 的
  `default_inventory_profile` 完成全部只读资产 gate，再用 `git ls-remote` 检查根仓库和
  所有受管 checkout；只有交互确认或显式 `--yes` 后才能执行 fast-forward-only 更新。
  对 benchmark
  target 或 asset pack，只有它明确声明支持时，
  带 `--profile` 或 profile preset 的 `check` 必须是只读本地资产检查：不得 fetch、
  pull、build、run、provision 或检查凭据，只能读取 task/revision 元数据并执行精确
  image tag 的 `docker image inspect` 与一次 `docker ps -a`。
- `scaffold` 是贡献者工具，它生成的内容不代表已经就绪。
- 技能和基准本地脚本可以是上述统一入口的薄适配层，但不得再公开一套
  等价的第二 CLI。
- 方法必须先出现在其 runner 的 `supported_methods` 中，`plan` 才能选择它。
  不受支持的方法必须在 setup 或 prepare 之前被拒绝。
- `catalog` 展示的能力就是契约。环境供给、后台运行、停止、恢复、任务单元并发或
  官方评测器在具备测试和可复现证据路径之前不得被声明为已支持。
- `--retain-containers` 只能用于 catalog 声明 `retain_containers=true` 的 runner。保留模式
  必须先停止 runner-owned debug container，再把精确 name/ID 和 disposition 写入 campaign；
  `finish` 不得自动删除该 container，且该能力不得触发 image 删除、重标记或重建。

## T/K/C/R 契约

- `T` 是一条 task trajectory 或一次 Goal Plus search 的墙钟探索时间预算。
- `K` 只在 Goal Plus 方法中生效，是同一个 task cell 内实际并行工作的内部 subagent
  数量。非 Goal Plus 方法必须固定 `K=1`，一个 cell 只启动一条 outer trajectory。
  `K` 不表示一个 run 累计产生过的 candidate 数量。
- Goal Plus adapter 把 `K` 映射为唯一的 `parallel-num`/`budget.max_parallel`。
  启用 `search_scheduler` 时，`budget.max_candidates` 可独立设置整个 run 的累计唯一
  candidate 上限；正整数必须不小于 `K`，`null` 表示不设累计上限。它不得代替或改写 `K`。
- `C` 是一个 campaign 同时运行的不同 task cell 数量。`C` 只控制 task 之间的并发，
  不能代替或改写每个 task 内部的 `K`。
- `R` 是独立重复次数或 seed 数量，不能用 `C` 代替。

不同方法必须遵守以下拓扑：

- Plain Codex、Plain Claude 和 Plain Pi：`K` 必须为 1，一个 task cell 启动一条
  outer Agent trajectory。Independent-parallel baseline 必须使用单独的方法/参数契约，
  不得复用 `K`。
- Goal Plus + Codex 和 Goal Plus + Pi：一个 task cell 只启动一个 outer Goal Plus
  主会话，由这个主会话启动 `K` 个共享同一 Search 状态的内部 subagent。
- `K=4,C=1` 只适用于 Goal Plus，表示一次只跑一个 task，由 1 个 outer 主会话运行
  4 个内部 subagent。
- `K=1,C=4` 表示同时跑 4 个 task cell；每个 cell 内仍只有 1 个 Agent 或 1 个
  Goal Plus subagent。
- `K=4,C=4` 只适用于 Goal Plus，表示同时跑 4 个 task cell，每个 cell 内有 4 个
  internal subagent。

Goal Plus 结束后必须统计实际 subagent 数量并与 `K` 核对：

- Goal Plus + Codex 使用不同的 `spawn_agent` worker thread 或已绑定的 Codex host
  handle 证明实际 subagent；仅分配 session 不代表已经启动 subagent。
- Goal Plus + Pi 使用不同的、已绑定 candidate 的 Pi worker session 作为实际
  subagent 证据。
- 启用 `search_scheduler` 时，初始实际 worker 数必须等于 `K`，runtime 必须证明 live
  worker 始终不超过 `K`；淘汰后派生的累计 candidate/session 数可以大于 `K`，必须另列。
- candidate 数、session 分配数、verifier 调用次数和 outer replica 数必须分别记录，
  不得互相替代。
- 启用 `search_scheduler` 时还必须冻结 scheduler 配置和 `max_candidates`，并记录实际累计
  candidate 数；`max_candidates=null` 不得在任一 adapter 中改写为 `K`。
- 固定候选模式实际 subagent 数量不等于 `K`，或 scheduler 模式缺少上述初始/live 证据时，
  保留已有分数和原始证据，
  但 cell/campaign 必须标记为 `partial`，不得进入 matched comparison。

## 目录职责

| 路径 | 负责内容 | 必须包含 | 不得包含 |
| --- | --- | --- | --- |
| `bench_goal_plus/` | 类型化应用、catalog、runner、state、event 和 report 契约 | 与具体 benchmark 无关、默认拒绝不完整输入的 Python 模块 | benchmark 专属 prompt、task ID 或停止逻辑 |
| `benchmarks/` | 声明式 runner、target、preset、dataset 和 evidence registry | 明确 schema、分支跟踪引用、方法与能力契约 | 可执行编排逻辑或凭据 |
| `environment/` | 可复现主机和上游定义 | 锁定依赖、每个受管 checkout 的唯一跟踪分支 | 手工固定的受管源码 commit 或复制的 virtualenv |
| `adapters/<benchmark>/` | common runner 的任务物化与官方评测边界 | task 发现和物化、evaluator 调用、raw metric 和方向 | 通用 campaign 控制或 vendored 上游源码 |
| `experiments/<benchmark>/` | benchmark 自己拥有的 native 生命周期集成 | profile、controller、reference 和 benchmark 专属 README | 跨 benchmark 策略或可复用应用逻辑 |
| `docker/` | 仓库自有的 benchmark 支持镜像 | 用途明确且输入锁定的最小 Dockerfile | 通用 runner 策略、凭据或复制的上游镜像 |
| `local_examples/` | 小型仓库自有任务 fixture | license/provenance、任务 README 和确定性 evaluator 边界 | 来源不明的上游 dataset 或完整 benchmark 覆盖声明 |
| `evidence/` | 可评审、已提交的验证记录 | 小型不可变 manifest/summary，包含命令、revision、metric 和状态 | 可变 campaign state、凭据或大型原始输出 |
| `legacy/` | 保留的旧控制面诊断工具 | 明确标注为兼容或直接 API 工具，并包含迁移说明 | 新的公开生命周期功能或 ready 声明 |
| `scripts/` | 稳定入口和小型仓库维护工具 | `bench_goal_plus/` 的薄调用；`bench.py` 是统一入口 | 重复的 runner 实现 |
| `.agents/skills/` | 统一生命周期的操作指南 | 调用 `scripts/bench.py` 的薄流程说明 | 宽泛仓库策略或替代 CLI |
| `docs/` | 说明、运行手册、协议原理和迁移说明 | 由代码或 Skill 链接的长篇材料 | registry/test 中不存在的可执行事实 |
| `tests/` | 控制面契约和回归证据 | 能在锁定环境中独立运行的 unit/contract test | 隐藏凭据、仅网络可运行假设或一次性 run 输出 |
| `.github/workflows/` | 自动化仓库门禁 | 锁定 setup、status 校验和统一 unit test suite | benchmark campaign 或带凭据的 smoke run |
| `runs/`、`.tmp/`、`.bench-env/`、`.venv/`、`.worktrees/`、`third_party/`、`.codebase-memory/`、`__pycache__/` | 忽略或自动生成的本地状态 | 保留的 campaign、仓库本地临时文件、可重建环境、受管 checkout 和派生索引 | 应提交到本仓库的手写源码 |

每个新的仓库自有顶层目录在使用之前，都必须先在此表中增加职责行。嵌套目录继承
最近上级目录的规则，除非它自己的 `AGENTS.md` 进一步收窄范围。

## 修改应放在哪里

1. 可复用生命周期或证据行为放在 `bench_goal_plus/`。
2. 声明式身份、支持状态和能力事实放在 `benchmarks/`。
3. common runner 的 benchmark 边界放在 `adapters/<benchmark>/`。
4. benchmark 专属 native controller/profile 放在 `experiments/<benchmark>/`。
5. 只有行为确实属于上游时才修改受管 upstream。改动保留在对应的
   `third_party/<checkout>` Git worktree，并分别汇报根仓库与 upstream diff。
6. 宽泛策略写在本文件中；操作步骤写在 docs 或 Skill 中。

不得把 benchmark 源码或 dataset vendor 到本仓库。受管源码 checkout 必须跟踪
`benchmarks/registry.json` 与 `environment/upstreams.json` 中共同声明的显式分支。
`prepare` 必须把解析后的 commit SHA 写入 campaign manifest。

## 基准接入契约

只有同时具备以下各项时，一个 benchmark 才是 ready：

- target/runner 映射，包含 Docker requirement、owner、provision mode 和 scope。
- 明确的 runner method 列表和 capability 声明。
- 分支跟踪的 upstream 条目，或者有文档说明的仓库自有 fixture。
- 保留 benchmark task、evaluator、raw metric 和 metric direction 的 native profile
  或 common adapter。
- 覆盖 schema 加载、method 拒绝、plan 生成和 capability 行为的 contract test。
- 可复现的 `doctor → prepare → run → status → finalize` 验收路径。
- 能证明每一项 `pass` 声明的 evidence 文件。

初始文件布局使用 `benchmark-adapt` 技能说明的适配 scaffold。生成的 placeholder
不代表已支持；验收路径没有实际执行之前，readiness 最多只能是 `partial`。

## 证据与比较不变量

- official verifier、native baseline、Plain Codex、Goal Plus + Codex、Plain Pi 和
  Goal Plus + Pi 的 readiness 声明必须分开。
- 固定 task/evaluator、model、reasoning、墙钟探索预算 `T`、task 内并行数 `K`、
  task cell 并发数 `C` 和重复数 `R`。记录 evaluator call、token/cost coverage、
  实际墙钟时间和 finalization grace；不得把缺失值静默写成零。
- 保留每种方法的原生控制流，以及 benchmark 的 raw metric 和 direction。
  method 或 benchmark 专属 completion evidence 写入选定的 runner reference，
  并由代码和测试强制执行。
- 定时自然调用开始之前，不得预建 Goal Plus goal、spec、run、candidate、session
  或 `.gp/`。不得向 Goal Plus core 增加 benchmark 专属停止逻辑。
- 缺少必需证据时必须是 `partial`，不得标记为 `pass`。

## 运行与安全不变量

- 不得持久化 API key、auth 文件、cookie、provider header 或包含凭据的命令行。
- 不得从 benchmark 源码 checkout 内运行 Goal Plus。必须在忽略的 `runs/` 下物化
  一次性 Git workspace，并把它的 `.gp/` 保留在该 workspace 内。
- `TMPDIR`、`TMP` 和 `TEMP` 必须通过 `bench_runtime_paths.py` 路由到仓库本地、
  已忽略的 `.tmp/`。controller state、build、test、evaluator output 或 subprocess
  scratch 不得使用主机全局 `/tmp`、`/private/tmp` 或 `/var/tmp`。
- 不得自动删除 workspace、campaign 或 cache。冲突路径必须用 `_bak` 后缀保留，
  并在结果中报告。
- 所有声明 inventory 的 target 和 asset pack 在任何镜像获取动作之前必须通过带
  profile 的 `check` 显示本地精确 tag、image ID 和关联容器。检查发现缺失时只报告；
  取得用户明确确认后才能进入 provision。
- 所有诊断性 `docker run` 必须显式使用 `--pull never`。
- setup 前必须执行 registry 的 `docker_requirement` 和 `docker_scope`。Docker
  不可用时，只能运行 `not_required` 路径；`mixed` target 只能运行其明确登记的
  portable task。不得用 host-only evaluator 代替容器化 official score。
- 必须保留 raw metric 及其 direction。normalized aggregate 只能是附加字段，
  不能取代原始指标。

## 必需验证

统一门禁必须使用仓库锁定环境：

```bash
.bench-env/venv/bin/python scripts/status.py --check
.bench-env/venv/bin/python -m unittest discover -s tests -v
```

修改受管 upstream 后，还必须在对应 checkout 中运行聚焦测试。声明某条 benchmark
路径 ready 之前，必须通过 `python3 scripts/bench.py` 执行其公开生命周期，并保留
生成的 manifest 和 evidence。
