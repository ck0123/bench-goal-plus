---
name: benchmark-adapt
description: 把新 benchmark、task family，或现有 runner 的新 method/provider/auth 执行路径接入 bench-goal-plus。用户要求快速适配 benchmark、登记上游 fork/branch、实现 materialize/evaluate adapter、复用 native harness、增加或删除 method/provider、定义认证与协议契约、定义并发标准、增加 install/run/report 支持或完成接入验收时使用。
---

# Benchmark 与方法适配

完整执行 [adaptation-checklist.md](references/adaptation-checklist.md)。先判断 benchmark 是否适合 common artifact adapter；native harness 已拥有容器、服务、浏览器、调度或 hidden judge 时保留 native lifecycle，只接控制面契约。

## 流程

1. 先判断是新增 benchmark/task family，还是给现有 runner 增删 method/provider/auth
   执行路径。新增 benchmark 时记录 official task、artifact、evaluator、raw metric、
   direction、环境、数据 revision、license 和 secret 边界，并先生成不覆盖现有文件的结构计划：

   ```bash
   python3 scripts/bench.py scaffold --benchmark-id <id> --shape common
   python3 scripts/bench.py scaffold --benchmark-id <id> --shape common --write
   ```

   native harness 使用 `--shape native`。scaffold 只生成目录、模板、契约测试和
   registration fragment，不自动把未完成实现登记为 supported。现有 runner 的方法适配不运行
   scaffold，直接修改所属 registry、runner/controller 和 reference。
2. 在独立 fork 建 benchmark-specific 改动；同时在 `benchmarks/registry.json` 与 `environment/upstreams.json` 登记同一显式 tracking branch。
3. 声明 readiness 的 `docker_requirement`/`docker_scope`，并在 `benchmarks/runners.json` 声明 Docker `owner`/`provision_mode`。自带 Docker/native harness 用 runner owner；common adapter 选择 eager hooks 或 lazy evaluator。
4. 选择接入面：单 artifact + controller evaluator 使用 `adapters/` contract；复杂 native harness 增加 `experiments/<benchmark>/` lifecycle 和一个可复用 runner；不要强塞进不匹配的 adapter。
5. 在 runner 的 `supported_methods` 登记 canonical method；需要方法级输入约束时同时声明
   `method_contracts`。固定 provider/model、auth mode 和 wire API 的来源，确保 `plan`、doctor、
   prepare 和 launch 使用同一解析结果，并在准备环境前拒绝冲突或不完整配置。
6. 固定 `T/K/C/R` 语义和资源上限。无法安全迁移并发时先声明 `K=1`，用小任务验证后再开放。
7. 保留 native raw metric；增加 manifest、status、final evidence、telemetry coverage 和统一报告字段。
   为 benchmark 接入开放式 ViewAgent 补充评价时，完整执行
   [supplemental-view-evaluation.md](references/supplemental-view-evaluation.md)；adapter 只映射
   可见任务背景、候选产物、硬 Evidence 和 peer incumbent，不定义评价维度。
8. 增加 registry/adapter/lifecycle/report 测试，完成 model-free seed smoke，再做最小真实 E2E。
9. 更新对应 Skill reference，而不是把 benchmark 特例堆进根 `AGENTS.md`。

## 验收

```bash
.bench-env/venv/bin/python scripts/status.py --check
.bench-env/venv/bin/python -m unittest discover -s tests -v
```

输出 readiness matrix：official verifier 与 runner `supported_methods` 中每个已声明方法
分别为 `pass`/`partial`/`fail`，并逐项链接命令和 evidence。即使底层 agent 相同，只要
OAuth、API credential、provider registry 或 wire API 的执行路径不同，也要分行记录。

`finish` 的 `reported` 只表示 campaign-local JSON、Markdown 和 XLSX 已生成，不会自动提升
source registry。真实 E2E 通过后，必须在同一验收变更中把经过审计和脱敏的最小证据投影到
`evidence/runs/`，通过 registry `stage_evidence` 绑定到 exact method，再把该 method 提升为
`pass`。不得让已有完整 PASS evidence 的 method 继续停在 `partial`，也不得创建没有
method-specific evidence 的 `pass`。

## Gotchas

- registry 中“存在”不等于 E2E ready；没有实际命令/evidence 只能是 `partial`。
- 不用兼容别名掩盖已删除或重命名的 method/provider；除非公共契约明确保留，否则旧名称应 fail closed。
- 不为模仿其他方法 round 数而向 Goal Plus core 加 benchmark-specific stop logic。
- 不预建 Goal Plus goal/spec/search run/candidate；这些必须在计时内从自然 prompt 开始。
- 不用 host-only evaluator 冒充官方容器 score。
