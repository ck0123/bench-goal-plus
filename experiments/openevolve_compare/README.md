# OpenEvolve task multi-backend comparison

This harness runs one OpenEvolve example from the managed branch snapshot through independent search backends while sharing the seed, official evaluator, model identity, wall-clock budget `T`, and live concurrency cap `K`. The experiment manifest records the resolved source commits:

- `openevolve`: native OpenEvolve population/island search;
- `plain-codex`: `K` independent Codex lanes, followed by controller selection;
- `goal-plus-codex`: Goal Plus fixed parallel lineages hosted by Codex;
- `goal-plus-pi`: the same Goal Plus contract hosted by Pi RPC workers;
- `skydiscover-best-of-n`: SkyDiscover native Best-of-N;
- `skydiscover-evox`: SkyDiscover EvoX co-evolves the candidate and search strategy;
- `skydiscover-adaevolve`: SkyDiscover AdaEvolve adaptive multi-island search.

All three SkyDiscover methods use the same controller-owned
candidate-workspace evaluator bridge.

Defaults are `T=300s`, `K=2`, model `gpt-5.6-luna`, and reasoning `high`. OpenEvolve, Pi, and SkyDiscover require an explicit OpenAI-compatible `--api-base` and inherit `OPENAI_API_KEY`; the key is never serialized. Codex paths can omit `--api-base` and use the machine's native Codex login, or use the same explicit endpoint as the other paths.

`T` is a total cap, not a minimum duration or success criterion. The default prompt asks all paths to reserve the final 60 seconds for making the best verified artifact ready; a method may finish earlier when it has satisfied the objective.

## What is outside and inside T

`prepare` performs only task/config/workspace materialization. For Goal Plus it copies the portable project hook, skill, and MCP assets, but it does not create `.gp`, a Goal record, a frozen SearchSpec, a Search run, candidates, or sessions.

Plain Codex receives one common task prompt. Codex + Goal Plus receives exactly the same common prompt with `$goal-plus mode=autonomous` prepended and a complete Goal Plus configuration appended; Pi uses its native `/goal-plus mode=autonomous` command. Goal intake, triage, spec discovery/freezing, candidate creation, worker inference, selection, and promotion therefore all happen inside `T`. Outside `T`, the controller may perform only deterministic process cleanup, idempotent closeout, and the common final evaluator. The manifest stores the common-prompt hash and Goal Plus transformation for audit.

## Prepare

Bootstrap first, then prepare one persistent but Git-ignored directory per method:

```bash
python3 scripts/repro_env.py bootstrap --only openevolve
python3 scripts/repro_env.py doctor --only openevolve

for method in openevolve plain-codex goal-plus-codex goal-plus-pi; do
  .bench-env/venv/bin/python experiments/openevolve_compare/experiment.py prepare \
    --method "$method" \
    --task-id function_minimization \
    --wall-time-seconds 300 \
    --concurrency 2 \
    --model gpt-5.6-luna \
    --seed 1
done
```

Each command prints `runs/openevolve-compare/<run-id>`. Run directories are never automatically deleted. Goal Plus state stays under that run's `workspace/.gp`; evaluator tickets stay in the controller-owned run directory rather than candidate workspaces.

Before spending model budget, the optional seed check is:

```bash
.bench-env/venv/bin/python experiments/openevolve_compare/experiment.py seed-smoke \
  --run-dir runs/openevolve-compare/<run-id>
```

Do not run `seed-smoke` on the same directory intended for a later capped campaign unless that extra evaluator call is explicitly part of the protocol.

For the current SkyDiscover functional paths, bootstrap its managed editable
runtime and prepare each native algorithm explicitly:

```bash
python3 scripts/repro_env.py bootstrap --only skydiscover

for method in \
  skydiscover-best-of-n \
  skydiscover-evox \
  skydiscover-adaevolve
do
  .bench-env/venv/bin/python experiments/openevolve_compare/experiment.py prepare \
    --method "$method" \
    --task-id function_minimization \
    --wall-time-seconds 180 \
    --soft-closeout-seconds 30 \
    --concurrency 1 \
    --iterations-ceiling 1 \
    --model gpt-5.6-luna \
    --reasoning-effort medium
done
```

The bridge preserves one isolated workspace per candidate, maps minimize
metrics to an internal negated `combined_score`, keeps raw metrics unchanged,
and shares the controller evaluator ledger. EvoX uses the same explicit run
model for candidate generation and search-strategy evolution
(`search.share_llm=true`); one requested EvoX iteration can therefore make
multiple model calls. AdaEvolve keeps its adaptive, UCB, migration, archive,
dynamic-island, and paradigm-breakthrough controls enabled.

SkyDiscover upstream still evaluates its seed and best candidate inside the
timed process and does not persist response usage or observed peak concurrency.
Until those intrinsic runtime gaps are fixed, these paths are functional
smokes rather than matched baselines.

Docker is not required for the current Circle Packing or
`function_minimization` compatibility paths. SkyDiscover's separate no-Torch
Math/ADRS evaluator pack uses 19 Docker tags: `8.57 GB` logical total, about
`2.49 GB` unique image-store increment on the measured host, and `10 GB`
recommended free space. See the
[task-pack environment note](../../docs/benchmarks/skydiscover-task-packs.md).

Two real `glm-5.2/medium` examples now cover the richer algorithms:

- [EvoX, one iteration](../../evidence/runs/2026-07-24-skydiscover-evox-function-minimization-smoke.json)
  completed strategy meta-evolution and automatic variation-operator generation,
  retried one invalid candidate, and improved `combined_score` from
  `1.4286455109` to `1.4995399684`.
- [AdaEvolve, one iteration](../../evidence/runs/2026-07-24-skydiscover-adaevolve-function-minimization-smoke.json)
  exercised two islands and the adaptive/UCB controls. Its generated candidate
  was valid but worse, so native selection correctly retained the seed at
  `1.4286455109`.

These are execution-path evidence. Do not interpret a single short iteration
as an algorithm ranking.

## Batch campaign

`cpu_portable` is a screened 12-task set requiring no GPU/NPU, downloaded dataset, network service, compiler, or external executable. Prepare the full task × method matrix in one command:

```bash
.bench-env/venv/bin/python experiments/openevolve_compare/experiment.py prepare-batch \
  --task-set cpu_portable \
  --methods \
    openevolve plain-codex goal-plus-codex goal-plus-pi \
    skydiscover-best-of-n skydiscover-evox skydiscover-adaevolve \
  --run-root runs/openevolve-campaigns/<campaign-id> \
  --wall-time-seconds 300 \
  --concurrency 2 \
  --model gpt-5.6-luna \
  --seed 1
```

This expands all 84 cells into persistent isolated directories and writes
`campaign.json`. A failure in one cell is recorded without discarding the rest.
To prepare only the two Codex paths, pass
`--methods plain-codex goal-plus-codex`.

Run the prepared cells sequentially with one outer command:

```bash
export OPENAI_API_KEY='<secret>'
.bench-env/venv/bin/python experiments/openevolve_compare/experiment.py run-batch \
  --campaign runs/openevolve-campaigns/<campaign-id> \
  --model gpt-5.6-luna \
  --api-base https://api.example.com/v1
```

`run-batch` preserves each cell's native `T` and `K`, continues after individual failures by default, and incrementally writes `campaign-results.json`. Re-running the same command resumes the ledger and skips every already-recorded cell; create a new campaign directory for a deliberate rerun. Use `--methods goal-plus-codex` to select a subset or `--fail-fast` for debugging. The API base and credentials are deliberately not copied into the campaign result.

Every `prepare-batch` and completed `run-batch` cell also refreshes
`campaign-summary.json` and `campaign-summary.md` in that campaign directory.
These are derived views: `campaign.json`, `campaign-results.json`, and each
cell's raw evidence remain authoritative. The report preserves the evaluator's
raw metric and direction. A positive `directional_gain` means improvement over
the best observed seed in that cell (`final - seed` for maximize, `seed - final`
for minimize); it is not a cross-task normalized score.

To merge arbitrary campaign directories, `campaign.json` files, or individual
run directories without hard-coding task or agent names:

```bash
.bench-env/venv/bin/python scripts/openevolve_report.py \
  runs/openevolve-campaigns/<campaign-a> \
  runs/openevolve-campaigns/<campaign-b> \
  --markdown-out runs/openevolve-reports/comparison.md \
  --json-out runs/openevolve-reports/comparison.json
```

Sources may overlap; run directories are de-duplicated. Repeat `--method` or
`--task` to filter the derived view. New methods and tasks require no reporter
code changes as long as they retain the experiment manifest and evaluation
contracts.

## Execute

Set the key only in the shell and run each prepared directory with the same endpoint/model:

```bash
export OPENAI_API_KEY='<secret>'

.bench-env/venv/bin/python experiments/openevolve_compare/experiment.py run \
  --run-dir runs/openevolve-compare/<run-id> \
  --model gpt-5.6-luna \
  --api-base https://api.example.com/v1
```

When `--api-base` is provided, Codex uses an explicit run-local custom provider over the Responses wire API. When it is omitted, Codex uses native login/auth while the benchmark still injects all Goal Plus MCP and headless-tool configuration explicitly. Pi receives a run-local `models.json` whose credential field is only `$OPENAI_API_KEY`; model prices and tiers are copied from the active or installed Pi catalog. Missing pricing does not block execution and is reported as unavailable rather than zero cost.

The outer controller sends `SIGTERM` at `T`, allows a fixed grace period, and marks a hard kill incomplete. A normal deadline signal is accepted only after deterministic closeout succeeds. `experiment.json`, `final-eval.json`, event logs, Goal Plus state, selected artifact, evaluator call counts, and available usage telemetry remain in the ignored run directory.

Goal Plus completion records all of the following:

- one naturally created, complete Goal Plus record linked to the promoted Search run;
- exactly `K` candidate workspaces and one session per candidate;
- completed worker verifier evidence for every candidate;
- successful controller closeout, promotion/report, and common final evaluation.

The natural Codex path is exercised by the [standard-prompt end-to-end run](../../evidence/runs/2026-07-22-goal-plus-codex-natural-prompt.md). The older [controller-prepared strict rerun](../../evidence/runs/2026-07-22-goal-plus-codex-pi-strict-rerun.md) remains historical diagnostic evidence, not the standard experiment entrypoint.

If the host process is interrupted after workers finish but before Goal Plus selection/reporting completes, recover the same directory without starting another model run:

```bash
.bench-env/venv/bin/python experiments/openevolve_compare/experiment.py closeout \
  --run-dir runs/openevolve-compare/<run-id>
```

Closeout is idempotent: it reuses an existing promotion, applies its patch to the task source at most once, and completes linked Goal Plus records/reporting.

The first real four-path smoke and its telemetry limitations are recorded in [the sanitized evidence summary](../../evidence/runs/2026-07-22-openevolve-four-path-5m-summary.md).

## Interpretation

This is a wall-time/concurrency comparison, not a token-matched causal ablation. Always report:

- final raw benchmark metric and direction;
- actual wall time and whether the deadline fired;
- candidate/iteration/evaluator-call counts;
- top-level and worker token/cost coverage;
- setup exclusions and controller closeout time;
- any retry, incomplete state, or missing telemetry.

Use evaluator-call matching only in a separately labeled mechanism ablation.
