# vact

A CLI-driven refactor of vACT. Same verification-guided JML spec-synthesis loop
and the same memory / monitoring / trajectory recording, but the agent now drives
**command-line tools** in a think → run-CLI → observe ReAct loop instead of
executing Python via CodeAct.

## What changed vs `vact/`

| | `vact/` (CodeAct) | `vact/` (CLI-ReAct) |
|---|---|---|
| Agent action | emits `{thought, code}`; Python runs in a sandbox and calls tool objects | emits `{thought, tool, tool_input}`; the tool CLI runs as a **subprocess**, stdout is the observation |
| Tools | 4 (`verify_with_openjml`, `analyze_openjml_errors`, `run_spec_harness`, `task_complete`) | **3** (`verify` with analyze merged in, `run_spec_harness`, `task_complete`) |
| Code execution | in-process `exec` with AST safety checks | none — only CLI subprocesses |
| Memory / monitoring / trajectory | preserved | **preserved, unchanged** |

The three tools are the subcommands of [`cli.py`](vact/cli.py):

- `verify` — OpenJML ESC; output includes the error analysis (`failure_modes`,
  `repair_hints`, `summary`), so the separate analyze tool is gone.
- `harness` — the four spec-harness metrics.
- `submit` — records the final submission (`task_complete`).

The agent supplies the full annotated code in `tool_input.jml_annotated_code` each
step; the agent writes it to `Solution.java` in a per-task workspace and runs the
CLI against it.

**RQ4 settings (identical to the Claude Code / Codex tools).**

- *Budget:* 10 tool calls per task (`--max-tool-calls`), `verify` and `run_spec_harness`
  combined. Every call submitted to a tool counts, including calls rejected by the
  integrity rule and calls that time out. After 10 calls both tools refuse without
  running and return
  `{"error": "attempt budget exhausted (10/10)", "action": "call task_complete with your best spec", "attempts": 10, "max_attempts": 10}`.
  Every `verify` result (and every error) carries top-level `attempts` / `max_attempts`;
  every `run_spec_harness` score carries `"_budget": {"attempts", "max_attempts"}`.
  LLM steps that are not tool calls (unparseable output, missing `jml_annotated_code`,
  unknown/unavailable tool, `task_complete`) do not use the budget. The step limit
  (`--max-steps`, default 20) only guards against endless unparseable output; when it is
  hit, the final code is the last accepted code.
- *Integrity / assume rule:* `harness_tool.agent_code_violation` rejects a call whose code
  changes the class name, the method signature/body or any other class member, or puts a
  JML `assume` in the method body (`code_modified`, the tool is not run, the call counts).
  Only accepted code is written to `Solution.java`.
- *Tool timeout:* 1800 s per tool call (`--tool-timeout`). The CLI runs in its own session;
  on timeout the agent SIGTERMs its group (the CLI's stop handler kills every OpenJML
  group it started), waits 10 s, then SIGKILLs the group. Observation:
  `{"error": "tool timed out", "timeout": 1800, "attempts": .., "max_attempts": ..}`.
- *Harness feedback:* `run_spec_harness` scores the task's first 5 feedback pairs
  (`--harness-max-pairs`) plus all its invalid inputs.
- *Model:* OpenAI models get `reasoning_effort` (`--reasoning-effort`, default `medium`,
  as Codex runs gpt-6.1-sol); it is recorded in `llm_calls.jsonl` (`reasoning_effort`,
  `request_params`) and `run_meta.json`.
- `run_single` exits with status 1 if a task failed (the failures are also written to
  `failed_tasks.json`).

## Run (same UX as vACT)

Run from the repo root.

```bash
# one task at a time
python -m vact.run.run_single \
    --benchmark benchmarks/specgenbench/sgb.json \
    --model gpt-4o --output-dir out --openjml-path openjml \
    --max-steps 20 --max-tool-calls 10 --tool-timeout 1800 \
    --reasoning-effort medium --planning_interval 5

# parallel
python -m vact.run.run_batch \
    --benchmark benchmarks/specgenbench/sgb.json \
    --model gpt-4o --threads 4 --output-dir out --max-steps 20
```

### Ablation: no-harness

Pass `--no-harness` to either runner to run with **only `verify` + `task_complete`**
(no `run_spec_harness`). It uses `prompts/vact_cli_no_harness_prompt.yaml` and
success is defined as **verification passing** (last `verify` returns `verified:
true`). If the model calls `run_spec_harness` anyway, the tool replies that it is
unavailable.

```bash
python -m vact.run.run_batch --benchmark benchmarks/specgenbench/sgb.json \
    --model gpt-4o --threads 4 --output-dir out_nh --max-steps 20 --no-harness
```

**Scoring the no-harness output offline.** Since the no-harness arm never runs the
spec-harness, score its final specs afterward for comparison against the harness
arm. `score_no_harness` walks `<run-dir>/workspaces/<task_id>/Solution.java` and
writes `harness_scores.{json,csv}`:

```bash
python -m vact.run.score_no_harness \
    --run-dir out_nh/vact__gpt-4o__<timestamp> \
    --openjml openjml --threads 4
```

### Aggregate a run

`pass@{0.25,0.5,0.75,1.0}` + mean metrics for a run. Reads `harness_scores.json`
(no-harness arm) if present, else extracts the last `run_spec_harness` scores from
`trajectories.jsonl` (with-harness arm):

```bash
python -m vact.run.aggregate --run-dir out/vact__gpt-4o__<timestamp>
# -> aggregate.json
```

The pass threshold (`HARNESS_PASS_THRESHOLD`) is **0.75** by default; the aggregator
reports pass-rate at 0.25/0.5/0.75/1.0 so the comparison isn't tied to one cutoff.

Outputs match vACT's layout: `out/<run>/trajectories.jsonl`,
`out/<run>/trajectories/<task_id>_vact_trajectory.json`, and per-task
workspaces under `out/<run>/workspaces/<task_id>/` (Solution.java, Test.java,
harness/task.json, submission.json, stubs).

## Layout

```
vact/
  __init__.py  config.py  README.md
  agent/
    agent.py          # vACTAgent: workspace setup + trajectory (entry point)
    cli_agent.py      # MultiStepAgent + CLIAgent (the ReAct-over-CLI loop)
  tools/
    cli.py            # the 3 tools as CLI subcommands (verify | harness | submit)
    descriptors.py    # tool descriptors (name/description/inputs) for prompts
    base.py           # Tool base class
    default_tools.py  # task_complete
    harness_tool.py   # spec-harness scorer
    verifier_tool.py  # OpenJML verifier
  run/
    run_single.py  run_batch.py
  prompts/
    vact_cli_prompt.yaml
  core/
    memory.py  monitoring.py  models.py  data_types.py
    agent_types.py  file_utility.py  utility.py
```

The OpenJML flags and mutant budget (k=5) in `tools/harness_tool.py` and `tools/verifier_tool.py`
match the original vACT scorer/verifier. The harness tool scores on `max_pairs=5` pairs (the agent tool; final scoring uses 10): with a
`*_v2_feedback.json` benchmark these are the feedback list, disjoint from the evaluation pairs used
for final scoring. When the task carries `invalid_inputs` (validated T-, see benchmarks/README.md), PreComp uses exactly
those; otherwise the old heuristic generator.

## What gets recorded (per task, under `<run-dir>/workspaces/<task>/harness/`)

| File | Content |
|---|---|
| `llm_calls.jsonl` | One line per model call (planning and action): request messages, the other request parameters actually sent (`request_params`, e.g. `reasoning_effort`), response text, latency, response id, and normalized token usage (input incl. cached, cached, cache creation, output, reasoning, plus the provider's raw usage). Recording is per thread, so parallel tasks sharing one model object never mix. |
| `tool_calls.jsonl` | One line per `verify` / `harness` / `submit` CLI call (code snapshot and hash, output, duration). |
| `run_meta.json` | Model, reasoning effort, model config, mode, max steps, tool-call budget / used / refused, tool timeout (and timeouts hit), harness pairs, host, start/end, wall time, usage totals, tool-call counts, integrity blocks, step durations, hashes of the tools and the prompt, error if the run crashed. |
| `previous_runs/<timestamp>/` | Logs of earlier runs of the same workspace. |

The trajectory JSON also carries `usage`, `integrity_blocks` and `step_durations_s`.

**Integrity check:** a `verify` / `run_spec_harness` call whose code changes the class name, method signature or method body is rejected before the tool runs, with a `code_modified` error. `Solution.java` keeps the last valid code.

Summarize with `python agent_harness/templates/usage_report.py --root <run-dir>`.
