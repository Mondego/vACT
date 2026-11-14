import os
import json
import time
import signal
import shutil
import logging
import threading
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict
from concurrent.futures import ThreadPoolExecutor, as_completed


from baselines.utils.file_utility import (
    load_json,
    load_jsonl,
    read_from_file,
    write_to_file,
    dump_json,
)
from baselines.utils.logger import create_logger
from baselines.utils.verifier import verify_with_openjml


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


class DaikonResult(TypedDict):
    status: str
    esc_annotated_code: str
    jml_annotated_code: str
    esc_verified: bool
    jml_verified: bool
    timed_out: bool
    final_error: str
    final_code: str


class _WorkerResultRequired(TypedDict):
    task_id: str
    status: str
    class_name: str


class WorkerResult(_WorkerResultRequired, total=False):
    verifier_calls: int
    log_file: str
    final_code: str
    esc_annotated_code: str
    jml_annotated_code: str
    esc_verified: bool
    jml_verified: bool
    final_error: str
    message: str


class Daikon:

    def __init__(
        self,
        run_id: str,
        code: str,
        test_code: str,
        test_inputs: list[TestCase],
        class_name: str,
        test_name: str,
        out_dir: str,
        openjml_timeout: int,
        daikon_timeout: int,
        logger: logging.Logger,
        verbose: bool = False,
    ) -> None:
        self.run_id = run_id
        self.code = code
        self.test_code = test_code
        self.test_inputs = test_inputs
        self.class_name = class_name
        self.test_name = test_name
        self.out_dir = out_dir
        self.openjml_timeout = openjml_timeout
        self.daikon_timeout = daikon_timeout
        self.verbose = verbose
        self.logger = logger
        self._BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run_command_in_popen(
        self, _command: list[str] | str, _wait_time: Optional[float] = None, cwd: Optional[str] = None
    ) -> int:
        process = subprocess.Popen(
            _command,
            cwd=cwd,  # per-task working directory: os.chdir would be shared by all worker threads
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=isinstance(_command, str),
            start_new_session=True,  # own process group so killpg reaches Java children
        )

        timed_out_flag: list[bool] = [False]

        def _kill_group() -> None:
            timed_out_flag[0] = True
            self.logger.info(
                f"Timeout reached. Killing process group for {self.run_id}..."
            )
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # process already finished

        timer: Optional[threading.Timer] = None
        if _wait_time is not None:
            timer = threading.Timer(_wait_time, _kill_group)
            timer.start()

        try:
            for line in process.stdout:
                if self.verbose:
                    self.logger.info(line.strip())
                else:
                    print(line, end="")

            process.wait()

        except KeyboardInterrupt:
            self.logger.info(
                f"Interrupted by user. Killing process group for {self.run_id}..."
            )
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            return -1

        finally:
            if timer is not None:
                timer.cancel()
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

        if timed_out_flag[0]:
            return -9  # killed by Python-side timer (SIGKILL)

        return process.returncode

    def _prepare_task_environment(self) -> None:
        try:
            write_to_file(
                self.code, os.path.join(self.out_dir, f"{self.class_name}.java")
            )
            write_to_file(
                self.test_code,
                os.path.join(self.out_dir, f"{self.test_name}.java"),
            )
            for index in range(0, len(self.test_inputs)):
                write_to_file(
                    self.test_inputs[index].input,
                    os.path.join(self.out_dir, f"{index}.txt"),
                )

            _script_path = os.path.join(self._BASE_DIR, "daikon")
            shutil.copy(os.path.join(_script_path, "daikon.sh"), self.out_dir)
            shutil.copy(os.path.join(_script_path, "inv_config.config"), self.out_dir)
            self._run_command_in_popen(
                ["chmod", "+x", os.path.join(_script_path, "daikon.sh")]
            )

        except Exception as e:
            self.logger.error(f"Failed to prepare task environment: {e}")
            raise e

    def run(self) -> DaikonResult:
        self._prepare_task_environment()
        test_count: int = len(self.test_inputs)
        command: list[str] = [
            os.path.join(os.path.abspath(self.out_dir), "daikon.sh"),
            self.class_name,
            self.test_name,
            str(test_count),
        ]
        self.logger.info(f"Running Daikon with command: {command} for {self.run_id}")
        exit_code: int = self._run_command_in_popen(command, self.daikon_timeout, cwd=os.path.abspath(self.out_dir))
        if exit_code != 0:
            # Map shell exit codes to statuses
            _exit_messages: dict[int, tuple[str, str]] = {
                3: ("timed_out", "DynComp timed out (exit 3)"),
                -9: ("timed_out", "Daikon pipeline killed by Python-side timeout"),
                -1: ("timed_out", "Daikon pipeline interrupted"),
                2: ("unverified", "Compilation failed (exit 2)"),
                4: ("unverified", "Chicory trace generation failed (exit 4)"),
                5: ("unverified", "Daikon inference failed (exit 5)"),
                6: ("unverified", "JML annotation failed (exit 6)"),
                7: ("unverified", "ESC annotation failed (exit 7)"),
            }
            _status, _msg = _exit_messages.get(
                exit_code,
                ("unverified", f"Daikon execution failed (exit code {exit_code})"),
            )
            self.logger.error(f"[{self.run_id}] {_msg}")
            return DaikonResult(
                status=_status,
                esc_annotated_code="",
                jml_annotated_code="",
                final_code="",
                esc_verified=False,
                jml_verified=False,
                timed_out=_status == "timed_out",
                final_error=_msg,
            )
        self.logger.info(f"Daikon execution completed successfully for {self.run_id}")

        escspec_path = os.path.join(
            self.out_dir, f"{self.class_name}.java-escannotated"
        )
        jmlspec_path = os.path.join(
            self.out_dir, f"{self.class_name}.java-jmlannotated"
        )

        if not os.path.exists(escspec_path) and not os.path.exists(jmlspec_path):
            self.logger.error(
                f"Daikon produced no annotated output files for {self.run_id}"
            )
            return DaikonResult(
                status="unverified",
                esc_annotated_code="",
                jml_annotated_code="",
                esc_verified=False,
                jml_verified=False,
                timed_out=False,
                final_error="Daikon produced no annotated output files.",
                final_code="",
            )

        code_with_escspec: str = (
            read_from_file(escspec_path) if os.path.exists(escspec_path) else ""
        )
        code_with_jmlspec: str = (
            read_from_file(jmlspec_path) if os.path.exists(jmlspec_path) else ""
        )
        jml_error_info, jml_returncode = verify_with_openjml(
            code_with_jmlspec,
            self.class_name,
            self.openjml_timeout,
            self.out_dir,
            self.logger,
        )
        esc_error_info, esc_returncode = verify_with_openjml(
            code_with_escspec,
            self.class_name,
            self.openjml_timeout,
            self.out_dir,
            self.logger,
        )
        self.logger.info(f"Daikon run completed for task {self.run_id}")

        esc_verified: bool = esc_returncode == 0
        jml_verified: bool = jml_returncode == 0

        # final_code / final_error belong to the variant that verified (JML preferred, else ESC); if neither
        # verified, the JML variant (or ESC if Daikon wrote no JML file) with its own error
        if jml_verified or not esc_verified:
            chosen_code = code_with_jmlspec if (code_with_jmlspec or not code_with_escspec) else code_with_escspec
            final_error: str = jml_error_info if (code_with_jmlspec or not code_with_escspec) else esc_error_info
        else:
            chosen_code, final_error = code_with_escspec, esc_error_info

        # OpenJML timeout counts as timed_out, not unverified
        if "Timeout:" in final_error or "timeout" in final_error.lower():
            status = "timed_out"
            timed_out = True
        elif esc_verified or jml_verified:
            status = "verified"
            timed_out = False
        else:
            status = "unverified"
            timed_out = False

        return DaikonResult(
            status=status,
            esc_annotated_code=code_with_escspec,
            jml_annotated_code=code_with_jmlspec,
            final_code=chosen_code,
            esc_verified=esc_verified,
            jml_verified=jml_verified,
            timed_out=timed_out,
            final_error=final_error,
        )


class DaikonRunner:

    def __init__(
        self,
        name: str,
        input: str,
        output: str,
        openjml_timeout: int,
        daikon_timeout: int,
        threads: int,
        verbose: bool = False,
    ) -> None:
        self.name = name
        self.input_path: str = input
        self.output = output
        self.openjml_timeout = openjml_timeout
        self.daikon_timeout = daikon_timeout
        self.threads = threads
        self.verbose = verbose
        self.input_length: int = 0
        self._results_lock = threading.Lock()
        self._results_file: str = ""

    def _run_daikon(self, task: Task) -> WorkerResult:
        class_name: str = task.class_name
        input_code: str = task.code
        task_id: str = task.task_id
        test_code: str = task.test_code
        test_inputs: list[TestCase] = task.test_inputs
        all__test_inputs: list[TestCase] = test_inputs + task.generated_test_cases

        _BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_dir: str = os.path.abspath(self.output)
        _thread_id: Optional[int] = threading.current_thread().ident
        log_file: str = "unknown"
        try:
            logger, log_file = create_logger(task_id, _thread_id, output_dir)
        except Exception as e:
            print(f"Failed to create logger for {task_id}: {e}")
            return WorkerResult(
                task_id=task_id,
                status="error",
                message=f"Logger creation failed: {str(e)}",
                class_name=class_name,
                log_file="unknown",
            )
        logger.info(f"Starting Daikon for {task_id} (Thread ID: {_thread_id})")

        _thread_daikon_artifacts: str = os.path.join(
            output_dir, f"{task_id}_{_thread_id}"
        )
        Path(_thread_daikon_artifacts).mkdir(parents=True, exist_ok=True)

        daikon = Daikon(
            run_id=f"{task_id}_{_thread_id}",
            code=input_code,
            test_code=test_code,
            test_inputs=all__test_inputs,
            class_name=class_name,
            test_name=task.test_name,
            out_dir=_thread_daikon_artifacts,
            openjml_timeout=self.openjml_timeout,
            daikon_timeout=self.daikon_timeout,
            logger=logger,
            verbose=self.verbose,
        )

        try:
            _result = daikon.run()

            if _result.get("esc_verified", False) or _result.get("jml_verified", False):
                verified_status = "✓ VERIFIED"
            elif _result.get("timed_out", False):
                verified_status = "⏱ TIMED OUT"
            else:
                verified_status = "✗ UNVERIFIED"

            logger.info(
                f"{verified_status} - Completed {class_name} with 2 verifier calls"
            )

            return WorkerResult(
                task_id=task_id,
                status=_result["status"],
                class_name=class_name,
                verifier_calls=2,
                log_file=log_file,
                esc_annotated_code=_result["esc_annotated_code"],
                jml_annotated_code=_result["jml_annotated_code"],
                final_code=_result["jml_annotated_code"],
                esc_verified=_result.get("esc_verified", False),
                jml_verified=_result.get("jml_verified", False),
                final_error=_result.get("final_error", ""),
            )

        except Exception as e:
            logger.error(f"Error processing {task_id}: {e}", exc_info=True)
            return WorkerResult(
                task_id=task_id,
                status="unknown",
                message=str(e),
                class_name=class_name,
                log_file=log_file,
            )

        finally:
            logger.info(f"Finished Daikon for {task_id} (Thread ID: {_thread_id})")

    def _append_result(self, result: WorkerResult) -> None:
        """Append a single result to the JSONL file (thread-safe)."""
        with self._results_lock:
            with open(self._results_file, "a") as f:
                f.write(json.dumps(result) + "\n")

    def _save_results(self, duration: float, results: list[WorkerResult]) -> None:
        """Save summary statistics to a JSON file"""
        verified = [r for r in results if r["status"] == "verified"]
        unverified = [r for r in results if r["status"] == "unverified"]
        timed_out = [r for r in results if r["status"] == "timed_out"]
        unknown = [r for r in results if r["status"] == "unknown"]

        total_verifier_calls = sum(
            r.get("verifier_calls", 0) for r in (verified + unverified + timed_out)
        )
        avg_verifier_calls = (
            total_verifier_calls / len(verified + unverified + timed_out)
            if (verified + unverified + timed_out)
            else 0
        )

        summary = {
            "total_files_processed": self.input_length,
            "verified": len(verified),
            "unverified": len(unverified),
            "timed_out": len(timed_out),
            "unknown": len(unknown),
            "verification_rate_percent": (
                round(len(verified) / self.input_length * 100, 1)
                if self.input_length > 0
                else 0
            ),
            "total_processing_time_seconds": round(duration, 2),
            "total_verifier_calls": total_verifier_calls,
            "average_verifier_calls_per_case": round(avg_verifier_calls, 1),
            "threads_used": self.threads,
            "verified_cases": [
                {
                    "task_id": r["task_id"],
                    "class_name": r["class_name"],
                    "verifier_calls": r["verifier_calls"],
                    "log_file": r["log_file"],
                }
                for r in verified
            ],
            "unverified_cases": [
                {
                    "task_id": r["task_id"],
                    "class_name": r["class_name"],
                    "verifier_calls": r["verifier_calls"],
                    "log_file": r["log_file"],
                }
                for r in unverified
            ],
            "timed_out_cases": [
                {
                    "task_id": r["task_id"],
                    "class_name": r["class_name"],
                    "verifier_calls": r["verifier_calls"],
                    "log_file": r["log_file"],
                }
                for r in timed_out
            ],
            "unknown_cases": [
                {
                    "task_id": r["task_id"],
                    "message": r["message"],
                    "class_name": r.get("class_name", "unknown"),
                    "log_file": r.get("log_file", "unknown"),
                }
                for r in unknown
            ],
        }

        print(
            f"All results saved to: {os.path.join(self.output, f'{self.name}_results_all.jsonl')}"
        )

        dump_json(
            summary, os.path.join(self.output, f"{self.name}_results_summary.json")
        )

        print(f"\nProcessing completed in {duration:.2f} seconds")
        print(
            f"Verified: {len(verified)}/{self.input_length} ({len(verified)/self.input_length*100:.1f}%)"
        )
        print(
            f"Unverified: {len(unverified)}/{self.input_length} ({len(unverified)/self.input_length*100:.1f}%)"
        )
        print(
            f"Timed out: {len(timed_out)}/{self.input_length} ({len(timed_out)/self.input_length*100:.1f}%)"
        )
        print(f"Unknown (errors): {len(unknown)}/{self.input_length}")
        print(
            f"Summary saved to: {os.path.join(self.output, f'{self.name}_results_summary.json')}"
        )

    def run_workers(self) -> None:
        _input_tasks: list[Task] = [
            Task.from_dict(t) for t in load_json(self.input_path)
        ]
        self.input_length = len(_input_tasks)
        print(f"Found {self.input_length} input tasks to process")

        output_dir: str = os.path.abspath(self.output)
        Path(output_dir).mkdir(parents=True, exist_ok=True)  # base output dir

        self._results_file = os.path.join(output_dir, f"{self.name}_results_all.jsonl")
        # Clear/create the results file
        with open(self._results_file, "w") as f:
            pass

        start_time: float = time.time()
        completed_count: int = 0

        print(f"Starting processing with {self.threads} threads...")
        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            future_to_path = {
                # path will be replaced with data class containing all necessary info for the worker to run daikon
                executor.submit(self._run_daikon, task): task
                for task in _input_tasks
                # for path, task in zip(input_paths, self._create_tasks(input_paths))
            }
            for future in as_completed(future_to_path):
                task = future_to_path[future]
                try:
                    result = future.result()
                    completed_count += 1
                    self._append_result(result)

                    if result["status"] == "verified":
                        print(
                            f"✓ [{completed_count}/{len(_input_tasks)}] {result['class_name']} - VERIFIED - {result['verifier_calls']} verifier calls"
                        )
                    elif result["status"] == "unverified":
                        print(
                            f"✗ [{completed_count}/{len(_input_tasks)}] {result['class_name']} - UNVERIFIED - {result['verifier_calls']} verifier calls"
                        )
                    elif result["status"] == "timed_out":
                        print(
                            f"⏱ [{completed_count}/{len(_input_tasks)}] {result['class_name']} - TIMED OUT - {result['verifier_calls']} verifier calls"
                        )
                    else:  # unknown
                        print(
                            f"✗ [{completed_count}/{len(_input_tasks)}] {result['class_name']} - ERROR: {result.get('message', 'Unknown error')}"
                        )

                except Exception as exc:
                    error_result = WorkerResult(
                        task_id=task.task_id,
                        status="unknown",
                        message=str(exc),
                        class_name=task.class_name,
                    )
                    completed_count += 1
                    self._append_result(error_result)
                    print(
                        f"✗ [{completed_count}/{len(_input_tasks)}] {task.task_id} - Exception: {exc}"
                    )

        end_time: float = time.time()
        duration: float = end_time - start_time
        print(f"\nAll tasks completed in {duration:.2f} seconds")

        # Load results from JSONL file for summary generation
        results = load_jsonl(self._results_file)
        self._save_results(duration, results)
