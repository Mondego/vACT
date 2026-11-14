"""Default agent tools (TaskCompletionTool)."""

from vact.tools.base import Tool


class TaskCompletionTool(Tool):
    name = "task_complete"
    description = "Mark the given task completed"
    inputs = {
        "summary": {
            "type": "string",
            "description": "A concise summary of the steps carried out to complete the task",
        }
    }
    output_type = "any"

    def forward(self, summary: str) -> str:
        return summary


TOOL_MAPPING = {tool_class.name: tool_class for tool_class in [TaskCompletionTool]}
