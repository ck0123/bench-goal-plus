from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench_goal_plus.application import BenchmarkAgent
from bench_goal_plus.catalog import Catalog
from bench_goal_plus.errors import ContractError
from bench_goal_plus.runners.factory import create_runner
from experiments.frontier_engineering import config
from experiments.frontier_engineering import cli
from experiments.frontier_engineering import environment
from experiments.frontier_engineering import openevolve_runtime
from experiments.frontier_engineering import reporting
from experiments.frontier_engineering import runtime
from experiments.frontier_engineering import task_adapter


ROOT = Path(__file__).resolve().parents[1]
PLAIN_CODEX_EVIDENCE = (
    ROOT
    / "evidence/runs/2026-08-07-frontier-engineering-energy-storage-plain-codex"
)
GOAL_PLUS_PI_EVIDENCE = (
    ROOT
    / "evidence/runs/2026-08-07-frontier-engineering-energy-storage-goal-plus-pi"
)
CPU_DEFAULT_EVIDENCE = (
    ROOT
    / "evidence/environment/2026-08-07-frontier-engineering-cpu-default-doctor.json"
)
EXPECTED_V1_LITE_TASKS = (
    "ComputerSystems/MallocLab",
    "QuantumComputing/task_01_routing_qftentangled",
    "JobShop/abz",
    "InventoryOptimization/disruption_eoqd",
    "EnergyStorage/BatteryFastChargingSPMe",
    "Robotics/RobotArmCycleTimeOptimization",
    "Optics/holographic_multiplane_focusing",
    "WirelessChannelSimulation/HighReliableSimulation",
    "ReactionOptimisation/snar_multiobjective",
    "StructuralOptimization/TopologyOptimization",
)
EXPECTED_V1_LITE_CPU_TASKS = tuple(
    task_id
    for task_id in EXPECTED_V1_LITE_TASKS
    if task_id != "Robotics/RobotArmCycleTimeOptimization"
)


def initialize_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test Controller"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "config",
            "user.email",
            "test-controller@example.invalid",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "fixture"],
        check=True,
    )


def make_upstream(root: Path, task_id: str, initial: str) -> Path:
    task_dir = root / "benchmarks" / task_id
    metadata = task_dir / "frontier_eval"
    metadata.mkdir(parents=True)
    candidate = task_dir / initial
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("score = 1\n", encoding="utf-8")
    (task_dir / "Task.md").write_text("Optimize the fixture.\n", encoding="utf-8")
    values = {
        "initial_program.txt": initial,
        "candidate_destination.txt": initial,
        "eval_command.txt": "{python} verification.py {candidate}\n",
        "copy_files.txt": ".\n",
        "readonly_files.txt": "Task.md\n",
    }
    for name, value in values.items():
        (metadata / name).write_text(value, encoding="utf-8")
    (metadata / "agent_files.txt").write_text("Task.md\n", encoding="utf-8")
    unified = root / "frontier_eval/tasks/unified"
    (unified / "evaluator").mkdir(parents=True)
    (unified / "evaluator/python.py").write_text("# evaluator\n", encoding="utf-8")
    (unified / "spec.py").write_text("# spec\n", encoding="utf-8")
    initialize_git(root)
    return candidate


class FrontierEngineeringTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        upstream_patch = mock.patch.object(openevolve_runtime, "UPSTREAM_ROOT", Path(directory.name))
        upstream_patch.start()
        self.addCleanup(upstream_patch.stop)
        for name in ("PAPER_BATCH_CONFIG", "ALGORITHM_CONFIG", "LLM_CONFIG"):
            path = Path(directory.name) / f"{name}.yaml"
            path.write_text("fixture: true\n")
            patch = mock.patch.object(openevolve_runtime, name, path)
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        task_adapter.configure_task("ComputerSystems/MallocLab")

    def test_v1_lite_profile_preserves_upstream_task_and_runtime_matrix(self) -> None:
        _, profile = config.load_profile("v1-lite-codex-1h")
        _, cpu_profile = config.load_profile("v1-lite-cpu-codex-1h")

        self.assertEqual(tuple(profile["task_ids"]), EXPECTED_V1_LITE_TASKS)
        self.assertEqual(profile["accelerator_policy"], "nvidia-cuda-opt-in")
        self.assertEqual(tuple(cpu_profile["task_ids"]), EXPECTED_V1_LITE_CPU_TASKS)
        self.assertEqual(cpu_profile["accelerator_policy"], "cpu-only")
        self.assertEqual(tuple(config.V1_LITE_TASKS), EXPECTED_V1_LITE_TASKS)
        self.assertEqual(config.V1_LITE_CPU_TASKS, EXPECTED_V1_LITE_CPU_TASKS)
        self.assertEqual(
            config.V1_LITE_TASKS["QuantumComputing/task_01_routing_qftentangled"].runtime_env,
            "frontier-quantum",
        )
        self.assertEqual(
            config.V1_LITE_TASKS["JobShop/abz"].runtime_env,
            "frontier-eval-driver",
        )
        self.assertEqual(
            config.V1_LITE_TASKS["JobShop/abz"].runtime_python_env,
            "frontier-v1-main",
        )
        reaction = config.V1_LITE_TASKS[
            "ReactionOptimisation/snar_multiobjective"
        ]
        self.assertEqual(reaction.runtime_env, "frontier-eval-driver")
        self.assertEqual(reaction.runtime_python_env, "frontier-v1-summit")
        self.assertEqual(
            config.V1_LITE_TASKS[
                "EnergyStorage/BatteryFastChargingSPMe"
            ].evaluator_timeout_seconds,
            30,
        )
        self.assertEqual(
            config.V1_LITE_TASKS[
                "Robotics/RobotArmCycleTimeOptimization"
            ].evaluator_timeout_seconds,
            600,
        )
        self.assertEqual(
            config.V1_LITE_TASKS[
                "Robotics/RobotArmCycleTimeOptimization"
            ].accelerator,
            "nvidia-cuda",
        )

    def test_cpu_policy_rejects_known_cuda_tasks(self) -> None:
        _, profile = config.load_profile("v1-lite-cpu-codex-1h")
        for task_id in (
            "Robotics/RobotArmCycleTimeOptimization",
            "Aerodynamics/CarAerodynamicsSensing",
            "KernelEngineering/MLA",
        ):
            invalid = json.loads(json.dumps(profile))
            invalid["task_ids"].append(task_id)
            with self.subTest(task_id=task_id), self.assertRaisesRegex(
                config.FrontierEngineeringContractError,
                "NVIDIA CUDA tasks are excluded by cpu-only policy",
            ):
                config.validate_profile(str(invalid["id"]), invalid)

    def test_profile_rejects_unproven_c_and_invalid_worker_minimum(self) -> None:
        _, profile = config.load_profile("jobshop-codex-smoke")
        with self.assertRaisesRegex(config.FrontierEngineeringContractError, "C=1"):
            config.resolve_profile(profile, cell_concurrency=2)
        with self.assertRaisesRegex(
            config.FrontierEngineeringContractError, "require K=1"
        ):
            config.resolve_profile(profile, concurrency=2)
        invalid = json.loads(json.dumps(profile))
        invalid["worker_min_runtime_seconds"] = invalid["worker_runtime_seconds"] + 1
        with self.assertRaisesRegex(
            config.FrontierEngineeringContractError, "worker minimum"
        ):
            config.validate_profile(str(invalid["id"]), invalid)

    def test_paper_openevolve_profile_freezes_100_iterations(self) -> None:
        _, profile = config.load_profile("energy-storage-openevolve-paper-100")

        self.assertEqual(profile["methods"], ["openevolve"])
        self.assertEqual(profile["openevolve_protocol"], "paper")
        self.assertEqual(profile["iterations"], 100)
        self.assertEqual(profile["concurrency"], 1)
        command = openevolve_runtime.build_command(
            profile,
            task_id="EnergyStorage/BatteryFastChargingSPMe",
            output_dir=Path("/campaign/native-output"),
        )
        self.assertIn("algorithm.iterations=100", command)
        self.assertIn("+algorithm.oe.random_seed=42", command)
        self.assertIn("llm.temperature=0.7", command)
        self.assertIn("+algorithm.oe.evaluator.parallel_evaluations=1", command)
        self.assertIn("+algorithm.oe.llm.reasoning_effort=medium", command)
        self.assertFalse(any("OPENAI_API" in item for item in command))

        invalid = json.loads(json.dumps(profile))
        invalid["methods"].append("plain-pi")
        with self.assertRaisesRegex(
            config.FrontierEngineeringContractError, "dedicated profile"
        ):
            config.validate_profile(invalid["id"], invalid)

        invalid = json.loads(json.dumps(profile))
        invalid["iterations"] = 5
        with self.assertRaisesRegex(
            config.FrontierEngineeringContractError, "requires 100 iterations"
        ):
            config.validate_profile(invalid["id"], invalid)

    def test_openevolve_smoke_profile_freezes_5_iterations(self) -> None:
        _, profile = config.load_profile("energy-storage-openevolve-smoke-5")

        self.assertEqual(profile["openevolve_protocol"], "smoke")
        self.assertEqual(profile["iterations"], 5)
        command = openevolve_runtime.build_command(
            profile,
            task_id="EnergyStorage/BatteryFastChargingSPMe",
            output_dir=Path("/campaign/native-output"),
        )
        self.assertIn("algorithm.iterations=5", command)
        self.assertIn("+algorithm.oe.evaluator.parallel_evaluations=1", command)
        contract = openevolve_runtime.protocol_contract(profile)
        self.assertEqual(contract["kind"], "smoke")
        self.assertIn("not a paper result", contract["publication_scope"])

    def test_task_bridge_materializes_selected_artifact_and_official_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream = root / "upstream"
            workspace = root / "workspace"
            make_upstream(
                upstream,
                "Robotics/RobotArmCycleTimeOptimization",
                "baseline/solution.py",
            )
            task_adapter.configure_task("Robotics/RobotArmCycleTimeOptimization")

            result = task_adapter.materialize_workspace(upstream, workspace)
            metadata = json.loads((workspace / "task.json").read_text())

            self.assertEqual(result["task_id"], "Robotics/RobotArmCycleTimeOptimization")
            self.assertEqual(metadata["artifact_source_relative"], "baseline/solution.py")
            self.assertEqual(metadata["artifact_name"], "solution.py")
            self.assertEqual(metadata["evaluator_timeout_seconds"], 600)
            self.assertTrue((workspace / "solution.py").is_file())
            self.assertIn("Optimize the fixture", (workspace / "TASK.md").read_text())
            self.assertFalse((workspace / ".gp").exists())

    def test_task_bridge_preserves_raw_metric_and_runtime_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream = root / "upstream"
            workspace = root / "workspace"
            make_upstream(
                upstream,
                "Robotics/RobotArmCycleTimeOptimization",
                "baseline/solution.py",
            )
            task_adapter.configure_task("Robotics/RobotArmCycleTimeOptimization")
            task_adapter.materialize_workspace(upstream, workspace)
            (workspace / "solution.py").write_text("score = 2\n", encoding="utf-8")
            observed: dict[str, object] = {}

            def run_bridge(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                observed["command"] = command
                observed["environment"] = kwargs["env"]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "metrics": {
                                "valid": 1.0,
                                "combined_score": 12.5,
                                "feasible": 1.0,
                            },
                            "artifacts": {"native": "kept"},
                            "diagnostics": "official output",
                        }
                    ),
                    "",
                )

            metadata = json.loads((workspace / "task.json").read_text())
            with (
                mock.patch.object(
                    task_adapter, "candidate_changed_paths", return_value={"solution.py"}
                ),
                mock.patch.object(
                    task_adapter, "git_commit", return_value=metadata["upstream_commit"]
                ),
                mock.patch.object(task_adapter.subprocess, "run", side_effect=run_bridge),
            ):
                report = task_adapter.evaluate_workspace(workspace, upstream, "final")

            self.assertTrue(report["valid"])
            self.assertEqual(report["combined_score"], 12.5)
            self.assertEqual(report["raw_metrics"]["feasible"], 1.0)
            self.assertEqual(report["artifacts"]["native"], "kept")
            environment_values = observed["environment"]
            self.assertIsInstance(environment_values, dict)
            self.assertEqual(
                environment_values["FRONTIER_EVAL_EVALUATOR_TIMEOUT_S"], "600"
            )
            command = observed["command"]
            self.assertIn("frontier-v1-main", command)
            from goal_plus.infrastructure.artifacts.git import candidate_git_pathspecs, git_changed_files
            from goal_plus.workspaces import LocalGitWorkspace
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
            ).strip()
            checkpoint = LocalGitWorkspace("git_worktree").checkpoint(
                workspace, "verify candidate", candidate_git_pathspecs()
            )
            self.assertIsNotNone(checkpoint)
            self.assertEqual(git_changed_files(workspace, base, checkpoint.id), ["solution.py"])
            self.assertEqual(
                runtime.standalone.evaluator_budget(workspace)["total_claimed"], 1
            )

    def test_task_bridge_rejects_changed_native_verifier_after_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, workspace = root / "upstream", root / "workspace"
            task_id = "JobShop/abz"
            make_upstream(upstream, task_id, "baseline/init.py")
            verifier = upstream / "benchmarks" / task_id / "verification/evaluate.py"
            verifier.parent.mkdir()
            verifier.write_text("score = 1\n")
            task_adapter.configure_task(task_id)
            task_adapter.materialize_workspace(upstream, workspace)
            verifier.write_text("score = 1000000\n")
            with self.assertRaisesRegex(RuntimeError, "evaluator inputs changed"):
                task_adapter.evaluate_workspace(workspace, upstream, "final")

    def test_task_diagnostic_python_works_in_a_derived_worktree(self) -> None:
        for task_id, initial in (
            ("QuantumComputing/task_01_routing_qftentangled", "baseline/solve.py"),
            ("JobShop/abz", "baseline/init.py"),
        ):
            with self.subTest(task_id=task_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                upstream, workspace = root / "upstream", root / "workspace"
                make_upstream(upstream, task_id, initial)
                task_dir = upstream / "benchmarks" / task_id
                for path in (
                    upstream / "benchmarks/_shared/candidate_sandbox.py",
                    upstream / "benchmarks/_shared/qiskit_candidate_runner.py",
                    task_dir / "verification/utils.py",
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# official boundary fixture\n")
                (task_dir / "baseline/structural_optimizer.py").write_text("VALUE = 42\n")
                task = config.V1_LITE_TASKS[task_id]
                python = upstream / ".venvs" / (task.runtime_python_env or task.runtime_env) / "bin/python"
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                task_adapter.configure_task(task_id)
                task_adapter.materialize_workspace(upstream, workspace)
                probe = ("from structural_optimizer import VALUE\nprint(VALUE)\n"
                         if task_id.startswith("QuantumComputing/") else "print(42)\n")
                derived = root / "worker"
                subprocess.run(["git", "-C", str(workspace), "worktree", "add", "--detach", str(derived)],
                               check=True, capture_output=True)
                completed = subprocess.run(
                    [sys.executable, "task_python.py", "-"], cwd=derived,
                    input=probe,
                    text=True, capture_output=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout.strip(), "42")
                (derived / ".tmp").mkdir()
                (derived / ".tmp/probe.py").write_text(probe)
                completed = subprocess.run(
                    [sys.executable, "task_python.py", ".tmp/probe.py"], cwd=derived,
                    text=True, capture_output=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout.strip(), "42")
                self.assertFalse((workspace / ".gp").exists())

    @unittest.skipUnless(os.name == "posix", "requires process groups")
    def test_stop_reaches_detached_main_and_allows_controller_to_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            code = (
                "import signal,subprocess,sys\n"
                "signal.signal(signal.SIGTERM, lambda *_: None)\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)\n"
                "print(child.pid,flush=True)\n"
                "child.wait()\n"
            )
            controller = subprocess.Popen(
                [sys.executable, "-c", code], start_new_session=True,
                stdout=subprocess.PIPE, text=True,
            )
            child_pid = int(controller.stdout.readline())
            (destination / "controller.json").write_text(json.dumps({"pid": controller.pid}))
            try:
                runtime.stop_campaign(destination)
                controller.wait(timeout=3)
            finally:
                for pid in (child_pid, controller.pid):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                controller.wait()
                controller.stdout.close()

    def test_inventory_is_read_only_and_does_not_duplicate_missing_seed(self) -> None:
        _, profile = config.load_profile("jobshop-codex-smoke")
        with tempfile.TemporaryDirectory() as temporary:
            upstream = Path(temporary) / "missing-upstream"
            with mock.patch.object(environment, "UPSTREAM_ROOT", upstream):
                inventory = environment.local_inventory(profile)

        self.assertTrue(inventory["read_only"])
        self.assertFalse(inventory["acquisition_attempted"])
        accelerator = next(
            item for item in inventory["checks"] if item["kind"] == "accelerator-policy"
        )
        self.assertEqual(accelerator["policy"], "cpu-only")
        self.assertEqual(accelerator["selected_cuda_tasks"], [])
        self.assertEqual(accelerator["gpu_probe_stage"], "not-required")
        task = next(item for item in inventory["checks"] if item["kind"] == "task")
        self.assertEqual(len(task["missing"]), len(set(task["missing"])))
        self.assertFalse(inventory["passed"])

    def test_seed_probe_uses_the_registered_evaluator_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            upstream = Path(temporary) / "upstream"
            make_upstream(
                upstream,
                "EnergyStorage/BatteryFastChargingSPMe",
                "init.py",
            )
            observed: dict[str, object] = {}

            def run_bridge(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                observed["environment"] = kwargs["env"]
                observed["timeout"] = kwargs["timeout"]
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "metrics": {
                                "valid": 1.0,
                                "combined_score": 12.5,
                                "timeout_budget_s": 30.0,
                            }
                        }
                    ),
                    "",
                )

            with (
                mock.patch.object(environment, "UPSTREAM_ROOT", upstream),
                mock.patch.object(
                    environment.subprocess, "run", side_effect=run_bridge
                ),
            ):
                result = environment._seed_probe(
                    "EnergyStorage/BatteryFastChargingSPMe"
                )

        probe_environment = observed["environment"]
        self.assertIsInstance(probe_environment, dict)
        self.assertEqual(
            probe_environment["FRONTIER_EVAL_EVALUATOR_TIMEOUT_S"], "30"
        )
        self.assertEqual(observed["timeout"], 30)
        self.assertTrue(result["passed"])
        self.assertEqual(result["metrics"]["timeout_budget_s"], 30.0)

    def test_accelerator_doctor_probes_only_explicit_gpu_profile(self) -> None:
        _, cpu_profile = config.load_profile("v1-lite-cpu-codex-1h")
        _, gpu_profile = config.load_profile("v1-lite-codex-1h")

        self.assertEqual(environment._accelerator_checks(cpu_profile), [])
        with (
            mock.patch.object(
                environment,
                "_nvidia_driver_probe",
                return_value={"kind": "host-accelerator", "passed": True},
            ) as driver_probe,
            mock.patch.object(
                environment,
                "_cuda_runtime_probe",
                return_value={"kind": "runtime-accelerator", "passed": True},
            ) as runtime_probe,
        ):
            checks = environment._accelerator_checks(gpu_profile)

        driver_probe.assert_called_once_with()
        runtime_probe.assert_called_once_with("frontier-v1-main")
        self.assertEqual(
            [item["kind"] for item in checks],
            ["host-accelerator", "runtime-accelerator"],
        )

    def test_cpu_provision_avoids_unselected_full_runtime_dependencies(self) -> None:
        _, cpu = config.load_profile("jobshop-codex-smoke")
        _, cuda = config.load_profile("v1-lite-codex-1h")
        for profile, expected_spec in (
            (cpu, "runtime-specs/frontier-v1-main-cpu.json"),
            (cuda, "scripts/env/specs/frontier-v1-main.json"),
        ):
            with self.subTest(profile=profile["id"]), mock.patch.object(
                environment.subprocess, "run"
            ) as install:
                result = environment.provision(profile)
                main = [cmd for cmd in result["commands"]
                        if cmd[2].endswith("/frontier-v1-main.json")
                        or cmd[2].endswith("/frontier-v1-main-cpu.json")]
                self.assertEqual(len(main), 1)
                self.assertTrue(main[0][2].endswith(expected_spec), main[0])
                self.assertEqual(install.call_count, len(result["commands"]))

    def test_catalog_and_preset_select_native_runner(self) -> None:
        catalog = Catalog()
        agent = BenchmarkAgent(catalog=catalog)
        target = catalog.targets["frontier-engineering"]
        runner = catalog.runners[target.runner_id]

        self.assertEqual(target.runner_id, "frontier-engineering-native")
        self.assertIsNone(target.adapter_id)
        self.assertTrue(target.local_asset_inventory)
        self.assertEqual(target.default_inventory_profile, "v1-lite-cpu-codex-1h")
        self.assertEqual(
            runner.supported_methods,
            (
                "openevolve",
                "plain-codex",
                "plain-pi",
                "goal-plus-codex",
                "goal-plus-pi",
            ),
        )
        paper_spec = agent.resolve_spec(
            preset_id="frontier-engineering-energy-storage-openevolve-paper-100"
        )
        self.assertEqual(paper_spec.methods, ("openevolve",))
        self.assertEqual(
            paper_spec.concurrency(), {"T": 43200, "K": 1, "C": 1, "R": 1}
        )
        _, paper_profile = config.load_profile(paper_spec.profile)
        self.assertEqual(paper_profile["iterations"], 100)
        smoke_spec = agent.resolve_spec(
            preset_id="frontier-engineering-energy-storage-openevolve-smoke-5"
        )
        self.assertEqual(smoke_spec.methods, ("openevolve",))
        self.assertEqual(
            smoke_spec.concurrency(), {"T": 1800, "K": 1, "C": 1, "R": 1}
        )
        _, smoke_profile = config.load_profile(smoke_spec.profile)
        self.assertEqual(smoke_profile["iterations"], 5)
        smoke_plan = agent.start(
            smoke_spec,
            skip_bootstrap=False,
            skip_provision=False,
            prepare_only=False,
            foreground=False,
            dry_run=True,
        )
        repro_commands = [
            command
            for command in smoke_plan["commands"]
            if "scripts/repro_env.py" in command
        ]
        self.assertTrue(repro_commands)
        self.assertTrue(all("--only frontier_engineering_official" in item for item in repro_commands))
        self.assertTrue(all("--exact-only" in item for item in repro_commands))
        self.assertTrue(all("goal_plus" not in item for item in repro_commands))
        energy_spec = agent.resolve_spec(
            preset_id="frontier-engineering-energy-storage-codex-smoke"
        )
        self.assertEqual(energy_spec.profile, "energy-storage-codex-smoke")
        self.assertEqual(
            energy_spec.concurrency(), {"T": 300, "K": 1, "C": 1, "R": 1}
        )
        _, energy_profile = config.load_profile(energy_spec.profile)
        self.assertEqual(
            energy_profile["task_ids"], ["EnergyStorage/BatteryFastChargingSPMe"]
        )
        for preset_id, method in (
            ("frontier-engineering-energy-storage-pi-smoke", "plain-pi"),
            (
                "frontier-engineering-energy-storage-goal-plus-pi-smoke",
                "goal-plus-pi",
            ),
        ):
            pi_spec = agent.resolve_spec(preset_id=preset_id)
            self.assertEqual(pi_spec.methods, (method,))
            self.assertEqual(
                pi_spec.concurrency(),
                {
                    "T": 600 if method == "goal-plus-pi" else 300,
                    "K": 1,
                    "C": 1,
                    "R": 1,
                },
            )
            _, pi_profile = config.load_profile(pi_spec.profile)
            self.assertEqual(
                pi_profile["task_ids"], ["EnergyStorage/BatteryFastChargingSPMe"]
            )
        spec = agent.resolve_spec(
            preset_id="frontier-engineering-jobshop-codex-smoke"
        )
        self.assertEqual(spec.concurrency(), {"T": 300, "K": 1, "C": 1, "R": 1})
        commands, campaign = create_runner(spec.runner).prepare_commands(spec)
        self.assertIn("experiments/frontier_engineering/experiment.py", commands[0])
        self.assertIn("--method", commands[0])
        self.assertIn("--seeds", commands[0])
        self.assertEqual(
            campaign.path,
            ROOT / "runs/frontier-engineering" / spec.campaign_id,
        )
        with self.assertRaisesRegex(ContractError, "use C=1"):
            agent.resolve_spec(
                target_ids=("frontier-engineering",),
                profile="jobshop-codex-smoke",
                methods=("plain-codex",),
                model="gpt-5.6-sol",
                reasoning_effort="medium",
                wall_time_seconds=300,
                live_search_concurrency=1,
                cell_concurrency=2,
            )

    def test_native_runner_forwards_repeat_seeds_to_frontier_prepare(self) -> None:
        spec = BenchmarkAgent(catalog=Catalog()).resolve_spec(
            target_ids=("frontier-engineering",),
            profile="v1-lite-cpu-codex-1h",
            methods=("plain-pi",),
            seeds=(1, 2, 3, 4),
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            wall_time_seconds=3600,
            live_search_concurrency=1,
            cell_concurrency=1,
        )

        command = create_runner(spec.runner).prepare_commands(spec)[0][0]

        seed_flag = command.index("--seeds")
        self.assertEqual(command[seed_flag + 1 : seed_flag + 5], ["1", "2", "3", "4"])
        _, profile = config.load_profile(spec.profile)
        resolved = config.resolve_profile(profile, seeds=list(spec.seeds))
        self.assertEqual(resolved["seeds"], [1, 2, 3, 4])

    def test_pi_doctor_checks_exact_model_without_persisting_credentials(self) -> None:
        _, profile = config.load_profile("energy-storage-pi-smoke")
        with (
            mock.patch.dict(
                environment.os.environ,
                {
                    "OPENAI_BASE_URL": "https://provider.example/v1",
                    "OPENAI_API_KEY": "secret-value",
                },
            ),
            mock.patch.object(environment.shutil, "which", return_value="/bin/pi"),
            mock.patch.object(
                environment,
                "command_output",
                side_effect=[
                    (True, "0.83.0"),
                    (
                        True,
                        "provider model context\n"
                        "bench-openai gpt-5.6-sol 272K",
                    ),
                ],
            ),
            mock.patch(
                "experiments.openevolve_compare.experiment.write_pi_models_config"
            ) as write_models,
        ):
            checks = environment._pi_agent_checks(profile)

        self.assertTrue(all(item["passed"] for item in checks))
        provider = checks[1]
        self.assertEqual(provider["model"], "bench-openai/gpt-5.6-sol")
        self.assertEqual(provider["api_key_env"], "OPENAI_API_KEY")
        serialized = json.dumps(checks)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("https://provider.example/v1", serialized)
        write_models.assert_called_once()

    def test_flash_provider_is_preserved_through_prepare_and_execution(self) -> None:
        profile_path, profile = config.load_profile("energy-storage-goal-plus-pi-glm53flash-smoke")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(config, "RUNS_ROOT", Path(temporary)),
            mock.patch.object(runtime.standalone, "prepare", return_value=0) as prepare,
            mock.patch.dict(environment.os.environ, {"ZAI_BASE_URL": "https://provider.example/v4"}),
        ):
            destination = runtime.prepare("flash", profile, profile_path)
            args = prepare.call_args.args[0]
            self.assertEqual((args.model, args.pi_provider_id, args.pi_api, args.pi_api_key_env),
                             ("glm-5.3-flash", "zai", "openai-completions", "ZAI_API_KEY"))

            def execute(args):
                self.assertEqual((args.model, args.pi_provider_id, args.pi_api, args.pi_api_key_env),
                                 ("glm-5.3-flash", "zai", "openai-completions", "ZAI_API_KEY"))
                self.assertEqual(args.api_base, "https://provider.example/v4")
                config.write_json(args.run_dir / "experiment.json", {"status": "finished"})
                config.write_json(args.run_dir / "final-eval.json", {"valid": True})
                return 0

            with mock.patch.object(runtime.standalone, "execute", side_effect=execute):
                self.assertEqual(runtime.execute_campaign(destination), 0)
            campaign = json.loads((destination / "campaign.json").read_text())
            self.assertEqual(campaign["pi_provider"], profile["pi_provider"])
            self.assertNotIn("https://provider.example", json.dumps(campaign))

    def test_flash_doctor_uses_exact_provider_and_rejects_unknown_model(self) -> None:
        _, profile = config.load_profile("energy-storage-goal-plus-pi-glm53flash-smoke")
        with (
            mock.patch.dict(environment.os.environ, {"ZAI_BASE_URL": "https://provider.example/v4", "ZAI_API_KEY": "secret"}),
            mock.patch.object(environment.shutil, "which", return_value="/bin/pi"),
            mock.patch.object(environment, "command_output", side_effect=[(True, "0.83.0"), (True, "zai glm-5.3-flash 200K")]),
            mock.patch("experiments.openevolve_compare.experiment.write_pi_models_config") as write_models,
        ):
            self.assertTrue(all(check["passed"] for check in environment._pi_agent_checks(profile)))
            self.assertEqual(write_models.call_args.kwargs["provider_id"], "zai")
            self.assertEqual(write_models.call_args.kwargs["api_key_env"], "ZAI_API_KEY")
            write_models.side_effect = ValueError("missing exact model")
            with mock.patch.object(environment, "command_output", return_value=(True, "0.83.0")):
                self.assertFalse(all(check["passed"] for check in environment._pi_agent_checks(profile)))

    def test_doctor_accepts_runner_reasoning_override(self) -> None:
        with mock.patch.object(cli, "doctor", return_value=0) as doctor:
            self.assertEqual(cli.main(["doctor", "--profile", "energy-storage-pi-smoke",
                                       "--reasoning-effort", "low"]), 0)
        self.assertEqual(doctor.call_args.args[0]["reasoning_effort"], "low")

    def test_native_plan_records_exact_source_and_profile_provider(self) -> None:
        identity = {"source_kind": "external", "source_dir": "/external/goal-plus",
                    "expected_ref": "feature", "branch": "feature", "commit": "a" * 40}
        with mock.patch.object(cli, "runtime_source", return_value=identity), mock.patch("builtins.print") as output:
            self.assertEqual(cli.main([
                "runtime-source", "--profile", "energy-storage-goal-plus-pi-glm53flash-smoke",
                "--method", "goal-plus-pi",
            ]), 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["runtime_sources"]["goal_plus"], identity)
        self.assertEqual(payload["runtime_configuration"]["pi_provider"]["id"], "zai")
        spec = BenchmarkAgent().resolve_spec(
            target_ids=["frontier-engineering"],
            profile="energy-storage-goal-plus-pi-glm53flash-smoke",
            methods=["goal-plus-pi"],
        )
        with mock.patch("bench_goal_plus.runners.native_profile.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
            self.assertEqual(create_runner(spec.runner).runtime_metadata(spec)["runtime_sources"],
                             payload["runtime_sources"])

    def test_evaluator_shell_preserves_cwd_and_disables_login_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [str(task_adapter.SHELL_PATH), "-lc",
                 '[[ $0 == bash ]] && shopt -q login_shell && pwd'],
                cwd=temporary, text=True, capture_output=True, check=True,
            )
            self.assertEqual(Path(completed.stdout.strip()), Path(temporary))
            self.assertEqual(completed.stderr, "")

    def test_goal_plus_doctor_accepts_validated_external_source(self) -> None:
        _, profile = config.load_profile("energy-storage-goal-plus-pi-smoke")
        identity = {"source_kind": "external", "source_dir": "/external/goal-plus",
                    "expected_ref": "feature", "branch": "feature", "commit": "abc"}
        with (
            mock.patch.object(environment, "external_goal_plus_source", return_value=identity),
            mock.patch.object(environment, "local_inventory", return_value={"passed": True}),
            mock.patch.object(environment.shutil, "which", return_value="/bin/tool"),
            mock.patch.object(environment, "_pi_agent_checks", return_value=[{"passed": True}]),
            mock.patch.object(environment, "_runtime_probe", return_value={"passed": True}),
            mock.patch.object(environment, "_seed_probe", return_value={"passed": True}),
            mock.patch.object(environment, "git_value", side_effect=[
                environment.registered_upstream_branch(config.UPSTREAM_KEY, repository_root=ROOT),
                None, "upstream-commit",
            ]),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(environment.doctor(profile), 0)
        payload = json.loads(output.call_args.args[0])
        source = next(item for item in payload["checks"] if item.get("kind") == "runtime-source")
        self.assertEqual(source["expected_ref"], "feature")

    def test_plain_codex_stage_is_backed_by_archived_native_evidence(self) -> None:
        registry = json.loads((ROOT / "benchmarks/registry.json").read_text())
        benchmark = next(
            item
            for item in registry["items"]
            if item["id"] == "frontier-engineering-lite"
        )
        summary_path = PLAIN_CODEX_EVIDENCE / "summary.json"
        official_path = PLAIN_CODEX_EVIDENCE / "official-report.json"
        candidate_path = PLAIN_CODEX_EVIDENCE / "candidate.py"
        summary = json.loads(summary_path.read_text())
        official = json.loads(official_path.read_text())

        self.assertEqual(benchmark["stages"]["plain_codex"], "pass")
        self.assertIn(
            str(summary_path.relative_to(ROOT)),
            benchmark["stage_evidence"]["plain_codex"],
        )
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["budget"], {
            "T": 300,
            "K": 1,
            "C": 1,
            "R": 1,
            "soft_closeout_seconds": 60,
            "hard_kill_grace_seconds": 30,
        })
        self.assertTrue(official["valid"])
        self.assertEqual(official["primary_metric"]["direction"], "maximize")
        self.assertEqual(official["primary_metric"]["value"], 121.2063096578825)
        self.assertEqual(summary["result"]["final_score"], 121.2063096578825)
        self.assertGreater(
            summary["result"]["final_score"], summary["result"]["seed_score"]
        )
        self.assertTrue(candidate_path.is_file())

    def test_goal_plus_pi_stage_is_backed_by_archived_native_evidence(self) -> None:
        registry = json.loads((ROOT / "benchmarks/registry.json").read_text())
        benchmark = next(
            item
            for item in registry["items"]
            if item["id"] == "frontier-engineering-lite"
        )
        summary_path = GOAL_PLUS_PI_EVIDENCE / "summary.json"
        official_path = GOAL_PLUS_PI_EVIDENCE / "official-report.json"
        candidate_path = GOAL_PLUS_PI_EVIDENCE / "candidate.py"
        summary = json.loads(summary_path.read_text())
        official = json.loads(official_path.read_text())

        self.assertEqual(benchmark["stages"]["goal_plus_pi"], "pass")
        for path in (summary_path, official_path, candidate_path):
            self.assertIn(
                str(path.relative_to(ROOT)),
                benchmark["stage_evidence"]["goal_plus_pi"],
            )
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(
            {key: summary["budget"][key] for key in ("T", "K", "C", "R")},
            {"T": 600, "K": 1, "C": 1, "R": 1},
        )
        self.assertEqual(summary["goal_plus"]["actual_subagent_count"], 1)
        self.assertTrue(summary["goal_plus"]["subagent_count_matches_K"])
        self.assertEqual(summary["execution"]["selected_iteration"], 5)
        self.assertEqual(
            summary["execution"]["selected_git_revision"],
            "a8fef7a9f76aedc99e25029874a1a81791865121",
        )
        self.assertTrue(official["valid"])
        self.assertEqual(official["primary_metric"]["direction"], "maximize")
        self.assertEqual(
            official["primary_metric"]["value"], summary["result"]["final_score"]
        )
        self.assertGreater(
            summary["result"]["final_score"], summary["result"]["seed_score"]
        )
        self.assertTrue(candidate_path.is_file())

    def test_default_cpu_doctor_evidence_excludes_gpu_tasks(self) -> None:
        payload = json.loads(CPU_DEFAULT_EVIDENCE.read_text())

        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["profile"], "v1-lite-cpu-codex-1h")
        self.assertEqual(payload["accelerator_policy"]["mode"], "cpu-only")
        self.assertEqual(payload["accelerator_policy"]["selected_cuda_tasks"], [])
        self.assertFalse(payload["accelerator_policy"]["gpu_checks_executed"])
        task_ids = tuple(item["task_id"] for item in payload["seed_evaluations"])
        self.assertEqual(task_ids, EXPECTED_V1_LITE_CPU_TASKS)
        self.assertTrue(
            all(
                item["valid"] == 1.0 and item["timeout"] == 0.0
                for item in payload["seed_evaluations"]
            )
        )

    def test_prepare_passes_dynamic_task_to_standalone_runner(self) -> None:
        _, profile = config.load_profile("jobshop-codex-smoke")
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary) / "runs"
            with (
                mock.patch.object(config, "RUNS_ROOT", runs_root),
                mock.patch.object(runtime.standalone, "prepare", return_value=0) as prepare,
            ):
                destination = runtime.prepare("native-test", profile, Path("profile.json"))

            prepared = prepare.call_args.args[0]
            self.assertEqual(prepared.task_id, "JobShop/abz")
            self.assertEqual(prepared.adapter_module, runtime.ADAPTER_MODULE)
            campaign = json.loads((destination / "campaign.json").read_text())
            self.assertEqual(campaign["state"], "prepared")
            self.assertEqual(campaign["budget"]["live_search_concurrency"], 1)

    def test_fork_profile_freezes_context_mode_and_rejects_incompatible_method(self) -> None:
        profile_path, profile = config.load_profile("quantum-routing-goal-plus-pi-sol-1h-k4-fork")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(config, "RUNS_ROOT", Path(temporary)),
                mock.patch.object(runtime.standalone, "prepare", return_value=0) as prepare,
            ):
                destination = runtime.prepare("quantum-fork", profile, profile_path)
            self.assertEqual(prepare.call_args.args[0].goal_plus_spawn_mode, "fork")
            campaign = json.loads((destination / "campaign.json").read_text())
            self.assertEqual(campaign["goal_plus_spawn_mode"], "fork")
        for changes in ({"methods": ["plain-pi"], "concurrency": 1}, {"concurrency": 1}):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                config.FrontierEngineeringContractError, "fork requires"
            ):
                config.validate_profile(profile["id"], {**profile, **changes})

    def test_prepare_routes_openevolve_without_agent_runner(self) -> None:
        _, profile = config.load_profile("energy-storage-openevolve-paper-100")
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary) / "runs"
            with (
                mock.patch.object(config, "RUNS_ROOT", runs_root),
                mock.patch.object(
                    runtime.openevolve_runtime, "prepare_cell"
                ) as prepare_openevolve,
                mock.patch.object(runtime.standalone, "prepare") as prepare_agent,
            ):
                destination = runtime.prepare(
                    "openevolve-test", profile, Path("profile.json")
                )
                campaign = json.loads((destination / "campaign.json").read_text())

        prepare_openevolve.assert_called_once()
        prepare_agent.assert_not_called()
        self.assertEqual(campaign["budget"]["iterations"], 100)

    def test_openevolve_doctor_redacts_provider_endpoint_and_key(self) -> None:
        _, profile = config.load_profile("energy-storage-openevolve-paper-100")
        with (
            mock.patch.dict(
                environment.os.environ,
                {
                    "OPENAI_BASE_URL": "https://provider.example/v1",
                    "OPENAI_API_KEY": "secret-value",
                },
            ),
            mock.patch.object(
                environment,
                "command_output",
                return_value=(True, "0.2.26"),
            ),
            mock.patch.object(
                environment,
                "_openevolve_chat_probe",
                return_value={
                    "ok": True,
                    "http_status": 200,
                    "object": "chat.completion",
                    "model": "gpt-5.6-sol",
                },
            ),
        ):
            checks = environment._openevolve_agent_checks(profile)

        self.assertTrue(all(item["passed"] for item in checks))
        serialized = json.dumps(checks)
        self.assertNotIn("provider.example", serialized)
        self.assertNotIn("secret-value", serialized)

    def test_openevolve_doctor_does_not_require_goal_plus_checkout(self) -> None:
        _, profile = config.load_profile("energy-storage-openevolve-smoke-5")
        with (
            mock.patch.object(
                environment, "local_inventory", return_value={"passed": True}
            ),
            mock.patch.object(environment, "_openevolve_agent_checks", return_value=[]),
            mock.patch.object(
                environment,
                "_openevolve_config_check",
                return_value={"kind": "native-config-resolution", "passed": True},
            ),
            mock.patch.object(
                environment, "_runtime_probe", return_value={"passed": True}
            ),
            mock.patch.object(
                environment, "_seed_probe", return_value={"passed": True}
            ),
            mock.patch.object(environment.shutil, "which", return_value="/bin/tool"),
            mock.patch.object(
                environment,
                "git_value",
                side_effect=[
                    environment.registered_upstream_branch(config.UPSTREAM_KEY, repository_root=ROOT),
                    "", "frontier-commit",
                ],
            ) as git_value,
        ):
            self.assertEqual(environment.doctor(profile), 0)

        self.assertEqual(git_value.call_count, 3)
        self.assertTrue(
            all(
                call.args[0] == environment.UPSTREAM_ROOT
                for call in git_value.call_args_list
            )
        )

    def test_openevolve_config_probe_uses_dummy_credentials(self) -> None:
        _, profile = config.load_profile("energy-storage-openevolve-paper-100")
        observed: dict[str, object] = {}

        def resolve_config(
            command: list[str], **kwargs: object
        ) -> tuple[bool, str]:
            observed["command"] = command
            observed["environment"] = kwargs["environment"]
            return (
                True,
                "iterations: 100\nrandom_seed: 42\nparallel_evaluations: 1\n"
                "reasoning_effort: medium\nmodel: gpt-5.6-sol\n",
            )

        with (
            mock.patch.object(
                environment, "command_output", side_effect=resolve_config
            ),
            mock.patch.dict(
                environment.os.environ,
                {
                    "OPENAI_API_BASE": "https://real-provider.example/v1",
                    "OPENAI_API_KEY": "real-secret",
                },
            ),
        ):
            check = environment._openevolve_config_check(profile)

        self.assertTrue(check["passed"])
        probe_environment = observed["environment"]
        self.assertIsInstance(probe_environment, dict)
        self.assertEqual(
            probe_environment["OPENAI_API_BASE"], "https://invalid.example/v1"
        )
        self.assertEqual(
            probe_environment["OPENAI_API_KEY"], "DUMMY_CONFIG_PROBE_KEY"
        )

    def test_openevolve_completion_requires_exact_iteration_ledger(self) -> None:
        for evolved_count, expected_returncode in ((100, 0), (99, 2)):
            with self.subTest(evolved_count=evolved_count), tempfile.TemporaryDirectory() as temporary:
                run_dir = Path(temporary) / "run"
                output_dir = run_dir / "native-output"
                history_dir = output_dir / "openevolve/history"
                best_dir = output_dir / "openevolve/best"
                workspace = run_dir / "workspace"
                history_dir.mkdir(parents=True)
                best_dir.mkdir(parents=True)
                workspace.mkdir(parents=True)
                (workspace / "init.py").write_text("seed = True\n")
                entries = [
                    {
                        "id": "seed",
                        "parent_id": None,
                        "iteration_found": 0,
                    },
                    *[
                        {
                            "id": f"candidate-{index}",
                            "parent_id": "seed",
                            "iteration_found": index,
                        }
                        for index in range(1, evolved_count + 1)
                    ],
                ]
                (history_dir / "index.jsonl").write_text(
                    "".join(json.dumps(item) + "\n" for item in entries)
                )
                seed_dir = history_dir / "iter_000000__seed"
                seed_dir.mkdir()
                (seed_dir / "metrics.json").write_text(
                    json.dumps(
                        {"combined_score": 66.0, "valid": 1.0, "timeout": 0.0}
                    )
                )
                (best_dir / "best_program.py").write_text("best = True\n")
                (best_dir / "best_program_info.json").write_text(
                    json.dumps({"iteration": evolved_count, "score": 100.0})
                )
                config.write_json(
                    run_dir / "experiment.json",
                    {
                        "schema_version": 1,
                        "status": "prepared",
                        "method": "openevolve",
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "medium",
                        "task_id": "EnergyStorage/BatteryFastChargingSPMe",
                        "seed": 1,
                        "budget": {
                            "iterations_ceiling": 100,
                            "wall_time_seconds": 43200,
                            "hard_kill_grace_seconds": 60,
                        },
                        "task": {
                            "artifact_name": "init.py",
                            "upstream_commit": "upstream-commit",
                            "initial_program_sha256": "seed-sha",
                        },
                        "workspace": str(workspace),
                        "output_dir": str(output_dir),
                        "command": ["frontier-eval"],
                    },
                )
                final = {
                    "valid": True,
                    "primary_metric": {
                        "name": "combined_score",
                        "value": 100.0,
                        "direction": "maximize",
                    },
                    "raw_metrics": {"combined_score": 100.0, "valid": 1.0},
                    "budget": {"total_claimed": 1},
                }
                with (
                    mock.patch.object(
                        openevolve_runtime,
                        "_git_value",
                        return_value="upstream-commit",
                    ),
                    mock.patch.object(
                        openevolve_runtime,
                        "_run_upstream",
                        return_value={
                            "duration_seconds": 10.0,
                            "returncode": 0,
                            "deadline_reached": False,
                            "hard_killed": False,
                        },
                    ),
                    mock.patch.object(
                        openevolve_runtime.task_adapter,
                        "evaluate_workspace",
                        return_value=final,
                    ),
                    mock.patch.object(
                        openevolve_runtime.task_adapter, "configure_task"
                    ),
                ):
                    result = openevolve_runtime.execute_cell(run_dir)

                self.assertEqual(result, expected_returncode)
                manifest = json.loads((run_dir / "experiment.json").read_text())
                self.assertEqual(
                    manifest["execution"]["iterations"]["completed_candidates"],
                    evolved_count,
                )
                self.assertEqual(
                    manifest["status"],
                    "finished" if evolved_count == 100 else "incomplete",
                )

    def test_execute_and_finalize_require_valid_official_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "campaign"
            run_dir = destination / "cell"
            run_dir.mkdir(parents=True)
            campaign = {
                "schema_version": 1,
                "campaign_id": "native-test",
                "benchmark": "frontier-engineering",
                "suite": "v1-lite",
                "state": "prepared",
                "model": "test-model",
                "reasoning_effort": "medium",
                "budget": {
                    "wall_time_seconds": 10,
                    "live_search_concurrency": 1,
                    "cell_concurrency": 1,
                    "attempts": 1,
                },
                "cells": [
                    {
                        "cell_id": "jobshop",
                        "task_id": "JobShop/abz",
                        "method": "plain-codex",
                        "seed": 1,
                        "run_dir": str(run_dir),
                        "state": "prepared",
                        "error": None,
                    }
                ],
            }
            config.write_json(destination / "campaign.json", campaign)

            def invalid_run(_args: object) -> int:
                config.write_json(
                    run_dir / "experiment.json",
                    {
                        "status": "finished",
                        "task_id": "JobShop/abz",
                        "method": "plain-codex",
                        "task": {
                            "artifact_name": "init.py",
                            "primary_metric": "combined_score",
                            "direction": "maximize",
                        },
                        "budget": {"wall_time_seconds": 10, "concurrency": 1},
                        "execution": {},
                    },
                )
                config.write_json(
                    run_dir / "final-eval.json",
                    {
                        "valid": False,
                        "primary_metric": {
                            "name": "combined_score",
                            "value": -1e18,
                            "direction": "maximize",
                        },
                        "raw_metrics": {"combined_score": -1e18, "valid": 0.0},
                    },
                )
                return 0

            with mock.patch.object(runtime.standalone, "execute", side_effect=invalid_run):
                self.assertEqual(runtime.execute_campaign(destination), 2)

            final_campaign = json.loads((destination / "campaign.json").read_text())
            self.assertEqual(final_campaign["state"], "partial")
            summary = reporting.finalize_campaign(destination)
            self.assertEqual(summary["state"], "partial")
            self.assertEqual(summary["records"][0]["score"]["final"], -1e18)
            self.assertFalse(summary["records"][0]["score"]["valid"])


if __name__ == "__main__":
    unittest.main()
