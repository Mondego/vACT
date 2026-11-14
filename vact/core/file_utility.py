"""File and JSON I/O helpers."""

import json
from typing import Any


def write_to_file(content: str, file_name: str) -> None:
    with open(file_name, "w") as f:
        f.write(content)

def read_from_file(file_name: str) -> str:
    with open(file_name, "r") as f:
        return f.read()

def load_json(file: str) -> Any:
    with open(file, "r") as j:
        return json.loads(j.read())

def dump_json(data: Any, file_name: str) -> None:
    with open(file_name, "w") as j:
        json.dump(data, j, indent=2)

def load_jsonl(file: str) -> list[Any]:
    data = []
    with open(file, "r") as j:
        for line in j:
            if line.strip():
                data.append(json.loads(line))
    return data

def dump_jsonl(data: list[Any], file_name: str) -> None:
    with open(file_name, "w") as j:
        for item in data:
            j.write(json.dumps(item) + "\n")
