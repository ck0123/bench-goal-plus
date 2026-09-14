# aibench coding native integration

This target adapts the `benchmarks/coding` source from the managed aibench fork.
It preserves upstream case materialization and `grade_case` as the official
hidden evaluator while reusing bench-goal-plus orchestration for four methods:
Plain Codex, Plain Pi, Goal Plus + Codex, and Goal Plus + Pi.

## Boundary

- The controller first materializes the complete upstream case in trusted
  scratch space. Files named by `grader.protected_paths` are copied into the
  ignored controller bundle `.bench-runtime/public-tests`; they are not present
  in the Git-tracked `submission/` given to an Agent.
- Public tests remain inspectable at `/aibench-public-tests`, but Linux
  Bubblewrap mounts that bundle read-only for the outer Agent and, for Goal Plus
  + Pi, for every nested worker. The outer boundary uses an explicit host
  filesystem allowlist and never mounts the managed aibench checkout.
- Each public evaluation verifies the controller-pinned boundary digest and
  per-file hashes, rejects a candidate that creates a reserved test path, and
  combines the candidate submission with the original public tests in a
  disposable evaluation directory. The outer Agent environment is not passed
  to the test process.
- The official evaluator independently rematerializes the clean case, restores
  the original protected files around the selected candidate, and calls
  upstream `grade_case` once. Hidden tests are injected only in this
  controller-owned workspace. Hidden score never participates in selection.
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
python3 scripts/bench.py check --preset aibench-coding-smoke
python3 scripts/bench.py plan --preset aibench-coding-smoke
```

Use `--benchmark aibench-coding --profile smoke` instead of the preset only
when overriding its methods, model, budget, concurrency, or seeds.

`setup`/`doctor` requires Linux, Bubblewrap, the exact managed source branch,
the locked aibench grading runtime, selected Agent binaries, and inherited
provider variables. Codex uses OpenAI Responses; Pi profiles may use either
OpenAI Responses or Anthropic Messages. Runs are foreground-only and
non-resumable. `finish` consumes terminal evidence without re-running the
official grader.

The initial integration remains `partial` until a real Linux+bwrap campaign is
archived for each method. For `K>1`, the report exposes selected-result success
but deliberately leaves pass@K/pass^K unset because unselected trajectories are
not sent to the hidden grader.
