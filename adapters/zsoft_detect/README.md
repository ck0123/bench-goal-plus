# ZSoft detect adapter

Wraps the cybergym-zsoft-detect static detection benchmark checked out at
`third_party/zsoft-bench/benchmarks/vulnerability/zsoft-detect`
(sparse checkout of `gitcode.com/linmalin/muyuan-sec.git` branch
`linmalin-zsoft-benchmarks-mr`,
framework 1.1.0).

The adapter:

- exports the public bench contract with the benchmark's own
  `scripts/show_bench.py` (project + commit pinned; ground truth and
  matching rules stay hidden);
- materializes an agent workspace with a clean source checkout of the
  exact bench commit (HEAD equality enforced by the runner), the bench
  contract, `TASK.md`, and a self-contained `public_check.py`;
- exposes only the `format_valid` public gate, which checks direct regular
  JSON files and the public finding schema without loading the official scorer
  or ground truth;
- uses the lowest candidate id and its latest compliant iteration only for the
  public closeout selection. After all agents exit and closeout completes, the
  trusted controller runs the official `score_submission.py` scorer over every
  publicly compliant committed iteration using release `0.1.0` and track `tp`,
  caches identical artifact snapshots, and publishes the highest-F1 result. F1
  ties choose the lowest candidate id and then that candidate's latest tied
  iteration;
- requires Goal Plus Pi workers to use the adapter-declared Bubblewrap policy:
  only the candidate workspace is mounted, with `source/` read-only, while
  scorer, ground-truth, full histories, official results, and controller runtime
  remain host-only. Workers may read only schema-filtered Global Evidence
  derived from the public `format_valid` verifier and, when enabled, verified
  shared-tool Views;
- reports the official final `f1`, maximize, only after closeout; the raw
  precision/recall/TP/FP/FN payload is preserved under `zsoft_score` in the
  controller-owned final report. There is no aggregate one-call limit: each
  unique immutable snapshot is scored once in an isolated controller runtime.
  Per-iteration scores and selection provenance stay outside the worker
  workspace in `posthoc-candidate-scores.json` and `final-eval.json`.

Constants:

- default project is `civetweb` @ `d7ba35b…`; `configure_task` accepts any
  of the five project ids (`civetweb`, `jiuwenswarm`, `libxml2`,
  `linux-rxrpc-sample`, `umdk`), optionally suffixed `-detect`.
- the campaign records the managed Muyuan checkout commit, while each
  workspace records the independently pinned audited-source revision as
  `source_revision`.

The benchmark's native runner remains separate from Goal Plus candidate
scoring. Its pinned SWE-agent profile is exposed only through the dedicated
`zsoft-detect-swe-agent` target and `zsoft-swe-agent` method; OpenCode and xiaoO
are not registered methods.

ZSoft official evaluation is permanently controller-only and is not a
configurable run mode. The common runner accepts only `goal-plus-pi` for this
adapter; Plain, `goal-plus-codex`, and SkyDiscover methods are rejected before
preparation because they lack the required Bubblewrap worker boundary.

The reproducible-environment bootstrap owns the default sparse checkout.
`BENCH_GOAL_PLUS_ZSOFT_ROOT` may select another clean checkout for controlled
experiments; the path must remain under this repository.
For projects without a public source URL,
`BENCH_GOAL_PLUS_ZSOFT_DETECT_SOURCE_CACHE` may point to an explicitly managed,
clean Git checkout at the pinned project commit. The adapter validates it and
copies its tracked tree into a real workspace `source/` directory without
`.git`; the cache path is not exposed to Pi workers.

## Smoke

```sh
python3 -m unittest tests.test_zsoft_detect_adapter -v
```
