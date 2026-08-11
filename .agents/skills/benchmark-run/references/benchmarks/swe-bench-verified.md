# SWE-bench Verified runner reference

## Selected contract

- Target: `swe-bench-verified`
- Runner: `swe-bench-native`
- Initial task: `sympy__sympy-16886`
- Raw metric: official `resolved` boolean, direction `maximize`
- Methods: `plain-codex`, `plain-pi`, `goal-plus-codex`, `goal-plus-codex-pi`, and `goal-plus-pi`
- Topology: Plain methods use one isolated outer trajectory. Goal Plus uses one outer main session
  with bound internal workers on the selected Codex or Pi host. Standard acceptance restricts
  most smoke methods to `K=1,C=1,R=1`; the profile-frozen Pi-worker path supports
  `K<=4,C<=2,R=1`
- Final source JSON: `campaign-summary.json`

The official harness owns final scoring in a separate container. The runner has no host-only score,
provision, stop, or resume. It supports detached execution, retained debug containers, and
profile-frozen cross-cell concurrency up to `C=2`.

## Presets

| Preset | Model | T/K/C/R | Auth |
| --- | --- | --- | --- |
| `swe-bench-verified-sympy-16886-codex-smoke` | `gpt-5.6-sol`, medium | `1800/1/1/1` | profile-frozen `OPENAI_BASE_URL` + `OPENAI_API_KEY`, Responses |
| `swe-bench-verified-sympy-16886-pi-smoke` | `zai/glm-5.2`, medium | `1800/1/1/1` | inherited `ZAI_API_KEY` |
| `swe-bench-verified-sympy-16886-goal-plus-pi-smoke` | `zai/glm-5.2`, medium | `1800/1/1/1` | inherited `ZAI_API_KEY` |
| `swe-bench-verified-sympy-16886-goal-plus-pi-luna-high-smoke` | `bench-openai/gpt-5.6-luna`, high | `1800/1/1/1` | profile-frozen `OPENAI_BASE_URL` + `OPENAI_API_KEY`, Responses |
| `swe-bench-verified-indices-39-goal-plus-pi-sol-deepseek-k4-c2` | Pi Main/View `bench-openai/gpt-5.6-sol`, Pi workers `deepseek/deepseek-v4-flash`, medium | `1800/4/2/1` | `OPENAI_BASE_URL` + `OPENAI_API_KEY`; inherited `DEEPSEEK_API_KEY` |
| `swe-bench-verified-sympy-16886-goal-plus-codex-smoke` | `gpt-5.6-sol`, low | `300/1/1/1` | native Codex lifecycle smoke |

The Pi credential value is never serialized. Docker receives only the selected environment variable
name. The complete dataset row is host-side evaluator input; the Agent receives only the public
issue allowlist.

The Luna profile materializes a campaign-local Pi `models.json` containing only the endpoint and
`$OPENAI_API_KEY` environment reference. A Linux loopback endpoint uses the same repository-owned
socket bridge as Plain Codex; doctor must pass host Responses, task-container Responses, and Pi's
exact `bench-openai/gpt-5.6-luna` model listing before launch.

The 39-task pure Pi profile uses the same isolated Pi Responses route for the outer MainAgent and
independent Pi ViewAgent, while four bound Pi worker sessions use DeepSeek through the allowlisted
built-in-provider proxy. Launch and closeout receive both credential environment names without
serializing either value. Supplemental evaluation is required, Global Evidence is `auto`, and the
profile does not require a `share_dir` option.

Supplemental ViewAgent evaluation is an optional run condition. The baseline publishes only the
candidate evidence description. When enabled, the same independent ViewAgent also publishes fresh
open-ended dimensions from the immutable public task context and current cumulative diff, plus
non-directional comparisons to other candidates' settled hard-score incumbents. FrozenSpec never
contains a soft rubric, and official `resolved` remains the sole hard result. A missing or malformed
enabled output, disabled-condition output leakage, or incomplete ViewAgent task makes Goal Plus
evidence incomplete and therefore the campaign `partial`, while preserving a valid official raw
metric.

The Goal Plus + Codex pair uses the same Responses endpoint and key contract as Plain Codex and
does not mount OAuth. Matched baseline/enabled prompts, task, evaluator, model, reasoning, and
`T/K/C/R` must be byte-identical; only the supplemental-evaluation environment boolean differs.
Keep task/model-specific experiment profiles in ignored campaign evidence rather than the tracked
runner catalog. Promote a profile into the catalog only when it is a reusable lifecycle smoke.

At `K>1`, dynamic peer comparison is a mechanism experiment, not a matched `K=1` quality result.
MainAgent must create exactly `K` distinct initial candidates in one batch and bind one worker to
each. Completion additionally requires overlapping runtime lease intervals, at least one ViewAgent
output whose comparison basis contains another candidate's settled incumbent, and a persisted
Global Evidence read showing a worker consumed a completed peer View before its next verifier
attempt. Merely recording `budget.max_parallel=K` is incomplete evidence.

When a profile freezes `agent_network_policy=public-egress-blocked`, the runner
creates a campaign-owned Docker `--internal` bridge, binds the fixed loopback provider proxy to its
gateway, and attaches the Agent container to that network. Launch fails closed unless Docker inspect
reports the exact network and a direct public-IP connection probe is blocked; the model Responses
probe must still pass through the gateway bridge. Preserve the network verification and cleanup
disposition in final evidence. This prevents public web lookup without hiding the configured model
endpoint or changing the official evaluator network.
The profile-locked Goal Plus runtime may use a temporary default-bridge attachment during
controller-owned setup, before any Agent/model process starts. The runner must disconnect that
attachment and prove the internal network is the container's sole remaining network before the
Responses probe or Agent invocation; persist this setup-egress disposition with the isolation probe.
The timed Agent process also receives a loopback refusal proxy, with the campaign gateway in
`NO_PROXY`, so ordinary HTTP/Git lookup attempts fail promptly while the Docker internal network
remains the fail-closed boundary. One native campaign accepts exactly one positive attempt seed;
persist it in the profile snapshot, prompt strategy config, campaign manifest, and report.

The visible-test wrapper is a benchmark-owned read-only bind mount inside `/testbed`. Freeze records
its exact hash. The ranking verifier directly uses `--ranking-signal`, allowing a completed public
test failure to become a valid zero baseline for freeze preflight. The separate promotion verifier
must directly invoke the same wrapper without that flag, so a zero visible score exits nonzero and
cannot pass the hard promotion gate. Outer shell/Python exit-code suppressors are rejected by the
completion contract. MainAgent must use the repository-native runner already present in the task
image; missing pytest/plugins/dependencies are invalid verifier configuration, not candidate quality.

The archived Linux/amd64 `sympy__sympy-16886` smokes pass the complete `K=1,C=1`
official-harness contract for
[Plain Codex](../../../../../evidence/runs/2026-08-02-swe-bench-verified-plain-codex-sol/summary.json),
[Plain Pi](../../../../../evidence/runs/2026-08-02-swe-bench-verified-plain-pi-glm/summary.json),
and [Goal Plus + Pi](../../../../../evidence/runs/2026-08-03-swe-bench-verified-goal-plus-pi-luna/summary.json).
The two Plain development smokes preserve their dirty-at-prepare provenance and later acceptance
commit instead of rewriting it. `K>1`, other instances, and split-wide readiness remain separate
claims.

## Completion evidence

A cell is score-complete only when all of the following are present:

1. the Agent container was isolated before evaluation, either by confirmed removal or by confirmed
   stopped retention requested through `--retain-containers`;
2. a non-empty binary/full-index model patch was exported;
3. exactly one official evaluator attempt was recorded;
4. the official per-instance `report.json` contains a boolean `resolved` field;
5. raw `resolved` and `patch_successfully_applied` values are preserved.

An unresolved result is a valid completed score. Missing patch, missing report, unconfirmed
container isolation, or a second evaluator attempt is `partial` or `failed`, not a zero-filled
success.

For Goal Plus methods, score completion additionally requires exported durable state proving exactly
one terminal Goal Plus record and linked promoted Search run, a frozen spec with
`budget.max_parallel=K`, the method's bound `codex` or `pi-rpc` worker topology, the frozen
worker/closeout budgets, exactly `K` candidates, one bound worker session per candidate,
worker-origin verifier evidence for every candidate,
any profile-frozen minimum worker budget plus its satisfied runtime lease,
the registered visible-test wrapper with the benchmark-owned frozen hash, a passing promotion
`visible_test_score=1.0`, and no active Pi pool job. When the profile enables the
ViewAgent, every candidate iteration must
have a completed Global Evidence description and an immutable original-task context snapshot. ON
additionally requires 1–8 open dimensions and peer comparisons exactly matching the task snapshot;
OFF requires supplemental output to be absent. Both require FrozenSpec to contain no legacy soft
rubric. The controller exports `/testbed/.gp` before container disposal.
Missing Goal Plus evidence downgrades the cell to `partial` while preserving any complete official
raw score.

## Debug container retention

The exact task image is always retained: the controller invokes the official harness with
`cache_level=instance`, `clean=false`, and `force_rebuild=false`, and it never calls `docker rmi`.
Normal campaigns remove the Agent container after exporting the patch. For an inspectable Agent
filesystem, pass the same option to both plan and launch:

```bash
python3 scripts/bench.py plan \
  --preset swe-bench-verified-sympy-16886-codex-smoke \
  --campaign-id swe-debug-example \
  --retain-containers
python3 scripts/bench.py launch \
  --preset swe-bench-verified-sympy-16886-codex-smoke \
  --campaign-id swe-debug-example \
  --retain-containers
```

The controller stops rather than removes its Agent container, records its exact name/ID and cleanup
disposition in `campaign.json`, and then runs the official evaluator in a separate harness-owned
container. The current flag retains the Agent container only; the official harness still cleans its
evaluation container while preserving its report and logs. `status` and the final report expose the
retained Agent container. `finish` leaves it untouched; inspection or later explicit cleanup is a
user-controlled Docker operation. The temporary loopback API bridge closes when the Agent trajectory
ends, so retention preserves the filesystem and process state boundary, not a live provider route.

## Lifecycle

Run the profiled `check`, then `setup --skip-provision`, then `plan`. Before `launch`, show the
resolved confirmation block required by the benchmark-run Skill. Detached campaigns are
non-resumable and have no registered stop path. At terminal state, use unified
`status` and `finish`; do not invoke the native controller as a second public CLI.

`finish` creates campaign-local final evidence and reports; it does not edit the source registry.
During adaptation acceptance, project the reviewed, secret-free minimum into `evidence/runs/`, map
it to the exact method through registry `stage_evidence`, and promote that method in the same
change. A `benchmark_methods` pass without method-specific evidence fails repository validation.

Domestic mirrors are transport fallbacks only. They may not change the dataset revision, official
checkout branch, image tag, image ID, or evaluator implementation.

## Environment failures and recovery

- If `prepare` cannot reach `huggingface.co`, first confirm the exact image is already local. Route
  `XDG_CACHE_HOME` and `HF_HOME` below the repository `.tmp/`, then use `HF_ENDPOINT` only as a
  transport fallback for the registered dataset revision. Once cached, set `HF_HUB_OFFLINE=1` and
  `HF_DATASETS_OFFLINE=1` for the campaign.
- A task image HEAD different from the dataset `base_commit` is not by itself a mismatch. Official
  images may add an empty synthetic commit. Full doctor must prove equal Git tree IDs before launch;
  never edit, retag, rebuild, or replace the image to make the SHAs look equal.
- A failed `prepare` has not started an Agent and has not called the evaluator. Keep any empty or
  partial path with the normal `_bak` preservation rule, fix the environment, generate a fresh plan,
  and use the new planned campaign ID.
- The shared Codex runtime and Pi installations are read-only host prerequisites. Mutable cache,
  campaign, evaluator, and temp paths must remain below the selected bench-goal-plus checkout.
- Goal Plus + Pi installs the repository lock into the disposable Agent container and uses only the
  repository-local `.tmp/swe-bench-verified/goal-plus-pip-cache` as a persistent writable cache.
  Goal Plus source, Node, Pi, the dependency lock, and controller/verifier assets are mounted
  read-only. `PIP_INDEX_URL` may select a domestic transport mirror, but it cannot change the lock,
  image, dataset revision, or official evaluator.
- Plain Codex does not use an OAuth auth file on this target. The profile freezes
  `auth_mode=openai-compatible`, `base_url_env=OPENAI_BASE_URL`,
  `api_key_env=OPENAI_API_KEY`, and `wire_api=responses`; doctor must prove the exact model through
  host `POST /responses`, the Linux loopback bridge when needed, and task-container
  `POST /responses`. Seeing a request to `chatgpt.com` is a blocking routing regression, not a
  custom-provider outage. Do not fall back to OAuth or substitute `SFORGE_AGENT_*` when both
  protocol configurations exist.
- Codex runtime extraction must fit the same bounded tmpfs in doctor and run. A pre-Agent
`No space left on device` result has no model/evaluator call; finish that failed campaign, fix and
  test the tmpfs contract, then create a fresh planned campaign rather than retrying it in place.
  The runtime mount also needs explicit `exec` because the pinned binary runs from `/opt/codex`;
  retain `nosuid,nodev` and verify the exact mount through full doctor.
- The pre-Agent container Responses probe retries only transient transport outcomes (`408`, `425`,
  `429`, `5xx`, or no HTTP status) up to three attempts, with every attempt retained in runtime
  evidence. Authentication, protocol, and model-selection failures remain fail-fast. A retry must
  not change `T`, `K`, `C`, `R`, the task, or the evaluator.
- The official Astropy 4.3 image applies the upstream SWE-bench `pre_install` setuptools pin before
  creating its synthetic `SWE-bench` commit. The Astropy profiles freeze that commit HEAD, tree,
  changed-file list, and complete patch SHA-256. Full doctor accepts it only when all four values
  match exactly and the dataset base is its ancestor; the disposable Agent checkout is then reset
  to the dataset base. Any extra source change remains a blocking image mismatch.

The full installation and mirror procedure is in the
[benchmark setup matrix](../../../benchmark-setup/references/benchmark-matrix.md#swe-bench-verified-on-a-shared-linux-host).
