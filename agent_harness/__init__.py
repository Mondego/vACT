"""agent_harness — expose vACT's four tools as standalone CLIs and scaffold
self-contained per-task working directories so external coding agents (Codex,
Claude Code) can solve the same JML-spec-synthesis tasks with the same tools.

Modules
-------
cli        : 4-subcommand tool dispatcher (verify | analyze | harness | submit)
             — vendored into each generated task dir's ``harness/``.
scaffold   : generator that materializes one self-contained dir per benchmark task.
run_agents : parent-level driver that fans out one agent session per task dir.
collect    : aggregates every ``submission.json`` into a comparison table.
"""
