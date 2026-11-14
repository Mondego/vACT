"""Tool base class for agent tool definitions (trimmed for vact).

Only what vact needs: attribute/signature validation and a callable
interface. The original ``to_dict``/``save``/``from_code`` serialization helpers
(which pulled in extra type-hint/AST utilities) are omitted.
"""

import inspect
import logging
from functools import wraps
from typing import Dict, Union

from vact.core.agent_types import handle_agent_input_types, handle_agent_output_types
from vact.core.utility import is_valid_name

logger = logging.getLogger(__name__)

AUTHORIZED_TYPES = ["string", "boolean", "integer", "number", "image", "audio", "array", "object", "any", "null"]
CONVERSION_DICT = {"str": "string", "int": "integer", "float": "number"}


def validate_after_init(cls):
    original_init = cls.__init__

    @wraps(original_init)
    def new_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.validate_arguments()

    cls.__init__ = new_init
    return cls


class Tool:
    """Base class for agent tools. Subclass and implement ``forward()``."""

    name: str
    description: str
    inputs: Dict[str, Dict[str, Union[str, type, bool]]]
    output_type: str

    def __init__(self, *args, **kwargs):
        self.is_initialized = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        validate_after_init(cls)

    def validate_arguments(self):
        required = {"description": str, "name": str, "inputs": dict, "output_type": str}
        for attr, expected in required.items():
            val = getattr(self, attr, None)
            if val is None:
                raise TypeError(f"You must set attribute {attr}.")
            if not isinstance(val, expected):
                raise TypeError(f"Attribute {attr} should be {expected.__name__}, got {type(val)}")
        if not is_valid_name(self.name):
            raise Exception(f"Invalid Tool name '{self.name}'")
        for iname, icontent in self.inputs.items():
            assert isinstance(icontent, dict), f"Input '{iname}' should be a dict."
            assert "type" in icontent and "description" in icontent
            if icontent["type"] not in AUTHORIZED_TYPES:
                raise Exception(f"Input '{iname}': type '{icontent['type']}' not in {AUTHORIZED_TYPES}")
        assert self.output_type in AUTHORIZED_TYPES

        if not (hasattr(self, "skip_forward_signature_validation") and self.skip_forward_signature_validation):
            sig = inspect.signature(self.forward)
            actual = set(k for k in sig.parameters if k != "self")
            expected_keys = set(self.inputs.keys())
            if actual != expected_keys:
                raise Exception(f"Tool '{self.name}': forward params {actual} != inputs {expected_keys}")

    def forward(self, *args, **kwargs):
        raise NotImplementedError

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        if not self.is_initialized:
            self.setup()
        if len(args) == 1 and len(kwargs) == 0 and isinstance(args[0], dict):
            if all(key in self.inputs for key in args[0]):
                args, kwargs = (), args[0]
        if sanitize_inputs_outputs:
            args, kwargs = handle_agent_input_types(*args, **kwargs)
        outputs = self.forward(*args, **kwargs)
        if sanitize_inputs_outputs:
            outputs = handle_agent_output_types(outputs, self.output_type)
        return outputs

    def setup(self):
        self.is_initialized = True
