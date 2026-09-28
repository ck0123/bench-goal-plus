# aibench coding native integration

This target adapts the `benchmarks/coding` source from the managed aibench fork.
It preserves upstream case materialization and `grade_case` as the official
hidden evaluator while reusing bench-goal-plus orchestration for four methods:
Plain Codex, Plain Pi, Goal Plus + Codex, and Goal Plus + Pi.

## Boundary

- The Agent sees only the materialized `submission/`, the task prompt, and the
  visible tests. A Linux Bubblewrap boundary masks the whole managed aibench
  checkout, including hidden tests and gold files, from the outer Agent and all
  descendants.
- Selection uses visible-test evidence. Goal Plus can finish its public Search
  lifecycle normally; the controller calls the hidden upstream grader once on
  the selected result. Hidden score never participates in selection.
- `task_success` is the raw boolean metric; `task_success_rate` is the
  maximize-direction aggregate. Upstream `max_attempts` and `case_workers` are
  not mapped to benchmark `K` or `C`.

## T/K/C/R

- `T`: one Plain trajectory or one Goal Plus search wall-clock budget.
- `K`: Plain requires K=1; Goal Plus starts one main
  session with K internal subagents sharing one Search state.
- `C`: concurrent task cells in this native campaign controller.
- `R`: independent seeds.

The final report records observed outer trajectories, observed Goal Plus
subagents, evaluator calls, usage coverage, the upstream revision, and whether
each cell is eligible for matched comparison. A K mismatch or missing isolation
evidence makes the cell and campaign `partial` without discarding its score.

## Lifecycle

Use only the unified entrypoint:

```bash
python3 scripts/bench.py catalog
python3 scripts/bench.py plan --benchmark aibench-coding --profile smoke \
  --method plain-codex --model bench-openai/gpt-5.6-sol \
  --wall-time-seconds 300 --live-search-concurrency 1 \
  --cell-concurrency 1 --seed 1
```

`setup`/`doctor` requires Linux, Bubblewrap, the exact managed source branch,
the locked aibench grading runtime, selected Agent binaries, and inherited
provider variables. Pi profiles support OpenAI-compatible Responses/Completions
and Anthropic-compatible Messages; Codex profiles require OpenAI Responses. Runs are foreground-only and
non-resumable. `finish` consumes terminal evidence without re-running the
official grader.
Judge-enabled Goal Plus runs additionally require the managed runtime capabilities
`goal_plus.controller_exact_selection.v1` and `goal_plus.controller_owned_closeout.v1`;
older runtimes are rejected before worker launch.

The `goal-plus-pi-glm53flash-smoke` profile uses `zai/glm-5.3-flash`, low,
Completions, T=900, K=1, C=1, R=1, and inherited `ZAI_BASE_URL`/`ZAI_API_KEY`.
Pi retains public `visible_test_score` feedback; hidden `task_success` is
controller-only and does not enter Search. Codex profiles require Responses.
The exact Pi model metadata is projected from the host catalog into the isolated
runtime. An external Goal Plus checkout can be selected with
`BENCH_GOAL_PLUS_SOURCE_DIR` and `BENCH_GOAL_PLUS_EXPECTED_REF`; setup validates
its clean revision and prepare records that identity for execution checks.
Each Pi cell receives a private writable socket directory under `.tmp/`.
Isolated candidates use `search_run_verifier` (legacy host alias
`goal_plus_search_run_verifier`) to execute public tests in the
host grading environment; `python3 evaluate.py` is the Main/Plain entrypoint.

The initial integration remains `partial` until a real Linux+bwrap campaign is
archived for each method. For `K>1`, the report exposes selected-result success
but deliberately leaves pass@K/pass^K unset because unselected trajectories are
not sent to the hidden grader.

The managed CodingBench source is tracked by branch and fingerprint. The
openEuler PR #5 tree and the registered coding-benchmark fork have the same
`benchmarks/coding` tree; the adapter does not rewrite task prompts or inject
hidden requirements. Upstream task metadata such as `review_status` and
`validity_issues` is copied into `task.json` and evidence so an unpublished or
ambiguous case remains auditable instead of being made easier by the controller.

## Optional final candidate judge

This plug-in is wired into the AIBench native `benchmark_compare` controller path;
other native runners keep their existing closeout behavior unless they opt in.

Goal Plus can use a controller-only tie-breaker after the hard/process gate:

```bash
export GOAL_PLUS_JUDGE=off                 # default
# or: jev / llm-as-a-verifier
```

The switch is accepted only by `goal-plus-codex` and `goal-plus-pi`; plain
methods fail before launch when it is enabled.

`jev` uses a Decisions-API-compatible endpoint. By default it targets
OpenRouter with `OPENROUTER_API_KEY`; set `GOAL_PLUS_JEV_ENDPOINT` and
`GOAL_PLUS_DECISIONS_API_KEY_ENV` to use another provider's endpoint and key
environment variable. The older `GOAL_PLUS_JEV_API_KEY_ENV` spelling remains
accepted for compatibility. `GOAL_PLUS_JEV_MODEL` and
`GOAL_PLUS_JUDGE_TIMEOUT_SECONDS` remain optional. The key value is never
written to the campaign manifest.
`llm-as-a-verifier` loads the optional `llm_verifier` package and uses its
dedicated `GOAL_PLUS_LLM_VERIFIER_*` settings (a dedicated key must be paired
with `GOAL_PLUS_LLM_VERIFIER_BASE_URL`), or an OpenAI-compatible/native backend
key already present in the controller. Use a separate judge key when strict
controller/worker credential separation is required. The
controller runtime must provide the optional `llm_verifier` package; otherwise
the enabled run is recorded as incomplete rather than silently falling back.
Install the audited package in the controller environment from commit
`8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770`; the plug-in verifies its version
and package-source hashes before making a provider call.
The judge runs once per Search run on candidates tied for the best hard/process
score. Its input contains the controller-generated immutable Git diff and the
bounded public verifier result (`process_passed`, return code and test counts),
never hidden-grader output or an Agent assertion. The receipt binds the exact
candidate pool, iteration, settlement, artifact hash and input digest; a stale
or failed receipt makes the cell incomplete rather than falling back. It cannot
send feedback to workers or request another search round.
Native Goal Plus Evidence Annotation is disabled while either judge mode is on;
the judge is the only optional quality selector in that run.
`off` performs no network call. Judge-specific credentials are never written to
the manifest and are removed before worker launch; the Agent's own provider
credential remains available to that Agent. `llm-as-a-verifier` is pinned to the
audited upstream source commit and uses a fresh per-invocation cache so its PPT
ring and pivot phases aggregate the same scores. The receipt distinguishes
`selector_invocations`, `comparisons`, and unknown `provider_calls`; Jev choice
probabilities and LLM-as-a-verifier PPT scores are labeled with different
semantics and are not compared numerically.
