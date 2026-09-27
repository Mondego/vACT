"""Logging, monitoring, and console output for agent runs."""

import json
import sys
from enum import IntEnum
from typing import List, Optional
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from dataclasses import dataclass, field
from veriact.core.utility import escape_code_brackets

YELLOW_HEX = "#d4b702"
BLUE_HEX = "#1E90FF"
PURPLE_HEX = "#800080"
ORANGE_HEX = "#FFA500"
GREEN_HEX = "#008000"
RED_HEX = "#FF0000"


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int = field(init=False)
    def __post_init__(self):
        self.total_tokens = self.input_tokens + self.output_tokens
    def dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "total_tokens": self.total_tokens}


class LogLevel(IntEnum):
    OFF = -1
    ERROR = 0
    INFO = 1
    DEBUG = 2


class AgentLogger:
    def __init__(self, level: LogLevel = LogLevel.INFO):
        self.level = level
        self.console = Console(stderr=True)

    def log(self, *args, level: str | LogLevel = LogLevel.INFO, **kwargs) -> None:
        if isinstance(level, str):
            level = LogLevel[level.upper()]
        if level <= self.level:
            self.console.print(*args, **kwargs)

    def log_error(self, error_message: str) -> None:
        self.log(escape_code_brackets(error_message), style="bold red", level=LogLevel.ERROR)

    def log_markdown(self, content: str, title: Optional[str] = None, level=LogLevel.INFO, style=RED_HEX) -> None:
        md = Syntax(content, lexer="markdown", theme="github-dark", word_wrap=True)
        if title:
            self.log(Group(Rule("[bold italic]" + title, align="left", style=style), md), level=level)
        else:
            self.log(md, level=level)

    def log_code(self, title: str, content: str, level: int = LogLevel.INFO) -> None:
        self.log(Panel(Syntax(content, lexer="python", theme="monokai", word_wrap=True), title="[bold]" + title, title_align="left", box=box.HORIZONTALS), level=level)

    def log_rule(self, title: str, level: int = LogLevel.INFO) -> None:
        self.log(Rule("[bold]" + title, characters="━", style=RED_HEX), level=LogLevel.INFO)

    def log_task(self, content: str, subtitle: str, title: Optional[str] = None, level: int = LogLevel.INFO) -> None:
        self.log(Panel(f"\n[bold]{escape_code_brackets(content)}\n", title="[bold]New run" + (f" - {title}" if title else ""), subtitle=subtitle, border_style=RED_HEX, subtitle_align="left"), level=level)

    def log_messages(self, messages: List) -> None:
        s = "\n".join([json.dumps(dict(m), indent=4) for m in messages])
        self.log(Syntax(s, lexer="markdown", theme="github-dark", word_wrap=True))

    def visualize_agent_tree(self, agent):
        def create_tools_section(tools_dict):
            table = Table(show_header=True, header_style="bold")
            table.add_column("Name", style="#1E90FF")
            table.add_column("Description")
            table.add_column("Arguments")
            for name, tool in tools_dict.items():
                args = [f"{an} (`{info.get('type','Any')}`): {info.get('description','')}" for an, info in getattr(tool, "inputs", {}).items()]
                table.add_row(name, getattr(tool, "description", str(tool)), "\n".join(args))
            return Group("🛠️ [italic #1E90FF]Tools:[/italic #1E90FF]", table)
        def headline(a, name=None):
            nh = f"{name} | " if name else ""
            return f"[bold {RED_HEX}]{nh}{a.__class__.__name__} | {a.model.model_id}"
        def build(parent, a):
            parent.add(create_tools_section(a.tools))
            if getattr(a, "managed_agents", None):
                branch = parent.add("🤖 [italic #1E90FF]Managed agents:")
                for n, ma in a.managed_agents.items():
                    t = branch.add(headline(ma, n))
                    build(t, ma)
        tree = Tree(headline(agent))
        build(tree, agent)
        self.console.print(tree)


class Monitor:
    def __init__(self, tracked_model, logger):
        self.step_durations = []
        self.tracked_model = tracked_model
        self.logger = logger
        if getattr(self.tracked_model, "last_input_token_count", "Not found") != "Not found":
            self.total_input_token_count = 0
            self.total_output_token_count = 0

    def get_total_token_counts(self):
        return {"input": self.total_input_token_count, "output": self.total_output_token_count}

    def reset(self):
        self.step_durations = []
        self.total_input_token_count = 0
        self.total_output_token_count = 0

    def update_metrics(self, step_log):
        step_duration = step_log.duration
        self.step_durations.append(step_duration)
        out = f"[Step {len(self.step_durations)}: Duration {step_duration:.2f}s"
        # Per-thread usage of the last call (the model object is shared by parallel
        # tasks, so its last_*_token_count attributes can belong to another task).
        from veriact.core.models import thread_last_usage
        usage = thread_last_usage()
        if usage and "input_tokens" in usage:
            self.total_input_token_count += usage.get("input_tokens", 0)
            self.total_output_token_count += usage.get("output_tokens", 0)
        elif getattr(self.tracked_model, "last_input_token_count", None) is not None:
            self.total_input_token_count += self.tracked_model.last_input_token_count
            self.total_output_token_count += self.tracked_model.last_output_token_count
        out += f"| In: {self.total_input_token_count:,} | Out: {self.total_output_token_count:,}"
        out += "]"
        self.logger.log(Text(out, style="dim"), level=1)
