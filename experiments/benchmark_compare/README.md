# Standalone benchmark Plain Codex / Goal Plus + Codex/Pi runner

This runner applies one experiment contract to the standalone benchmark cases
that already have a portable, controller-owned evaluator. It is intentionally
not another benchmark framework: every adapter materializes one upstream task,
one editable artifact, and its native raw metric; the common runner only owns
Codex/Pi launch, Goal Plus launch, wall deadline, concurrency, and evidence.

## Supported task IDs

| `--benchmark` | Editable artifact | Native metric | Docker | Docker space | Current host requirement |
|---|---|---|---|---|---|
| `ale-bench-lite` | `solution.cpp` | `overall_absolute_score` minimize | **必需** | C++ + judge 逻辑 `4.03 GB`；预留 `10 GB` | Images `ale-bench:cpp20-202301` and pinned Rust judge |
| `autolab-toy-isa` | `program.s` | `cycles` minimize | 不需要 | `0 GB` | C compiler and `make` |
| `frontier-cs-problem-0` | `solution.cpp` | `checker_score_percent` maximize | **必需** | `1.27 GB`；预留 `2 GB` | Image `bench-goal-plus/frontier-cs-judge:07500f9` |
| `frontier-engineering-malloclab` | `mm.c` | `combined_score` maximize | 不需要 | `0 GB` | C compiler and `make` |
| `heurigym` | `solver.py` | `total_cost` minimize | 不需要 | `0 GB` | Python only after dataset bootstrap |
| `local-vliw` | `solution.py` | `cycles` minimize | 不需要 | `0 GB` | Python standard library；local replica，非官方 EdgeBench |
| `zsoft-detect` | `submission/` | `format_valid` public; `f1` final-only | 不需要 | `0 GB` | pinned project source + controller-only deterministic scorer |
| `zsoft-l1` | `poc` | live binary `success` | **必需** | 依任务镜像和源码缓存而定 | controller-owned Docker Compose vulnerable/fixed differential judge |

Every upstream checkout lives in the ignored `third_party/` directory. Run a
task-specific bootstrap instead of cloning beside the repository:

```bash
python3 scripts/repro_env.py bootstrap --only autolab
python3 scripts/repro_env.py doctor --only autolab
```

ZSoft Detect keeps official F1 controller-only and invisible during Search.
Workers see the public task material and `format_valid` checker, while the Pi
tool proxy returns minimal candidate-local context, opaque direct verifier
receipts, and a schema-filtered Global Evidence view of settled public-verifier
Evidence from every candidate. Objective peer Views are available as reference;
when `--shared-dir` is enabled, verified shared-tool metadata and bounded
stage/copy operations are available through the same session-bound proxy. Full
candidate histories, official metrics and commands, ground truth, benchmark
roots, peer workspaces, and transcripts remain controller-only. After
deterministic public-compliance selection and complete Goal Plus
promotion/closeout, the controller makes one official final call. L1 instead
runs the controller-owned judge for each iteration, exposes only binary
`success`, and stops exploration after the first clean settled pass. Private
judge inputs and raw results remain outside the Bubblewrap worker. Use the
Bubblewrap-backed `goal-plus-pi` method for the protected ZSoft paths; other
methods are rejected before workspace preparation.

The upstream keys are `ale_bench`, `autolab`, `frontier_cs`,
`frontier_engineering`, `heurigym`, and `zsoft_l1`. OpenEvolve and Goal Plus
are always included because they are always-managed shared runtimes.

`local-vliw` 不使用 `third_party/` benchmark checkout；它从仓内
`local_examples/vliw_kernel_optimization` materialize。Goal Plus runtime 仍
来自 branch-managed `third_party/muyuan` 的 `plugins/goal-plus`。其 manifest 固定标记
`source_kind=local_example` 和 `official_benchmark_comparable=false`；
workspace evaluator report 另保留 `official_edgebench_comparable=false`。

The Docker-backed adapters (`ale-bench-lite` and `frontier-cs-problem-0`) and
the host-CUDA `torchbench` adapter launch Codex with `danger-full-access` and
explicit `approval_policy=never`: a workspace sandbox cannot access the host
Docker socket or GPU devices. Other adapters keep `workspace-write`. This
sandbox choice is written to the run manifest; use these host-resource cases
only on an isolated benchmark host or VM.

## Prepare, inspect, and run

The same command shape works for every table row:

```bash
.bench-env/venv/bin/python experiments/benchmark_compare/experiment.py prepare \
  --benchmark autolab-toy-isa --method plain-codex \
  --wall-time-seconds 360 --soft-closeout-seconds 60 \
  --worker-runtime-seconds 120 --concurrency 2 --model gpt-5.6-sol

.bench-env/venv/bin/python experiments/benchmark_compare/experiment.py prepare \
  --benchmark autolab-toy-isa --method goal-plus-codex \
  --wall-time-seconds 360 --soft-closeout-seconds 60 \
  --worker-runtime-seconds 120 --concurrency 2 --model gpt-5.6-sol

.bench-env/venv/bin/python experiments/benchmark_compare/experiment.py prepare \
  --benchmark autolab-toy-isa --method goal-plus-pi \
  --wall-time-seconds 360 --soft-closeout-seconds 60 \
  --worker-runtime-seconds 120 --concurrency 2 --model gpt-5.6-sol
```

`goal-plus-pi` 使用同一个 materializer、task prompt 和 evaluator，但把 worker
host 固定为 `pi-rpc`。运行时必须显式传 `--api-base`；run-local
`pi-home/models.json` 只引用宿主环境中的 `$OPENAI_API_KEY`，不持久化密钥。

These historical commands keep their previous behavior. For a claimable B3 or
B4 ablation cell, pass `--condition B3 --coordination-variant way2` or
`--condition B4 --coordination-variant way1`; the runner then requires the
matching `observe` or `enforce` Search Space to exist before marking the cell
finished. Use `experiments/benchmark_campaign/experiment.py` for a paired
B0/B1/B3/B4 matrix.

Preparation prints a new ignored run directory. For Goal Plus, confirm that
`workspace/.gp` is absent before `run`; Goal Plus state is created only by the
timed host-native prompt (`$goal-plus` for Codex, `/goal-plus` for Pi). Seed
evaluation uses a controller runtime
outside that workspace, so `.bench-runtime/` is not copied into Goal Plus
candidate Git histories. A model-free evaluator check is available:

```bash
.bench-env/venv/bin/python experiments/benchmark_compare/experiment.py seed-smoke \
  --run-dir runs/benchmark-compare/<run-id>

.bench-env/venv/bin/python experiments/benchmark_compare/experiment.py run \
  --run-dir runs/benchmark-compare/<run-id> --model gpt-5.6-sol
```

Do not mix `seed-smoke` into a strict campaign ledger because it intentionally
claims another public evaluator call. Failed and partial run directories are
preserved; create another run ID instead of deleting or reusing one.

## Budget sizing

`T` is a total method budget, not a fixed number of rounds. Goal Plus includes
intake, triage, spec freeze, candidate creation, worker launch, and search in
that same `T`. In the measured AutoLab smoke, `T=240s` launched both workers
too late for either to verify, while `T=360s` leaves a usable 120-second worker
window. That shorter run is diagnostic evidence, not a valid Goal Plus result.

Start with these wiring budgets on the current Mac, then freeze one matched
campaign budget per task:

| Task | Suggested wiring `T / closeout / worker` |
|---|---|
| AutoLab / HeuriGym | `360 / 60 / 120` seconds |
| Local VLIW replica | `360 / 60 / 120` seconds |
| Frontier-Engineering MallocLab | `420 / 60 / 180` seconds |
| Frontier-CS problem 0 | `480 / 90 / 180` seconds |
| ALE-Bench Lite AHC027 | `480 / 60 / 180` seconds after cache warm-up |

ALE's first post-bootstrap evaluation took 145 seconds while building and
caching the official Rust tools; the next identical five-case evaluation took
11.08 seconds. The cache lives under ignored `.bench-env/cache/`, not in an
agent workspace. Its adapter still freezes a 180-second Goal Plus verifier
timeout so a cold host fails explicitly instead of being mistaken for a bad
candidate. Preserve cold setup time, actual evaluator calls, and timed search
wall time separately; these wiring budgets are not paper-ready matched budgets.
The first completed generic Goal Plus run used `T=480s,K=2`, obtained durable
results from both workers, and promoted score `52,693,209` from seed
`55,181,186` after 9 process iterations.

Frontier-CS problem 0 takes about 14-16 seconds per compile/run/checker call.
The upstream reference uses clock-seeded search, so repeated seed scores vary
slightly (about 92.8-93.1 in the local smokes). A formal campaign must repeat
final evaluation or require deterministic candidate programs; a single noisy
reference score is only wiring evidence.
The completed host-capable smokes used Plain `T=180s,K=2` (final
`93.4561753`, 12 calls) and Goal Plus `T=420s,K=2` (7 process iterations,
search best `93.3980341`, promotion `93.2217282`, independent final
`93.3097979`, 10 calls). The score drift is why these are wiring results rather
than a method comparison.

Plain Codex uses `K` isolated lanes and selects by the adapter's declared
metric direction. Goal Plus uses `K` candidate lineages and must produce `K`
bound sessions plus at least one worker-submitted verifier result for a wiring
smoke to count as complete. Deterministic closeout cannot turn an unverified
worker session into a passing run.
