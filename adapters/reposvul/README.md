# ReposVul adapter(repository-level 漏洞审计)

把 [ReposVul](https://github.com/Eshe0922/ReposVul)(ICSE 2024,6,134 CVE / 236 CWE /
1,491 项目,语句/函数/文件三级人工核验)接入 bench-goal-plus,作为公开可引用的
审计臂基准(与 zsoft-detect 同任务形态:仓库检出 + finding JSON + 盲评测)。

## 组件

| 文件 | 作用 |
|---|---|
| `build_catalog.py` | 从上游 `ReposVul.jsonl` 构建任务目录 `tasks/catalog.json`(按 (project, fix commit) 分组;ground truth 在父 commit 恢复) |
| `tasks/catalog.json` | 5,677 个任务(C 3,186 / Python 1,127 / Java 781 / C++ 583;1,632 个 finding_count≥3),5.7MB |
| `adapter.py` | bench 适配器:物化 / 公共校验 / 官方判定 / CLI |
| `assets/finding.schema.json` | finding JSON schema(与 zsoft-detect 同构) |

## 任务模型

- **一个 task = 一个 (project, fixing commit) 组**;workspace 的 `source/` 是
  **父 commit(漏洞版本)**的检出(blobless clone + checkout,缓存在
  `REPOSVUL_CHECKOUT_CACHE`,默认 `/home/tangzhiyun/workspace/ReposVul/checkouts`)。
- **Ground truth**(不进 workspace):`details[].function_before` 中 `target=1`
  的函数,定位到 `code_before` 得到 1-based 行区间 + 函数名;无函数文本时回退
  patch hunk `@@ -a,b` 行区间。每条 finding 带 CVE 的 CWE 作为接受类型。
- **提交物**:`submission/*.json`,字段 `location{path,function,start_line,
  end_line}` + `bug_type`(CWE id)+ `root_cause{cause,trigger,impact}`。

## 判定器(位置→类型→一对一)

与 zsoft-detect 同策略:同文件 且(行区间重叠 或 函数名相同)→ 位置命中;
`bug_type` 与该真值 CWE 相等(大小写/连字符归一,如 `cwe_120`≡`CWE-120`)
→ TP;一对一最大匹配,重复报告计 FP。指标 = F1(微平均,`PRIMARY_METRIC`)。

## 协议

盲评测(`EVALUATION_MODE=blind`):搜索期间 worker 只见 `format_valid`;
官方判定控制器私有,final 每工作区仅一次;事后全迭代选取契约与 zsoft-detect
一致。ground truth 从不进入 worker 可见文件。

## 防作弊(远程查询 ground truth)

ReposVul 的任务来自真实公开仓库 + 真实 CVE,存在"一次网络查询即得答案"的
泄露面。三层防护:

1. **离线泄露面已清除(代码强制)**:`task_id` 为不透明哈希(不含 fix commit
   /CVE 号);TASK.md 只含项目名;`task.json` 不含 fix commit、CVE/CWE 号、
   任何 commit 号——每个键都无法反查补丁。`source/` 检出剥离 `.git`。
   `MaterializationLeakTest` 对真实物化的全部 worker 可见文件做逐键扫描。
2. **运行时断网**:goal-plus-pi worker 走共享 bwrap 沙箱(loopback-only +
   桩 DNS + api-only LLM 中继,与 zsoft-l1/detect 同链路),远程查询不可达;
   跑后用 `scripts/audit_network_access.py` 复核全部会话记录。
   已知边界(与 detect 相同):宿主侧主会话不经 bwrap,但受审计覆盖。
3. **参数化污染(训练记忆)无法用代码消除**:CVE 均为 2024 年前公开数据,
   可能进入模型训练语料。目录每任务带 `publish_date`,可按模型知识截止
   时间过滤(如只取 publish_date 晚于截止的任务)缓解;论文中应作为
   有效性威胁显式报告。

## 运行(run 脚本)

```bash
# 列任务(语言/密度过滤)
./adapters/reposvul/run_all_reposvul_cases.sh --list-tasks --lang C --min-findings 3

# 单任务验证
./adapters/reposvul/run_all_reposvul_cases.sh --task-id jpirko-libndp-71401ba1

# 开 Shared Knowledge Board(audit 模板)+ path-validation skill + REA
export BENCH_GOAL_PLUS_SOURCE_DIR=/data/disk/tangzhiyun/muyuan/plugins/goal-plus
REPOSVUL_SHARED_KNOWLEDGE=1 REPOSVUL_REA=1 \
  ./adapters/reposvul/run_all_reposvul_cases.sh --lang C --min-findings 3
```

- **知识板模板**:reposvul 与 zsoft-detect 共用 audit 模板
  (`function_signatures` / `value_ranges` / `fp_verdicts` 三分节,
  `shared_knowledge_template()` 显式路由)——finding 审计任务形态一致。
- **Skills**(均 opt-in,Goal Plus 冻结时自动发现并指示 worker 阅读):
  - `path-validation`(默认**跟随知识板**开关,`REPOSVUL_SKILL` 可强制):
    source→sink 值域可行性判定,写入 `fp_verdicts` 的 verdict 需带证据链;
  - `rea-analysis`(`REPOSVUL_REA=1`,需 REA 包):二进制级验证辅助。
- provider/断网预检/goal-plus 检出校验与 zsoft 系脚本同款。

## 重建目录

```bash
python3 adapters/reposvul/build_catalog.py            # 默认读本地部署的数据集
REPOSVUL_DATASET=/path/ReposVul.jsonl python3 adapters/reposvul/build_catalog.py
```

## 物化/评测(手动冒烟)

```bash
.bench-env/venv/bin/python -m adapters.reposvul.adapter materialize \
    --upstream-root . --workspace /tmp/ws --task-id jpirko-libndp-a4892df306
# 写 finding JSON 到 /tmp/ws/submission/ 后:
.bench-env/venv/bin/python -m adapters.reposvul.adapter evaluate \
    --workspace /tmp/ws --upstream-root . --mode public   # 或 final
```

## 已验证

- 端到端实活测试(libndp @ 父 commit):完美提交 **F1=1.000(4/0/0)**;
  边界提交(错类型/行不重叠/重复+小写 CWE)TP=1/FP=2/FN=3,符合一一匹配语义
- 17 项单元测试(judge 语义 / 目录构建 / 注册表契约 / 工作区流)
- 回归:zsoft detect+l1 35 测试通过,status --check 通过

## Smoke

```sh
python3 -m unittest tests.test_reposvul_adapter -v
```

## 引用

Wang et al., *A Repository-Level Dataset For Detecting, Classifying and
Repairing Software Vulnerabilities*, ICSE 2024 Challenge Track
(arXiv:2401.13169)。
