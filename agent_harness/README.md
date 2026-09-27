# agent_harness

Expose VeriAct's four tools as standalone CLIs and scaffold a self-contained
working directory per benchmark task, so external coding agents (Codex, Claude
Code) can solve the same JML-spec-synthesis tasks with the same tools, prompt, and
success threshold as VeriAct — enabling an apples-to-apples comparison.

The tools (mirroring `veriact/tools.py` + `default_tools.py`), with `analyze`
folded into `verify` so a single verification call returns the error analysis too:

| CLI subcommand | VeriAct tool(s) | Output |
|----------------|-----------------|--------|
| `verify`  | `verify_with_openjml` + `analyze_openjml_errors` | `{verified, return_code, errors, failure_modes, repair_hints, summary, raw_output}` |
| `harness` | `run_spec_harness`    | `{task_id: {post_correctness, post_completeness, pre_correctness, pre_completeness}}` |
| `submit`  | `task_complete`       | writes `submission.json` (the comparison artifact) |

This package is **self-contained**: `templates/` vendors the scoring/verification
code (`harness_tool.py`, `verifier_tool.py`) and the CLI, so it can live in its own
repository with no dependency on `veriact/`. Generated dirs need only the
`javalang` pip package and the `openjml` binary.

## Scaffold

```bash
# SpecGenBench (120 tasks)
python -m agent_harness.scaffold \
    --benchmark benchmarks/specgenbench/sgb.json --out-root out/sgb

# FormalBench (the pre-sampled 120 tasks)
python -m agent_harness.scaffold \
    --benchmark benchmarks/formalbench/fb_120.json --out-root out/fb
```

Both benchmarks are already the final 120-task sets (`fb_120.json` is the
category-matched FormalBench sample), so scaffold loads them identically — no
sampling step.

Each task dir gets **10** IO pairs (`MAX_PAIRS`): from `test_inputs` first, topped up from
`generated_test_cases` (the same selection the harness tool scores with `max_pairs=10`). Scaffold from a
`*_v2_feedback.json` benchmark: its `test_inputs` are the T5 feedback list, disjoint from the evaluation
pairs of `*_v2_eval.json` used for final scoring; its `invalid_inputs` (validated T-) are carried into
`harness/task.json` and used for PreComp.

### Attempt budget (stopping criterion)

Each task has a fixed budget of tool attempts (`verify` + `harness` calls combined),
enforced by the vendored CLI so it's identical across VeriAct / Claude Code / Codex.
Set it at scaffold time with `--max-attempts N` (default **10**; `0` = unlimited).
This matches VeriAct's `--max-steps 11` (10 verify/harness calls + the final
submit step). It is stored in each dir's `harness/config.json` and can be overridden
per run with `AGENT_MAX_ATTEMPTS`. When the budget is reached the tools refuse
further calls and tell the agent to submit; `attempts` is recorded in
`submission.json`.

### Ablation: with vs without the harness tool

Pass `--no-harness` to scaffold an ablation arm where the agent works with
`verify` + `submit` only (no `run_specharness.sh`, and a harness-free `AGENTS.md`
whose success criterion is "verification passes"):

```bash
python -m agent_harness.scaffold \
    --benchmark benchmarks/specgenbench/sgb.json --out-root out/sgb_no_harness --no-harness
```

`harness/cli.py` is still vendored in this arm, so after the agents finish you score
their generated JML with the spec-harness they never had — `score_all.py` (dropped
into every out-root) runs it over every dir's `Solution.java`:

```bash
VERIACT_PYTHON=.venv/bin/python OPENJML=/path/to/openjml \
    python out/sgb_no_harness/score_all.py --threads 8
# -> out/sgb_no_harness/harness_scores.{json,csv} and fills each submission.json
```

(`collect.py --score-missing` does the same on the fly for submissions still lacking
scores.) Run both arms (with- and without-harness) and diff the score tables to
measure the harness tool's effect on spec quality.

## Run agents + compare

From an out-root:

```bash
python run_agents.py --agent claude --threads 4   # fan out, 1 session per task
python collect.py                                  # comparison.json / comparison.csv
python aggregate.py                                # pass@{0.25,0.5,0.75,1.0} + mean metrics
```

`aggregate.py` reads `harness_scores.json` (from `score_all.py`) if present, else
the per-task `submission.json` scores, and writes `aggregate.json`. The pass
threshold is **0.75** by default (`--threshold`); pass@ is reported at 0.25/0.5/0.75/1.0
so you aren't tied to one cutoff.

See the generated `out-root/README.md` for the per-root details.

## Running in a `screen` session

Batch runs are long, so launch them inside `screen` (or `tmux`) and detach. The
driver fans out one session per task dir; sessions load the shared
`<out-root>/.venv` created at scaffold time (set `OPENJML` so OpenJML is found, or
`VERIACT_PYTHON` to use a specific interpreter). Per-task agent logs land in
`<task>/harness/agent_run.log`; the `tee` below also captures the driver log.

`screen` cheat-sheet: detach `Ctrl-a d` · reattach `screen -r <name>` · list
`screen -ls` · kill `screen -X -S <name> quit`.

### Claude Code

`claude -p "<prompt>"` runs headless and exits. For an unattended batch it must edit
files and run the `*.sh` tools without prompting, so pass
`--dangerously-skip-permissions` (only inside an isolated/sandboxed host). The driver
substitutes `{agents_md}` with each dir's `AGENTS.md`:

```bash
screen -dmS sgb_claude bash -lc '
  cd /path/to/out/sgb
  export VERIACT_PYTHON=/path/to/.venv/bin/python OPENJML=/path/to/openjml
  python run_agents.py --threads 4 --timeout 1800 \
    --cmd "claude -p {agents_md} --dangerously-skip-permissions --model claude-opus-4-8" \
    2>&1 | tee run_claude.log
'
screen -r sgb_claude     # watch progress
```

To run a **single** task interactively instead, just work inside its dir:

```bash
screen -S one_task
cd /path/to/out/sgb/SGB_Abs
export VERIACT_PYTHON=/path/to/.venv/bin/python OPENJML=/path/to/openjml
claude --dangerously-skip-permissions   # then paste/point it at AGENTS.md
# detach with Ctrl-a d
```

### Codex

`codex exec "<prompt>"` runs non-interactively. Add `--skip-git-repo-check` (the task
dirs aren't git repos) and `--dangerously-bypass-approvals-and-sandbox` to disable
Codex's bubblewrap sandbox — in many containers/VMs bwrap cannot create its loopback
(`RTM_NEWADDR: Operation not permitted`), which makes every edit/command fail; the
bypass is the working path on an already-isolated host (and mirrors the claude
default). The built-in `--agent codex` does **not** pass `-m` — set your model in
`~/.codex/config.toml` (e.g. `model = "gpt-4o"`, which needs an OpenAI API-key login;
a ChatGPT-account login rejects `gpt-4o`). Override per run with `--cmd '... -m <id>
...'`.

```bash
screen -dmS sgb_codex bash -lc '
  cd /path/to/out/sgb
  export VERIACT_PYTHON=/path/to/.venv/bin/python OPENJML=/path/to/openjml
  python run_agents.py --agent codex --threads 4 --timeout 1800 \
    2>&1 | tee run_codex.log
'
screen -r sgb_codex
```

After either run completes, aggregate with `python collect.py` (add
`--score-missing` for a `--no-harness` root). Use a **separate out-root per agent**
(scaffold twice, or copy the root) so their `submission.json` / `comparison.csv`
don't overwrite each other, then diff the tables.

> Agent CLI flags change over time — if `--dangerously-skip-permissions`,
> `--full-auto`, or the model id differ in your install, adjust the `--cmd` string;
> the `{agents_md}` / `{agents_md_path}` / `{task_dir}` / `{task_id}` tokens stay the
> same.

## Layout of this package

```
agent_harness/
├── scaffold.py     # generator (selection, 5-pair pick)
├── templates/
│   ├── cli.py              # the 4-subcommand CLI (vendored into each dir's harness/)
│   ├── harness_tool.py     # vendored scorer (copy of veriact/harness_tool.py)
│   ├── verifier_tool.py    # vendored verifier (copy of veriact/verifier_tool.py)
│   ├── _env.sh             # interpreter resolver (prefers shared <out-root>/.venv)
│   ├── verify.sh run_specharness.sh submit.sh
│   ├── AGENTS.md.tmpl AGENTS_no_harness.md.tmpl   # per-task prompt (both arms)
│   ├── requirements.txt    # javalang
│   ├── run_agents.py collect.py score_all.py aggregate.py   # parent-root helpers
│   └── root_README.md.tmpl
└── README.md
```

> **Keeping the vendored copies in sync:** `templates/harness_tool.py` and
> `templates/verifier_tool.py` are verbatim copies of the same-named files in
> `veriact/`. If those change while this package still lives in the VeriAct repo,
> re-copy them. Once split into its own repo, `templates/` is the source of truth.

## What gets recorded (per task, under `<task>/harness/`)

| File | Content |
|---|---|
| `agent_run.log` | The agent's stdout as structured events. `run_agents.py` enforces `--output-format stream-json --verbose` for Claude Code and `exec --json` for Codex, even with a custom `--cmd`. |
| `agent_stderr.log` | The agent's stderr, kept separate so the event log stays parseable. |
| `codex_rollout.jsonl` | Codex only: the full session rollout copied from `$CODEX_HOME/sessions` (complete conversation, reasoning items, cumulative token counts). |
| `run_meta.json` | Command (prompt elided), CLI version, model, host, start/end, wall time, exit code, timeout, hashes of `AGENTS.md` and the vendored tools, attempts used. |
| `tool_calls.jsonl` | One line per `verify` / `harness` / `submit` call, written by the tools themselves: timestamps, duration, the exact `Solution.java` snapshot and hash, the full output, and the budget state. Offline scoring (`--no-budget`) is marked `offline: true`. |
| `previous_runs/<timestamp>/` | Earlier sessions of the same task; nothing is overwritten. |

`run_agents.py --fresh` archives the previous run's state (attempt counter, scores, submission, edited `Solution.java`) and restores the original code, so a re-run starts clean.

Summarize a run with `python usage_report.py` (in the out-root, or `--root <dir>`). It writes `usage_per_task.csv` and `usage_summary.json`: tokens (input, cached, output, reasoning), cost where the CLI reports it, wall time, turns, and tool calls. It works on Claude Code / Codex out-roots and on VeriAct run dirs.
