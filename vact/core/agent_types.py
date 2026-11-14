"""Agent output type wrappers."""

import logging

logger = logging.getLogger(__name__)


class AgentType:
    def __init__(self, value):
        self._value = value

    def __str__(self):
        return self.to_string()

    def to_raw(self):
        return self._value

    def to_string(self) -> str:
        return str(self._value)


class AgentText(AgentType, str):
    def to_raw(self):
        return self._value

    def to_string(self):
        return str(self._value)


_AGENT_TYPE_MAPPING = {"string": AgentText}


def handle_agent_input_types(*args, **kwargs):
    args = [(arg.to_raw() if isinstance(arg, AgentType) else arg) for arg in args]
    kwargs = {k: (v.to_raw() if isinstance(v, AgentType) else v) for k, v in kwargs.items()}
    return args, kwargs


def handle_agent_output_types(output, output_type=None):
    if output_type in _AGENT_TYPE_MAPPING:
        return _AGENT_TYPE_MAPPING[output_type](output)
    if isinstance(output, str):
        return AgentText(output)
    return output
