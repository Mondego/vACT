"""Utility functions: errors, parsing, code helpers."""

import ast
import base64
import importlib.metadata
import importlib.util
import inspect
import json
import keyword
import math
import os
import re
import types
import tokenize
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, Tuple


BASE_PYTHON_TOOLS = {
    "print": print, "isinstance": isinstance, "range": range, "float": float,
    "int": int, "bool": bool, "str": str, "set": set, "list": list, "dict": dict,
    "tuple": tuple, "round": round, "ceil": math.ceil, "floor": math.floor,
    "log": math.log, "exp": math.exp, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "atan2": math.atan2, "degrees": math.degrees, "radians": math.radians,
    "pow": pow, "sqrt": math.sqrt, "len": len, "sum": sum, "max": max,
    "min": min, "abs": abs, "enumerate": enumerate, "zip": zip,
    "reversed": reversed, "sorted": sorted, "all": all, "any": any,
    "map": map, "filter": filter, "ord": ord, "chr": chr, "next": next,
    "iter": iter, "divmod": divmod, "callable": callable, "getattr": getattr,
    "hasattr": hasattr, "setattr": setattr, "issubclass": issubclass,
    "type": type, "complex": complex,
}


@lru_cache
def _is_package_available(package_name: str) -> bool:
    try:
        importlib.metadata.version(package_name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


BASE_BUILTIN_MODULES = [
    "collections", "datetime", "itertools", "math", "queue",
    "random", "re", "stat", "statistics", "time", "unicodedata",
]


def escape_code_brackets(text: str) -> str:
    def replace_bracketed_content(match):
        content = match.group(1)
        cleaned = re.sub(r"bold|red|green|blue|yellow|magenta|cyan|white|black|italic|dim|\s|#[0-9a-fA-F]{6}", "", content)
        return f"\\[{content}\\]" if cleaned.strip() else f"[{content}]"
    return re.sub(r"\[([^\]]*)\]", replace_bracketed_content, text)


class AgentError(Exception):
    def __init__(self, message, logger=None):
        super().__init__(message)
        self.message = message
        if logger:
            logger.log_error(message)

    def dict(self) -> Dict[str, str]:
        return {"type": self.__class__.__name__, "message": str(self.message)}

class AgentParsingError(AgentError):
    pass

class AgentExecutionError(AgentError):
    pass

class AgentMaxStepsError(AgentError):
    pass

class AgentToolCallError(AgentExecutionError):
    pass

class AgentToolExecutionError(AgentExecutionError):
    pass

class AgentGenerationError(AgentError):
    pass


def make_json_serializable(obj: Any) -> Any:
    if obj is None:
        return None
    elif isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, str):
            try:
                if (obj.startswith("{") and obj.endswith("}")) or (obj.startswith("[") and obj.endswith("]")):
                    return make_json_serializable(json.loads(obj))
            except json.JSONDecodeError:
                pass
        return obj
    elif isinstance(obj, (list, tuple)):
        return [make_json_serializable(item) for item in obj]
    elif isinstance(obj, dict):
        return {str(k): make_json_serializable(v) for k, v in obj.items()}
    elif hasattr(obj, "__dict__"):
        return {"_type": obj.__class__.__name__, **{k: make_json_serializable(v) for k, v in obj.__dict__.items()}}
    else:
        return str(obj)


def parse_json_blob(json_blob: str) -> Tuple[Dict[str, str], str]:
    try:
        first = json_blob.find("{")
        last = [a.start() for a in list(re.finditer("}", json_blob))][-1]
        json_data = json.loads(json_blob[first : last + 1], strict=False)
        return json_data, json_blob[:first]
    except IndexError:
        raise ValueError("The JSON blob is invalid")
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON decode error: {e}")


def parse_code_blobs(text: str) -> str:
    pattern = r"```(?:py|python)?\n(.*?)\n```"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        _action = "\n\n".join(match.strip() for match in matches)
        try:
            ast.parse(_action)
            return _action
        except SyntaxError:
            raise ValueError(f"Invalid Python code:\n{_action}")
    try:
        ast.parse(text)
        return text
    except SyntaxError:
        pass
    raise ValueError(f"No valid code block found in:\n{text[:200]}")


CODEAGENT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "schema": {
            "additionalProperties": False,
            "properties": {
                "thought": {"description": "Free form reasoning.", "title": "Thought", "type": "string"},
                "code": {"description": "Valid Python code snippet.", "title": "Code", "type": "string"},
            },
            "required": ["thought", "code"],
            "title": "ThoughtAndCodeAnswer",
            "type": "object",
        },
        "name": "ThoughtAndCodeAnswer",
        "strict": True,
    },
}


def extract_code_from_text(text: str, code_block_tags: tuple[str, str]) -> str | None:
    pattern = rf"{code_block_tags[0]}(.*?){code_block_tags[1]}"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        return "\n\n".join(match.strip() for match in matches)
    return None


MAX_LENGTH_TRUNCATE_CONTENT = 20000

def truncate_content(content: str, max_length: int = MAX_LENGTH_TRUNCATE_CONTENT) -> str:
    if len(content) <= max_length:
        return content
    return content[: max_length // 2] + "\n..._truncated_...\n" + content[-max_length // 2 :]


class ImportFinder(ast.NodeVisitor):
    def __init__(self):
        self.packages = set()
    def visit_Import(self, node):
        for alias in node.names:
            self.packages.add(alias.name.split(".")[0])
    def visit_ImportFrom(self, node):
        if node.module:
            self.packages.add(node.module.split(".")[0])


def get_source(obj) -> str:
    if not (isinstance(obj, type) or callable(obj)):
        raise TypeError(f"Expected class or callable, got {type(obj)}")
    try:
        source = getattr(obj, "__source__", None) or inspect.getsource(obj)
        return dedent(source).strip()
    except OSError as e:
        try:
            import IPython
            shell = IPython.get_ipython()
            if not shell:
                raise ImportError
            all_cells = "\n".join(shell.user_ns.get("In", [])).strip()
            tree = ast.parse(all_cells)
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == obj.__name__:
                    return dedent("\n".join(all_cells.split("\n")[node.lineno - 1 : node.end_lineno])).strip()
            raise ValueError(f"Could not find source for {obj.__name__}")
        except ImportError:
            raise e


def get_method_source(method):
    if isinstance(method, types.MethodType):
        method = method.__func__
    return get_source(method)


def is_same_method(m1, m2):
    try:
        s1 = "\n".join(l for l in get_method_source(m1).split("\n") if not l.strip().startswith("@"))
        s2 = "\n".join(l for l in get_method_source(m2).split("\n") if not l.strip().startswith("@"))
        return s1 == s2
    except (TypeError, OSError):
        return False


def instance_to_source(instance, base_cls=None):
    cls = instance.__class__
    class_name = cls.__name__
    class_lines = []
    if base_cls:
        class_lines.append(f"class {class_name}({base_cls.__name__}):")
    else:
        class_lines.append(f"class {class_name}:")
    if cls.__doc__ and (not base_cls or cls.__doc__ != base_cls.__doc__):
        class_lines.append(f'    """{cls.__doc__}"""')
    class_attrs = {
        name: value for name, value in cls.__dict__.items()
        if not name.startswith("__") and not callable(value)
        and not (base_cls and hasattr(base_cls, name) and getattr(base_cls, name) == value)
    }
    for name, value in class_attrs.items():
        if isinstance(value, str):
            if "\n" in value:
                class_lines.append(f'    {name} = """{value.replace(chr(34)*3, chr(92)+chr(34)*3)}"""')
            else:
                class_lines.append(f"    {name} = {json.dumps(value)}")
        else:
            class_lines.append(f"    {name} = {repr(value)}")
    if class_attrs:
        class_lines.append("")
    methods = {
        name: func for name, func in cls.__dict__.items()
        if callable(func) and (
            not base_cls or not hasattr(base_cls, name)
            or isinstance(func, (staticmethod, classmethod))
            or (getattr(base_cls, name).__code__.co_code != func.__code__.co_code)
        )
    }
    for name, method in methods.items():
        method_source = get_source(method)
        method_lines = method_source.split("\n")
        indent = len(method_lines[0]) - len(method_lines[0].lstrip())
        method_lines = [line[indent:] for line in method_lines]
        method_source = "\n".join(["    " + line if line.strip() else line for line in method_lines])
        class_lines.append(method_source)
        class_lines.append("")
    import_finder = ImportFinder()
    import_finder.visit(ast.parse("\n".join(class_lines)))
    final_lines = []
    if base_cls:
        final_lines.append(f"from {base_cls.__module__} import {base_cls.__name__}")
    for package in import_finder.packages:
        final_lines.append(f"import {package}")
    if final_lines:
        final_lines.append("")
    final_lines.extend(class_lines)
    return "\n".join(final_lines)


def encode_image_base64(image):
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

def make_image_url(base64_image):
    return f"data:image/png;base64,{base64_image}"

def is_valid_name(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name) if isinstance(name, str) else False


def find_functions_regex(code, function_list):
    found = []
    for func in function_list:
        if re.search(r'\b' + re.escape(func) + r'\s*\(', code):
            found.append(func)
    return found

def find_functions_ast(code, function_list):
    found = set()
    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id in function_list:
                found.add(node.func.id)
            self.generic_visit(node)
    try:
        V().visit(ast.parse(code))
        return list(found)
    except SyntaxError:
        return find_functions_regex(code, function_list)

def find_tool_usage(code: str, tool_list: list[str]):
    try:
        return find_functions_ast(code, tool_list)
    except Exception:
        return find_functions_regex(code, tool_list)
