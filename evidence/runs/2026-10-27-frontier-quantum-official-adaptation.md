# Frontier Quantum 官方修复版适配

已接入 bench-goal-plus 的原生 Frontier runner，统一入口为 `python3 scripts/bench.py`。
新 campaign 使用官方 `Einsia/Frontier-Engineering` main，验收时解析为
`bafa7d53162a3b1f128fec396bbde22fe972bf24`，独立位于
`third_party/frontier-engineering-official`。上游源码无修改；旧 checkout 和历史 campaign 保留。

## 已实现与验证

- 原样调用官方 UnifiedTask：QASM3 输入/输出、候选独立进程、bubblewrap PID namespace，随后执行零态加四个随机态的 unitary gate。官方候选进程使用兼容文件系统模式，不声称是受限文件系统沙箱。
- 任务依赖固定为 Qiskit 2.3.1、MQT Bench 2.3.0、qiskit-qasm3-import 0.6.0。Plain/Main/worker 工作区均提供只读 `task_python.py` 与官方结构 helper；无需向工具提交越过工作区的绝对解释器路径。
- 公开 setup/doctor 已实际执行工作区导入、Statevector 和官方 seed 评测；seed 为 0.20898716119828828。
- 完整官方 evaluator 接受常规 transpile（3.0950238871070774），拒绝空电路、直接制备固定输出态、历史 6.054 交付代码。拒绝理由和三个 case 的 gate 结果均已保存。
- prepare 冻结实际 verifier、case、shell 与共享候选 runner；修改 verifier 的回归用例会被拒绝。协议标记、源码 SHA 与 evaluator hash 写入最终证据。

## Agent 短测试

| 方法 | T/K/C/R | 官方有效分数 | Campaign 状态 | 证据范围 |
| --- | --- | ---: | --- | --- |
| Plain Pi | 300/1/1/1 | 3.8019253949585052 | partial | 实际调用工作区量子 Python，官方 final valid；预算到期被终止，未自然退出 |
| Goal Plus Pi fork | 600/4/1/1 | 0.20898716119828828 | partial | 四条绑定 worker 已启动，控制器完成官方验证、选择及提升；fork 准备占 377.88 秒，执行区间证据不完整，未完成 K4 搜索验收 |

两者均已通过统一 `finish` 导出 JSON、Markdown 和 XLSX，并检查了结果行与冻结表头。
这不是同预算性能对照。GP 分数仍是 seed，不能用本次短测试评价优化效果，也不能将
“四个启动收据”写成已证明持续 K4。

Campaign：

- `runs/frontier-engineering/frontier-engineering-quantum-official-plain-pi-smoke-20261027`
- `runs/frontier-engineering/frontier-engineering-quantum-official-gp-k4-fork-smoke-20261027`

## 一小时配置

已通过 plan 的 profile：

- `quantum-routing-plain-pi-sol-1h`：T=3600/K=1/C=1/R=1。
- `quantum-routing-goal-plus-pi-sol-1h-k4-fork`：T=3600/K=4/C=1/R=1，fork。

两者均为 gpt-5.6-sol / medium，使用同一官方任务与依赖。正式运行已完成，见下表。
下面展示 Plain 的生命周期；GP 用对应 profile 执行同样步骤。

```bash
# GP fork 必须使用包含准备回合恢复修复的源码；独立分支保留验收版本。
git clone --branch fix/frontier-fork-preparation-resume --single-branch \
  https://github.com/ck0123/muyuan.git third_party/muyuan-frontier
export BENCH_GOAL_PLUS_SOURCE_DIR="$PWD/third_party/muyuan-frontier/plugins/goal-plus"
export BENCH_GOAL_PLUS_EXPECTED_REF=3b2cfa89a7a45b71d2574de9650668003e8f9e61

python3 scripts/bench.py check --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h
python3 scripts/bench.py setup --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h --skip-provision
python3 scripts/bench.py plan --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h
python3 scripts/bench.py launch --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h --skip-provision
```

模型凭据与 endpoint 仅继承环境，不在此记录。新机器先做 inventory；缺失项须按安装授权
补齐，不能拿 skip 参数绕过 source/doctor 校验。该一小时 Agent 协议不同于论文
OpenEvolve 100 iterations；本次也未验收其它 Frontier 任务或 Codex/OpenEvolve 方法。

## 测试与 readiness

Frontier 33 项、registry 7 项、统一生命周期 35 项测试通过；两个新增回归均已证明在
修复前失败。统一 status check 通过。

发布前在隔离 worktree（不含其他未提交修复）验证：Frontier、parallel audit、fork admission、
command 共 41 项通过，统一 status check 通过。全仓 587 项测试有 16 failures、7 errors、
1 skipped；同环境远端主线基线为 578 项、相同的 16 failures 与 7 errors，新增 9 项测试
全部通过，没有新增失败。既存失败涉及 fixed_search、EdgeBench、OpenEvolve、repro-env、
SWE-bench 和 ZSoft。Goal Plus 依赖的 control 模块另有 26 项通过。
全仓门禁未全绿，不将其写成通过。

官方 Quantum verifier、环境、native seed、Plain Pi 和 Goal Plus Pi fork 生命周期为
pass；matched comparison 尚未进行。对应独立 readiness 条目为
`frontier-engineering-quantum-official`，旧协议证据没有提升新版方法状态。

机器证据：

- [环境与官方 verifier](../environment/2026-10-27-frontier-quantum-official-protocol.json)
- [Agent 短测试与导出哈希](2026-10-27-frontier-quantum-official-smokes.json)
- 诊断日志：`.tmp/frontier-official-adapt-20261027/`。

## 一小时运行与 partial 误判修复

| 方法 | T/K/C/R | 官方有效分数 | 实际 Agent 时间 | 最终状态 |
| --- | --- | ---: | ---: | --- |
| Plain Pi | 3600/1/1/1 | 3.908047300767494 | 676.8 秒 | completed |
| Goal Plus Pi fork | 3600/4/1/1 | 4.021314277529203 | 3602.3 秒 | completed |

Plain 的原生会话明确返回最终回答并退出；没有后续续跑。GP 的 4 个 worker 均有
worker verifier 证据，21 个真实执行区间、零个无效区间、峰值 4；每条 lineage
分别调用 5/5/6/5 次。此证据表示 worker 执行并行，不表示瞬时 API 调用重叠。

原审计只看 Main poll 收据，误把最后两次已实际结束、但 Main 未再次 poll 的调用视为
unsettled。新版按 run/candidate/native session 与唯一请求时间窗口，关联持久化 native
dispatch 的真实起止时间；缺失、错配、歧义和超时空闲仍拒绝。`finish` 可重审旧 schema 1
的此类 terminal partial，复用已有 final 分数，重新过原 closeout gate；保留全部旧报告
与状态的 `_bak` 备份，不启动模型或重跑搜索。此次重新归档后 GP 为 completed，分数未变。

两条运行的 K 不同，且 Plain 未用满 T，不能作为墙钟覆盖一致的性能对照。
脱敏完成证据与归档哈希见 [一小时运行证据](2026-10-27-frontier-quantum-official-1h.json)。
