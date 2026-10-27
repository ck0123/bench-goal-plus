# Frontier-Engineering native runner

Target `frontier-engineering` preserves the upstream `v1_lite.yaml` task
contracts and UnifiedTask evaluator. New native campaigns use the corrected
official `Einsia/Frontier-Engineering` `main` checkout at
`third_party/frontier-engineering-arm64` (managed key
`frontier_engineering_official`). The old `frontier-engineering` checkout and
its historical evidence remain available to the legacy MallocLab adapter;
they do not qualify the corrected protocol. Its default `v1-lite-cpu-codex-1h`
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
- Quantum routing uses the official QASM3 subprocess transport and bubblewrap
  candidate PID isolation. The official sampled unitary gate checks zero plus
  four Haar-random inputs, classical measurement mapping and declared layout
  before any score. Fixed-output state preparation and empty circuits are
  invalid. This is the corrected main protocol, not the old v1-arxiv verifier.
  The upstream candidate runner uses its compatibility filesystem mode; do not
  describe PID isolation as a restricted filesystem sandbox.
- Quantum workspaces expose the read-only `task_python.py` runtime entry and
  `structural_optimizer.py` helper to Plain and Goal Plus alike. Use
  `python3 task_python.py -` for stdin experiments or
  `python3 task_python.py .tmp/script.py` for scratch scripts. Official scores still
  come only from `python3 evaluate.py`; only `solve.py` is editable. Fork
  worktrees inherit the same runtime entry. Doctor executes an actual import
  and Statevector probe through that entry.
- Prepare records source commit, `evaluation_protocol`, and the hash of native
  verifier, test cases, metadata, shell scripts and shared sandbox/runner code.
  Evaluation rejects changes to those inputs. Final records preserve the
  protocol tag and evaluator hash, so legacy results cannot silently enter a
  corrected-protocol comparison.
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
- The `quantum-routing-goal-plus-pi-sol-1h-k4-fork` profile explicitly freezes
  `goal_plus_spawn_mode=fork` in both the typed command and manifest. Main first
  prepares its complete Search batch, records `fork_checkpoint_ready`, and ends
  that turn. The controller validates the exact Goal, session, control version,
  preparation event and native checkpoint before resuming the same Main once.
  This handoff shares the original T deadline. It does not automatically resume
  ordinary blocked states, user pauses or interrupted trajectories. Actual fork
  worker evidence remains required before accepting K.
- New multi-worker Goal Plus + Pi cells without an explicit score-based early
  stop freeze `goal_plus_config.parallel_execution.mode=continuous`. The bench
  Pi extension requires resuming returned lanes before another blocking wait
  and reserves healthy-worker shutdown and selection/promotion for the outer
  deadline minus closeout time. Failed invocations may be recovered earlier;
  accepted Goal pause/block control permits cleanup.
  Worker runtime is per invocation, not a cumulative candidate lifetime. Native
  Main receipts produce `parallel-execution.json`: peak K, invocation/continuation
  counts per lineage, and idle gaps (180-second scheduling allowance). Missing
  execution receipts or longer gaps keep the result partial. User stop/pause
  and real dependency blockers are preserved; this is not a core Search scheduler.
  Audit joins Main launches with persisted native dispatch observations using
  run/candidate/session identity and unique invocation request windows. A worker
  need not be polled by Main again after its real terminal observation was saved.
  Binding, cleanup time and the deadline never substitute for execution intervals.
  `finish` re-audits legacy receipt-only partial classifications through the
  existing closeout checks, reuses the saved final evaluation, and preserves the
  previous manifest, campaign and reports with `_bak` suffixes. It does not resume
  workers or remove other completion requirements.
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

Before setup, run the profile inventory. Provision creates only the uv
runtimes required by selected tasks (`frontier-eval-driver`, `frontier-v1-main`,
`frontier-v1-summit`, or the dedicated `frontier-quantum` runtime); it does not
install host packages or unrelated v1 assets. Quantum routing's native spec
pins Qiskit 2.3.1 and MQT Bench 2.3.0, avoiding unrelated optics/Qt dependencies.
It also pins `qiskit-qasm3-import==0.6.0`; Linux requires working bubblewrap
user/PID namespaces, exercised by the official seed evaluation.
The EnergyStorage acceptance profiles freeze `K=1, C=1, R=1`; Plain Pi uses
`T=300`, while Goal Plus + Pi uses the exercised `T=600` closeout budget.
The paper-protocol OpenEvolve profile uses a 12-hour operational `T` guard and
an authoritative fixed budget of 100 iterations; do not compare that guard to
the time-budgeted Agent profiles as if it were their search budget.

## Corrected Quantum profiles

`quantum-routing-plain-pi-sol-1h` uses T=3600/K=1/C=1/R=1.
`quantum-routing-goal-plus-pi-sol-1h-k4-fork` uses T=3600/K=4/C=1/R=1,
fork, the same model and medium reasoning. Both use the corrected official
source; historical campaigns retain their own pinned identity.

Registered acceptance presets are
`frontier-engineering-quantum-routing-plain-pi-sol-smoke` (T=300/K=1) and
`frontier-engineering-quantum-routing-goal-plus-pi-sol-k4-fork-smoke`
(T=600/K=4). These short runs diagnose execution and correctness; they are
not one-hour matched results. A valid score after forced deadline termination
is preserved but the campaign remains partial under the normal completion
contract. Fork preparation consumes the same T budget as worker execution.

```bash
python3 scripts/bench.py check --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h
python3 scripts/bench.py setup --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h --skip-provision
python3 scripts/bench.py plan --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h
python3 scripts/bench.py launch --benchmark frontier-engineering \
  --profile quantum-routing-plain-pi-sol-1h --skip-provision
```

Use the fork profile in the same lifecycle for GP. On a new host, a failed
inventory lists missing source/runtimes; provision them only when installation
is authorized. Quantum readiness evidence does not establish the other eight
CPU tasks or the CUDA task on the new source revision.

The 2026-10-27 ARM64 acceptance used Goal Plus source commit
`3b2cfa89a7a45b71d2574de9650668003e8f9e61`, including the authorized
fork-preparation resume fix. The tested version is published on a separate
branch, preserving the managed default upstream and existing checkouts:

```bash
git clone --branch fix/frontier-fork-preparation-resume --single-branch \
  https://github.com/ck0123/muyuan.git third_party/muyuan-frontier
export BENCH_GOAL_PLUS_SOURCE_DIR="$PWD/third_party/muyuan-frontier/plugins/goal-plus"
export BENCH_GOAL_PLUS_EXPECTED_REF=3b2cfa89a7a45b71d2574de9650668003e8f9e61
```

Set these inputs before check/setup/plan/launch. On an installed host,
`setup --skip-provision` performs doctor without replacing local runtimes.
On a new host, inventory first; authorized `setup` provisions the selected
runtimes and installs the selected Goal Plus source. Provider credentials and
endpoint are inherited as `OPENAI_API_KEY` and `OPENAI_BASE_URL`; model is
`gpt-5.6-sol`, reasoning medium. Never persist their values.

The completed one-hour profiles scored 3.908047300767494 (Plain Pi) and
4.021314277529203 (GP Pi fork). GP has four verified worker lineages,
21 real intervals and peak four. Plain naturally returned after 676.8 seconds;
these are lifecycle acceptance runs, not wall-time-matched performance results.
Reviewed evidence: `evidence/runs/2026-10-27-frontier-quantum-official-1h.json`.
Do not substitute `--skip-bootstrap` for source validation or reset an existing
checkout to obtain this source.

## CPU8 official-protocol rerun on ARM64

`v1-lite-cpu8-plain-pi-sol-1h` excludes Quantum and CUDA RobotArm, selects Plain Pi
with gpt-5.6-sol medium, T=3600/K=1/C=1/R=1, seed 1. Each cell has one outer
trajectory. Natural Agent completion may precede T.

The managed checkout now follows `ck0123/Frontier-Engineering` branch
`fix/malloc-wasm-arm64`, derived from official main
`bafa7d53162a3b1f128fec396bbde22fe972bf24`. Its production changes select
same-version official ARM64 wasi-sdk 34.0 / Wasmtime 48.0.2 assets with pinned
SHA256 and preserve PYTHONDONTWRITEBYTECODE through the sampler child environment
allowlist to avoid self-induced source fingerprint violations; it preserves the Wasm64 allocator, trusted scorer and all other evaluators.
The original official checkout and previous Quantum evidence remain intact.

CPU setup uses `runtime-specs/frontier-v1-main-cpu.json`, includes slmsuite's
reference solver with same-version headless OpenCV, and constrains SUMMIT's
ARM source builds to NumPy 1.x headers. Evaluator subprocesses discard host
PYTHONPATH and disable bytecode writes before official source fingerprints.
Every task exposes the same read-only `task_python.py` diagnostic entry in
Plain and Goal Plus workspaces, respecting interpreter overrides.

Reaction uses AF_UNIX sockets under repository-local scratch. A long checkout
path can exceed the OS socket-name limit; use a short repository/worktree path
(such as `.worktrees/f8`) rather than system temporary directories.
Malloc's hash-verified toolchain is provisioned under repository-local `.tmp/`.
Historical scores must be reported separately from historical-code replays under
the new official evaluator and the new model trajectories.
