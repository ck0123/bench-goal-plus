# Frontier-Engineering v1-lite native controller

This controller preserves the upstream `v1_lite.yaml` task contracts and its
`UnifiedTask` evaluators. The default profile contains the 9 CPU tasks; the
full 10-task profile explicitly opts into NVIDIA CUDA for RobotArm and is
blocked by doctor unless both the driver and runtime CUDA probes pass. It owns campaign state only; benchmark runtimes,
candidate sandboxing, raw `metrics.json`, and `artifacts.json` remain upstream.

The registered lifecycle is:

```bash
python3 scripts/bench.py check --preset frontier-engineering-energy-storage-codex-smoke
python3 scripts/bench.py setup --preset frontier-engineering-energy-storage-codex-smoke
python3 scripts/bench.py plan --preset frontier-engineering-energy-storage-codex-smoke
python3 scripts/bench.py launch --preset frontier-engineering-energy-storage-codex-smoke --campaign-id <id>
python3 scripts/bench.py status --campaign runs/frontier-engineering/<id>
python3 scripts/bench.py finish --campaign runs/frontier-engineering/<id>
```

The Plain Pi and Goal Plus + Pi acceptance presets use the same EnergyStorage
task with `K=1, C=1, R=1`. Plain Pi uses `T=300` and starts one isolated outer
lane. Goal Plus + Pi uses the exercised `T=600` budget and starts one outer Pi
session whose one internal worker shares the Goal Plus Search state.

Pi profiles may set `pi_provider.id`, `api`, `api_key_env`, and `api_base_env`.
The defaults are `bench-openai`, `openai-responses`, `OPENAI_API_KEY`, and
`OPENAI_BASE_URL`. The model remains a bare ID in the profile; doctor, Main,
and workers use the same qualified provider/model. Credentials and endpoint
values are inherited, while only environment variable names are recorded.
`energy-storage-goal-plus-pi-glm53flash-smoke` selects `zai/glm-5.3-flash`,
low reasoning, T=600, K=1, C=1, R=1, using `ZAI_API_KEY` and `ZAI_BASE_URL`.
The exact model must exist in the host Pi catalog; no model substitution occurs.

Goal Plus uses the common runner's source installation (`install.sh --pi`),
typed host command, Git worktrees, `autoresearch`, `parallel_loops`, disabled
shared-dir, and public promotion/apply lifecycle. To exercise a clean external
checkout, set `BENCH_GOAL_PLUS_SOURCE_DIR` (used at its current HEAD;
`BENCH_GOAL_PLUS_EXPECTED_REF` optionally pins an exact ref).
Plan and prepared evidence record its full source identity; changing it before
execution is rejected. This does not change the registered upstream branch.

The official evaluator runs through UnifiedTask's configurable shell with
host login profiles disabled, so shell startup cannot change the evaluation
directory. The bridge and shell are included in the frozen evaluator hash.
EnergyStorage uses a 30-second evaluator timeout (the official seed takes less
than a second). The previous blanket 300-second value prevented short searches
from passing Goal Plus's verifier deadline admission gate. This task timeout is
recorded in task metadata and shared by doctor, process and final evaluation.
Unit tests use small local protocol fixtures; the full upstream checkout and
seed evaluator remain mandatory doctor gates for a real campaign.

The EnergyStorage preset is the primary acceptance smoke: its editable policy is
small, its shipped candidate is feasible, and its official evaluator is fast
enough for repeated feedback inside a five-minute Agent budget. The JobShop
preset remains available as a slower scheduling regression.

`K` is implemented by the selected method inside one task cell. `C` remains
restricted to one until cross-task resource isolation has separate evidence.
The legacy `frontier-engineering-malloclab` common target remains a fast portable
regression and is not the v1-lite campaign target.

The `frontier-engineering-energy-storage-openevolve-paper-100` preset preserves
the upstream Experiment 1 search protocol on the same EnergyStorage task. It
uses the shipped initial program, frozen UnifiedTask verifier, OpenEvolve 0.2.26
defaults, temperature 0.7, and 100 evolution iterations. The 12-hour wall value
is a fail-safe ceiling rather than the selection budget. Completion requires an
audited initial-program record, exactly 100 evolved candidates, and a valid
controller final evaluation.

The `frontier-engineering-energy-storage-openevolve-smoke-5` preset exercises
the same initial program, verifier, model configuration, and OpenEvolve search
path with five evolution iterations. It is diagnostic rather than a paper
result. Completion requires one initial-program record, exactly five evolved
candidates, a saved best program, and a valid controller final evaluation.
