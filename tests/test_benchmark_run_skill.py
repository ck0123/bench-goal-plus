from __future__ import annotations

import unittest
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

from adapters.registry import adapter_modules
from adapters.registry import load_adapter
from bench_goal_plus.application import BenchmarkAgent
from bench_goal_plus.catalog import Catalog
from bench_goal_plus.cli import build_parser
from bench_goal_plus.errors import ContractError, UnsupportedOperation
from bench_goal_plus.runners.factory import create_runner
from bench_goal_plus.models import CampaignRef
from bench_goal_plus.runtime import RuntimeManager
from bench_goal_plus.state import create_agent_state


ROOT = Path(__file__).resolve().parents[1]


class RecordingExecutor:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def execute(self, commands: list[list[str]], *, dry_run: bool) -> None:
        self.commands.extend(commands)


class BenchmarkAgentContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = Catalog()
        self.executor = RecordingExecutor()
        self.agent = BenchmarkAgent(catalog=self.catalog, executor=self.executor)

    def test_skill_requires_typed_goal_plus_host_commands(self) -> None:
        skill = (
            ROOT / ".agents/skills/benchmark-run/SKILL.md"
        ).read_text(encoding="utf-8")

        for fragment in (
            "$goal-plus mode=autonomous max_parallel=K",
            "/goal-plus mode=autonomous max_parallel=K",
            "workspace_backend=git_worktree",
            "promotion_mode=MODE",
            "strategy=STRATEGY",
            "workers=MODEL*K",
            "annotator=MODEL",
            "campaign manifest",
        ):
            self.assertIn(fragment, skill)
        self.assertIn("不能只在后续 prompt prose", skill)
        self.assertIn("promotion_mode=artifact_only", skill)
        self.assertIn("promotion_mode=apply", skill)

    def test_catalog_reuses_runner_families_and_covers_common_adapters(self) -> None:
        common = {
            target.adapter_id
            for target in self.catalog.targets.values()
            if target.runner_id == "common-matrix"
        }
        self.assertEqual(common, set(adapter_modules()))
        self.assertEqual(self.catalog.targets["edgebench"].runner_id, "edgebench-native")
        self.assertTrue(
            self.catalog.targets["edgebench"].local_asset_inventory
        )
        self.assertFalse(
            self.catalog.targets["local-vliw"].local_asset_inventory
        )
        self.assertTrue(
            self.catalog.targets["frontier-cs-problem-0"].local_asset_inventory
        )
        self.assertTrue(
            self.catalog.targets["ale-bench-lite"].local_asset_inventory
        )
        self.assertEqual(
            self.catalog.targets["edgebench"].default_inventory_profile,
            "vliw-smoke",
        )
        self.assertEqual(
            self.catalog.targets["ale-bench-lite"].default_inventory_profile,
            "ahc027-cpp20-202301",
        )
        self.assertEqual(
            self.catalog.targets[
                "frontier-cs-problem-0"
            ].default_inventory_profile,
            "problem-0",
        )
        self.assertIn("skydiscover-cpu-evaluators", self.catalog.asset_packs)
        self.assertIn("openevolve-cpu-portable", self.catalog.targets)
        self.assertIn("swe-bench-verified", self.catalog.targets)
        self.assertEqual(len(self.catalog.runners), 7)
        self.assertEqual(
            self.catalog.targets["frontier-engineering"].runner_id,
            "frontier-engineering-native",
        )
        self.assertEqual(
            self.catalog.runners["edgebench-native"].supported_methods,
            (
                "plain-codex",
                "goal-plus-codex",
                "plain-claude",
                "plain-pi",
                "plain-pi-provider",
                "goal-plus-pi",
                "goal-plus-pi-provider",
            ),
        )
        self.assertEqual(
            self.catalog.runners["edgebench-native"].method_contracts,
            {
                "goal-plus-codex": {
                    "runtime_source_command": "plan-metadata"
                },
                "plain-pi-provider": {"model_format": "provider/model"},
                "goal-plus-pi": {
                    "runtime_source_command": "plan-metadata"
                },
                "goal-plus-pi-provider": {
                    "model_format": "provider/model",
                    "runtime_source_command": "plan-metadata",
                },
            },
        )

    def test_runner_rejects_unknown_method_before_prepare(self) -> None:
        with self.assertRaisesRegex(
            ContractError, "edgebench-native does not support.*plain-pi-typo"
        ):
            self.agent.resolve_spec(
                target_ids=("edgebench",),
                profile="vliw-pi-sol-medium-local-smoke",
                methods=("plain-pi-typo",),
            )

    def test_edgebench_pi_presets_resolve_to_canonical_methods(self) -> None:
        plain = self.agent.resolve_spec(
            preset_id="edgebench-vliw-pi-local-smoke"
        )
        goal_plus = self.agent.resolve_spec(
            preset_id="edgebench-vliw-goal-plus-pi-local-smoke"
        )
        api_provider = self.agent.resolve_spec(
            preset_id="edgebench-vliw-goal-plus-pi-glm-provider-1h"
        )
        zai_provider = self.agent.resolve_spec(
            preset_id="edgebench-vliw-goal-plus-pi-zai-glm-5-2-1h"
        )

        self.assertEqual(plain.methods, ("plain-pi",))
        self.assertEqual(plain.concurrency(), {"T": 300, "K": 1, "C": 1, "R": 1})
        self.assertEqual(goal_plus.methods, ("goal-plus-pi",))
        self.assertEqual(
            goal_plus.concurrency(), {"T": 600, "K": 2, "C": 1, "R": 1}
        )
        self.assertEqual(
            api_provider.methods, ("goal-plus-pi-provider",)
        )
        self.assertEqual(api_provider.model, "glm-proxy/GLM-5.2")
        self.assertEqual(
            api_provider.concurrency(),
            {"T": 3600, "K": 2, "C": 1, "R": 1},
        )
        self.assertEqual(zai_provider.methods, ("goal-plus-pi-provider",))
        self.assertEqual(zai_provider.model, "zai/glm-5.2")
        self.assertEqual(
            zai_provider.concurrency(),
            {"T": 3600, "K": 2, "C": 1, "R": 1},
        )

    def test_edgebench_goal_plus_codex_preset_resolves_fifteen_minute_contract(
        self,
    ) -> None:
        spec = self.agent.resolve_spec(
            preset_id="edgebench-vliw-goal-plus-codex-local-smoke"
        )

        self.assertEqual(spec.methods, ("goal-plus-codex",))
        self.assertEqual(spec.model, "gpt-5.5")
        self.assertEqual(spec.reasoning_effort, "high")
        self.assertEqual(spec.concurrency(), {"T": 900, "K": 2, "C": 2, "R": 1})

    def test_goal_plus_plan_includes_native_runtime_source_metadata(self) -> None:
        spec = self.agent.resolve_spec(
            preset_id="edgebench-vliw-goal-plus-codex-local-smoke"
        )
        probe = mock.Mock(
            return_value=mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_sources": {
                            "goal_plus": {
                                "source_kind": "external",
                                "source_path": ".worktrees/goal-plus/plugins/goal-plus",
                                "expected_ref": "experiment/ref",
                                "branch": "experiment/ref",
                                "commit": "a" * 40,
                            }
                        },
                        "runtime_configuration": {
                            "goal_plus": {
                                "shared_dir_enabled": False,
                                "supplemental_evaluation_enabled": False,
                            }
                        },
                    }
                ),
                stderr="",
            )
        )

        with mock.patch(
            "bench_goal_plus.runners.native_profile.subprocess.run", probe
        ):
            plan = self.agent.start(
                spec,
                skip_bootstrap=True,
                skip_provision=True,
                prepare_only=False,
                foreground=False,
                dry_run=True,
            )

        source = plan["resolved_spec"]["runtime_sources"]["goal_plus"]
        self.assertEqual(source["source_kind"], "external")
        self.assertEqual(source["expected_ref"], "experiment/ref")
        self.assertEqual(source["commit"], "a" * 40)
        self.assertEqual(
            plan["resolved_spec"]["runtime_configuration"]["goal_plus"],
            {
                "shared_dir_enabled": False,
                "supplemental_evaluation_enabled": False,
            },
        )
        self.assertIn("plan-metadata", probe.call_args.args[0])

    def test_pi_provider_method_requires_qualified_provider_and_model(self) -> None:
        arguments = {
            "target_ids": ("edgebench",),
            "profile": "vliw-goal-plus-pi-glm-5-2-provider-1h-k2-c1",
            "methods": ("goal-plus-pi-provider",),
            "reasoning_effort": "high",
            "wall_time_seconds": 3600,
            "live_search_concurrency": 2,
            "cell_concurrency": 1,
        }
        with self.assertRaisesRegex(ContractError, "PROVIDER/MODEL"):
            self.agent.resolve_spec(model="GLM-5.2", **arguments)

        spec = self.agent.resolve_spec(model="zai/glm-5.2", **arguments)
        self.assertEqual(spec.model, "zai/glm-5.2")
        doctor = create_runner(spec.runner).provision_commands(
            spec, skip_provision=True
        )[0]
        self.assertEqual(
            doctor[-6:],
            [
                "--model",
                "zai/glm-5.2",
                "--reasoning-effort",
                "high",
                "--method",
                "goal-plus-pi-provider",
            ],
        )

    def test_plain_pi_provider_uses_the_same_qualified_model_contract(self) -> None:
        arguments = {
            "target_ids": ("edgebench",),
            "profile": "vliw-goal-plus-pi-zai-glm-5-2-1h-k2-c1",
            "methods": ("plain-pi-provider",),
            "reasoning_effort": "high",
            "wall_time_seconds": 3600,
            "live_search_concurrency": 1,
            "cell_concurrency": 1,
        }
        with self.assertRaisesRegex(ContractError, "PROVIDER/MODEL"):
            self.agent.resolve_spec(model="glm-5.2", **arguments)

        spec = self.agent.resolve_spec(model="zai/glm-5.2", **arguments)
        self.assertEqual(spec.methods, ("plain-pi-provider",))

    def test_profiled_check_routes_to_read_only_native_inventory(self) -> None:
        args = build_parser().parse_args(
            [
                "check",
                "--benchmark",
                "edgebench",
                "--profile",
                "vliw-smoke",
            ]
        )
        self.assertEqual(args.benchmark, ["edgebench"])
        self.assertEqual(args.profile, "vliw-smoke")

        result = self.agent.check(
            "edgebench", profile="vliw-smoke", dry_run=True
        )

        self.assertEqual(len(self.executor.commands), 1)
        command = self.executor.commands[0]
        self.assertEqual(
            command[-4:],
            ["doctor", "--profile", "vliw-smoke", "--local-assets-only"],
        )
        self.assertNotIn("provision", command)
        inventory = result["local_asset_inventory"]
        self.assertTrue(inventory["read_only"])
        self.assertFalse(inventory["acquisition_attempted"])

    def test_setup_inventory_can_report_missing_assets_before_provision(self) -> None:
        target = self.catalog.targets["edgebench"]
        self.agent.setup(
            (target,),
            profile="vliw-smoke",
            skip_bootstrap=True,
            skip_provision=False,
            dry_run=True,
        )

        inventory = self.executor.commands[0]
        self.assertIn("--local-assets-only", inventory)
        self.assertIn("--allow-missing-local-assets", inventory)
        provision_index = next(
            index
            for index, command in enumerate(self.executor.commands)
            if "provision" in command
        )
        self.assertGreater(provision_index, 0)

    def test_setup_routes_selected_method_and_model_to_native_doctor(self) -> None:
        args = build_parser().parse_args(
            [
                "setup",
                "--benchmark",
                "edgebench",
                "--profile",
                "vliw-smoke",
                "--method",
                "goal-plus-codex",
                "--model",
                "gpt-5.6-sol",
                "--reasoning-effort",
                "medium",
            ]
        )
        self.assertEqual(args.method, ["goal-plus-codex"])
        self.assertEqual(args.model, "gpt-5.6-sol")
        self.assertEqual(args.reasoning_effort, "medium")

        target = self.catalog.targets["edgebench"]
        self.agent.setup(
            (target,),
            profile="vliw-smoke",
            methods=("goal-plus-codex",),
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            skip_bootstrap=True,
            skip_provision=True,
            dry_run=True,
        )

        doctor = next(
            command
            for command in self.executor.commands
            if "experiments/edgebench/experiment.py" in command
            and "doctor" in command
            and "--local-assets-only" not in command
        )
        self.assertEqual(doctor[doctor.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(
            doctor[doctor.index("--reasoning-effort") + 1], "medium"
        )
        self.assertEqual(
            doctor[doctor.index("--method") + 1], "goal-plus-codex"
        )

    def test_profiled_check_fails_closed_for_unsupported_runner(self) -> None:
        with self.assertRaisesRegex(UnsupportedOperation, "target local-vliw"):
            self.agent.check(
                "local-vliw", profile="cpu_portable", dry_run=True
            )

    def test_profiled_check_routes_to_adapter_inventory_by_target(self) -> None:
        result = self.agent.check(
            "frontier-cs-problem-0", profile="problem-0", dry_run=True
        )

        self.assertEqual(len(self.executor.commands), 1)
        self.assertEqual(self.executor.commands[0][0], sys.executable)
        self.assertEqual(
            self.executor.commands[0][-6:],
            [
                "bench_goal_plus.docker_hooks",
                "inventory",
                "--target",
                "frontier-cs-problem-0",
                "--profile",
                "problem-0",
            ],
        )
        self.assertTrue(result["local_asset_inventory"]["read_only"])

    def test_asset_pack_check_and_setup_use_inventory_before_provision(self) -> None:
        pack = self.agent.resolve_asset_packs(
            ("skydiscover-cpu-evaluators",)
        )[0]
        checked = self.agent.check_asset_pack(
            pack, profile=None, dry_run=True
        )
        self.assertEqual(checked["profile"], "cpu-no-torch-19")
        self.assertIn("environment.py inventory", checked["commands"][0])

        self.executor.commands.clear()
        setup = self.agent.setup_asset_packs(
            (pack,),
            profile=None,
            skip_bootstrap=False,
            skip_provision=False,
            dry_run=True,
        )
        rendered = setup["commands"]
        self.assertIn("environment.py inventory", rendered[0])
        self.assertIn("docker info", rendered[1])
        self.assertTrue(any("environment.py provision" in item for item in rendered))
        self.assertTrue(rendered[-1].endswith("environment.py doctor --profile cpu-no-torch-19"))

    def test_environment_check_gates_assets_before_offering_git_updates(self) -> None:
        args = build_parser().parse_args(
            ["check", "--environment", "--yes", "--dry-run"]
        )
        self.assertTrue(args.environment)
        self.assertTrue(args.yes)

        result = self.agent.check_environment(assume_yes=True, dry_run=True)

        rendered = result["commands"]
        self.assertTrue(rendered[0].endswith("scripts/status.py --check"))
        update_index = next(
            index
            for index, command in enumerate(rendered)
            if "scripts/repro_env.py check" in command
        )
        self.assertEqual(update_index, len(rendered) - 1)
        self.assertIn("--inventory-gated", rendered[-1])
        self.assertTrue(rendered[-1].endswith("--yes"))
        for profile in (
            "vliw-smoke",
            "sympy-16886-codex-smoke",
            "smoke",
            "ahc027-cpp20-202301",
            "problem-0",
            "v1-lite-cpu-codex-1h",
            "cpu-no-torch-19",
        ):
            self.assertTrue(
                any(profile in command for command in rendered[1:update_index]),
                profile,
            )
        self.assertEqual(
            [item["id"] for item in result["inventory_gates"]],
            [
                "edgebench",
                "swe-bench-verified",
                "aibench-coding",
                "ale-bench-lite",
                "frontier-cs-problem-0",
                "frontier-engineering",
                "zsoft-detect-swe-agent",
                "skydiscover-cpu-evaluators",
            ],
        )

    def test_native_inventory_works_before_managed_environment_exists(self) -> None:
        runner = create_runner(self.catalog.runners["aibench-coding-native"])
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-python"
            with mock.patch(
                "bench_goal_plus.runners.native_profile.managed_python",
                return_value=missing,
            ):
                command = runner.local_asset_check_commands("smoke")[0]
        self.assertEqual(command[0], sys.executable)
        self.assertIn("--local-assets-only", command)

    def test_every_target_has_an_explicit_docker_owner_and_mode(self) -> None:
        for target in self.catalog.targets.values():
            self.assertIn(target.docker.owner, {"runner", "adapter", "host"})
            self.assertIn(
                target.docker.provision_mode, {"eager", "lazy", "external", "none"}
            )
            self.assertTrue(target.docker.scope)
            if target.docker.owner == "adapter" and target.docker.provision_mode == "eager":
                loaded = load_adapter(target.adapter_id)
                self.assertTrue(callable(getattr(loaded.module, "provision_environment", None)))
                self.assertTrue(callable(getattr(loaded.module, "doctor_environment", None)))
            if target.docker.owner == "adapter" and target.local_asset_inventory:
                loaded = load_adapter(target.adapter_id)
                self.assertTrue(
                    callable(getattr(loaded.module, "local_asset_inventory", None))
                )

    def test_skip_bootstrap_does_not_require_uv(self) -> None:
        target = self.catalog.targets["local-vliw"]

        def available(name: str) -> str | None:
            return None if name == "uv" else f"/bin/{name}"

        with mock.patch("bench_goal_plus.runtime.shutil.which", side_effect=available):
            self.assertEqual(
                RuntimeManager().validate_host(
                    (target,), dry_run=False, require_uv=False
                ),
                [],
            )
            with self.assertRaisesRegex(ContractError, "uv"):
                RuntimeManager().validate_host(
                    (target,), dry_run=False, require_uv=True
                )

    def test_edgebench_example_resolves_to_explicit_reproducible_values(self) -> None:
        spec = self.agent.resolve_spec(preset_id="edgebench-codex-2h")
        self.assertEqual(spec.model, "gpt-5.6-sol")
        self.assertEqual(spec.methods, ("plain-codex",))
        self.assertEqual(spec.concurrency(), {"T": 7200, "K": 1, "C": 2, "R": 1})
        result = self.agent.start(
            spec,
            skip_bootstrap=False,
            skip_provision=False,
            prepare_only=False,
            foreground=False,
            dry_run=True,
        )
        rendered = "\n".join(result["commands"])
        self.assertIn("experiments/edgebench/experiment.py provision", rendered)
        self.assertIn("--cell-concurrency 2", rendered)
        self.assertIn("--detach", rendered)

    def test_edgebench_local_smoke_resolves_to_frozen_plain_codex_values(self) -> None:
        spec = self.agent.resolve_spec(
            preset_id="edgebench-vliw-codex-local-smoke"
        )

        self.assertEqual(spec.model, "gpt-5.6-sol")
        self.assertEqual(spec.reasoning_effort, "medium")
        self.assertEqual(spec.methods, ("plain-codex",))
        self.assertEqual(spec.concurrency(), {"T": 300, "K": 1, "C": 1, "R": 1})
        result = self.agent.start(
            spec,
            skip_bootstrap=False,
            skip_provision=False,
            prepare_only=False,
            foreground=False,
            dry_run=True,
        )
        rendered = "\n".join(result["commands"])
        self.assertIn(
            "experiments/edgebench/experiment.py doctor "
            "--profile vliw-codex-sol-medium-local-smoke",
            rendered,
        )
        self.assertIn("--method plain-codex", rendered)
        self.assertIn("--detach", rendered)

    def test_common_matrix_defaults_controller_concurrency_to_one(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("local-vliw",),
            campaign_id="generic-smoke",
            conditions=("B0",),
            model="test-model",
            reasoning_effort="medium",
            wall_time_seconds=60,
            live_search_concurrency=1,
        )
        self.assertEqual(spec.concurrency(), {"T": 60, "K": 1, "C": 1, "R": 1})
        runner = create_runner(spec.runner)
        commands, campaign = runner.prepare_commands(spec)
        self.assertIn("--benchmarks", commands[0])
        self.assertEqual(campaign.path, ROOT / "runs/benchmark-campaigns/generic-smoke")

    def test_common_matrix_can_select_a_method_without_an_ablation_condition(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("local-vliw",),
            campaign_id="goal-plus-smoke",
            methods=("goal-plus-codex",),
            model="test-model",
            reasoning_effort="medium",
            wall_time_seconds=180,
            live_search_concurrency=2,
            worker_runtime_seconds=150,
            worker_min_runtime_seconds=60,
        )
        command, _ = create_runner(spec.runner).prepare_commands(spec)
        self.assertIn("--methods", command[0])
        self.assertIn("goal-plus-codex", command[0])
        self.assertNotIn("--conditions", command[0])
        self.assertIn("--worker-runtime-seconds", command[0])
        self.assertIn("150", command[0])
        self.assertIn("--worker-min-runtime-seconds", command[0])
        self.assertIn("60", command[0])

    def test_common_matrix_exposes_adapter_task_selection(self) -> None:
        args = build_parser().parse_args(
            [
                "plan",
                "--benchmark",
                "zsoft-detect",
                "--task-id",
                "libxml2-detect",
                "--method",
                "goal-plus-pi",
                "--model",
                "test-model",
                "--reasoning-effort",
                "medium",
                "--wall-time-seconds",
                "180",
                "--live-search-concurrency",
                "1",
                "--shared-dir",
            ]
        )
        spec = self.agent.resolve_spec(
            target_ids=args.benchmark,
            task_id=args.task_id,
            shared_dir=args.shared_dir,
            methods=args.method,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            wall_time_seconds=args.wall_time_seconds,
            live_search_concurrency=args.live_search_concurrency,
        )

        command, _ = create_runner(spec.runner).prepare_commands(spec)
        self.assertEqual(spec.task_id, "libxml2-detect")
        self.assertIn("--task-id", command[0])
        self.assertIn("libxml2-detect", command[0])
        self.assertIn("--shared-dir", command[0])
        self.assertEqual(load_adapter("zsoft-detect").module.TASK_ID, "civetweb-detect")

    def test_common_matrix_rejects_shared_dir_for_plain_method(self) -> None:
        with self.assertRaisesRegex(
            ContractError,
            "--shared-dir requires an explicit common-matrix Goal Plus method",
        ):
            self.agent.resolve_spec(
                target_ids=("torchbench",),
                task_id="alexnet",
                shared_dir=True,
                methods=("plain-pi",),
                model="test-model",
                reasoning_effort="medium",
                wall_time_seconds=180,
                live_search_concurrency=1,
            )

    def test_unproven_common_cell_concurrency_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "use C=1"):
            self.agent.resolve_spec(
                target_ids=("local-vliw",),
                model="test-model",
                reasoning_effort="medium",
                wall_time_seconds=60,
                live_search_concurrency=1,
                cell_concurrency=2,
            )

    def test_runner_without_repeat_seed_capability_rejects_r_greater_than_one(self) -> None:
        with self.assertRaisesRegex(ContractError, "does not support R>1"):
            self.agent.resolve_spec(
                target_ids=("edgebench",),
                profile="vliw-smoke",
                methods=("plain-codex",),
                seeds=(1, 2),
            )

    def test_plain_methods_accept_k_as_outer_trajectory_count(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("aibench-coding",),
            profile="smoke",
            methods=("plain-codex",),
            model="bench-openai/gpt-5.6-sol",
            live_search_concurrency=2,
        )
        self.assertEqual(spec.live_search_concurrency, 2)

    def test_methods_without_parallel_topology_reject_k_greater_than_one(self) -> None:
        with self.assertRaisesRegex(ContractError, "K>1"):
            self.agent.resolve_spec(
                target_ids=("zsoft-detect-swe-agent",),
                profile="civetweb-swe-agent-smoke",
                methods=("zsoft-swe-agent",),
                live_search_concurrency=2,
            )

    def test_common_runner_rejects_method_outside_its_contract(self) -> None:
        with self.assertRaisesRegex(
            ContractError, "common-matrix does not support method.*plain-pi"
        ):
            self.agent.resolve_spec(
                target_ids=("local-vliw",),
                methods=("plain-pi",),
                model="test-model",
                reasoning_effort="medium",
                wall_time_seconds=60,
                live_search_concurrency=1,
            )

    def test_preset_rejects_overrides_that_would_mislabel_campaign(self) -> None:
        with self.assertRaisesRegex(ContractError, "preset.*is frozen"):
            self.agent.resolve_spec(
                preset_id="edgebench-codex-2h", model="different-model"
            )

    def test_openevolve_batch_preserves_native_controller_and_resume(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("openevolve-cpu-portable",),
            campaign_id="oe-smoke",
            model="test-model",
            reasoning_effort="medium",
            wall_time_seconds=60,
            live_search_concurrency=1,
        )
        runner = create_runner(spec.runner)
        commands, campaign = runner.prepare_commands(spec)
        rendered = " ".join(commands[0])
        self.assertIn("prepare-batch", rendered)
        self.assertIn("--task-set cpu_portable", rendered)
        self.assertTrue(spec.runner.capabilities.resume)
        self.assertEqual(campaign.path, ROOT / "runs/openevolve-campaigns/oe-smoke")

    def test_campaign_directory_cannot_escape_runs(self) -> None:
        spec = self.agent.resolve_spec(
            target_ids=("local-vliw",),
            campaign_dir=ROOT.parent / "outside-runs",
            model="test-model",
            reasoning_effort="medium",
            wall_time_seconds=60,
            live_search_concurrency=1,
        )
        with self.assertRaisesRegex(ContractError, "must stay under"):
            create_runner(spec.runner).prepare_commands(spec)

    def test_agent_state_tracks_runner_status_and_resume_command(self) -> None:
        (ROOT / "runs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="agent-state-", dir=ROOT / "runs") as temporary:
            campaign_path = Path(temporary)
            spec = self.agent.resolve_spec(
                target_ids=("local-vliw",),
                campaign_id="state-smoke",
                model="test-model",
                reasoning_effort="medium",
                wall_time_seconds=60,
                live_search_concurrency=1,
            )
            manifest = {
                "schema_version": 1,
                "campaign_id": "state-smoke",
                "state": "prepared",
                "model": "test-model",
                "cells": [{"state": "prepared"}],
            }
            (campaign_path / "campaign.json").write_text(json.dumps(manifest) + "\n")
            ref = CampaignRef(
                "state-smoke", campaign_path, "local-vliw", "common-matrix"
            )
            create_agent_state(spec, ref, commands=["prepare"], follow_up={})

            status = self.agent.status(campaign_path)
            self.assertEqual(status["agent_phase"], "prepared")
            self.assertEqual(status["runner"]["state"], "pending")
            resumed = self.agent.resume(campaign_path, benchmark=None, dry_run=True)
            self.assertIn("experiment.py run", resumed["command"])
            self.assertIn("--model test-model", resumed["command"])

            manifest["state"] = "finished"
            manifest["cells"][0]["state"] = "finished"
            (campaign_path / "campaign.json").write_text(json.dumps(manifest) + "\n")
            finished = self.agent.finish(
                campaign_path,
                benchmark=None,
                markdown_out=None,
                xlsx_out=None,
                dry_run=True,
            )
            self.assertEqual(
                finished["artifacts"]["xlsx"],
                str(campaign_path / f"{campaign_path.name}.xlsx"),
            )


if __name__ == "__main__":
    unittest.main()
