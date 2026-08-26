from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench_goal_plus.pi_pricing import resolve_pi_model_cost


class PiPricingTest(unittest.TestCase):
    def test_resolves_exact_provider_from_active_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agent_dir = Path(temp_dir)
            (agent_dir / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "bench-openai": {
                                "api": "openai-responses",
                                "models": [
                                    {
                                        "id": "model-a",
                                        "cost": {
                                            "input": 1,
                                            "output": 2,
                                            "cacheRead": 0.1,
                                            "cacheWrite": 1.25,
                                        },
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            cost = resolve_pi_model_cost(
                provider_id="bench-openai",
                model_id="model-a",
                api="openai-responses",
                environment={"PI_CODING_AGENT_DIR": str(agent_dir)},
            )

        self.assertEqual(cost["output"], 2)

    def test_falls_back_to_installed_pi_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent_dir = root / "agent"
            agent_dir.mkdir()
            (agent_dir / "models.json").write_text(
                json.dumps(
                    {
                        "bench-openai": {
                            "api": "openai-responses",
                            "models": [
                                {
                                    "id": "gpt-5.6-terra",
                                    "cost": dict.fromkeys(
                                        ("input", "output", "cacheRead", "cacheWrite"),
                                        0,
                                    ),
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            package_root = root / "pi-coding-agent"
            pi_cli = package_root / "dist/cli.js"
            pi_cli.parent.mkdir(parents=True)
            pi_cli.touch()
            catalog_dir = (
                package_root
                / "node_modules/@earendil-works/pi-ai/dist/providers/data"
            )
            catalog_dir.mkdir(parents=True)
            expected = {
                "input": 2,
                "output": 12,
                "cacheRead": 0.2,
                "cacheWrite": 2.5,
                "tiers": [{"inputTokensAbove": 272000, "input": 4}],
            }
            (catalog_dir / "openai.json").write_text(
                json.dumps(
                    {
                        "openai-responses": {
                            "gpt-5.6-terra": {
                                "id": "gpt-5.6-terra",
                                "provider": "openai",
                                "cost": expected,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            cost = resolve_pi_model_cost(
                provider_id="bench-openai",
                model_id="gpt-5.6-terra",
                api="openai-responses",
                pi_bin=pi_cli,
                environment={"PI_CODING_AGENT_DIR": str(agent_dir)},
            )

        self.assertEqual(cost, expected)

    def test_zero_price_is_reported_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "models.json"
            catalog.write_text(
                json.dumps(
                    {
                        "bench-openai": {
                            "api": "openai-responses",
                            "models": [
                                {
                                    "id": "gpt-5.6-sol",
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            cost = resolve_pi_model_cost(
                provider_id="bench-openai",
                model_id="gpt-5.6-sol",
                api="openai-responses",
                catalog_path=catalog,
            )

        self.assertIsNone(cost)


if __name__ == "__main__":
    unittest.main()
