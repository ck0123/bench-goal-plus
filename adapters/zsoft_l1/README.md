# ZSoft L1 PoC adapter

Wraps the CyberGym ZSoft L1 PoC framework checked out at
`third_party/zsoft-bench/benchmarks/vulnerability/zsoft-l1`
(sparse checkout of `gitcode.com/linmalin/muyuan-sec.git` branch
`linmalin-zsoft-benchmarks-mr`,
framework version 0.1.0).

Model providers: `run_all_l1_cases.sh` selects DeepSeek (default) or GLM
(Zhipu) via `L1_PROVIDER` (`L1_MODEL` overrides the model; GLM defaults to
`glm-4.6` at `https://open.bigmodel.cn/api/paas/v4` with
`openai-completions`; set `L1_PI_API=anthropic-messages` with a
`GLM_BASE_URL` like `https://api.z.ai/api/anthropic` for the Anthropic-style
endpoint). The worker network relay preserves either path prefix, matching
the verified zsoft-detect GLM wiring.

The adapter:

- calls the benchmark's own `zsoft_poc` CLI via `PYTHONPATH=src` (no `pip
  install`, zero mutation of the benchmark tree);
- materializes one Git workspace per task: the task's exported `public/`
  bundle, a placeholder `poc` artifact, `TASK.md`, `AGENTS.md`, and a
  self-contained `public_check.py`;
- keeps `public_check.py` as a local format gate, then runs `python3 -m
  zsoft_poc evaluate <task-id> <file> --submission-kind final` as the
  controller-owned process verifier for every submitted Goal Plus iteration;
- freezes the selected task's official evaluator timeout plus the existing
  CLI/cleanup margin for its process verifier: the 180-second ASan sample uses
  360 seconds, while a 900-second task uses 1080 seconds;
- records only binary `success` in Search evidence. Once a clean, settled
  iteration records `success=1`, the campaign controller stops exploration,
  selects and promotes that verifier-backed candidate, and runs the final
  judge again. A configured worker minimum runtime remains frozen and audited,
  but its completion requirement is waived only when that controlled stop is
  confirmed by the controller-owned final judge;
- requires Goal Plus Pi workers to use Bubblewrap with only the candidate
  workspace mounted and `public/` read-only; upstream `private/`, judge, and
  reference-PoC files and raw judge output remain host-only. Worker-visible
  verifier and Global Evidence responses contain the binary `success` signal,
  objective Views, and, when enabled, verified shared-tool metadata;
- preserves the full EvaluationResult only under `zsoft_result` in the
  controller-owned final report. Parallel process evaluations use isolated
  staging directories, and a second final claim is rejected before the judge
  is invoked.

Constants:

- `TASK_ID` defaults to `sample-asan-crash`; `configure_task` selects any of
  the 33 task directories (27 formal + 3 samples + kernel tasks).
- the campaign records the managed Muyuan checkout commit, while the pinned
  per-task subject ref is recorded in each workspace as `source_revision`.

Docker is required (`docker compose` must be available). Task preparation must
produce the upstream image references before campaign preparation. Missing
images or Bubblewrap remain environment failures; model-free unit checks do
not prove the protected worker or Docker judge lifecycle.

The reproducible-environment bootstrap owns the default sparse checkout.
`BENCH_GOAL_PLUS_ZSOFT_ROOT` may select another clean checkout for controlled
experiments; the path must remain under this repository.

The official judge remains controller-owned, while its binary result is live
Search feedback. The common runner accepts only `goal-plus-pi` for this adapter,
so every candidate worker uses the protected Bubblewrap execution path.

## REA tools (opt-in)

`L1_REA=1 ./run_all_l1_cases.sh ...` (root override: `L1_REA_ROOT`, default
`/home/tangzhiyun/workspace/rea`) exports `BENCH_GOAL_PLUS_REA_ROOT` and
`BENCH_GOAL_PLUS_ZSOFT_L1_REA_TOOLS=1`. The shared Pi worker launcher then
bind-mounts the REA package read-only at `/opt/rea` inside every worker
sandbox (`rea` CLI via `/opt/bench-goal-plus/bin/rea`; scratch compiles go to
the private `/tmp`), and the adapter materializes the Git-tracked
`.agents/skills/rea-analysis` Skill into each workspace so Goal Plus instructs
workers to use REA for binary-level verification while crafting the PoC.
Default campaigns (`L1_REA=0`) keep both exports unset and copy no Skill.

## Worker network isolation (LLM cannot reach the network)

Every `goal-plus-pi` worker session is intercepted by the shared bench Pi
shim and launched through the Bubblewrap sandbox: an unshared network
namespace with loopback only, stub DNS/hosts, and a single egress — the
api-only LLM relay (`127.0.0.1:8787` inside the sandbox, forwarded over a
private Unix socket to the one configured model API upstream). Workers
therefore cannot fetch upstream source repositories, CVE write-ups, or
ready-made PoCs from the network; the campaign manifest records this as
`pi_worker_sandbox.network_isolation = "loopback-relay-api-only"`. The
Goal Plus final-checker session is host-side but its Pi tool allowlist is
read-only (`read`, `goal_plus_status`, `goal_plus_submit_final_check`), so it
has no network-capable tool either.

The run script enforces the prerequisites before any campaign starts
(same hardening as `zsoft-detect`): a private short `XDG_RUNTIME_DIR`, the
repo venv plus `~/.local/bin` on `PATH`, `bwrap` resolvable, and `node` as a
real binary under `/usr` or `~/.local/bin` (the in-sandbox `llm-relay` and
the Pi runtime are node programs; an `~/.nvm` symlink is invisible inside the
sandbox). Setting `BENCH_GOAL_PLUS_SOURCE_DIR` +
`BENCH_GOAL_PLUS_EXPECTED_REF` pins the Goal Plus runtime checkout to a ref
that carries the worker-sandbox contract; without them the managed
`third_party/muyuan` checkout is used. After a run,
`scripts/audit_network_access.py runs/benchmark-campaigns-v3` (or any
directory holding `l1-*`/`detect-*` campaigns, or one campaign dir directly)
re-checks worker egress evidence; exit code 0 means clean.

Host-side controller sessions are deliberately outside the worker sandbox:
besides the read-only final checker, the Goal Plus evidence annotator runs as
a one-shot structured-inference Pi process (`--mode json --print
--no-session --no-builtin-tools`) with a fixed annotation-only tool
allowlist. The bench Pi shim recognizes that exact launch signature and
passes it to the real binary, while every other non-RPC `GOAL_PLUS_PI_ROLE=
worker` launch still fails closed ("must use RPC mode"). Candidate workers
are therefore always sandboxed RPC sessions, and the annotation layer keeps
its objective peer descriptions instead of dying with
`PermanentAnnotationError`.

## Smoke

```sh
python3 -m unittest tests.test_zsoft_l1_adapter -v
```

For model runs use the comparison runner in
[`experiments/benchmark_compare`](../../experiments/benchmark_compare/README.md)
with `--benchmark zsoft-l1`.
