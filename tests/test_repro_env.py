from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("repro_env", ROOT / "scripts/repro_env.py")
assert SPEC and SPEC.loader
repro_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repro_env)


class ReproEnvironmentTest(unittest.TestCase):
    @staticmethod
    def update_payload(*, current: bool) -> dict:
        root_check = {
            "name": "bench_goal_plus",
            "path": str(ROOT),
            "branch": "main",
            "repository": "https://example.invalid/bench-goal-plus.git",
            "passed": True,
            "current_head": "a" * 40,
            "advertised_head": "a" * 40,
            "update_available": False,
            "repair_required": False,
            "clone_required": False,
            "action_required": False,
            "query_ok": True,
            "transport": "fixture",
            "query_error": None,
            "blockers": [],
        }
        checkout = {
            **root_check,
            "name": "goal_plus",
            "path": str(ROOT / "third_party/goal-plus"),
            "repository": "https://example.invalid/goal-plus.git",
        }
        if not current:
            checkout.update(
                {
                    "passed": False,
                    "advertised_head": "b" * 40,
                    "update_available": True,
                    "action_required": True,
                }
            )
        checks = [root_check, checkout]
        return {
            "schema_version": 1,
            "ok": current,
            "action_required": not current,
            "updates_available": 0 if current else 1,
            "repairs_required": 0,
            "clones_required": 0,
            "query_failures": 0,
            "blocked": False,
            "checks": checks,
        }

    def test_manifest_tracks_portable_upstream_branches(self) -> None:
        manifest = repro_env.load_manifest(ROOT / "environment/upstreams.json")
        self.assertEqual(repro_env.DEFAULT_CHECKOUT_ROOT, ROOT / "third_party")
        self.assertEqual(manifest["python"], "3.12")
        self.assertEqual(manifest["pi_min_version"], "0.80.6")
        self.assertTrue(
            {
                "openevolve",
                "goal_plus",
                "edgebench",
                "heurigym",
                "ale_bench",
                "autolab",
            }
            <= set(manifest["upstreams"])
        )
        for upstream in manifest["upstreams"].values():
            self.assertRegex(
                upstream["tracking_branch"], r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
            )
            self.assertNotIn("pinned_commit", upstream)
            self.assertTrue(upstream["repository"].startswith("https://github.com/"))
            self.assertNotIn("/Users/", upstream["checkout_dir"])
            self.assertEqual(Path(upstream["checkout_dir"]).parent, Path("."))
        selected = repro_env.selected_upstreams(manifest, ["heurigym"])
        self.assertEqual(set(selected), {"openevolve", "goal_plus", "heurigym"})
        selected_edgebench = repro_env.selected_upstreams(manifest, ["edgebench"])
        self.assertEqual(
            set(selected_edgebench), {"openevolve", "goal_plus", "edgebench"}
        )
        self.assertTrue(selected_edgebench["edgebench"]["editable"])
        selected_sky = repro_env.selected_upstreams(manifest, ["skydiscover"])
        self.assertEqual(
            set(selected_sky), {"openevolve", "goal_plus", "skydiscover"}
        )
        self.assertTrue(selected_sky["skydiscover"]["editable"])
        selected_exact = repro_env.selected_upstreams(
            manifest, ["swebench"], include_always=False
        )
        self.assertEqual(set(selected_exact), {"swebench"})
        task_catalog = json.loads(
            (ROOT / "adapters/openevolve_examples/tasks.json").read_text()
        )
        self.assertEqual(
            manifest["upstreams"]["openevolve"]["tracking_branch"],
            task_catalog["upstream"]["tracking_branch"],
        )

    def test_checkout_follows_branch_and_fast_forwards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            remote = temp / "remote.git"
            checkout = temp / "checkout"
            subprocess.run(["git", "init", "-q", "-b", "main", source], check=True)
            subprocess.run(
                ["git", "-C", source, "config", "user.name", "Test Controller"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    source,
                    "config",
                    "user.email",
                    "test@example.invalid",
                ],
                check=True,
            )
            (source / "README.md").write_text("one\n")
            subprocess.run(["git", "-C", source, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", source, "commit", "-q", "-m", "initial"], check=True
            )
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "-C", source, "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", source, "push", "-q", "-u", "origin", "main"],
                check=True,
            )
            entry = {
                "repository": str(remote),
                "tracking_branch": "main",
            }

            repro_env.ensure_checkout(checkout, entry)
            first = repro_env.git_state(checkout, "main")
            self.assertEqual(first["branch"], "main")
            self.assertEqual(first["upstream"], "origin/main")
            self.assertEqual(first["head"], first["remote_head"])

            subprocess.run(
                ["git", "-C", checkout, "remote", "add", "upstream", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", checkout, "fetch", "-q", "upstream", "main"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    checkout,
                    "branch",
                    "--set-upstream-to",
                    "upstream/main",
                    "main",
                ],
                check=True,
                capture_output=True,
            )
            repro_env.ensure_checkout(checkout, entry)
            repaired = repro_env.git_state(checkout, "main")
            self.assertEqual(repaired["upstream"], "origin/main")

            (source / "README.md").write_text("two\n")
            subprocess.run(["git", "-C", source, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", source, "commit", "-q", "-m", "second"], check=True
            )
            subprocess.run(
                ["git", "-C", source, "push", "-q", "origin", "main"], check=True
            )
            expected = subprocess.run(
                ["git", "-C", source, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            repro_env.ensure_checkout(checkout, entry)
            second = repro_env.git_state(checkout, "main")
            self.assertEqual(second["head"], expected)
            self.assertNotEqual(first["head"], second["head"])

    def test_lock_and_manifests_do_not_contain_local_identity_or_keys(self) -> None:
        text = "\n".join(
            path.read_text()
            for path in (
                ROOT / "environment/upstreams.json",
                ROOT / "environment/requirements.in",
                ROOT / "environment/requirements.lock",
            )
        )
        self.assertNotRegex(text, r"/Users/[^/\s]+")
        self.assertNotRegex(text, r"\bsk-[A-Za-z0-9_-]{16,}\b")
        self.assertIn("fastmcp==", text)
        self.assertIn("openai==", text)

    def test_codex_version_parser(self) -> None:
        self.assertEqual(
            repro_env.parse_codex_version("codex-cli 0.144.6"), (0, 144, 6)
        )
        self.assertIsNone(repro_env.parse_codex_version("unknown"))

    def test_pi_is_diagnostic_by_default(self) -> None:
        manifest = repro_env.load_manifest(ROOT / "environment/upstreams.json")
        with patch.object(repro_env.shutil, "which", return_value=None):
            check = repro_env.host_pi_check(manifest)
        self.assertTrue(check["passed"])
        self.assertFalse(check["required"])
        self.assertFalse(check["available"])
        self.assertFalse(check["compatible"])

    def test_pi_can_be_required_for_goal_plus_pi(self) -> None:
        manifest = repro_env.load_manifest(ROOT / "environment/upstreams.json")
        with patch.object(repro_env.shutil, "which", return_value=None):
            check = repro_env.host_pi_check(manifest, required=True)
        self.assertFalse(check["passed"])
        self.assertTrue(check["required"])

    def test_codex_is_diagnostic_by_default(self) -> None:
        manifest = repro_env.load_manifest(ROOT / "environment/upstreams.json")
        with patch.object(repro_env.shutil, "which", return_value=None):
            check = repro_env.host_codex_check(manifest)
        self.assertTrue(check["passed"])
        self.assertFalse(check["required"])
        self.assertFalse(check["available"])
        self.assertFalse(check["compatible"])

    def test_codex_can_be_required_for_codex_methods(self) -> None:
        manifest = repro_env.load_manifest(ROOT / "environment/upstreams.json")
        with patch.object(repro_env.shutil, "which", return_value=None):
            check = repro_env.host_codex_check(manifest, required=True)
        self.assertFalse(check["passed"])
        self.assertTrue(check["required"])

    def test_repro_parser_exposes_agent_specific_requirements(self) -> None:
        parser = repro_env.build_parser()

        bootstrap = parser.parse_args(
            ["bootstrap", "--require-pi", "--require-codex"]
        )
        doctor = parser.parse_args(
            ["doctor", "--require-pi", "--require-codex"]
        )
        check = parser.parse_args(
            ["check", "--inventory-gated", "--yes"]
        )

        self.assertTrue(bootstrap.require_pi)
        self.assertTrue(bootstrap.require_codex)
        self.assertTrue(doctor.require_pi)
        self.assertTrue(doctor.require_codex)
        self.assertTrue(check.inventory_gated)
        self.assertTrue(check.yes)

    def test_remote_update_probe_does_not_fetch_tracking_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            remote = temp / "remote.git"
            checkout = temp / "checkout"
            subprocess.run(["git", "init", "-q", "-b", "main", source], check=True)
            subprocess.run(
                ["git", "-C", source, "config", "user.name", "Test Controller"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    source,
                    "config",
                    "user.email",
                    "test@example.invalid",
                ],
                check=True,
            )
            (source / "README.md").write_text("one\n")
            subprocess.run(["git", "-C", source, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", source, "commit", "-q", "-m", "initial"],
                check=True,
            )
            subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
            subprocess.run(
                ["git", "-C", source, "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", source, "push", "-q", "-u", "origin", "main"],
                check=True,
            )
            entry = {"repository": str(remote), "tracking_branch": "main"}
            repro_env.ensure_checkout(checkout, entry)
            before = repro_env.git_state(checkout, "main")

            (source / "README.md").write_text("two\n")
            subprocess.run(["git", "-C", source, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", source, "commit", "-q", "-m", "second"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", source, "push", "-q", "origin", "main"],
                check=True,
            )

            update = repro_env.repository_update_check("fixture", checkout, entry)
            after = repro_env.git_state(checkout, "main")

            self.assertTrue(update["query_ok"])
            self.assertTrue(update["update_available"])
            self.assertTrue(update["action_required"])
            self.assertEqual(after["head"], before["head"])
            self.assertEqual(after["remote_head"], before["remote_head"])
            self.assertNotEqual(update["advertised_head"], before["head"])

    def test_update_decision_requires_tty_or_explicit_yes(self) -> None:
        self.assertEqual(
            repro_env.update_decision(
                action_required=True,
                assume_yes=False,
                interactive=False,
            ),
            "non-interactive",
        )
        self.assertEqual(
            repro_env.update_decision(
                action_required=True,
                assume_yes=True,
                interactive=False,
            ),
            "accepted",
        )
        self.assertEqual(
            repro_env.update_decision(
                action_required=True,
                assume_yes=False,
                interactive=True,
                response="no",
            ),
            "declined",
        )
        self.assertEqual(
            repro_env.update_decision(
                action_required=True,
                assume_yes=False,
                interactive=True,
                response="yes",
            ),
            "accepted",
        )

    def test_noninteractive_environment_check_never_updates_implicitly(self) -> None:
        args = argparse.Namespace(
            manifest=ROOT / "environment/upstreams.json",
            checkout_root=ROOT / "third_party",
            venv=ROOT / ".bench-env/venv",
            lock=ROOT / "environment/requirements.lock",
            only=None,
            inventory_gated=True,
            yes=False,
        )
        doctor = {"ok": True}
        updates = self.update_payload(current=False)
        with (
            patch.object(repro_env, "collect_doctor", return_value=doctor),
            patch.object(
                repro_env,
                "collect_repository_updates",
                return_value=updates,
            ),
            patch.object(repro_env, "bootstrap_environment") as bootstrap,
            patch.object(repro_env.sys.stdin, "isatty", return_value=False),
            patch("builtins.print"),
        ):
            result = repro_env.environment_check(args)

        self.assertEqual(result, 1)
        bootstrap.assert_not_called()

    def test_explicit_yes_refreshes_managed_environment(self) -> None:
        args = argparse.Namespace(
            manifest=ROOT / "environment/upstreams.json",
            checkout_root=ROOT / "third_party",
            venv=ROOT / ".bench-env/venv",
            lock=ROOT / "environment/requirements.lock",
            only=None,
            inventory_gated=True,
            yes=True,
        )
        doctor = {"ok": True}
        with (
            patch.object(repro_env, "collect_doctor", return_value=doctor),
            patch.object(
                repro_env,
                "collect_repository_updates",
                side_effect=(
                    self.update_payload(current=False),
                    self.update_payload(current=True),
                ),
            ),
            patch.object(
                repro_env,
                "bootstrap_environment",
                return_value=doctor,
            ) as bootstrap,
            patch.object(repro_env.sys.stdin, "isatty", return_value=False),
            patch("builtins.print"),
        ):
            result = repro_env.environment_check(args)

        self.assertEqual(result, 0)
        bootstrap.assert_called_once()

    def test_repository_normalization_equates_https_and_ssh_remotes(self) -> None:
        expected = "https://github.com/example/project"

        self.assertEqual(
            repro_env.normalize_repository("git@github.com:example/project.git"),
            expected,
        )
        self.assertEqual(
            repro_env.normalize_repository(
                "ssh://git@github.com/example/project.git"
            ),
            expected,
        )
        self.assertEqual(
            repro_env.normalize_repository(
                "https://github.com/example/project.git"
            ),
            expected,
        )
        self.assertEqual(
            repro_env.repository_transport_url(
                "git@github.com:example/project.git"
            ),
            "https://github.com/example/project.git",
        )
        self.assertEqual(
            repro_env.repository_transport_url("/tmp/project.git"),
            "/tmp/project.git",
        )
        self.assertEqual(
            repro_env.redact_git_text(
                "fatal: https://user:secret@example.invalid/project.git"
            ),
            "fatal: https://<redacted>@example.invalid/project.git",
        )

    def test_edgebench_rust_runtime_download_is_pinned_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            edgebench = temp / "edgebench"
            manifest = edgebench / "sforge" / "harness" / "runtime_assets.json"
            manifest.parent.mkdir(parents=True)
            source = temp / "rust.tar.xz"
            source.write_bytes(b"rust runtime")
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rust": {
                            "version": "1.2.3",
                            "target": "x86_64-unknown-linux-gnu",
                            "archive_name": "rust.tar.xz",
                            "url": source.as_uri(),
                            "sha256": checksum,
                        },
                    }
                )
            )
            cache = temp / "cache"

            result = repro_env.ensure_edgebench_rust_runtime(edgebench, cache)

            self.assertEqual(result.read_bytes(), b"rust runtime")
            self.assertFalse(
                (cache / "rust.tar.xz_bootstrap_incomplete").exists()
            )

    def test_edgebench_rust_runtime_preserves_invalid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            edgebench = temp / "edgebench"
            manifest = edgebench / "sforge" / "harness" / "runtime_assets.json"
            manifest.parent.mkdir(parents=True)
            source = temp / "source.tar.xz"
            source.write_bytes(b"new runtime")
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rust": {
                            "version": "1.2.3",
                            "target": "x86_64-unknown-linux-gnu",
                            "archive_name": "rust.tar.xz",
                            "url": source.as_uri(),
                            "sha256": checksum,
                        },
                    }
                )
            )
            cache = temp / "cache"
            cache.mkdir()
            (cache / "rust.tar.xz").write_bytes(b"old runtime")

            repro_env.ensure_edgebench_rust_runtime(edgebench, cache)

            self.assertEqual((cache / "rust.tar.xz").read_bytes(), b"new runtime")
            self.assertEqual(
                (cache / "rust.tar.xz_bak").read_bytes(), b"old runtime"
            )

    def test_missing_git_checkout_is_reported_without_mutation(self) -> None:
        state = repro_env.git_state(ROOT / "does-not-exist")
        self.assertEqual(
            state,
            {
                "exists": False,
                "is_git": False,
                "head": None,
                "branch": None,
                "upstream": None,
                "remote_head": None,
                "origin_url": None,
                "dirty": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
