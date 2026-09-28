import atexit
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("veriact_verifier_tool")

# Cap OpenJML's JVM heap (its default max heap is ~1/4 of RAM). Overridable via env.
os.environ.setdefault("OPENJML_JVM", "-Xmx8g")

# OpenJML runs in its own session (setsid) so a timeout kills its whole tree; such groups do not get the
# caller's Ctrl-C / kill, so live ones are tracked and killed on exit (and by a runner's stop handler).
_LIVE_GROUPS: set[int] = set()
_LIVE_LOCK = threading.Lock()


def kill_live_groups() -> None:
    with _LIVE_LOCK:
        groups = list(_LIVE_GROUPS)
    for g in groups:
        try:
            os.killpg(g, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


atexit.register(kill_live_groups)


# TASK SCHEMA
@dataclass
class TestCase:
    input: str
    output: str

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "TestCase":
        return cls(input=data["input"], output=data["output"])


@dataclass
class Task:
    task_id: str
    code: str
    class_name: str
    test_name: str
    javadoc: str
    category: str
    origin_id: str
    test_code: str = ""
    test_inputs: list[TestCase] = field(default_factory=list)
    generated_test_cases: list[TestCase] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            code=data["code"],
            class_name=data["class_name"],
            test_name=data["test_name"],
            javadoc=data["javadoc"],
            category=data["category"],
            origin_id=data["origin_id"],
            test_code=data.get("test_code", ""),
            test_inputs=[TestCase.from_dict(tc) for tc in data.get("test_inputs", [])],
            generated_test_cases=[
                TestCase.from_dict(tc) for tc in data.get("generated_test_cases", [])
            ],
        )



# ERROR CLASSIFICATION
def extract_errors(error_message: str) -> list[tuple[str, str]]:
    pattern = r"(/tmp/[^:]+:\d+: )(\w+):"
    matches = re.split(r"(?=/tmp/[^:]+:\d+: \w+:)", error_message)
    errors = []
    for match in matches:
        if match.strip():
            level_match = re.search(pattern, match)
            if level_match:
                error_level = level_match.group(2)
                if error_level != "warning":
                    if (
                        "Associated declaration:" in match
                        or "Associated method exit" in match
                    ):
                        if len(errors) == 0:
                            continue
                        errors[-1] = (errors[-1][0], errors[-1][1] + match)
                    else:
                        errors.append((error_level, match.strip()))
    return errors


def verification_failure_map(error_message: str) -> str:
    mapping = [
        ("LoopInvariantBeforeLoop", "LoopInvariantFailure"),
        ("ArithmeticOperationRange", "ArithmeticOperationRange"),
        ("Assignable", "AssignableFailure"),
        ("Postcondition:", "PostconditionFailure"),
        ("Assert)", "AssertFailure"),
        ("UndefinedNullDeReference", "NullDeReference"),
        ("PossiblyNullDeReference", "NullDeReference"),
        ("LoopInvariant)", "LoopInvariantFailure"),
        ("PossiblyNegativeIndex", "ArrayIndex"),
        ("PossiblyNegativeSize", "NegativeSize"),
        ("PossiblyTooLargeIndex", "ArrayIndex"),
        ("LoopDecreases", "RankingFunctionFailure"),
        ("PossiblyBadArrayAssignment", "BadArrayAssignment"),
        ("Precondition:", "PreconditionFailure"),
        ("Precondition conjunct is false:", "PreconditionFailure"),
        ("UndefinedTooLargeIndex", "ArrayIndex"),
        ("PossiblyDivideByZero", "DivideByZero"),
        ("PossiblyBadCast", "BadCast"),
        ("UndefinedDivideByZero", "DivideByZero"),
        ("ExceptionalPostcondition:", "ExceptionalPostconditionFailure"),
        ("UndefinedCalledMethodPrecondition:", "CalledMethodPrecondition"),
        ("UndefinedNegativeIndex", "ArrayIndex"),
        ("PossiblyNullUnbox", "NullUnbox"),
        ("LoopDecreasesNonNegative", "RankingFunctionFailure"),
        ("Postcondition)", "PostconditionFailure"),
        ("ArithmeticCastRange", "ArithmeticCastRange"),
        ("UndefinedNullUnbox", "NullUnbox"),
        ("PossiblyLargeShift", "LargeShift"),
    ]
    for pat, failure_type in mapping:
        if pat in error_message:
            return failure_type
    return "UnknownVerificationFailure"


def classify_failures(error_level: str, error_message: str) -> str:
    if error_level == "error":
        return "SyntaxError"
    elif error_level == "verify":
        return verification_failure_map(error_message)
    else:
        return "UnknownError"



# OPENJML VERIFICATION
@dataclass
class VerificationResult:
    success: bool
    error_log: str
    return_code: int
    classified_errors: list[dict] = field(default_factory=list)


def write_to_file(content: str, filepath: str) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)


def verify_with_openjml(
    code_with_spec: str,
    classname: str,
    timeout: int = 300,
    output_dir: str = "veriact_outputs/tmp",
) -> VerificationResult:

    run_id = uuid.uuid4().hex
    output_dir = os.path.join(output_dir, f"veriact_{run_id}")
    os.makedirs(output_dir, exist_ok=True)

    tmp_filename = os.path.join(output_dir, f"{classname}.java")
    write_to_file(code_with_spec, tmp_filename)

    cmd = [
        "openjml",
        "--esc",
        "--esc-max-warnings",
        "1",
        "--prover=cvc4",
        "--nonnull-by-default",
        "--arithmetic-failure=quiet",
        "-nowarn",
        tmp_filename,
    ]

    with _LIVE_LOCK:                  # launch and registration are atomic w.r.t. kill_live_groups
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )
        _LIVE_GROUPS.add(proc.pid)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        raw_output = stdout + stderr
        return_code = proc.returncode
        return return_verification_result(raw_output, return_code)
    except subprocess.TimeoutExpired:
        raw_output = (
            f"Timeout: OpenJML verification exceeded time limit of {timeout} seconds"
        )
        return_code = 1
        return return_verification_result(raw_output, return_code)
    except Exception as e:
        raw_output = f"Error: {str(e)}"
        return_code = 1
        return return_verification_result(raw_output, return_code)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        with _LIVE_LOCK:
            _LIVE_GROUPS.discard(proc.pid)


def return_verification_result(raw_output: str, return_code: int) -> VerificationResult:
    classified_errors = []
    if return_code != 0 and raw_output.strip():
        try:
            errors = extract_errors(raw_output)
            for error_level, error_msg in errors:
                error_type = classify_failures(error_level, error_msg)
                classified_errors.append({"type": error_type, "raw": error_msg[:500]})
        except Exception:
            classified_errors.append({"type": "ParseError", "raw": raw_output[:500]})

    success = return_code == 0 and len(classified_errors) == 0
    return VerificationResult(
        success=success,
        error_log=raw_output,
        return_code=return_code,
        classified_errors=classified_errors,
    )



# GRADUATED SCORING
def compute_graduated_score(result: VerificationResult) -> float:
    """
    Graduated scoring for optimization. Gives the optimizer a gradient.

        1.0  - Verification passes
        0.3  - 1 verification error (almost correct)
        0.1  - 2+ verification errors (valid JML, semantically wrong)
        0.0  - Syntax error or empty output
    """
    if result.success:
        return 1.0
    if not result.classified_errors:
        return 0.0

    syntax_errors = [e for e in result.classified_errors if e["type"] == "SyntaxError"]
    verification_errors = [
        e for e in result.classified_errors if e["type"] != "SyntaxError"
    ]

    if syntax_errors:
        return 0.0
    if len(verification_errors) == 1:
        return 0.3
    return 0.1



# HELPERS
def clean_code_fences(text: str) -> str:
    text = re.sub(r"```java\s*\n?", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def format_error_feedback(result: VerificationResult, task_id: str) -> str:
    if result.success:
        return f"[PASS] Task '{task_id}': Specification verified by OpenJML."

    error_counts: dict[str, int] = {}
    for err in result.classified_errors:
        t = err["type"]
        error_counts[t] = error_counts.get(t, 0) + 1

    error_summary = ", ".join(
        f"{t} (x{c})" if c > 1 else t for t, c in error_counts.items()
    )

    raw_log = result.error_log
    if len(raw_log) > 800:
        raw_log = raw_log[:800] + "\n... [truncated]"

    return (
        f"[FAIL] Task '{task_id}': {error_summary}\n"
        f"--- OpenJML Log ---\n{raw_log}\n"
        f"--- Fix Guidance ---\n"
        f"SyntaxError: Fix JML syntax (missing semicolons, wrong keywords).\n"
        f"PostconditionFailure: The @ensures clause is logically incorrect.\n"
        f"LoopInvariantFailure: The @maintaining clause doesn't hold.\n"
        f"RankingFunctionFailure: The @decreases expression is wrong.\n"
        f"ArrayIndex: Missing array bounds check in @requires.\n"
        f"NullDeReference: Missing null check in @requires.\n"
        f"ArithmeticOperationRange: Integer overflow not guarded."
    )
