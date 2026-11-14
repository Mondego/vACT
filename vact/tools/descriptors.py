"""Tool *descriptors* for the CLI-ReAct agent.

These exist for prompt rendering, planning, and trajectory metadata (name /
description / inputs). They are **not** executed via ``forward`` — the CLIAgent
dispatches each tool name to a CLI subprocess (see [cli_agent.py]) and observes
its stdout. ``forward`` is therefore a stub kept only to satisfy the Tool base.
"""

from vact.tools.base import Tool


class VerifyTool(Tool):
    name = "verify"
    description = (
        "Run OpenJML Extended Static Checking on the given JML-annotated Java "
        "code. Returns verification status plus error analysis (failure modes "
        "and repair hints): {verified, return_code, errors, failure_modes, "
        "repair_hints, summary, raw_output}."
    )
    inputs = {
        "jml_annotated_code": {
            "type": "string",
            "description": "Complete Java source (class Solution) with JML annotations.",
        }
    }
    output_type = "string"

    def forward(self, jml_annotated_code: str) -> str:  # pragma: no cover - dispatched via CLI
        raise NotImplementedError("verify is executed as a CLI subprocess by CLIAgent")


class RunHarnessTool(Tool):
    name = "run_spec_harness"
    description = (
        "Evaluate JML spec quality on four metrics in [0,1]: post_correctness, "
        "post_completeness, pre_correctness, pre_completeness. Call after "
        "verification passes."
    )
    inputs = {
        "task_id": {
            "type": "string",
            "description": "Identifier of the task being evaluated.",
        },
        "jml_annotated_code": {
            "type": "string",
            "description": "Complete Java source (class Solution) with JML annotations.",
        },
    }
    output_type = "string"

    def forward(self, task_id: str, jml_annotated_code: str) -> str:  # pragma: no cover
        raise NotImplementedError("run_spec_harness is executed as a CLI subprocess by CLIAgent")
