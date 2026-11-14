"""
vact — CLI-driven refactor of vACT.

Same verification-guided spec-synthesis loop, but the agent drives **command-line
tools** (verify, run_spec_harness, task_complete) in a think → run-CLI → observe
ReAct loop, instead of executing Python via CodeAct. Memory, monitoring, and
trajectory recording are preserved.

    from vact import vACTAgent, OpenAIServerModel
    agent = vACTAgent(model=OpenAIServerModel(model_id="gpt-4o"))
    agent.run(task)
"""

from vact.agent import vACTAgent
from vact.core.data_types import Task, HARNESS_PASS_THRESHOLD
from vact.core.models import (
    OpenAIServerModel,
    AnthropicModel,
    GeminiModel,
    VLLMModel,
)

__version__ = "0.1.0"
