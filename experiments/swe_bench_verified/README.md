# SWE-bench Verified native lifecycle

This controller keeps the official SWE-bench harness as the final evaluator while exposing the
repository lifecycle through `python3 scripts/bench.py`.

## Initial scope

| Contract | Frozen value |
| --- | --- |
| Dataset | `SWE-bench/SWE-bench_Verified` at `91aa3ed51b709be6457e12d00300a6a596d4c6a3` |
| Instance | `sympy__sympy-16886` |
| Image | `swebench/sweb.eval.x86_64.sympy_1776_sympy-16886:latest` |
| Methods | `plain-codex`, `plain-pi`, `goal-plus-codex`, `goal-plus-codex-pi`, `goal-plus-pi` |
| Standard budget | `T=1800`, `K=1`, `C=1`, `R=1` |
| Metric | official `resolved`, maximize |
| Codex auth | `OPENAI_BASE_URL` + `OPENAI_API_KEY`, OpenAI-compatible Responses |

Detached execution, stop/resume, `C>1`, and automatic image provisioning are not supported by this
initial acceptance path. Plain Codex, Plain Pi, and Goal Plus + Pi at `K=1,C=1` passed
archived Linux/amd64 official-harness smokes under `evidence/runs/`; this does not extend the claim
to other topologies or the full Verified split. The two Plain development smokes retain their
dirty-at-prepare provenance and later acceptance commit explicitly.

## Isolation boundary

`prepare` loads the pinned dataset row and stores the complete instance only in the ignored host
campaign as the official-loader-compatible array `evaluator/instances.json` with mode `0600`. The Agent receives an allowlisted task
file containing the issue statement and public repository identity; gold patches and official test
lists are excluded.

The Agent works in a fresh container created from the exact task image. Its only output is a binary,
full-index Git diff. By default the controller confirms removal of that container before invoking the
official harness in a separate evaluation container. With unified `--retain-containers` debug mode,
it instead confirms the Agent container is stopped, records its name/ID, and leaves it available for
inspection. An evaluator attempt is persisted before the harness starts, so the same campaign cannot
silently call it twice.

Plain Codex never mounts the host OAuth file. The profile selects the same explicit custom provider
used by the repository's other direct-API Codex paths. On Linux, a loopback base URL is exposed to
the task container through the shared `systemd-socket-proxyd` bridge; setup verifies both host and
container `POST /responses` before a campaign can start.

Profiles with `agent_network_policy=public-egress-blocked` run the Agent on a campaign-specific
Docker `--internal` bridge. The provider socket bridge listens on that bridge's gateway, so model
traffic remains available without a public route. Before the trajectory starts, the controller
requires Docker inspect to show the exact internal network and requires a direct public-IP probe to
fail; the verification and network cleanup disposition are persisted in the final evidence.
The container temporarily joins Docker's default bridge only while the controller installs the
profile-locked Goal Plus runtime, before any Agent or model process starts. The controller then
disconnects that setup network and requires the internal network to be the sole remaining attachment
before either the Responses probe or Agent invocation.
The pre-Agent container Responses probe retries only transient transport outcomes (`408`, `425`,
`429`, `5xx`, or no HTTP status) up to three attempts and records every attempt. Deterministic
authentication, protocol, and model-selection failures remain fail-fast.
During the timed process, ordinary HTTP and Git clients inherit a loopback refusal proxy while the
model gateway is exempt through `NO_PROXY`; public lookups fail quickly and the internal network
still blocks direct-socket bypasses. A campaign accepts one positive attempt seed and records the
same value in its Search strategy configuration, manifest, and report.

Goal Plus + Pi starts one outer Pi JSON session through the project extension, then requires one
candidate-bound `pi-rpc` worker in the shared Search state. Its frozen SearchSpec uses only an
Agent-selected visible test command wrapped by the repository-owned numeric verifier. The wrapper
does not read hidden dataset fields and is not the official score. On completion or timeout, the
controller closes Pi pools, performs idempotent select/promote/apply closeout, exports `.gp` into the
campaign, and only then disposes the Agent container. The separate official harness remains the sole
owner of `resolved`.

Goal Plus profiles may also run an independent Codex ViewAgent for every verifier-settled
candidate iteration. It always writes a concise evidence description into Global Evidence View.
The supplemental-evaluation ON condition additionally gives ViewAgent the immutable public task
context, current cumulative diff, verifier evidence, and at most one hard-score incumbent snapshot
per peer candidate. ViewAgent derives fresh open-ended dimensions for that commit and records only
non-directional peer relationships; FrozenSpec contains no soft rubric. OFF runs the same ViewAgent
and records descriptions, keeping the model, provider, prompt, budget, and call count matched apart
from the supplemental output and its extra tokens.

During controller closeout, any ViewAgent work already queued by verifier-settled Evidence is drained
before `search_select`. This preserves the search-period feedback contract for the final iteration;
annotation errors remain durable evidence failures rather than being converted to a soft score or a
hard verifier result.

## Public lifecycle

Use the registered presets through the unified entrypoint:

```bash
python3 scripts/bench.py check \
  --preset swe-bench-verified-sympy-16886-codex-smoke
python3 scripts/bench.py setup \
  --preset swe-bench-verified-sympy-16886-codex-smoke \
  --skip-provision
python3 scripts/bench.py plan \
  --preset swe-bench-verified-sympy-16886-codex-smoke
```

Use `swe-bench-verified-sympy-16886-goal-plus-pi-smoke` for the Goal Plus + Pi path. It freezes
`T=1800,K=1,C=1,R=1`, a 1500-second worker budget, and a 300-second Search closeout reserve.
Use `swe-bench-verified-sympy-16886-goal-plus-pi-luna-high-smoke` for the same topology with the
profile-frozen `bench-openai/gpt-5.6-luna` Responses provider and high reasoning.
Use `swe-bench-verified-sympy-16886-goal-plus-pi-sol-medium-view-smoke` for a pure Pi topology:
the outer MainAgent, independent no-session/no-tools Evidence ViewAgent, and search worker all use
`bench-openai/gpt-5.6-sol`; no Codex runtime is mounted.

Use `swe-bench-verified-sympy-16886-goal-plus-codex-smoke` for a five-minute native
Codex host check. It uses the host Codex ChatGPT login and therefore requires outbound access to
`chatgpt.com`; connectivity failure is a campaign partial, never a verifier result.

The `swe-bench-verified-indices-39-goal-plus-codex-pi-sol-deepseek-k4-c2` preset covers the 39
workbook-selected tasks with a GPT Sol Codex MainAgent, four DeepSeek Pi workers, a GPT Sol
ViewAgent, and `T=1800,K=4,C=2,R=1`. Its Agent containers share a
campaign-owned internal Docker network and reach only the selected provider through an
ephemeral host allowlist proxy. The official evaluator still runs with Docker network
mode `none`.

Use `swe-bench-verified-indices-39-goal-plus-pi-sol-deepseek-k4-c2` for the pure Pi form of the
same campaign. The outer MainAgent and independent ViewAgent run through Pi with
`bench-openai/gpt-5.6-sol` at medium reasoning; the four bound worker sessions run through Pi with
`deepseek/deepseek-v4-flash` at medium reasoning. Supplemental evaluation is required and Global
Evidence is `auto`; `T=1800,K=4,C=2,R=1` is profile-frozen. This profile does not depend on a
`share_dir` option.

Supplemental evaluation sets both `GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED=1` and
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=1`, so a requested post-settlement evaluation cannot
silently degrade into OFF. The official SWE-bench `resolved` result remains the sole hard score.
Reports preserve per-iteration Global Evidence, immutable comparison bases, ViewAgent token usage,
and completion checks proving that FrozenSpec contained no legacy soft rubric. Goal Plus also
persists every Global Evidence read with the completed View commit references visible at that time;
the report distinguishes a supplemental evaluation read before a later verifier from one published
only during closeout. Before freezing the hard verifier, the MainAgent builds a public behavior
inventory from the issue, implementation, and existing tests, and keeps relevant regression tests in
the candidate edit surface without allowing them to redefine the frozen verifier.

Run `launch` only after reviewing and confirming the resolved `T/K/C/R` block. A terminal campaign
is archived with `finish`, which consumes `campaign-summary.json` and exports `report.md` plus the
campaign-named workbook.

That `finish` archive is campaign-local. For adaptation readiness, review and sanitize the minimum
evidence into `evidence/runs/`, bind it to the exact method with registry `stage_evidence`, and
promote the method in the same change. Repository validation rejects a method pass without that
mapping.

The task image itself is never removed by this controller. The official evaluator is fixed to
`cache_level=instance`, `clean=false`, and `force_rebuild=false`. To preserve the stopped Agent
container as well, add `--retain-containers` to both `plan` and `launch`; the resolved spec, manifest,
status, and final report record the retained Agent container. The official harness still cleans its
separate evaluation container and preserves its logs. `finish` does not clean up the retained Agent
container.

## Mirrors

The controller does not rewrite Docker references or dataset revisions. A domestic PyPI or
Hugging Face endpoint may accelerate transfer when the official endpoint is unavailable, but the
profile SHA and exact image tag remain authoritative. Existing local assets do not trigger a pull.
