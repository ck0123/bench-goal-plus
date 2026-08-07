"""Load and validate benchmark targets, runner families, and presets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ContractError
from .models import (
    AssetPackDefinition,
    DockerContract,
    PresetDefinition,
    RunnerCapabilities,
    RunnerDefinition,
    TargetDefinition,
)
from .paths import (
    ADAPTER_REGISTRY,
    ASSET_PACK_REGISTRY,
    ROOT,
    RUNNER_REGISTRY,
    UPSTREAM_REGISTRY,
)


SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
RUNNER_KINDS = {"native-profile", "common-matrix", "openevolve-batch"}
DOCKER_REQUIREMENTS = {"required", "mixed", "not_required"}
DOCKER_OWNERS = {"runner", "adapter", "host"}
DOCKER_PROVISION_MODES = {"eager", "lazy", "external", "none"}


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return payload


class Catalog:
    def __init__(
        self,
        *,
        runner_registry: Path = RUNNER_REGISTRY,
        asset_pack_registry: Path = ASSET_PACK_REGISTRY,
        upstream_registry: Path = UPSTREAM_REGISTRY,
        adapter_registry: Path = ADAPTER_REGISTRY,
    ) -> None:
        self.runner_registry = runner_registry
        self.asset_pack_registry = asset_pack_registry
        self.upstream_registry = upstream_registry
        self.adapter_registry = adapter_registry
        self.runners: dict[str, RunnerDefinition] = {}
        self.targets: dict[str, TargetDefinition] = {}
        self.asset_packs: dict[str, AssetPackDefinition] = {}
        self.presets: dict[str, PresetDefinition] = {}
        self._load()

    def _load(self) -> None:
        payload = read_json(self.runner_registry)
        if payload.get("schema_version") != 2:
            raise ContractError("runner registry schema_version must be 2")
        upstreams = set(read_json(self.upstream_registry).get("upstreams", {}))
        adapters = {
            item.get("id")
            for item in read_json(self.adapter_registry).get("adapters", [])
        }

        for index, entry in enumerate(payload.get("runners", [])):
            runner_id = self._safe_id(entry.get("id"), f"runner {index}")
            if runner_id in self.runners:
                raise ContractError(f"duplicate runner id: {runner_id}")
            kind = entry.get("kind")
            if kind not in RUNNER_KINDS:
                raise ContractError(f"{runner_id}: unsupported runner kind {kind!r}")
            controller_value = entry.get("controller")
            if not isinstance(controller_value, str):
                raise ContractError(f"{runner_id}: controller must be a path")
            controller = ROOT / controller_value
            if not controller.is_file():
                raise ContractError(
                    f"{runner_id}: controller does not exist: {controller_value!r}"
                )
            evidence_filename = entry.get("evidence_filename")
            if (
                not isinstance(evidence_filename, str)
                or Path(evidence_filename).name != evidence_filename
                or not evidence_filename.endswith(".json")
            ):
                raise ContractError(
                    f"{runner_id}: evidence_filename must be a JSON basename"
                )
            raw_methods = entry.get("supported_methods")
            if not isinstance(raw_methods, list) or not raw_methods:
                raise ContractError(f"{runner_id}: supported_methods must be non-empty")
            supported_methods = tuple(
                self._safe_id(value, f"{runner_id} method {method_index}")
                for method_index, value in enumerate(raw_methods)
            )
            if len(set(supported_methods)) != len(supported_methods):
                raise ContractError(f"{runner_id}: supported_methods must be unique")
            raw_method_contracts = entry.get("method_contracts") or {}
            if not isinstance(raw_method_contracts, dict):
                raise ContractError(f"{runner_id}: method_contracts must be an object")
            unknown_contracts = set(raw_method_contracts) - set(supported_methods)
            if unknown_contracts:
                raise ContractError(
                    f"{runner_id}: method_contracts name unsupported methods: "
                    + ", ".join(sorted(unknown_contracts))
                )
            method_contracts: dict[str, dict[str, Any]] = {}
            for method, contract in raw_method_contracts.items():
                if not isinstance(contract, dict):
                    raise ContractError(
                        f"{runner_id}: contract for {method} must be an object"
                    )
                unknown_fields = set(contract) - {"model_format"}
                if unknown_fields:
                    raise ContractError(
                        f"{runner_id}: contract for {method} has unknown fields: "
                        + ", ".join(sorted(unknown_fields))
                    )
                model_format = contract.get("model_format")
                if model_format != "provider/model":
                    raise ContractError(
                        f"{runner_id}: contract for {method} has unsupported "
                        f"model_format {model_format!r}"
                    )
                method_contracts[method] = dict(contract)
            raw_capabilities = entry.get("capabilities")
            if not isinstance(raw_capabilities, dict):
                raise ContractError(f"{runner_id}: capabilities must be an object")
            bool_fields = (
                "provision",
                "detach",
                "stop",
                "resume",
                "cell_concurrency",
                "retain_containers",
                "official_evaluator",
                "attempt_seed",
            )
            if not all(isinstance(raw_capabilities.get(name), bool) for name in bool_fields):
                raise ContractError(f"{runner_id}: capability flags must be boolean")
            resume_semantics = raw_capabilities.get("resume_semantics")
            if not isinstance(resume_semantics, str) or not resume_semantics:
                raise ContractError(f"{runner_id}: resume_semantics is required")
            capabilities = RunnerCapabilities(
                **{name: raw_capabilities[name] for name in bool_fields},
                resume_semantics=resume_semantics,
            )
            self.runners[runner_id] = RunnerDefinition(
                runner_id=runner_id,
                kind=kind,
                controller=controller,
                evidence_filename=evidence_filename,
                supported_methods=supported_methods,
                capabilities=capabilities,
                method_contracts=method_contracts,
            )

        for index, entry in enumerate(payload.get("targets", [])):
            target_id = self._safe_id(entry.get("id"), f"target {index}")
            if target_id in self.targets:
                raise ContractError(f"duplicate target id: {target_id}")
            runner_id = entry.get("runner")
            if runner_id not in self.runners:
                raise ContractError(f"{target_id}: unknown runner {runner_id!r}")
            adapter_id = entry.get("adapter")
            runner = self.runners[str(runner_id)]
            if runner.kind == "common-matrix" and adapter_id not in adapters:
                raise ContractError(
                    f"{target_id}: common-matrix target needs a registered adapter"
                )
            if runner.kind == "native-profile" and adapter_id is not None:
                raise ContractError(f"{target_id}: native target must not declare an adapter")
            if runner.kind == "openevolve-batch" and adapter_id is not None:
                raise ContractError(
                    f"{target_id}: openevolve-batch uses its native task catalog"
                )
            bootstrap = entry.get("bootstrap_targets")
            if not isinstance(bootstrap, list) or not all(
                isinstance(item, str) and item in upstreams for item in bootstrap
            ):
                raise ContractError(
                    f"{target_id}: bootstrap_targets must name managed upstreams"
                )
            docker = self._docker_contract(target_id, entry.get("docker"))
            local_asset_inventory = entry.get("local_asset_inventory")
            if not isinstance(local_asset_inventory, bool):
                raise ContractError(
                    f"{target_id}: local_asset_inventory must be boolean"
                )
            default_inventory_profile = entry.get("default_inventory_profile")
            if local_asset_inventory:
                if (
                    not isinstance(default_inventory_profile, str)
                    or SAFE_ID.fullmatch(default_inventory_profile) is None
                ):
                    raise ContractError(
                        f"{target_id}: local asset inventory requires a safe "
                        "default_inventory_profile"
                    )
            elif default_inventory_profile is not None:
                raise ContractError(
                    f"{target_id}: default_inventory_profile requires "
                    "local_asset_inventory=true"
                )
            if docker.owner == "adapter" and adapter_id is None:
                raise ContractError(
                    f"{target_id}: adapter-owned Docker needs an adapter"
                )
            if docker.owner == "host" and (
                docker.requirement != "not_required" or docker.provision_mode != "none"
            ):
                raise ContractError(
                    f"{target_id}: host-owned paths must be not_required/none"
                )
            if (
                docker.owner == "runner"
                and docker.provision_mode == "eager"
                and not runner.capabilities.provision
            ):
                raise ContractError(
                    f"{target_id}: eager runner Docker needs provision capability"
                )
            self.targets[target_id] = TargetDefinition(
                target_id=target_id,
                runner_id=str(runner_id),
                adapter_id=str(adapter_id) if adapter_id is not None else None,
                bootstrap_targets=tuple(bootstrap),
                docker=docker,
                local_asset_inventory=local_asset_inventory,
                default_inventory_profile=default_inventory_profile,
            )

        asset_payload = read_json(self.asset_pack_registry)
        if asset_payload.get("schema_version") != 1:
            raise ContractError("asset pack registry schema_version must be 1")
        for index, entry in enumerate(asset_payload.get("asset_packs", [])):
            pack_id = self._safe_id(entry.get("id"), f"asset pack {index}")
            if pack_id in self.asset_packs:
                raise ContractError(f"duplicate asset pack id: {pack_id}")
            controller_value = entry.get("controller")
            if not isinstance(controller_value, str):
                raise ContractError(f"{pack_id}: controller must be a path")
            controller = ROOT / controller_value
            if not controller.is_file():
                raise ContractError(
                    f"{pack_id}: controller does not exist: {controller_value!r}"
                )
            bootstrap = entry.get("bootstrap_targets")
            if not isinstance(bootstrap, list) or not all(
                isinstance(item, str) and item in upstreams for item in bootstrap
            ):
                raise ContractError(
                    f"{pack_id}: bootstrap_targets must name managed upstreams"
                )
            default_profile = entry.get("default_profile")
            if (
                not isinstance(default_profile, str)
                or SAFE_ID.fullmatch(default_profile) is None
            ):
                raise ContractError(f"{pack_id}: default_profile must be a safe id")
            provision = entry.get("provision")
            if not isinstance(provision, bool):
                raise ContractError(f"{pack_id}: provision must be boolean")
            self.asset_packs[pack_id] = AssetPackDefinition(
                pack_id=pack_id,
                controller=controller,
                bootstrap_targets=tuple(bootstrap),
                default_profile=default_profile,
                provision=provision,
            )

        for index, entry in enumerate(payload.get("presets", [])):
            preset_id = self._safe_id(entry.get("id"), f"preset {index}")
            if preset_id in self.presets:
                raise ContractError(f"duplicate preset id: {preset_id}")
            benchmarks = entry.get("benchmarks")
            if not isinstance(benchmarks, list) or not benchmarks:
                raise ContractError(f"{preset_id}: benchmarks must be non-empty")
            unknown = set(benchmarks) - set(self.targets)
            if unknown:
                raise ContractError(
                    f"{preset_id}: unknown targets: {', '.join(sorted(unknown))}"
                )
            expected = entry.get("expected_profile") or {}
            if not isinstance(expected, dict):
                raise ContractError(f"{preset_id}: expected_profile must be an object")
            self.presets[preset_id] = PresetDefinition(
                preset_id=preset_id,
                description=str(entry.get("description") or ""),
                benchmarks=tuple(str(item) for item in benchmarks),
                profile=str(entry["profile"]) if entry.get("profile") else None,
                campaign_id_template=(
                    str(entry["campaign_id_template"])
                    if entry.get("campaign_id_template")
                    else None
                ),
                expected_profile=expected,
            )

    def runner_for(self, target: TargetDefinition) -> RunnerDefinition:
        return self.runners[target.runner_id]

    def as_dict(self) -> dict[str, Any]:
        return {
            "runners": [item.as_dict() for item in self.runners.values()],
            "targets": [item.as_dict() for item in self.targets.values()],
            "asset_packs": [item.as_dict() for item in self.asset_packs.values()],
            "presets": [item.as_dict() for item in self.presets.values()],
        }

    @staticmethod
    def _safe_id(value: Any, label: str) -> str:
        if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
            raise ContractError(f"{label} has unsafe id {value!r}")
        return value

    @staticmethod
    def _docker_contract(target_id: str, value: Any) -> DockerContract:
        if not isinstance(value, dict):
            raise ContractError(f"{target_id}: docker contract must be an object")
        requirement = value.get("requirement")
        owner = value.get("owner")
        provision_mode = value.get("provision_mode")
        scope = value.get("scope")
        if requirement not in DOCKER_REQUIREMENTS:
            raise ContractError(f"{target_id}: invalid Docker requirement")
        if owner not in DOCKER_OWNERS:
            raise ContractError(f"{target_id}: invalid Docker owner")
        if provision_mode not in DOCKER_PROVISION_MODES:
            raise ContractError(f"{target_id}: invalid Docker provision_mode")
        if not isinstance(scope, str) or not scope:
            raise ContractError(f"{target_id}: Docker scope is required")
        if requirement == "not_required" and provision_mode != "none":
            raise ContractError(
                f"{target_id}: not_required Docker path must use provision_mode=none"
            )
        if requirement != "not_required" and provision_mode == "none":
            raise ContractError(
                f"{target_id}: Docker-required path cannot use provision_mode=none"
            )
        return DockerContract(
            requirement=requirement,
            owner=owner,
            provision_mode=provision_mode,
            scope=scope,
        )
