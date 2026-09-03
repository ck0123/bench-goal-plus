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
- The controller selects with visible-test evidence and calls the hidden
  upstream grader once on the selected result. Hidden score never participates
  in selection.
- `task_success` is the raw boolean metric; `task_success_rate` is the
  maximize-direction aggregate. Upstream `max_attempts` and `case_workers` are
  not mapped to benchmark `K` or `C`.

## T/K/C/R

- `T`: one Plain trajectory or one Goal Plus search wall-clock budget.
- `K`: Plain starts K isolated outer trajectories; Goal Plus starts one main
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
OpenAI-compatible provider variables. Runs are foreground-only and
non-resumable. `finish` consumes terminal evidence without re-running the
official grader.

The initial integration remains `partial` until a real Linux+bwrap campaign is
archived for each method. For `K>1`, the report exposes selected-result success
but deliberately leaves pass@K/pass^K unset because unselected trajectories are
not sent to the hidden grader.
