# Standalone runners: Spec-Harness scoring (RQ3) and agents (RQ4)

These scripts run the two large experiments, possibly on several machines, with a frozen copy of the code. Results are copied back and merged into CSVs with `collect.py`. All commands run from the **repository root**.

| Script | Purpose |
|---|---|
| `rq3_run.py` | Score every verified RQ1 specification with Spec-Harness on the v2 evaluation suites |
| `rq4_run.py` | Run the agents (Claude Code, Codex, vACT) with and without the Spec-Harness tool, then score their final specifications |
| `rq4_one.sh` | One RQ4 run end to end: scaffold → agent sessions → retry failed sessions → scoring → status |
| `collect.py` | Merge the results of one or more machines into CSVs |
| `preflight.py` | Check a machine before a long run (tools, versions, unit tests, reference smoke verdicts) |
| `pack.sh` | Build a portable bundle of the committed code plus the slim RQ1 inputs |
| `slim_rq1.py` | Write the slim RQ1 input files for the bundle (used by `pack.sh`) |
| `make_reference.py` | Record the reference smoke verdicts that `preflight.py` compares against (`reference/`) |
| `common.py` | Shared helpers: locks, atomic writes, sharding, provenance, progress |

## 0. Requirements

**Every machine:**
- `openjml` on `PATH`, version **21-0.21** (the verifier and command of RQ1);
- Python ≥ 3.10 with the package's dependencies (`pip install -e .` from the repository root, or `pip install -r requirements.txt` inside a bundle).

**RQ4 machines also need:**
- `claude` (Claude Code) and `codex` installed and logged in;
- the vACT key: `OPENAI_API_KEY` in the environment or in `config/.env`;
- Codex logged in **with an API key** (`printenv OPENAI_API_KEY | codex login --with-api-key`). An account login may not offer the API models.

## 1. Bundle and preflight

On the reference machine, build a bundle from the committed code (`pack.sh` refuses to run with uncommitted changes):

```bash
experiments/runners/pack.sh [output-dir]      # -> spec_harness_bundle_<commit>_<date>.tar.gz
```

On each run machine:

```bash
tar xzf spec_harness_bundle_<commit>_<date>.tar.gz && cd spec_harness_bundle_<commit>_<date>
pip install -r requirements.txt
python experiments/runners/preflight.py --rq3 --out /data/rq3_out     # RQ3 machine
python experiments/runners/preflight.py --rq4 --out /data/rq4_out     # RQ4 machines
```

The preflight must print `PREFLIGHT PASSED`. Among other checks, it scores the reference smoke specifications and requires every verdict to equal the one recorded on the reference machine.

## 2. RQ3: Spec-Harness scoring

```bash
python experiments/runners/rq3_run.py run --out /data/rq3_out --jvm-slots 60
python experiments/runners/rq3_run.py status --out /data/rq3_out
```

- **Unit:** one verified RQ1 specification (`status == "verified"` and `final_error == ""`, duplicates included), scored on `benchmarks/*/<bench>_v2_eval.json`.
- **Harness settings:** evaluation pairs plus the evaluation invalid inputs (T-), dual accept/refute decisions, 300 s per decision.
- **Pairs:** `--pairs N` scores the first N evaluation pairs (default: all 10; the study uses N = 5). The run directory remembers N, and a resume with another N is refused. `collect.py --pairs M` re-scores any prefix M ≤ N.
- **No postcondition:** a specification without an `ensures` clause on the method scores 0 on all four metrics without running OpenJML (`no_postcondition=True`, kept in the denominators).
- **Duplicate reuse (default):** byte-identical specifications on the same task are scored once, and the others get a copy of the result (`reused_from`). `--no-reuse` scores every specification itself.
- **Parallelism:** `--jvm-slots` caps the concurrent OpenJML JVMs (2 per decision). Leave headroom (about one core per slot); contention pushes decisions into the 300 s timeout.
- **Splitting:** `--shard i/n` splits the work over machines or sessions, each with its own `--out`; `--bench sgb` / `--bench fb` works too. `collect.py` merges them.
- **Stopping and resuming:** Ctrl-C or `kill <pid>` stops the runner and every OpenJML/cvc4 process it started. Re-run the same command to resume; finished units are skipped.
- **Output:** `results/<file>/<task>.json`, `stubs/<file>/<task>.tar.gz`, `progress.log` and `run_meta.json`.

## 3. RQ4: agents with and without Spec-Harness feedback

One run (arm × agent × benchmark), end to end:

```bash
experiments/runners/rq4_one.sh OUT ARM AGENT BENCH [SESSIONS] [JVM_SLOTS]
#   ARM    with_harness | no_harness
#   AGENT  claude | codex | vact
#   BENCH  sgb | fb
```

Or phase by phase:

```bash
python experiments/runners/rq4_run.py scaffold --out /data/rq4_out --arm with_harness
python experiments/runners/rq4_run.py agents   --out /data/rq4_out --arm with_harness --sessions 4
python experiments/runners/rq4_run.py score    --out /data/rq4_out --arm with_harness --jvm-slots 60
python experiments/runners/rq4_run.py status   --out /data/rq4_out --arm with_harness
```

- **Tasks:** `sgb` = SpecGenBench (120); `fb` = FormalBench-120 (`fb_120_fbdist`, drawn in proportion to FormalBench's categories).
- **What the agents see:** `*_v2_feedback.json`: feedback pairs and feedback invalid inputs.
- **What scoring uses:** `*_v2_eval.json`: evaluation pairs and invalid inputs, disjoint from the feedback suite.
- **Models** (defaults, passed explicitly to every agent): Claude Code `claude-sonnet-4-6`; Codex `gpt-6.1-sol` and vACT `gpt-6.1-sol`, both with reasoning effort `medium`.
- **Budget:** 10 tool calls per task for every agent (vACT: `--max-tool-calls 10`, `--max-steps 20`).
- **Agent logs** stay in each task directory: `agent_run.log`, `codex_rollout.jsonl`, `tool_calls.jsonl`, `run_meta.json`. vACT keeps its own under `runs/<task>/`.
- **Scoring:** every final `Solution.java` is first verified (RQ1 command, 300 s), and only verified specifications are scored. A changed program counts as a failure.
- **Resumable:** every phase can be re-run; finished tasks are skipped.

## 4. Collect

```bash
python experiments/runners/collect.py rq3 --in rq3_m1 [rq3_m2 ...] --out results/rq3 --rq1-dir <rq1 outputs> --pairs 5
python experiments/runners/collect.py rq4 --in rq4_m2 rq4_m3 --out results/rq4 --pairs 5
```

**Outputs:**
- **RQ3:** `rq3_per_spec.csv`, `rq3_fig4_cells.csv` (per benchmark × model × approach × metric × τ), `rq3_by_config.csv` and `rq3_collect_report.json` (completeness, statuses, bundle hashes).
- **RQ4:** `rq4_per_task.csv`, `rq4_summary.csv` and `rq4_collect_report.json`.

**Scoring conventions** (identical in RQ3 and RQ4):
- **Failures score 0 on every metric and stay in every denominator:** a changed program (class, signature, method body or another member changed, or code that does not parse), and a specification without `ensures`.
- **Fixed denominators:** every fraction ≥ τ is (specifications meeting τ) / (tasks × prompt configurations), whatever a run produced. Not verified, no postcondition, changed code and undecided checks all count as not meeting τ, and their counts are reported next to each cell.
- **Suite rules at collect time:** invalid inputs whose recorded failure is a JVM `Error` (resource-dependent) are dropped, and `FB_Array3d` (no parsable output) is excluded, so RQ3 uses 661 FormalBench tasks.
- **PreComp** is measured over the tasks whose evaluation suite has invalid inputs.
- **Post-Harness@τ** = PostCorr ≥ τ and PostComp ≥ τ, over all tasks.
- **Means** are over evaluable specifications and are descriptive only.

**Completeness:** `collect.py` exits with code 4 (after writing its report) when units are missing or failed. Finish the runs (`rq3_run.py run --retry-errors`, `rq4_run.py score --retry-errors`) or pass `--allow-incomplete` for preliminary numbers. Results scored with different `--pairs` must be collected with `--pairs <smallest>`.
