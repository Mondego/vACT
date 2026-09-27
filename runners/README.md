# Standalone RQ3 / RQ4 runners

These run the FSE experiments on other machines with the frozen code. Results are copied back and merged into CSVs with `collect.py`; the figures are drawn from those CSVs only.

## 0. Machines

| Machine | Runs | Command family |
|---|---|---|
| 1 (60 cores) | RQ3: re-score all 14,881 verified RQ1 specs | `rq3_run.py` |
| 2 | RQ4, with harness: claude / codex / veriact × sgb / fb | `rq4_run.py --arm with_harness` |
| 3 | RQ4, without harness: same 6 runs | `rq4_run.py --arm no_harness` |

**Requirements on every machine:**
- `openjml` on PATH, version **21-0.21** (the same verifier and command as RQ1);
- Python ≥ 3.10 with `pip install -r requirements.txt` (the package's own dependencies from `pyproject.toml`: `javalang`, `PyYAML`, `openai`, `python-dotenv`, `dspy`, …).

**RQ4 machines also need:**
- `claude` (Claude Code) and `codex` logged in;
- the VeriAct key: `OPENAI_API_KEY` in the environment, or in `<repo>/.env` / `config/.env` (`OPENAI_API_KEY=...`);
- Codex logged in **with an API key** (`printenv OPENAI_API_KEY | codex login --with-api-key`): a ChatGPT-account login refuses `gpt-5.4`.

## 1. Unpack and check

```bash
tar xzf spec_harness_bundle_<commit>_<date>.tar.gz && cd spec_harness_bundle_<commit>_<date>
pip install -r requirements.txt
python runners/preflight.py --rq3 --out /data/rq3_out        # machine 1
python runners/preflight.py --rq4 --out /data/rq4_out        # machines 2, 3
```

The preflight must print `PREFLIGHT PASSED`. Among other checks, it scores the reference smoke specs and requires every verdict to equal the reference machine's.

## 2. RQ3 (machine 1)

```bash
screen -S rq3
python runners/rq3_run.py run --out /data/rq3_out --jvm-slots 60
python runners/rq3_run.py status --out /data/rq3_out
```

- **Unit:** one verified RQ1 spec (`status == verified` and `final_error == ""`, duplicates included), scored on `benchmarks/*/<bench>_v2_eval.json`.
- **Harness settings:** 10 evaluation pairs plus the evaluation T−, dual accept/refute decisions, 300 s per decision, `exclude_collection_tasks=False`.
- **Parallelism:** `--jvm-slots` caps the concurrent OpenJML JVMs (2 per decision), and every timer starts at launch.
- **No postcondition (D13):** a spec without an `ensures` clause on the method scores 0 on all four metrics without running OpenJML (`no_postcondition=True`, kept in the denominators).
- **Stopping:** Ctrl-C, `kill <pid>` or closing the screen stops the runner **and** every OpenJML/cvc4 process it started. A task interrupted mid-way is not saved.
- **Resumable:** re-run the same command after any interruption; finished units are skipped.
- **Splitting:** `--shard i/n` splits the work over several machines or sessions; `--bench sgb` / `--bench fb` works too. Two machines: `--shard 0/2` on one and `--shard 1/2` on the other, each with its own `--out`; `collect.py` merges them.
- **Slots:** about one core per slot (the JVM waits while cvc4 solves). Leave headroom, e.g. `--jvm-slots 96` on a 112-core machine with nothing else running; contention pushes decisions into the 300 s timeout (verdict unknown).
- **Output:** `results/<file>/<task>.json`, `stubs/<file>/<task>.tar.gz`, `progress.log` (with ETA) and `run_meta.json`.

## 3. RQ4 (machines 2 and 3)

```bash
screen -S rq4
ARM=with_harness     # machine 3: ARM=no_harness
python runners/rq4_run.py scaffold --out /data/rq4_out --arm $ARM
python runners/rq4_run.py agents   --out /data/rq4_out --arm $ARM --parallel-runs 6 --agent-threads 4
python runners/rq4_run.py score    --out /data/rq4_out --arm $ARM --jvm-slots 60
python runners/rq4_run.py status   --out /data/rq4_out --arm $ARM
```

- **Tasks:** `sgb` = SpecGenBench (120); `fb` = the FormalBench re-sample `fb_120_fbdist` (120, FormalBench's category distribution).
- **What the agents see:** `*_v2_feedback.json`, i.e. 10 feedback pairs plus the feedback T−.
- **What scoring uses:** `*_v2_eval.json`, i.e. 10 disjoint evaluation pairs plus the evaluation T−.
- **Models** (passed explicitly): Claude Code `claude-sonnet-4-6`; Codex `gpt-5.4` with reasoning effort `medium`; VeriAct `gpt-5.4`.
- **Budget:** 10 tool calls per task; VeriAct `--max-steps 11`.
- **Agent logs** are kept in each task directory: `agent_run.log`, `codex_rollout.jsonl`, `tool_calls.jsonl`, `run_meta.json`. VeriAct keeps its own under `runs/<task>/`.
- **Scoring:** every final `Solution.java` is first verified (RQ1 command, 300 s), and only verified specs are scored. An integrity failure (the body changed) counts as a failure.
- **Resumable:** every phase can be re-run; finished tasks are skipped.

## 4. Copy back and collect (reference machine)

```bash
rsync -a machine1:/data/rq3_out/ results_raw/rq3_m1/
rsync -a machine2:/data/rq4_out/ results_raw/rq4_m2/
rsync -a machine3:/data/rq4_out/ results_raw/rq4_m3/
python runners/collect.py rq3 --in results_raw/rq3_m1 --out results/rq3 --rq1-dir ../artifacts/rq1
python runners/collect.py rq4 --in results_raw/rq4_m2 results_raw/rq4_m3 --out results/rq4
```

**Outputs:**
- **RQ3:** `rq3_per_spec.csv`, `rq3_fig4_cells.csv` (per benchmark × model × {SG, AS, FB} × metric × τ), `rq3_by_config.csv`, and `rq3_collect_report.json` (completeness, statuses, bundle hashes).
- **RQ4:** `rq4_per_task.csv`, `rq4_summary.csv`, `rq4_collect_report.json`.

**Scoring conventions:**
- An integrity failure makes every metric 0 (still evaluable).
- A metric with no decided check (an empty T−, or all-unknown post checks) is left out of that metric's denominator and counted.
- Post-Harness@τ = PostCorr ≥ τ and PostComp ≥ τ over all tasks (unverified = 0). Tasks whose post checks are all unknown are left out of it and counted.
