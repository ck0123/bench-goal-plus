# Frontier-CS 2 本地任务级路径

FrontierCS 2（论文 arXiv 2610.29932）官方部署形态是 Terraform + AWS EKS 站点。
本目录记录并固化的是**本地任务级替代路径**：每个任务树
（`tasks/frontier-cs-2.0-demo/problems/<Category>/<task_id>/`）自带
agent-base / verifier Dockerfile 与 judge 材料（evaluator、reference patch、
beat spec、workloads），官方 verifier 镜像可以直接从任务树构建，按平台
相同的 `FRONTIER_*` 环境合同调用，CPU-only 任务本地 Docker 即可评测，
不需要 AWS EKS 站点。

## 已验证范围

`binary_fuse_filter`（Theory，C，patch 类任务）在 macOS Intel x86_64 +
OrbStack（linux/amd64 容器）上完成端到端验证：

1. **reference parity**：`verify_reference.py` eval 模式，reference patch
   复现 50.72 分（锚定 reference_score 50.0，margin +0.72），与任务自带的
   `judge/reference/runtime-qualification.json` 行为一致（strict FAIL 属预期：
   阈值锚定在 reference 统计量上）。
2. **plain codex**：codex-cli 0.164.0（gpt-6.1-sol，reasoning medium）实现
   binary fuse filter，train 52.61 / final eval 51.96（margin +1.96，
   `beats_reference: 0`，strict FAIL——lookup_ns 59/144）。
3. **官方对照**：HF `FrontierCS2/FrontierCS-2-runs` formal-results 中同模型
   （sol）官方成绩 53.91 FAIL；本地单轮 -1.95pp，verdict 一致。

完整记录（命令、commit、逐指标达标数、协议偏差）见
`evidence/environment/2026-10-27-frontier-cs-2-local-verifier-smoke.json`。

## 使用

统一经 `adapters/frontier_cs2/adapter.py`（受管 checkout 默认在
`third_party/frontier-cs-2`，可用 `--checkout` 指向已有 clone）：

```sh
# 列出任务
python3 adapters/frontier_cs2/adapter.py discover

# 构建官方 verifier 镜像
python3 adapters/frontier_cs2/adapter.py build --task binary_fuse_filter

# 物化 solver workspace（复制 agent-base 仓库 + baseline commit）
python3 adapters/frontier_cs2/adapter.py workspace --task binary_fuse_filter

# agent 在 workspace 内只改 include/；之后打包官方格式提交
python3 adapters/frontier_cs2/adapter.py submit --workspace <ws> --run-dir <run>

# train 反馈 / final 评测（--reference-only 跑 reference parity）
python3 adapters/frontier_cs2/adapter.py verify --task binary_fuse_filter \
  --run-dir <run> --mode train
python3 adapters/frontier_cs2/adapter.py verify --task binary_fuse_filter \
  --run-dir <run> --mode eval
```

## 协议保真度

与官方 run 的已知偏差（详见 evidence 文件）：

- agent 跑在宿主机有网络（官方 agent 沙箱离线）；
- 单轮提交：无 train 反馈迭代、无 agent 自选 final patch；
- 未强制 5 小时预算；
- timing 阈值在 AWS 硬件上校准；verifier 的同 CPU 配对测量部分抵消
  跨硬件差异（reference parity +0.72pp 佐证），跨机绝对比较仍需打折。

## 后续

- fork 上游到受管 namespace（registry `source_forked` 当前为 todo）；
- 扩展到其余 CPU-only 任务（Edlib、SCOPE 等轻量任务优先）；
- 补 train 反馈多轮迭代与 agent 自选 final patch 的驱动循环；
- goal-plus-codex 方法接入（T/K/C/R 契约：K=1）。
