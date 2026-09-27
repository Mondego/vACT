# veriact

A CLI-driven refactor of VeriAct. Same verification-guided JML spec-synthesis loop
and the same memory / monitoring / trajectory recording, but the agent now drives
**command-line tools** in a think → run-CLI → observe ReAct loop instead of
executing Python via CodeAct.

## What changed vs `veriact/`

| | `veriact/` (CodeAct) | `veriact/` (CLI-ReAct) |
|---|---|---|
| Agent action | emits `{thought, code}`; Python runs in a sandbox and calls tool objects | emits `{thought, tool, tool_input}`; the tool CLI runs as a **subprocess**, stdout is the observation |
| Tools | 4 (`verify_with_openjml`, `analyze_openjml_errors`, `run_spec_harness`, `task_complete`) | **3** (`verify` with analyze merged in, `run_spec_harness`, `task_complete`) |
| Code execution | in-process `exec` with AST safety checks | none — only CLI subprocesses |
| Memory / monitoring / trajectory | preserved | **preserved, unchanged** |

The three tools are the subcommands of [`cli.py`](veriact/cli.py):

- `verify` — OpenJML ESC; output includes the error analysis (`failure_modes`,
  `repair_hints`, `summary`), so the separate analyze tool is gone.
- `harness` — the four spec-harness metrics.
- `submit` — records the final submission (`task_complete`).

The agent supplies the full annotated code in `tool_input.jml_annotated_code` each
step; the agent writes it to `Solution.java` in a per-task workspace and runs the
CLI against it. There is **no attempt budget** here — the loop is bounded by
`--max-steps` (each tool call ≈ one step, matching VeriAct).

## Run (same UX as VeriAct)

Run from the repo root.

```bash
# one task at a time
python -m veriact.run.run_single \
    --benchmark benchmarks/specgenbench/sgb.json \
    --model gpt-4o --output-dir out --openjml-path openjml \
    --max-steps 11 --planning_interval 5

# parallel
python -m veriact.run.run_batch \
    --benchmark benchmarks/specgenbench/sgb.json \
    --model gpt-4o --threads 4 --output-dir out --max-steps 11
```

### Ablation: no-harness

Pass `--no-harness` to either runner to run with **only `verify` + `task_complete`**
(no `run_spec_harness`). It uses `prompts/veriact_cli_no_harness_prompt.yaml` and
success is defined as **verification passing** (last `verify` returns `verified:
true`). If the model calls `run_spec_harness` anyway, the tool replies that it is
unavailable.

```bash
python -m veriact.run.run_batch --benchmark benchmarks/specgenbench/sgb.json \
    --model gpt-4o --threads 4 --output-dir out_nh --max-steps 11 --no-harness
```

**Scoring the no-harness output offline.** Since the no-harness arm never runs the
spec-harness, score its final specs afterward for comparison against the harness
arm. `score_no_harness` walks `<run-dir>/workspaces/<task_id>/Solution.java` and
writes `harness_scores.{json,csv}`:

```bash
python -m veriact.run.score_no_harness \
    --run-dir out_nh/veriact__gpt-4o__<timestamp> \
    --openjml openjml --threads 4
```

### Aggregate a run

`pass@{0.25,0.5,0.75,1.0}` + mean metrics for a run. Reads `harness_scores.json`
(no-harness arm) if present, else extracts the last `run_spec_harness` scores from
`trajectories.jsonl` (with-harness arm):

```bash
python -m veriact.run.aggregate --run-dir out/veriact__gpt-4o__<timestamp>
# -> aggregate.json
```

The pass threshold (`HARNESS_PASS_THRESHOLD`) is **0.75** by default; the aggregator
reports pass-rate at 0.25/0.5/0.75/1.0 so the comparison isn't tied to one cutoff.

Outputs match VeriAct's layout: `out/<run>/trajectories.jsonl`,
`out/<run>/trajectories/<task_id>_veriact_trajectory.json`, and per-task
workspaces under `out/<run>/workspaces/<task_id>/` (Solution.java, Test.java,
harness/task.json, submission.json, stubs).

## Layout

```
veriact/
  __init__.py  config.py  README.md
  agent/
    agent.py          # VeriActAgent: workspace setup + trajectory (entry point)
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
    veriact_cli_prompt.yaml
  core/
    memory.py  monitoring.py  models.py  data_types.py
    agent_types.py  file_utility.py  utility.py
```

The OpenJML flags and mutant budget (k=5) in `tools/harness_tool.py` and `tools/verifier_tool.py`
match the original VeriAct scorer/verifier. The harness tool scores on `max_pairs=10` pairs: with a
`*_v2_feedback.json` benchmark these are the T5 feedback list, disjoint from the evaluation pairs used
for final scoring. When the task carries `invalid_inputs` (validated T-, testset T6), PreComp uses exactly
those; otherwise the old heuristic generator.

## What gets recorded (per task, under `<run-dir>/workspaces/<task>/harness/`)

| File | Content |
|---|---|
| `llm_calls.jsonl` | One line per model call (planning and action): request messages, response text, latency, response id, and normalized token usage (input incl. cached, cached, cache creation, output, reasoning, plus the provider's raw usage). Recording is per thread, so parallel tasks sharing one model object never mix. |
| `tool_calls.jsonl` | One line per `verify` / `harness` / `submit` CLI call (code snapshot and hash, output, duration). |
| `run_meta.json` | Model, mode, max steps, host, start/end, wall time, usage totals, tool-call counts, integrity blocks, step durations, hashes of the tools and the prompt, error if the run crashed. |
| `previous_runs/<timestamp>/` | Logs of earlier runs of the same workspace. |

The trajectory JSON also carries `usage`, `integrity_blocks` and `step_durations_s`.

**Integrity check:** a `verify` / `run_spec_harness` call whose code changes the class name, method signature or method body is rejected before the tool runs, with a `code_modified` error. `Solution.java` keeps the last valid code.

Summarize with `python agent_harness/templates/usage_report.py --root <run-dir>`.
