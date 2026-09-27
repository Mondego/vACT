#!/usr/bin/env python3
"""Per-task usage report for one agent run: tokens, cost, time, tool calls.

Works on all three layouts:
  * an agent_harness out-root (Claude Code / Codex): <root>/<task>/harness/...
  * a VeriAct run dir: <root>/workspaces/<task>/harness/...
and on logs from before run_meta.json / tool_calls.jsonl existed (older runs), in
which case fields that were never recorded are left empty.

Sources, per task (whatever exists):
  run_meta.json        agent, model, command, start/end, wall time, exit code
  agent_run.log        Claude Code stream-json (final `result` event: usage, cost,
                       duration, turns) or Codex --json events (per-turn usage)
                       or old Codex text logs ("tokens used" total only)
  codex_rollout.jsonl  Codex session rollout (cumulative token_count events)
  llm_calls.jsonl      VeriAct per-call usage
  tool_calls.jsonl     verify / harness / submit calls (offline scoring excluded)
  .attempts            calls charged to the budget (Claude Code / Codex)

Usage:
    python usage_report.py                    # this out-root
    python usage_report.py --root <dir> --out usage
Writes <out>_per_task.csv and <out>_summary.json and prints a summary.
Self-contained (stdlib only).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_KEYS = ["input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
              "output_tokens", "reasoning_tokens", "total_tokens"]


def _jsonl(path):
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return


def _load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def task_dirs(root):
    base = os.path.join(root, "workspaces") if os.path.isdir(os.path.join(root, "workspaces")) else root
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if os.path.isdir(os.path.join(d, "harness")):
            yield name, d


def claude_usage(log):
    res = None
    turns = 0
    for ev in _jsonl(log):
        if ev.get("type") == "assistant":
            turns += 1
        if ev.get("type") == "result":
            res = ev
    if not res:
        return {}
    u = res.get("usage") or {}
    cached, creation = u.get("cache_read_input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0
    inp = (u.get("input_tokens", 0) or 0) + cached + creation
    out = u.get("output_tokens", 0) or 0
    thinking = (u.get("output_tokens_details") or {}).get("thinking_tokens")
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_creation_input_tokens": creation,
            "output_tokens": out, "reasoning_tokens": thinking, "total_tokens": inp + out,
            "cost_usd": res.get("total_cost_usd"), "agent_duration_s": (res.get("duration_ms") or 0) / 1000,
            "llm_turns": res.get("num_turns") or turns, "usage_source": "claude stream-json result",
            "agent_error": res.get("is_error")}


def codex_usage(task_harness):
    # 1) rollout: last cumulative token_count event
    last, calls = None, 0
    for ev in _jsonl(os.path.join(task_harness, "codex_rollout.jsonl")):
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
        if p.get("type") == "token_count":
            info = p.get("info") or {}
            if info.get("last_token_usage"):
                calls += 1                          # one token_count per model response
            last = info.get("total_token_usage") or last
    if last:
        return {"input_tokens": last.get("input_tokens"), "cached_input_tokens": last.get("cached_input_tokens"),
                "cache_creation_input_tokens": None, "output_tokens": last.get("output_tokens"),
                "reasoning_tokens": last.get("reasoning_output_tokens"), "total_tokens": last.get("total_tokens"),
                "llm_turns": calls or None, "usage_source": "codex rollout token_count"}
    # 2) --json events: sum per-turn usage
    tot, turns, seen = {k: 0 for k in TOKEN_KEYS}, 0, False
    for ev in _jsonl(os.path.join(task_harness, "agent_run.log")):
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            u, seen, turns = ev["usage"], True, turns + 1
            tot["input_tokens"] += u.get("input_tokens", 0) or 0
            tot["cached_input_tokens"] += u.get("cached_input_tokens", 0) or 0
            tot["output_tokens"] += u.get("output_tokens", 0) or 0
            tot["reasoning_tokens"] += u.get("reasoning_output_tokens", 0) or 0
    if seen:
        tot["total_tokens"] = tot["input_tokens"] + tot["output_tokens"]
        tot["cache_creation_input_tokens"] = None
        return {**tot, "llm_turns": turns, "usage_source": "codex --json turn.completed"}
    # 3) old text log: "tokens used\n<N>"
    try:
        lines = open(os.path.join(task_harness, "agent_run.log"), errors="ignore").read().splitlines()
        for i, l in enumerate(lines):
            if l.strip() == "tokens used" and i + 1 < len(lines):
                return {"total_tokens": int(lines[i + 1].replace(",", "").strip()),
                        "usage_source": "codex text log (total only)"}
    except (OSError, ValueError):
        pass
    return {}


def veriact_usage(task_harness):
    calls = list(_jsonl(os.path.join(task_harness, "llm_calls.jsonl")))
    if not calls:
        return {}
    tot = {k: sum((c.get("usage") or {}).get(k, 0) or 0 for c in calls) for k in TOKEN_KEYS}
    return {**tot, "llm_turns": len(calls), "llm_latency_s": round(sum(c.get("latency_s") or 0 for c in calls), 3),
            "usage_source": "veriact llm_calls.jsonl"}


def agent_tool_uses(agent, task_harness):
    """The agent's own tool invocations (its shell / file tools, including the harness scripts it runs):
    Claude: tool_use blocks in the stream-json log; Codex: function_call / custom_tool_call items in the
    rollout; VeriAct: its tool calls (tool_calls.jsonl). Returns (total, "name:n;...")."""
    names = []
    if agent == "claude":
        for ev in _jsonl(os.path.join(task_harness, "agent_run.log")):
            if ev.get("type") == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        names.append(b.get("name") or "?")
    elif agent == "codex":
        for ev in _jsonl(os.path.join(task_harness, "codex_rollout.jsonl")):
            p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if ev.get("type") == "response_item" and p.get("type") in ("function_call", "custom_tool_call"):
                names.append(p.get("name") or "?")
    elif agent == "veriact":
        for ev in _jsonl(os.path.join(task_harness, "tool_calls.jsonl")):
            if not ev.get("offline"):
                names.append(ev.get("command") or "?")
    if not names:
        return None, ""
    c = {}
    for n in names:
        c[n] = c.get(n, 0) + 1
    return len(names), ";".join(f"{k}:{v}" for k, v in sorted(c.items()))


def tool_counts(task_harness):
    c = {"verify_calls": 0, "harness_calls": 0, "submit_calls": 0, "tool_time_s": 0.0}
    found = False
    for ev in _jsonl(os.path.join(task_harness, "tool_calls.jsonl")):
        if ev.get("offline"):
            continue
        found = True
        key = {"verify": "verify_calls", "harness": "harness_calls", "submit": "submit_calls"}.get(ev.get("command"))
        if key:
            c[key] += 1
        c["tool_time_s"] += ev.get("duration_s") or 0
    c["tool_time_s"] = round(c["tool_time_s"], 3)
    return c if found else {}


def row_for(task_id, tdir):
    h = os.path.join(tdir, "harness")
    meta = _load(os.path.join(h, "run_meta.json")) or {}
    agent = meta.get("agent")
    log = os.path.join(h, "agent_run.log")
    if not agent:  # older runs: infer from the files present
        if os.path.exists(os.path.join(h, "llm_calls.jsonl")) or "workspaces" in tdir:
            agent = "veriact"
        elif any(ev.get("type") == "system" for ev in _jsonl(log)):
            agent = "claude"
        elif os.path.exists(log):
            agent = "codex"
    usage = (claude_usage(log) if agent == "claude" else codex_usage(h) if agent == "codex"
             else veriact_usage(h) if agent == "veriact" else {})
    if agent == "veriact" and not usage and isinstance(meta.get("usage"), dict):
        usage = {**meta["usage"], "usage_source": "veriact run_meta"}
    attempts = None
    try:
        attempts = int(open(os.path.join(h, ".attempts")).read().strip() or 0)
    except (OSError, ValueError):
        pass
    return {
        "task_id": task_id, "agent": agent, "model": meta.get("model"),
        "returncode": meta.get("returncode"), "timed_out": meta.get("timed_out"),
        "wall_s": meta.get("wall_s") if meta.get("wall_s") is not None else usage.get("agent_duration_s"),
        **{k: usage.get(k) for k in TOKEN_KEYS},
        "cost_usd": usage.get("cost_usd"), "llm_turns": usage.get("llm_turns"),
        **tool_counts(h), "attempts_charged": attempts if attempts is not None else meta.get("attempts_used"),
        **dict(zip(("agent_tool_uses", "agent_tool_uses_by_name"), agent_tool_uses(agent, h))),
        "integrity_blocks": meta.get("integrity_blocks"),
        "submitted": os.path.exists(os.path.join(tdir, "submission.json")),
        "usage_source": usage.get("usage_source"),
        "has_run_meta": bool(meta),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=HERE)
    ap.add_argument("--out", default=None, help="output prefix (default: <root>/usage)")
    a = ap.parse_args()
    rows = [row_for(t, d) for t, d in task_dirs(a.root)]
    if not rows:
        print("no task dirs found")
        return 1
    out = a.out or os.path.join(a.root, "usage")
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with open(out + "_per_task.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    def stats(k):
        v = [r[k] for r in rows if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
        return {"n": len(v), "mean": round(statistics.mean(v), 3), "median": round(statistics.median(v), 3),
                "sum": round(sum(v), 3)} if v else {"n": 0}

    summary = {"root": a.root, "tasks": len(rows), "agents": sorted({str(r["agent"]) for r in rows}),
               **{k: stats(k) for k in ["wall_s", *TOKEN_KEYS, "cost_usd", "llm_turns", "agent_tool_uses", "verify_calls",
                                        "harness_calls", "attempts_charged", "tool_time_s", "integrity_blocks"]},
               "timed_out": sum(1 for r in rows if r.get("timed_out")),
               "usage_sources": {s: sum(1 for r in rows if str(r.get("usage_source")) == s)
                                 for s in sorted({str(r.get("usage_source")) for r in rows})}}
    with open(out + "_summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps({k: summary[k] for k in ("tasks", "agents", "timed_out", "usage_sources")}, indent=1))
    for k in ("wall_s", "total_tokens", "cost_usd", "verify_calls", "harness_calls"):
        print(f"  {k:15s} {summary[k]}")
    print(f"-> {out}_per_task.csv, {out}_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
