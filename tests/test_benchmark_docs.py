import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs" / "benchmarks"
BENCHMARK_DOCS = (
    "ale-bench-lite.md",
    "heurigym.md",
    "frontier-engineering-v1-lite.md",
    "autolab-cpu.md",
    "swarmresearch-15.md",
    "frontier-cs-algorithmic.md",
    "edgebench.md",
    "swe-bench-verified.md",
)
TASK_PACK_DOCS = ("skydiscover-task-packs.md",)
REQUIRED_CASE_SECTIONS = (
    "## 30 秒理解",
    "### 输入是什么",
    "### Agent 要做什么",
    "### 期待输出是什么",
    "### Verifier 如何评分",
    "## 实验怎么用",
    "## 可复用对比数据",
    "## 代码与证据",
)


class BenchmarkDocsTest(unittest.TestCase):
    def test_root_readme_is_a_short_operator_overview(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for required in (
            "benchmark 的控制面仓库",
            "自动检查并部署环境",
            "python3 scripts/bench.py catalog",
            "edgebench-codex-2h",
            "python3 scripts/bench.py plan",
            "python3 scripts/bench.py launch",
            "python3 scripts/bench.py status",
            "python3 scripts/bench.py finish",
            "report.md",
            "<campaign-id>.xlsx",
            "Host 与鉴权矩阵",
            "benchmark-adapt",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertLessEqual(len(text.splitlines()), 200)
        self.assertNotIn("## 当前结果", text)

    def test_root_agents_declares_directory_ownership_and_public_cli(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for required in (
            "基准测试运行控制面",
            "## 智能体标准流程",
            "## 技能路由",
            "`bench-goal-plus`",
            "`benchmark-setup`",
            "`benchmark-run`",
            "`benchmark-report`",
            "`benchmark-adapt`",
            "## T/K/C/R 契约",
            "`K` 只在 Goal Plus 方法中生效",
            "`C` 是一个 campaign 同时运行的不同 task cell 数量",
            "`budget.max_candidates` 已弃用",
            "实际 subagent 数量不等于 `K`",
            "## 目录职责",
            "`bench_goal_plus/`",
            "`benchmarks/`",
            "`adapters/<benchmark>/`",
            "`experiments/<benchmark>/`",
            "`docker/`",
            "`local_examples/`",
            "`evidence/`",
            "`legacy/`",
            "`.agents/skills/`",
            "`.github/workflows/`",
            "python3 scripts/bench.py",
            "每个新的仓库自有顶层目录在使用之前，都必须先在此表中增加职责行",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_operator_skills_use_the_canonical_public_cli(self):
        skill_paths = (
            ROOT / ".agents/skills/bench-goal-plus/SKILL.md",
            ROOT / ".agents/skills/benchmark-run/SKILL.md",
            ROOT / ".agents/skills/benchmark-adapt/SKILL.md",
        )
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in skill_paths
        )
        self.assertIn("python3 scripts/bench.py", combined)
        self.assertNotRegex(
            combined,
            r"python3 \.agents/skills/.*/scripts/.*\.py",
        )

    def test_setup_skill_checks_local_assets_before_provision(self):
        skill = (
            ROOT / ".agents/skills/benchmark-setup/SKILL.md"
        ).read_text(encoding="utf-8")
        matrix = (
            ROOT
            / ".agents/skills/benchmark-setup/references/benchmark-matrix.md"
        ).read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for required in (
            "scripts/bench.py check --benchmark <id> --profile <profile>",
            "scripts/bench.py check --environment",
            "git ls-remote",
            "fast-forward",
            "local_asset_inventory=true",
            "--skip-provision",
            "不得把 provision 当作本地 inventory probe",
            "失败只报告缺失项",
            "全部通过时立即停止 setup",
        ):
            with self.subTest(required=required):
                self.assertIn(required, skill)
        for required in (
            "Mandatory local-first gate",
            "check --environment",
            "default_inventory_profile",
            "assets=True",
            "read_only: true",
            "acquisition_attempted: false",
            "docker image inspect <exact-ref>",
            "docker ps -a --no-trunc",
            "guaranteed not to run `provision`",
            "do not run `provision`",
            "even when those local tags already exist",
        ):
            with self.subTest(required=required):
                self.assertIn(required, matrix)
        for required in (
            "不带 `--profile` 的 benchmark target `check` 只检查仓库契约",
            "`check --environment` 是显式的全环境复合检查",
            "docker image inspect",
            "所有诊断性 `docker run` 必须显式使用 `--pull never`",
        ):
            with self.subTest(required=required):
                self.assertIn(required, agents)

    def test_benchmark_run_requires_explicit_k_c_confirmation(self):
        text = (
            ROOT / ".agents/skills/benchmark-run/SKILL.md"
        ).read_text(encoding="utf-8")
        for required in (
            "## K/C 启动确认门禁",
            "不得执行 `launch` 或 `e2e`",
            "K=<数量>：Plain 的隔离 outer trajectories 数",
            "C=<数量>：campaign 同时运行的不同 task cell 数",
            "方法运行时源码=<source kind；ref/branch；完整 commit SHA",
            "同时运行规模=<按该方法解释的 K × C>",
            "不能自动映射到配置",
            "只写版本号、目录名、tracking branch、短 SHA",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

        edgebench = (
            ROOT
            / ".agents/skills/benchmark-run/references/benchmarks/edgebench.md"
        ).read_text(encoding="utf-8")
        for required in (
            "source_kind + expected_ref/branch + 完整 commit SHA",
            "受管 Goal Plus tracking branch 没有变化",
            "goal_plus_source.commit",
        ):
            with self.subTest(required=required):
                self.assertIn(required, edgebench)

    def test_router_skill_routes_platform_and_benchmark_differences(self):
        text = (ROOT / ".agents/skills/bench-goal-plus/SKILL.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "只负责识别任务阶段并路由",
            "host-auth.md",
            "benchmark-matrix.md",
            "runner-map.md",
            "report-contract.md",
            "adaptation-checklist.md",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_runner_map_routes_each_runner_family(self):
        text = (
            ROOT
            / ".agents/skills/benchmark-run/references/runner-map.md"
        ).read_text(encoding="utf-8")
        for required in (
            "benchmarks/edgebench.md",
            "benchmarks/common-matrix.md",
            "benchmarks/openevolve.md",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_all_root_and_skill_markdown_links_resolve(self):
        markdown_files = [ROOT / "README.md", ROOT / "AGENTS.md"] + list(
            (ROOT / ".agents/skills").rglob("*.md")
        )
        link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        for markdown_file in markdown_files:
            text = markdown_file.read_text(encoding="utf-8")
            for target in link_pattern.findall(text):
                if target.startswith(("https://", "http://", "#", "mailto:")):
                    continue
                path_target = target.split("#", 1)[0]
                resolved = (markdown_file.parent / path_target).resolve()
                with self.subTest(file=markdown_file.name, target=target):
                    self.assertTrue(resolved.exists(), str(resolved))

    def test_overview_links_every_active_benchmark(self):
        overview = (DOCS_DIR / "README.md").read_text(encoding="utf-8")
        for filename in BENCHMARK_DOCS + TASK_PACK_DOCS:
            with self.subTest(filename=filename):
                self.assertIn(f"]({filename})", overview)

    def test_each_benchmark_explains_one_case_contract(self):
        for filename in BENCHMARK_DOCS:
            text = (DOCS_DIR / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("## 代表 case：", text)
                for heading in REQUIRED_CASE_SECTIONS:
                    self.assertIn(heading, text)

    def test_docker_requirement_is_visible_in_overview_and_each_benchmark(self):
        overview = (DOCS_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Docker 依赖速查", overview)
        self.assertIn("没有 Docker 时能否跑", overview)
        for filename in BENCHMARK_DOCS:
            text = (DOCS_DIR / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("Docker", text)
                self.assertIn("Docker 空间", text)
                self.assertIn("无 Docker 环境", text)
        self.assertIn("镜像逻辑大小 / 共享层实际增量 / 建议预留", overview)

    def test_skydiscover_task_pack_records_measured_space_and_exclusions(self):
        text = (DOCS_DIR / "skydiscover-task-packs.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "19 个镜像 tag",
            "8.57 GB",
            "2.49 GB",
            "10 GB",
            "ADRS/eplb",
            "math/second_autocorr_ineq",
            "kernelbench",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_local_markdown_links_resolve(self):
        markdown_files = (
            [DOCS_DIR / "README.md"]
            + [DOCS_DIR / filename for filename in BENCHMARK_DOCS]
            + [DOCS_DIR / filename for filename in TASK_PACK_DOCS]
            + [
                ROOT / "docs" / "goal-plus-benchmark-experiment.md",
                ROOT / "docs" / "openevolve-cpu-examples.md",
                ROOT / "docs" / "reproducible-environment.md",
            ]
        )
        link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        for markdown_file in markdown_files:
            text = markdown_file.read_text(encoding="utf-8")
            for target in link_pattern.findall(text):
                if target.startswith(("https://", "http://", "#")):
                    continue
                resolved = (markdown_file.parent / target).resolve()
                with self.subTest(file=markdown_file.name, target=target):
                    self.assertTrue(resolved.exists(), str(resolved))

    def test_docs_do_not_contain_local_identity_or_api_keys(self):
        extra_docs = [
            ROOT / "docs" / "goal-plus-benchmark-experiment.md",
            ROOT / "docs" / "openevolve-cpu-examples.md",
            ROOT / "docs" / "reproducible-environment.md",
        ]
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [DOCS_DIR / "README.md"]
            + [DOCS_DIR / filename for filename in BENCHMARK_DOCS]
            + [DOCS_DIR / filename for filename in TASK_PACK_DOCS]
            + extra_docs
        )
        self.assertNotRegex(combined, r"/Users/[^/\s]+")
        self.assertNotRegex(combined, r"\bsk-[A-Za-z0-9_-]{16,}\b")

    def test_experiment_protocol_covers_non_pass_at_k_claim(self):
        protocol = (ROOT / "docs" / "goal-plus-benchmark-experiment.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "Agent 并发",
            "Evaluator 并发",
            "Task 并发",
            "Independent Parallel",
            "OpenEvolve",
            "best-score AUC",
            "cross-lineage transfer",
        ):
            with self.subTest(required=required):
                self.assertIn(required, protocol)

    def test_protocol_has_every_active_benchmark(self):
        protocol = (ROOT / "docs" / "goal-plus-benchmark-experiment.md").read_text(
            encoding="utf-8"
        )
        for benchmark_name in (
            "ALE-Bench Lite",
            "HeuriGym",
            "Frontier-Engineering v1-lite",
            "AutoLab CPU subset",
            "SwarmResearch 15",
            "Frontier-CS Algorithmic",
            "EdgeBench open-source subset",
        ):
            with self.subTest(benchmark=benchmark_name):
                self.assertIn(f"### {benchmark_name}", protocol)

    def test_protocol_has_codex_integration_matrix(self):
        protocol = (ROOT / "docs" / "goal-plus-benchmark-experiment.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "## Codex 接入整改总表",
            "Plain Codex 当前证据",
            "Goal Plus + Codex 当前状态",
            "benchmark / fork 要改什么",
            "`bench-goal-plus` 要新增什么",
            "`goal-plus` core 要改什么",
            "OpenEvolve CPU examples（任务包 + 原生基线）",
            "ALE、HeuriGym、AutoLab、Frontier-Engineering",
            "Frontier-CS 与 EdgeBench 已各有至少一题的 Plain / Goal Plus 真实路径证据",
        ):
            with self.subTest(required=required):
                self.assertIn(required, protocol)

    def test_openevolve_tasks_are_separate_from_search_runners(self):
        audit = (ROOT / "docs" / "openevolve-cpu-examples.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "共同 task/evaluator substrate",
            "四套独立入口",
            "原生 OpenEvolve",
            "Plain Codex",
            "Goal Plus",
            "不需要增加 Codex provider",
            "通用 `openevolve_task` adapter",
        ):
            with self.subTest(required=required):
                self.assertIn(required, audit)
        self.assertNotIn("固定 fork 需增加 `codex_cli` provider", audit)


if __name__ == "__main__":
    unittest.main()
