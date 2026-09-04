# ZSoft vulnerability benchmarks

The repository exposes two ZSoft tracks through the common benchmark adapter
contract, plus a dedicated native SWE-agent execution target for Detect. All
paths use the benchmark-owned scorer or judge; Goal Plus never replaces the
official metric with an LLM rubric.

Both adapters follow the source MR branch
`https://gitcode.com/linmalin/muyuan-sec.git` /
`linmalin-zsoft-benchmarks-mr`. The environment manifest sparse-checks out only
the two benchmark directories and records the resolved commit in each campaign.

## ZSoft Detect

The representative task is `civetweb-detect`. Preparation checks out the pinned
project revision, exports only the public benchmark contract, and creates an
empty `submission/` directory. Agents write one schema-valid JSON finding per
file. Online Goal Plus selection uses only the public `format_valid` gate and a
frozen candidate-id tie-break. After every agent exits and controller closeout
completes, the official deterministic scorer evaluates every publicly compliant
committed iteration; identical artifact snapshots are cached. `final-eval.json`
contains the highest F1 result, with ties resolved by the lowest candidate id
and then that candidate's latest tied iteration. The complete precision,
recall, F1, TP, FP, and FN payload is retained for analysis. Candidate scores
remain controller-only under the run directory and are never written back into
the worker workspace or exposed during search.

Detect uses a directory artifact, so the common runner permits multiple changed
files inside `submission/`. The benchmark repository commit and audited project
revision are recorded separately.

The benchmark harness prepends a bench-owned `pi` shim to `PATH`. Ordinary Pi
invocations pass through unchanged; only Goal Plus worker RPC processes are
intercepted and launched through the fail-closed Bubblewrap boundary. This does
not require any Goal Plus source change or external-launcher protocol. The
candidate workspace is mounted read-only; only the adapter-declared
`submission/` artifact and launcher-owned `.tmp/` are writable, while `source/`
and `schemas/` are explicitly validated and kept read-only. The benchmark
repository, cases, scorer source, sibling runs, `.gp` state, runtime Git history,
and the rest of the host home directory are not mounted. A bench-owned CLI shim
sends the worker's limited Goal Plus calls over a session-bound host socket, so
verifier execution remains outside the sandbox and direct score-bearing
responses stay opaque. The proxy exposes only a field-validated Global Evidence
view derived from the public `format_valid` verifier. This lets workers use safe
peer Evidence and objective Views as reference without exposing ground truth or
official F1. With `--shared-dir`, verified shared tools use the same bounded
proxy path. The policy and environment-variable names, but never their values,
are recorded in the experiment manifest.
Declared read-only workspace entries must be real directories; symlinked source
or public bundles fail closed before worker startup.

### Native SWE-agent path

`zsoft-detect-swe-agent` is a separate executable target backed by runner
`zsoft-detect-native`. Its only method is `zsoft-swe-agent`, which delegates to
the upstream `runners/launch.py swe-agent` implementation. It pins SWE-agent
1.0.1 commit `6aff2155…`, SWE-ReX 1.4.0, LiteLLM 1.93.0, the upstream prompt and
config, Bubblewrap source isolation, host-side metered proxy, and exact
provider usage. OpenCode and xiaoO are deliberately not registered.

This path requires native Linux with Bubblewrap; OrbStack Docker on macOS is
not equivalent. Its `check --profile` command is read-only and reports the
exact clean SWE-agent and audited-source revisions. Full doctor additionally
requires `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, and optionally a
JSON-object `OPENAI_COMPAT_HEADERS_JSON`; values are never persisted.

The initial preset is `zsoft-detect-civetweb-swe-agent-smoke`, freezing
`T=300`, `K=1`, `C=1`, and `R=1`. A run is accepted only when the upstream
launcher completes, exact usage is complete, and the deterministic scorer
returns F1 plus TP/FP/FN. Missing evidence keeps the result `partial`.
The upstream launcher has no explicit reasoning-effort option, so this native
baseline is not eligible for reasoning-matched comparisons even after a
successful run; the recorded profile label is provenance, not an applied knob.

## ZSoft L1

The representative task is `sample-asan-crash`. Preparation exports the public
task bundle and a single `poc` artifact. The benchmark-owned Docker differential
judge evaluates the same submission against vulnerable and fixed builds. The
native metric is binary `success`; it is not averaged with Detect F1.
L1 has the same host-filesystem risk as Detect: each upstream task directory
contains private reference PoCs, negative PoCs, judge code, and fix patches.
Goal Plus Pi therefore uses the same Bubblewrap boundary for L1, exposing only
the candidate workspace with `public/` remounted read-only; private task and
judge directories are not mounted. Unlike Detect, L1 runs the official
differential judge for each submitted process iteration and exposes only its
binary `success` value. A clean settled `success=1` iteration stops exploration
early; controller-owned selection, promotion, and final verification still run.
The frozen worker minimum runtime remains part of the campaign contract, but
workers need not run out that lease after the final judge confirms the live-pass
stop. Without that confirmation, normal minimum-lease completeness still applies.

## Run

Bootstrap the sparse ZSoft and Goal Plus checkouts, then use the common campaign
controller with `--benchmarks zsoft-detect` or `--benchmarks zsoft-l1`. Docker is
mandatory for L1 and is not required by the common Detect scorer. Credentials,
provider URLs, campaign outputs, and fetched source trees are not repository
artifacts and must not be committed.

For the native Detect baseline, use only the unified preset lifecycle:

```bash
python3 scripts/bench.py check --preset zsoft-detect-civetweb-swe-agent-smoke
python3 scripts/bench.py setup --preset zsoft-detect-civetweb-swe-agent-smoke
python3 scripts/bench.py plan --preset zsoft-detect-civetweb-swe-agent-smoke
```

Provision is permitted only after the read-only inventory reports exact
missing assets and acquisition is explicitly requested.

Select a non-default Detect project with `--task-id`, for example
`--benchmarks zsoft-detect --task-id libxml2-detect`. A task selector applies to
exactly one benchmark and is persisted in both the campaign and cell manifests.

Adapter-level smoke tests:

```bash
python3 -m unittest \
  tests.test_zsoft_detect_adapter \
  tests.test_zsoft_l1_adapter
```
