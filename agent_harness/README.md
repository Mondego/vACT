# agent_harness

Expose vACT's four tools as standalone CLIs and scaffold a self-contained
working directory per benchmark task, so external coding agents (Codex, Claude
Code) can solve the same JML-spec-synthesis tasks with the same tools, prompt, and
success threshold as vACT — enabling an apples-to-apples comparison.

The tools (mirroring `vact/tools.py` + `default_tools.py`), with `analyze`
folded into `verify` so a single verification call returns the error analysis too:

| CLI subcommand | vACT tool(s) | Output |
|----------------|-----------------|--------|
| `verify`  | `verify_with_openjml` + `analyze_openjml_errors` | `{verified, return_code, failure_modes, repair_hints, summary, raw_output, attempts, max_attempts}` |
| `harness` | `run_spec_harness` (first 5 feedback pairs) | `{task_id: {post_correctness, post_completeness, pre_correctness, pre_completeness[, note]}, _budget}` — absent in the no-harness arm |
| `submit`  | `task_complete`       | writes `submission.json`: the code of the last accepted tool call |

The agent reaches them only through `verify.sh`, `run_specharness.sh` and `submit.sh`; the agent sees
only the JSON result (everything the tools print — OpenJML output, the harness banner — goes to
`harness/harness_stderr.log`). A killed tool call (e.g. an agent-side command timeout) kills its OpenJML
process groups (`harness_tool.install_stop_handlers` + `verifier_tool.kill_live_groups`).

**Integrity rule (shared with vACT).** A `verify`/`harness` call on code that changes the program
(class name, method signature or body, any other class member) or puts a JML `assume` in the method body
is rejected with `{"error": "code_modified", "detail", "instruction"}` without running the tool; the
attempt still counts. The rule is `harness_tool.agent_code_violation` (unparseable code is let through:
OpenJML reports the syntax error).

**Final submission (same rule for every agent).** Every accepted `verify`/`harness` call writes
`harness/last_valid_Solution.java`. `submit` submits that code (or the original program if no call was
ever accepted), records it in `submission.json` (`final_code`, `final_code_sha256`, `source`:
`"last_accepted"` | `"original"`) and writes it back to `Solution.java`.

This package is **self-contained**: `templates/` vendors the scoring/verification
code (`harness_tool.py`, `verifier_tool.py`) and the CLI, so it can live in its own
repository with no dependency on `vact/`. Generated dirs need only the
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

No `Test.java` and no `tests/` are written into task dirs. `harness/task.json` holds, in the with-harness
arm, the **first 5** pairs of the benchmark's `test_inputs` (`MAX_PAIRS = 5`, the pairs vACT's harness
tool uses with `max_pairs=5`) and its `invalid_inputs`; in the no-harness arm it holds no pairs, no invalid
inputs and no test code (`harness_tool.py` is still vendored there because `cli.py` takes the integrity
rule from it). Scaffold from a `*_v2_feedback.json` benchmark: its `test_inputs` are the feedback list,
disjoint from the evaluation pairs of `*_v2_eval.json` used for final scoring; its `invalid_inputs`
(validated T-) are used for PreComp.

### Attempt budget (stopping criterion)

Each task has a fixed budget of tool attempts (`verify` + `harness` calls combined),
enforced by the vendored CLI so it's identical across vACT / Claude Code / Codex.
Set it at scaffold time with `--max-attempts N` (default **10**; `0` = unlimited).
This matches vACT's `--max-steps 11` (10 verify/harness calls + the final
submit step). It is stored in each dir's `harness/config.json`, the only source (no
environment or command-line override; `--no-budget` no longer exists). Calls rejected
as `code_modified` count. When the budget is reached the tools refuse further calls
and tell the agent to submit; `attempts` is recorded in `submission.json`.

### Ablation: with vs without the harness tool

Pass `--no-harness` to scaffold an ablation arm where the agent works with
`verify` + `submit` only (no `run_specharness.sh`, and a harness-free `AGENTS.md`
whose success criterion is "verification passes"):

```bash
python -m agent_harness.scaffold \
    --benchmark benchmarks/specgenbench/sgb.json --out-root out/sgb_no_harness --no-harness
```

The no-harness task dirs contain no pairs at all, so they cannot be scored in place; final scoring of both
arms is done by `experiments/runners/rq4_run.py score` on `harness/last_valid_Solution.java` (checked against the
last accepted line of `harness/tool_calls.jsonl`) with the evaluation suite.

## Run agents + compare

From an out-root:

```bash
python run_agents.py --agent claude --threads 4 --only-missing   # 1 fresh session per task
python audit.py                                                   # forbidden actions -> audit.json
python usage_report.py                                            # tokens / time / tool calls
```

`run_agents.py` starts each session with stdin = /dev/null in its own session and process group, holds a
per-task lock (`harness/run.lock`, an flock inherited by the session; a task locked by a live process on
this host is skipped), and on SIGINT/SIGTERM/SIGHUP kills every running session — its process group and
every process carrying its `RQ4_AGENT_SESSION` environment marker, which includes setsid'd OpenJML
groups — and records it as interrupted. `--only-missing` re-runs only tasks that never ran (no
`harness/run_meta.json`) or whose last session failed for an infrastructure reason (non-zero exit,
timeout, interrupted); a session that ended normally is never re-run, with or without a submission. Every
session starts from a fresh state: whatever an earlier session left (edited `Solution.java`,
`submission.json`, `.attempts`, `last_valid_Solution.java`, logs, harness outputs, files the agent
created) is moved to `harness/attempt_<n>/` and the original `Solution.java` is restored.

`score_all.py` / `collect.py` are no longer shipped into out-roots (they used the removed
`cli.py --no-budget`).

See the generated `out-root/README.md` for the per-root details.

## Running in a `screen` session

Batch runs are long, so launch them inside `screen` (or `tmux`) and detach. The
driver fans out one session per task dir; sessions load the shared
`<out-root>/.venv` created at scaffold time (set `OPENJML` so OpenJML is found, or
`VACT_PYTHON` to use a specific interpreter). Per-task agent logs land in
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
  export VACT_PYTHON=/path/to/.venv/bin/python OPENJML=/path/to/openjml
  python run_agents.py --threads 4 --timeout 1800 --only-missing \
    --cmd "claude -p {agents_md} --dangerously-skip-permissions --model claude-opus-4-8" \
    2>&1 | tee run_claude.log
'
screen -r sgb_claude     # watch progress
```

To run a **single** task interactively instead, just work inside its dir:

```bash
screen -S one_task
cd /path/to/out/sgb/SGB_Abs
export VACT_PYTHON=/path/to/.venv/bin/python OPENJML=/path/to/openjml
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
  export VACT_PYTHON=/path/to/.venv/bin/python OPENJML=/path/to/openjml
  python run_agents.py --agent codex --threads 4 --timeout 1800 --only-missing \
    2>&1 | tee run_codex.log
'
screen -r sgb_codex
```

After either run completes, run `python audit.py` and `python usage_report.py`; final
scoring is done by `experiments/runners/rq4_run.py`. Use a **separate out-root per agent and arm**.

> Agent CLI flags change over time — if `--dangerously-skip-permissions`,
> `--full-auto`, or the model id differ in your install, adjust the `--cmd` string;
> the `{agents_md}` / `{agents_md_path}` / `{task_dir}` / `{task_id}` tokens stay the
> same.

## Layout of this package

```
agent_harness/
├── scaffold.py     # generator (selection, first-5-pair pick)
├── build_harness_tool.py   # generates templates/harness_tool.py from spec_harness
├── templates/
│   ├── cli.py              # verify | harness | submit CLI (vendored into each dir's harness/)
│   ├── harness_tool.py     # vendored scorer (generated by build_harness_tool.py; do not edit)
│   ├── verifier_tool.py    # vendored verifier (copy of vact/tools/verifier_tool.py)
│   ├── _env.sh             # interpreter resolver (prefers shared <out-root>/.venv)
│   ├── verify.sh run_specharness.sh submit.sh
│   ├── AGENTS.md.tmpl AGENTS_no_harness.md.tmpl   # per-task prompt (both arms)
│   ├── requirements.txt    # javalang
│   ├── run_agents.py audit.py usage_report.py aggregate.py   # parent-root helpers
│   ├── collect.py score_all.py   # legacy, not shipped (used the removed --no-budget)
│   └── root_README.md.tmpl
└── README.md
```

> **Keeping the vendored copies in sync:** `templates/harness_tool.py` and
> `templates/verifier_tool.py` are verbatim copies of the same-named files in
> `vact/`. If those change while this package still lives in the vACT repo,
> re-copy them. Once split into its own repo, `templates/` is the source of truth.

## What gets recorded (per task, under `<task>/harness/`)

| File | Content |
|---|---|
| `agent_run.log` | The agent's stdout as structured events. `run_agents.py` enforces `--output-format stream-json --verbose` for Claude Code and `exec --json` for Codex, even with a custom `--cmd`. |
| `agent_stderr.log` | The agent's stderr, kept separate so the event log stays parseable. |
| `codex_rollout.jsonl` | Codex only: the full session rollout copied from `$CODEX_HOME/sessions` (complete conversation, reasoning items, cumulative token counts). |
| `run_meta.json` | `attempt` (session number), command (prompt elided), CLI version, model, host, start/end, `seconds`, `returncode`, `timed_out`, `interrupted`, `submission_exists`, `last_valid_sha256`, hashes of `AGENTS.md` and the tools at start and end (`tools_modified`), leftover processes killed. |
| `tool_calls.jsonl` | Written by the tools themselves, one JSON line per `verify` / `harness` / `submit` call: `tool`, `accepted` (a verify/harness call that passed the budget and integrity checks; its code became `last_valid_Solution.java`; always false for submit), `code_sha256` (the code the call acted on; for submit the submitted code), `time` (= `ts_start`), `status` (`ok` / `code_modified` / `budget_exhausted` / `killed` / `error`), `attempt`, `ts_end`, `duration_s`, `exit_code`, `error`, `output`, `attempts`, `max_attempts`, `code`, `last_valid_sha256`, `argv`, `openjml`. A call killed by a signal is logged with `status: "killed"`. |
| `last_valid_Solution.java` | The code of the last accepted tool call (the final submission). |
| `harness_stderr.log` | Everything the tools printed (OpenJML, harness banner), never shown to the agent. |
| `attempt_<n>/` | The complete state of earlier sessions of the same task. |
| `run.lock` | Per-task lock (flock) held while a session of the task lives. |

Summarize a run with `python usage_report.py` (in the out-root, or `--root <dir>`). It writes `usage_per_task.csv` and `usage_summary.json`: tokens (input, cached, output, reasoning), cost where the CLI reports it, wall time, turns, and tool calls. It works on Claude Code / Codex out-roots and on vACT run dirs.
