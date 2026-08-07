# Runner contract

`benchmarks/runners.json` separates reusable lifecycle behavior from benchmark-specific inputs.

## Runner fields

| Field | Meaning |
|---|---|
| `id` / `kind` | Stable runner ID and implementation selected by `runners/factory.py` |
| `controller` | Existing repository controller; the Agent calls it instead of copying it |
| `evidence_filename` | Native final JSON basename consumed by the unified `finish` path |
| `supported_methods` | Canonical methods accepted during plan resolution; unknown methods fail before setup |
| `method_contracts` | Optional per-method input constraints; `model_format: provider/model` requires an exact `PROVIDER/MODEL` value, while `max_cell_concurrency` caps `C` for that method |
| `capabilities` | `provision`, `detach`, `stop`, `resume`, `cell_concurrency`, `retain_containers`, official evaluator, and exact resume semantics |

Current kinds are `native-profile`, `common-matrix`, and `openevolve-batch`. If a new native
lifecycle cannot implement this interface, add one runner implementation and tests; do not add
target-name branches to the CLI.

Every `method_contracts` key must also appear in `supported_methods`. Resolve these contracts during
`plan` and reject malformed method inputs or a method-specific `C` overflow before setup, doctor,
preparation, or launch. The same resolved method, provider, model, and concurrency must then flow
unchanged through the runner lifecycle.

## Target fields

| Field | Meaning |
|---|---|
| `id` | CLI benchmark/task-set target |
| `runner` | Reusable runner ID |
| `adapter` | Common artifact adapter, otherwise `null` |
| `bootstrap_targets` | Keys from `environment/upstreams.json` |
| `docker` | Exact execution-path contract described below |

## Docker contract

| Field | Values | Meaning |
|---|---|---|
| `requirement` | `required`, `mixed`, `not_required` | Whether this target path needs Docker |
| `owner` | `runner`, `adapter`, `host` | Which layer owns image/container behavior |
| `provision_mode` | `eager`, `lazy`, `external`, `none` | When provisioning happens |
| `scope` | text | What can and cannot run without Docker |

`runner/eager` means the native controller exposes provision/doctor, as EdgeBench does.
`adapter/eager` requires `provision_environment(upstream_root)` and
`doctor_environment(upstream_root)` hooks. `adapter/lazy` keeps container creation in the existing
evaluator path. `external` means the Agent checks the prerequisite but does not create it.

Presets are frozen examples over targets. They expand model, reasoning, T/K/C/R, methods, and
profile into `agent-run.json`; they are never generic defaults.

`retain_containers=true` only advertises support for the unified `plan/launch
--retain-containers` debug option. The selected benchmark reference defines which runner-owned
containers are retained. A retained container must be stopped and recorded in campaign evidence;
finalization does not remove it. This capability never authorizes image removal, retagging, pulling,
or rebuilding.

## Benchmark-specific completion

This contract does not define one universal completion signal. Read the benchmark reference
selected by [runner-map.md](runner-map.md) for:

- evaluator and native final-artifact ownership;
- required Goal Plus or host-worker evidence;
- detach, stop and resume semantics;
- report source and readiness gates.

Do not promote a benchmark-specific signal such as a SForge Judge trajectory, Codex collaboration
event, or OpenEvolve cell state into the generic runner interface.
