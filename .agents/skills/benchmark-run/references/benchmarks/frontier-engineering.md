# Frontier-Engineering native runner

Target `frontier-engineering` preserves the upstream `v1_lite.yaml` task
contracts and UnifiedTask evaluator. Its default `v1-lite-cpu-codex-1h`
profile excludes the CUDA-backed RobotArm task and runs 9 CPU tasks. The full
`v1-lite-codex-1h` profile preserves all 10 tasks but declares
`nvidia-cuda-opt-in`; its doctor must pass `nvidia-smi` and the selected
runtime's PyTorch CUDA probe before evaluating a seed. The legacy target
`frontier-engineering-malloclab` is only a portable one-task regression.

## Completion and evidence

- A cell is complete only when its controller-owned final evaluator returns a
  valid upstream `combined_score` and the selected Agent lane exits cleanly.
- Raw `metrics.json` and `artifacts.json` remain attached to each evaluator
  event; `combined_score` is maximize.
- Goal Plus cells additionally require actual worker/subagent evidence equal to
  `K`; missing evidence keeps the cell and campaign `partial`.
- Plain Pi requires `K=1` and uses one outer Pi trajectory. Goal Plus + Pi
  maps `K` to internal subagents. Both Pi methods require the
  profile model to be visible through the run-local Pi provider. Profile
  `pi_provider` sets `id`, `api`, `api_base_env`, and `api_key_env`; defaults
  remain `bench-openai`, `openai-responses`, `OPENAI_BASE_URL`, and
  `OPENAI_API_KEY`. Doctor, Main, and worker use the same exact model.
  Endpoint and credential values are inherited and never written to campaign
  manifests or reports. The `energy-storage-goal-plus-pi-glm53flash-smoke`
  profile uses Z.AI Flash with `ZAI_BASE_URL` and `ZAI_API_KEY`.
- Goal Plus uses the common typed-command and public apply lifecycle, with
  Git worktrees, autoresearch and no shared-dir. An external clean source
  checkout uses `BENCH_GOAL_PLUS_SOURCE_DIR` plus
  clean checkout (`BENCH_GOAL_PLUS_EXPECTED_REF` optionally pins an exact ref);
plan records its full identity.
- UnifiedTask uses the adapter's shell with host login profiles disabled to
  preserve the evaluator cwd. The shell is part of the frozen evaluator hash.
- Final source is `campaign-summary.json`; `finish` exports `report.md` and the
  campaign-named XLSX.
- The `openevolve` method preserves the upstream Experiment 1 protocol: the
  shipped initial program, frozen UnifiedTask verifier, OpenEvolve 0.2.26
  defaults, and exactly 100 evolution iterations. It requires `K=1, C=1` and
  records one initial-program evaluation separately from the 100 evolved
  candidates. Its `T` is only an operational hard ceiling; a cell is complete
  only when the full iteration ledger and controller final score are present.
- The registered 5-iteration OpenEvolve smoke uses the same upstream search
  path and frozen task assets, but is diagnostic rather than a paper result.
  Its completion ledger is one initial program plus exactly five evolved
  candidates.

## Lifecycle

The target supports native provision, detached execution, stop, and finalization.
It does not resume the same trajectory. A stopped campaign is finalized as
`partial`; a retry uses a new campaign ID.

`K` is Goal Plus task-internal subagent concurrency; non-Goal-Plus methods
require `K=1`. `C` is currently fixed to one because
several v1-lite evaluators are timing- or GPU-sensitive. Do not map Hydra batch
`max_parallel` to `K`.

Before setup, run the profile inventory. Provision creates only the three uv
runtimes used by v1-lite (`frontier-eval-driver`, `frontier-v1-main`, and
`frontier-v1-summit`); it does not install host packages or unrelated v1 assets.
The EnergyStorage acceptance profiles freeze `K=1, C=1, R=1`; Plain Pi uses
`T=300`, while Goal Plus + Pi uses the exercised `T=600` closeout budget.
The paper-protocol OpenEvolve profile uses a 12-hour operational `T` guard and
an authoritative fixed budget of 100 iterations; do not compare that guard to
the time-budgeted Agent profiles as if it were their search budget.
