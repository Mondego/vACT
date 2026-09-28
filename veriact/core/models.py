"""LLM model abstractions: base Model, ApiModel, and backends (OpenAI, Anthropic, Gemini, vLLM)."""

import datetime
import json
import logging
import os
import re
import threading
import time
import uuid
import warnings
from collections.abc import Generator
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from veriact.tools.base import Tool
from veriact.core.utility import _is_package_available, encode_image_base64, make_image_url, parse_json_blob

logger = logging.getLogger(__name__)



# Message types

def get_dict_from_nested_dataclasses(obj, ignore_key=None):
    def convert(o):
        if hasattr(o, "__dataclass_fields__"):
            return {k: convert(v) for k, v in asdict(o).items() if k != ignore_key}
        return o
    return convert(obj)


@dataclass
class ChatMessageToolCallDefinition:
    arguments: Any
    name: str
    description: str | None = None

@dataclass
class ChatMessageToolCall:
    function: ChatMessageToolCallDefinition
    id: str
    type: str

@dataclass
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: list[ChatMessageToolCall] | None = None
    raw: Any | None = None
    usage: dict | None = None          # normalized token usage of the call that produced it
    latency_s: float | None = None     # wall time of that call

    def model_dump_json(self):
        return json.dumps(get_dict_from_nested_dataclasses(self, ignore_key="raw"))

    @classmethod
    def from_dict(cls, data: dict, raw: Any | None = None) -> "ChatMessage":
        if data.get("tool_calls"):
            data["tool_calls"] = [
                ChatMessageToolCall(function=ChatMessageToolCallDefinition(**tc["function"]), id=tc["id"], type=tc["type"])
                for tc in data["tool_calls"]
            ]
        return cls(role=data["role"], content=data.get("content"), tool_calls=data.get("tool_calls"), raw=raw)

    def dict(self):
        return json.dumps(get_dict_from_nested_dataclasses(self))


@dataclass
class ChatMessageStreamDelta:
    content: str | None = None
    tool_calls: list[ChatMessageToolCall] | None = None


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_CALL = "tool-call"
    TOOL_RESPONSE = "tool-response"

    @classmethod
    def roles(cls):
        return [r.value for r in cls]


tool_role_conversions = {
    MessageRole.TOOL_CALL: MessageRole.ASSISTANT,
    MessageRole.TOOL_RESPONSE: MessageRole.USER,
}


def parse_json_if_needed(arguments: str | dict) -> str | dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        return json.loads(arguments)
    except Exception:
        return arguments


def get_tool_json_schema(tool: Tool) -> dict:
    properties = deepcopy(tool.inputs)
    required = []
    for key, value in properties.items():
        if value["type"] == "any":
            value["type"] = "string"
        if not ("nullable" in value and value["nullable"]):
            required.append(key)
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": {"type": "object", "properties": properties, "required": required}},
    }


def remove_stop_sequences(content: str, stop_sequences: list[str]) -> str:
    for stop in stop_sequences:
        if content[-len(stop):] == stop:
            content = content[:-len(stop)]
    return content


def get_clean_message_list(message_list, role_conversions=None, flatten_messages_as_text=False):
    role_conversions = role_conversions or {}
    output = []
    message_list = deepcopy(message_list)
    for msg in message_list:
        role = msg["role"]
        if role not in MessageRole.roles():
            raise ValueError(f"Unknown role {role}")
        if role in role_conversions:
            msg["role"] = role_conversions[role]
        if isinstance(msg["content"], list):
            for el in msg["content"]:
                assert isinstance(el, dict)
        if output and msg["role"] == output[-1]["role"]:
            if isinstance(msg["content"], list):
                if flatten_messages_as_text:
                    output[-1]["content"] += "\n" + msg["content"][0]["text"]
                else:
                    for el in msg["content"]:
                        if el["type"] == "text" and output[-1]["content"][-1]["type"] == "text":
                            output[-1]["content"][-1]["text"] += "\n" + el["text"]
                        else:
                            output[-1]["content"].append(el)
        else:
            content = msg["content"][0]["text"] if (flatten_messages_as_text and isinstance(msg["content"], list)) else msg["content"]
            output.append({"role": msg["role"], "content": content})
    return output


def get_tool_call_from_text(text, tool_name_key, tool_arguments_key):
    d, _ = parse_json_blob(text)
    tool_name = d[tool_name_key]
    tool_args = d.get(tool_arguments_key)
    if isinstance(tool_args, str):
        tool_args = parse_json_if_needed(tool_args)
    return ChatMessageToolCall(id=str(uuid.uuid4()), type="function", function=ChatMessageToolCallDefinition(name=tool_name, arguments=tool_args))


def supports_stop_parameter(model_id: str) -> bool:
    """OpenAI reasoning / gpt-5 models reject the 'stop' parameter."""
    model_name = model_id.split("/")[-1].lower()
    return not re.match(r"^(o1|o3|o4|gpt-5)", model_name)



# ---------------------------------------------------------------------------
# Per-call LLM logging (thread-local: one model object is shared by parallel tasks)
# ---------------------------------------------------------------------------

_TLS = threading.local()


def start_llm_call_log(path: str | None) -> None:
    """Record every model call made by THIS thread (and append it to `path`)."""
    _TLS.log_path = path
    _TLS.calls = []
    _TLS.last_usage = None


def stop_llm_call_log() -> list[dict]:
    """Stop recording for this thread; return the records collected."""
    calls = list(getattr(_TLS, "calls", []) or [])
    _TLS.log_path = None
    _TLS.calls = []
    return calls


def peek_llm_calls() -> list[dict]:
    """Records collected so far by this thread (recording continues)."""
    return list(getattr(_TLS, "calls", []) or [])


def thread_last_usage() -> dict | None:
    """Usage of the most recent call made by this thread."""
    return getattr(_TLS, "last_usage", None)


def _as_dict(obj) -> dict | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    for meth in ("model_dump", "to_dict", "to_json_dict"):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return d
            except Exception:  # noqa: BLE001
                pass
    out = {}
    for k in dir(obj):
        if k.startswith("_"):
            continue
        try:
            v = getattr(obj, k)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, (int, float, str, dict, type(None))):
            out[k] = v
    return out


def usage_from_raw(raw) -> dict:
    """
    Normalize token usage from any backend response:
      input_tokens (incl. cached), cached_input_tokens, cache_creation_input_tokens,
      output_tokens, reasoning_tokens, total_tokens, plus the provider's raw usage.
    """
    if raw is None:
        return {}
    u = _as_dict(getattr(raw, "usage", None)) or _as_dict(getattr(raw, "usage_metadata", None)) or {}

    def g(d, *path):
        for p in path:
            if not isinstance(d, dict):
                return None
            d = d.get(p)
        return d

    if "prompt_tokens" in u:                                   # OpenAI / vLLM
        inp, out = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        cached = g(u, "prompt_tokens_details", "cached_tokens") or 0
        reasoning = g(u, "completion_tokens_details", "reasoning_tokens") or 0
        creation, total = 0, u.get("total_tokens") or inp + out
    elif "input_tokens" in u:                                  # Anthropic
        cached = u.get("cache_read_input_tokens") or 0
        creation = u.get("cache_creation_input_tokens") or 0
        inp = (u.get("input_tokens") or 0) + cached + creation
        out, reasoning = u.get("output_tokens") or 0, 0
        total = inp + out
    elif "prompt_token_count" in u:                            # Gemini
        inp, out = u.get("prompt_token_count") or 0, u.get("candidates_token_count") or 0
        cached, creation = u.get("cached_content_token_count") or 0, 0
        reasoning = u.get("thoughts_token_count") or 0
        total = u.get("total_token_count") or inp + out + reasoning
    else:
        return {"raw": u}
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_creation_input_tokens": creation,
            "output_tokens": out, "reasoning_tokens": reasoning, "total_tokens": total, "raw": u}


def _jsonable(o):
    if isinstance(o, Enum):
        return o.value
    return str(o)


# Base Model
class Model:
    def __init__(self, flatten_messages_as_text=False, tool_name_key="name", tool_arguments_key="arguments", model_id=None, **kwargs):
        self.flatten_messages_as_text = flatten_messages_as_text
        self.tool_name_key = tool_name_key
        self.tool_arguments_key = tool_arguments_key
        self.kwargs = kwargs
        self.last_input_token_count: int | None = None
        self.last_output_token_count: int | None = None
        self.model_id: str | None = model_id

    def _prepare_completion_kwargs(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, custom_role_conversions=None, **kwargs):
        flatten = kwargs.pop("flatten_messages_as_text", self.flatten_messages_as_text)
        messages = get_clean_message_list(messages, role_conversions=custom_role_conversions or tool_role_conversions, flatten_messages_as_text=flatten)
        ckw = {**self.kwargs, "messages": messages}
        if stop_sequences and supports_stop_parameter(self.model_id or ""):
            ckw["stop"] = stop_sequences
        if grammar:
            ckw["grammar"] = grammar
        if tools_to_call_from:
            ckw["tools"] = [get_tool_json_schema(t) for t in tools_to_call_from]
            ckw["tool_choice"] = "required"
        ckw.update(kwargs)
        return ckw

    def get_token_counts(self):
        return {"input_token_count": self.last_input_token_count, "output_token_count": self.last_output_token_count}

    def generate(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> ChatMessage:
        raise NotImplementedError

    def __call__(self, *args, **kwargs):
        """Generate, and record the call (request, response, usage, latency) per thread."""
        messages = args[0] if args else kwargs.get("messages")
        ts_start = datetime.datetime.now().isoformat(timespec="milliseconds")
        t0 = time.time()
        msg, err = None, None
        _TLS.last_request_params = None      # set by generate() to the non-message request kwargs it sent
        try:
            msg = self.generate(*args, **kwargs)
            return msg
        except Exception as exc:  # noqa: BLE001  (recorded, then re-raised)
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            latency = round(time.time() - t0, 3)
            raw = getattr(msg, "raw", None) if msg is not None else None
            usage = usage_from_raw(raw)
            if msg is not None:
                msg.usage, msg.latency_s = usage, latency
            _TLS.last_usage = usage
            if getattr(_TLS, "calls", None) is not None or getattr(_TLS, "log_path", None):
                record = {
                    "call_index": len(getattr(_TLS, "calls", []) or []),
                    "ts_start": ts_start,
                    "latency_s": latency,
                    "model_id": self.model_id,
                    "reasoning_effort": getattr(self, "reasoning_effort", None),
                    # the request kwargs actually sent to the API, except the messages (logged separately)
                    "request_params": getattr(_TLS, "last_request_params", None),
                    "stop_sequences": kwargs.get("stop_sequences"),
                    "request_messages": messages,
                    "response_content": getattr(msg, "content", None) if msg is not None else None,
                    "response_id": getattr(raw, "id", None),
                    "response_model": getattr(raw, "model", None),
                    "usage": usage,
                    "error": err,
                }
                if not hasattr(_TLS, "calls") or _TLS.calls is None:
                    _TLS.calls = []
                _TLS.calls.append({k: v for k, v in record.items() if k != "request_messages"})
                path = getattr(_TLS, "log_path", None)
                if path:
                    try:
                        with open(path, "a") as fh:
                            fh.write(json.dumps(record, default=_jsonable) + "\n")
                    except Exception:  # noqa: BLE001  (logging must not break the agent)
                        pass

    def parse_tool_calls(self, message: ChatMessage) -> ChatMessage:
        message.role = MessageRole.ASSISTANT
        if not message.tool_calls:
            assert message.content is not None
            message.tool_calls = [get_tool_call_from_text(message.content, self.tool_name_key, self.tool_arguments_key)]
        for tc in message.tool_calls:
            tc.function.arguments = parse_json_if_needed(tc.function.arguments)
        return message

    def to_dict(self) -> dict:
        d = {**self.kwargs, "last_input_token_count": self.last_input_token_count, "last_output_token_count": self.last_output_token_count, "model_id": self.model_id}
        for attr in ["custom_role_conversion", "temperature", "max_tokens", "provider", "timeout", "api_base",
                     "reasoning_effort"]:
            if hasattr(self, attr):
                d[attr] = getattr(self, attr)
        return d

    @classmethod
    def from_dict(cls, d):
        inst = cls(**{k: v for k, v in d.items() if k not in ["last_input_token_count", "last_output_token_count"]})
        inst.last_input_token_count = d.get("last_input_token_count")
        inst.last_output_token_count = d.get("last_output_token_count")
        return inst


class ApiModel(Model):
    def __init__(self, model_id: str, custom_role_conversions=None, client=None, **kwargs):
        super().__init__(model_id=model_id, **kwargs)
        self.custom_role_conversions = custom_role_conversions or {}
        self.client = client or self.create_client()

    def create_client(self):
        raise NotImplementedError



# OpenAI (also used as base for vLLM)

class OpenAIServerModel(ApiModel):
    """OpenAI chat.completions backend.

    reasoning_effort: sent as the `reasoning_effort` request parameter on every call when not None
    (e.g. "medium" for gpt-5.4, the effort Codex runs it at in RQ4).
    """

    def __init__(self, model_id: str, api_base=None, api_key=None, organization=None, project=None, client_kwargs=None, custom_role_conversions=None, flatten_messages_as_text=False, reasoning_effort: str | None = None, **kwargs):
        self.client_kwargs = {**(client_kwargs or {}), "api_key": api_key, "base_url": api_base, "organization": organization, "project": project}
        self.reasoning_effort = reasoning_effort
        super().__init__(model_id=model_id, custom_role_conversions=custom_role_conversions, flatten_messages_as_text=flatten_messages_as_text, **kwargs)

    def create_client(self):
        import openai
        return openai.OpenAI(**self.client_kwargs)

    def build_request_kwargs(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> dict:
        """The exact kwargs passed to client.chat.completions.create (no API call)."""
        if self.reasoning_effort is not None:
            kwargs.setdefault("reasoning_effort", self.reasoning_effort)
        return self._prepare_completion_kwargs(messages=messages, stop_sequences=stop_sequences, grammar=grammar, tools_to_call_from=tools_to_call_from, model=self.model_id, custom_role_conversions=self.custom_role_conversions, **kwargs)

    def generate(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> ChatMessage:
        ckw = self.build_request_kwargs(messages, stop_sequences=stop_sequences, grammar=grammar, tools_to_call_from=tools_to_call_from, **kwargs)
        _TLS.last_request_params = {k: v for k, v in ckw.items() if k != "messages"}
        response = self.client.chat.completions.create(**ckw)
        self.last_input_token_count = response.usage.prompt_tokens
        self.last_output_token_count = response.usage.completion_tokens
        return ChatMessage.from_dict(response.choices[0].message.model_dump(include={"role", "content", "tool_calls"}), raw=response)

    def generate_stream(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> Generator[ChatMessageStreamDelta]:
        raise NotImplementedError("Streaming not yet implemented")



# Anthropic (Claude)
class AnthropicModel(ApiModel):
    """Anthropic Claude backend. Requires ``pip install anthropic``."""
    def __init__(self, model_id="claude-sonnet-4-6", api_key=None, max_tokens=4096, **kwargs):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        super().__init__(model_id=model_id, **kwargs)

    def create_client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.api_key)

    def generate(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> ChatMessage:
        system_msg, chat_msgs = self._split_system(messages)
        anthropic_msgs = self._to_anthropic_messages(chat_msgs)
        ckw: dict[str, Any] = {"model": self.model_id, "max_tokens": kwargs.pop("max_tokens", self.max_tokens), "messages": anthropic_msgs}
        if system_msg:
            ckw["system"] = system_msg
        if stop_sequences:
            ckw["stop_sequences"] = stop_sequences
        response_format = kwargs.pop("response_format", None)
        # If JSON output is requested, inject a reminder into the system prompt
        if response_format and response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema", {}).get("schema", {})
            required = schema.get("required", [])
            json_hint = (
                "\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object (no markdown, no extra text). "
                f"Required keys: {required}. Output nothing but the JSON object."
            )
            if ckw.get("system"):
                ckw["system"] += json_hint
            else:
                ckw["system"] = json_hint.strip()
        response = self.client.messages.create(**ckw)
        self.last_input_token_count = response.usage.input_tokens
        self.last_output_token_count = response.usage.output_tokens
        content = response.content[0].text if response.content else ""
        if stop_sequences:
            content = remove_stop_sequences(content, stop_sequences)
        return ChatMessage(role="assistant", content=content, raw=response)

    def _split_system(self, messages):
        sys_text, chat = None, []
        for m in messages:
            r = m.get("role", "")
            if r in (MessageRole.SYSTEM, "system"):
                c = m.get("content", "")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                sys_text = ((sys_text or "") + "\n" + c)
            else:
                chat.append(m)
        return (sys_text.strip() if sys_text else None), chat

    def _to_anthropic_messages(self, messages):
        result = []
        for m in messages:
            role = m.get("role", "user")
            if role in (MessageRole.TOOL_CALL, "tool-call"):
                role = "assistant"
            elif role in (MessageRole.TOOL_RESPONSE, "tool-response"):
                role = "user"
            elif role in (MessageRole.ASSISTANT, "assistant"):
                role = "assistant"
            else:
                role = "user"
            content = m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if result and result[-1]["role"] == role:
                result[-1]["content"] += "\n" + content
            else:
                result.append({"role": role, "content": content})
        return result



# Gemini

class GeminiModel(ApiModel):
    """Google Gemini backend. Requires ``pip install google-genai``."""
    def __init__(self, model_id="gemini-2.0-flash", api_key=None, **kwargs):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._system_instruction = None
        super().__init__(model_id=model_id, **kwargs)

    def create_client(self):
        from google import genai
        return genai.Client(api_key=self.api_key)

    def _to_prompt(self, messages):
        """Convert messages to Gemini Content objects with proper roles and system instruction."""
        from google.genai import types
        system_parts = []
        contents = []
        for m in messages:
            role = m.get("role", "user")
            if isinstance(role, MessageRole):
                role = role.value
            c = m.get("content", "")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            if role == "system":
                system_parts.append(c)
            else:
                gemini_role = "model" if role in ("assistant", "tool-call") else "user"
                if contents and contents[-1]["role"] == gemini_role:
                    contents[-1]["parts"][0]["text"] += "\n" + c
                else:
                    contents.append({"role": gemini_role, "parts": [{"text": c}]})
        self._system_instruction = "\n\n".join(system_parts) if system_parts else None
        return contents

    @staticmethod
    def _clean_schema_for_gemini(schema):
        """Remove fields that the Gemini API does not accept in response_schema."""
        if not isinstance(schema, dict):
            return schema
        # Keys not supported by Gemini's Schema proto
        _unsupported = {"additionalProperties", "title", "strict", "$schema"}
        cleaned = {k: v for k, v in schema.items() if k not in _unsupported}
        # Recurse into properties and nested schemas
        if "properties" in cleaned:
            cleaned["properties"] = {
                k: GeminiModel._clean_schema_for_gemini(v)
                for k, v in cleaned["properties"].items()
            }
        for key in ("items", "anyOf", "oneOf", "allOf"):
            if key in cleaned:
                val = cleaned[key]
                if isinstance(val, dict):
                    cleaned[key] = GeminiModel._clean_schema_for_gemini(val)
                elif isinstance(val, list):
                    cleaned[key] = [GeminiModel._clean_schema_for_gemini(v) for v in val]
        return cleaned

    def generate(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> ChatMessage:
        from google.genai import types
        contents = self._to_prompt(messages)
        config = {}
        if self._system_instruction:
            config["system_instruction"] = self._system_instruction
        if stop_sequences:
            config["stop_sequences"] = stop_sequences
        response_format = kwargs.pop("response_format", None)
        if response_format and response_format.get("type") == "json_schema":
            config["response_mime_type"] = "application/json"
            schema = response_format.get("json_schema", {}).get("schema")
            if schema:
                config["response_schema"] = self._clean_schema_for_gemini(schema)
        response = self.client.models.generate_content(
            model=self.model_id,
            contents=contents,
            config=types.GenerateContentConfig(**config) if config else None,
        )
        usage = getattr(response, "usage_metadata", None)
        self.last_input_token_count = getattr(usage, "prompt_token_count", 0) if usage else 0
        self.last_output_token_count = getattr(usage, "candidates_token_count", 0) if usage else 0
        content = response.text if response.text else ""
        if stop_sequences:
            content = remove_stop_sequences(content, stop_sequences)
        return ChatMessage(role="assistant", content=content, raw=response)



# vLLM (OpenAI-compatible)

class VLLMModel(ApiModel):
    """vLLM-served models via OpenAI-compatible API. Requires ``pip install openai``."""
    def __init__(self, model_id: str, api_base="http://localhost:8000/v1", api_key=None, **kwargs):
        self.api_key = api_key or os.getenv("VLLM_API_KEY", "EMPTY")
        self.api_base = api_base
        super().__init__(model_id=model_id, **kwargs)

    def create_client(self):
        import openai
        return openai.OpenAI(api_key=self.api_key, base_url=self.api_base)

    def generate(self, messages, stop_sequences=None, grammar=None, tools_to_call_from=None, **kwargs) -> ChatMessage:
        clean = get_clean_message_list(messages, role_conversions=self.custom_role_conversions or tool_role_conversions)
        ckw: dict[str, Any] = {"model": self.model_id, "messages": clean}
        if stop_sequences:
            ckw["stop"] = stop_sequences
        if "response_format" in kwargs:
            ckw["response_format"] = kwargs.pop("response_format")
        if grammar:
            ckw["extra_body"] = {"guided_json": grammar}
        response = self.client.chat.completions.create(**ckw)
        u = response.usage
        self.last_input_token_count = u.prompt_tokens if u else 0
        self.last_output_token_count = u.completion_tokens if u else 0
        return ChatMessage.from_dict(response.choices[0].message.model_dump(include={"role", "content", "tool_calls"}), raw=response)
