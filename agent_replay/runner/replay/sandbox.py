"""Shared E2B operations and replay bookkeeping, without client request throttling."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ...report.results import (
    SCHEMA_VERSION,
    TIMING_CONTRACT,
    absolute_timestamp,
    append_text,
    job_log_line,
    write_json,
)

from ..loop_scheduling import LoopSchedulingRecorder
from ..models import ReplayOptions, ReplayStep, event_loop_metadata
from ..running_scheduler import RunningLease, RunningSlotScheduler
from .trajectory import (
    SUBMIT_ACTIONS,
    load_trajectory,
    normalize_str_replace_editor_action,
    wrap_action,
)


class SandboxRunner:
    """Common replay flow. Subclasses define initialization, each step and recovery."""

    execution_mode: str

    def __init__(
        self,
        trajectory_path: Path,
        output_dir: Path,
        options: ReplayOptions,
        *,
        running_slot_scheduler: RunningSlotScheduler,
        console_output: bool = False,
        loop_scheduling_recorder: LoopSchedulingRecorder | None = None,
    ):
        self.trajectory_path = trajectory_path.resolve()
        self.output_dir = output_dir.resolve()
        self.options = options
        self.running_slot_scheduler = running_slot_scheduler
        self.console_output = console_output
        self.run_slot_task_id = self.output_dir.name
        self.sandbox: Any = None
        self.sandbox_id: str | None = None
        self.sandbox_create_key = uuid.uuid4().hex
        self._create_state_uncertain = False
        self._paused = False
        self._running_lease: RunningLease | None = None
        self._first_command_started_at = None
        self._first_command_started_monotonic = None
        self._last_command_completed_at = None
        self._last_command_completed_monotonic = None
        self._create_started_at = None
        self._create_completed_at = None
        self._create_started = None
        self._create_completed = None
        self._deleted_at = None
        self._deleted = None
        self._step_results: list[dict[str, Any]] = []
        self._api_timings: list[dict[str, Any]] = []
        self._mock_api: Any = None
        self._loop_scheduling_recorder = loop_scheduling_recorder

    @property
    def _sandbox_api(self) -> Any:
        if self.options.mock:
            if self._mock_api is None:
                from .mock import MockSandboxAPI

                self._mock_api = MockSandboxAPI(self._loop_scheduling_recorder, self.run_slot_task_id)
            return self._mock_api
        from e2b import AsyncSandbox

        return AsyncSandbox

    async def _api_call(self, operation: str, request: Any) -> Any:
        """Measure only the SDK await, before logging, parsing or retry handling."""
        started_at = absolute_timestamp()
        succeeded = False
        started = time.monotonic()
        try:
            value = await request
            succeeded = True
            return value
        finally:
            completed = time.monotonic()
            self._api_timings.append({
                "operation": operation,
                "started_at": started_at,
                "completed_at": absolute_timestamp(),
                "started_monotonic": started,
                "completed_monotonic": completed,
                "elapsed_sec": completed - started,
                "succeeded": succeeded,
            })

    def _api_total(self, operation: str) -> float:
        return sum(x["elapsed_sec"] for x in self._api_timings
                   if x["operation"] == operation)

    def _event(self, level: str, message: str) -> None:
        line = job_log_line(level, message)
        append_text(self.output_dir / "run.log", line)
        append_text(self.output_dir / "job.log", line)
        if self.console_output:
            print(line, flush=True)

    def _job_event(
        self, level: str, message: str, *, timestamp: str | None = None
    ) -> None:
        append_text(
            self.output_dir / "job.log",
            job_log_line(level, message, timestamp=timestamp),
        )

    def _block(self, text: str) -> None:
        append_text(self.output_dir / "run.log", text)
        if self.console_output:
            print(text, flush=True)

    async def _release_if_safe(self) -> None:
        if self._running_lease is not None and (
            self._paused or (self.sandbox is None and not self._create_state_uncertain)
        ):
            await self._running_lease.release()
            self._running_lease = None

    async def _find_sandboxes_by_create_key(self) -> list[Any]:
        if self.options.mock:
            return await self._api_call(
                "create_lookup", self._sandbox_api.find_by_create_key(self.sandbox_create_key)
            )
        from e2b.sandbox.sandbox_api import SandboxQuery

        paginator = self._sandbox_api.list(
            query=SandboxQuery(metadata={"replay_create_key": self.sandbox_create_key}),
            limit=100,
        )
        matches = []
        while paginator.has_next:
            matches.extend(await self._api_call("create_lookup", paginator.next_items()))
        return matches

    async def _recover_created(self, matches: list[Any]) -> bool:
        raise NotImplementedError

    async def _reconcile_ambiguous_create(self) -> bool:
        empty_checks = 0
        last_error = None
        for check in range(3):
            # Bounded reconciliation, independent of the removed rate limiter.
            if check:
                await asyncio.sleep(1)
            try:
                matches = await asyncio.wait_for(
                    self._find_sandboxes_by_create_key(),
                    self.options.request_timeout_sec,
                )
            except Exception as exc:
                last_error = exc
                self._event("warning", f"Create reconciliation query failed: {exc!r}")
                continue
            if matches:
                matches.sort(
                    key=lambda item: (
                        str(getattr(item, "started_at", "")),
                        item.sandbox_id,
                    )
                )
                return await self._recover_created(matches)
            empty_checks += 1
            if empty_checks == 2:
                return False
        raise RuntimeError(
            f"Cannot reconcile create key {self.sandbox_create_key}"
        ) from last_error

    async def _create_sandbox(self) -> None:
        sandbox_api = self._sandbox_api

        self._create_started_at = absolute_timestamp()
        self._create_started = time.perf_counter()
        for attempt in range(self.options.create_retries + 1):
            self._create_state_uncertain = True
            try:
                self.sandbox = await self._api_call("create", sandbox_api.create(
                    template=self.options.template,
                    metadata={
                        "runner": "agent-benchmark",
                        "execution_mode": self.execution_mode,
                        "trajectory": self.trajectory_path.name,
                        "replay_create_key": self.sandbox_create_key,
                    },
                    timeout=self.options.sandbox_timeout_sec,
                    request_timeout=self.options.request_timeout_sec,
                ))
                self.sandbox_id = self.sandbox.sandbox_id
                self._create_state_uncertain = False
                break
            except asyncio.CancelledError:
                # A cancelled create may still finish remotely. Reconcile before cleanup.
                try:
                    await self._reconcile_ambiguous_create()
                except Exception as exc:
                    self._event("error", f"Cancelled create reconciliation: {exc!r}")
                raise
            except Exception as exc:
                self._event("warning", f"Create attempt {attempt + 1} failed: {exc!r}")
                wait_ready = re.search(
                    r"\b500: sandbox ([0-9a-fA-F-]{36}) operation WaitReady failed:",
                    str(exc),
                )
                if wait_ready:
                    self.sandbox_id = wait_ready.group(1)
                    await asyncio.wait_for(
                        self._api_call("create_cleanup_delete", sandbox_api.kill(self.sandbox_id)),
                        self.options.request_timeout_sec,
                    )
                    self._create_state_uncertain = False
                    if attempt >= self.options.create_retries:
                        raise
                    await asyncio.sleep(2)
                    continue
                if is_timeout_error(exc) or is_transient_sandbox_error(exc):
                    if await self._reconcile_ambiguous_create():
                        break
                    # A timeout with no visible match is still ambiguous: never retry it.
                    if is_timeout_error(exc):
                        raise RuntimeError(
                            f"Create timed out; refusing duplicate create for key {self.sandbox_create_key}"
                        ) from exc
                    self._create_state_uncertain = False
                    if attempt < self.options.create_retries:
                        await asyncio.sleep(1)
                        continue
                else:
                    # Retain uncertainty for unexpected server errors; a definite 4xx
                    # rejection does not represent a successfully created sandbox.
                    if re.search(
                        r"\b40[0134]\b|unauthorized|forbidden|not found", str(exc), re.I
                    ):
                        self._create_state_uncertain = False
                raise
        self._create_completed = time.perf_counter()
        self._create_completed_at = absolute_timestamp()
        self._event("info", f"Created sandbox {self.sandbox_id} "
                    f"create_attempts={attempt + 1} elapsed={self._api_total('create'):.3f}s")

    async def _delete_sandbox(self) -> None:
        if self.sandbox is not None:
            await asyncio.wait_for(
                self._api_call("delete", self.sandbox.kill()), self.options.request_timeout_sec
            )
            timing = self._api_timings[-1]
            self._deleted = timing["completed_monotonic"]
            self._deleted_at = timing["completed_at"]
            self.sandbox = None
            self._paused = False
            self._create_state_uncertain = False
            self._event("info", f"Deleted sandbox {self.sandbox_id} "
                        f"elapsed={self._api_total('delete'):.3f}s")

    async def _cleanup(self) -> None:
        try:
            await self._delete_sandbox()
            if self._create_state_uncertain:
                raise RuntimeError(
                    f"Create outcome is uncertain; key={self.sandbox_create_key}"
                )
        except BaseException:
            await self.running_slot_scheduler.close()
            raise
        finally:
            # A successful delete may be followed by a failed log write.
            await self._release_if_safe()

    async def _initialize(self) -> None:
        raise NotImplementedError

    async def _run_step(
        self, step: ReplayStep, total: int, trajectory_total: int
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self) -> dict[str, Any]:
        # Match the original task clock: parse inputs before starting replay timing.
        environment, instance_id, steps = load_trajectory(self.trajectory_path)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "execution_mode": self.execution_mode,
            "mock": self.options.mock,
            "skip_delays": self.options.skip_delays,
            **event_loop_metadata(),
            "trajectory_path": str(self.trajectory_path),
            "template": self.options.template,
            "started_at": absolute_timestamp(),
            "command_timeout_sec": self.options.command_timeout_sec,
            "command_timeout_continue": self.options.command_timeout_continue,
            "replay_completed": False,
            "succeeded": False,
            "submitted": False,
            "stopped_early": False,
            "cleanup_succeeded": False,
            "steps": self._step_results,
            "timing_contract": TIMING_CONTRACT,
            "api_timings": self._api_timings,
        }
        self._event("info", f"Replay started trajectory={self.trajectory_path}")
        cancelled = False
        try:
            terminal = next(
                (
                    i
                    for i, step in enumerate(steps)
                    if step.action.lower() in SUBMIT_ACTIONS
                ),
                None,
            )
            executable = steps if terminal is None else steps[:terminal]
            if terminal is not None:
                self._event("info", f"Terminal action at position {terminal + 1}; "
                            f"executable commands={len(executable)} trajectory actions={len(steps)}")
            result.update(
                environment=environment,
                instance_id=instance_id,
                trajectory_actions=len(steps),
                executable_actions=len(executable),
                terminal_action_position=None if terminal is None else terminal + 1,
            )
            self._running_lease = await self.running_slot_scheduler.acquire(
                self.run_slot_task_id + ":prepare"
            )
            result["initial_running_slot_queue_wait_sec"] = (
                self._running_lease.queue_wait_sec
            )
            await self._create_sandbox()
            await self._initialize()
            for step in executable:
                item = await self._run_step(step, len(executable), len(steps))
                # Modes record a command immediately, even if the following pause fails.
                if item not in self._step_results:
                    self._step_results.append(item)
                if item["infrastructure_error"]:
                    raise RuntimeError(
                        f"Command infrastructure failed: {item['stderr']}"
                    )
                if item["timed_out"] and not self.options.command_timeout_continue:
                    raise TimeoutError(f"Command timed out at step {step.index + 1}")
                if (
                    self.options.stop_on_error
                    and not item["succeeded"]
                    and not item["timed_out"]
                ):
                    result["stopped_early"] = True
                    break
            result["replay_completed"] = not result["stopped_early"]
            result["succeeded"] = result["replay_completed"]
            result["submitted"] = terminal is not None and result["replay_completed"]
        except asyncio.CancelledError:
            cancelled = True
            result["error"] = "Replay cancelled"
            self._event("error", "Replay failed: Replay cancelled")
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self._event("error", f"Replay failed: {result['error']}")
        finally:
            # Preserve partial results before potentially slow cleanup.
            result.update(
                sandbox_id=self.sandbox_id,
                create_key=self.sandbox_create_key,
                commands=len(self._step_results),
                elapsed_sec=time.perf_counter() - started,
            )
            write_error: Exception | None = None
            try:
                write_json(self.output_dir / "replay-result.json", result)
            except Exception as exc:
                # Preserve the exception, but never let it skip sandbox cleanup.
                write_error = exc
            cleanup_task = asyncio.create_task(self._cleanup())
            try:
                await asyncio.shield(cleanup_task)
                result["cleanup_succeeded"] = True
            except asyncio.CancelledError:
                cancelled = True
                result["error"] = "Replay cancelled during cleanup"
                try:
                    await cleanup_task
                    result["cleanup_succeeded"] = True
                except Exception as exc:
                    result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            result["succeeded"] = (
                result["succeeded"] and result["cleanup_succeeded"] and not cancelled
            )
            result.update(
                completed_at=absolute_timestamp(),
                elapsed_sec=time.perf_counter() - started,
                succeeded_commands=sum(
                    bool(x["succeeded"]) for x in self._step_results
                ),
                failed_commands=sum(not x["succeeded"] for x in self._step_results),
                requested_delay_sec=sum(
                    x.get("requested_delay_sec", 0) for x in self._step_results
                ),
                sandbox_create_started_at=self._create_started_at,
                sandbox_create_completed_at=self._create_completed_at,
                sandbox_create_sec=self._api_total("create"),
                first_command_started_at=self._first_command_started_at,
                last_command_completed_at=self._last_command_completed_at,
                sandbox_deleted_at=self._deleted_at,
                first_command_to_last_command_sec=self._duration(
                    self._first_command_started_monotonic,
                    self._last_command_completed_monotonic,
                ),
                delete_cleanup_sec=self._api_total("delete"),
                first_command_to_delete_sec=self._duration(
                    self._first_command_started_monotonic, self._deleted
                ),
            )
            if write_error is not None:
                if cancelled:
                    raise asyncio.CancelledError from write_error
                raise write_error
            write_json(self.output_dir / "replay-result.json", result)
            if result.get("cleanup_error"):
                self._event("error", f"Replay cleanup failed: {result['cleanup_error']}")
            if not result.get("error"):
                self._event("info" if result["succeeded"] else "error",
                            f"Replay completed succeeded={result['succeeded']} "
                            f"commands={result['commands']} failed_commands={result['failed_commands']} "
                            f"elapsed={result['elapsed_sec']:.3f}s")
        if cancelled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    def _duration(start: float | None, end: float | None) -> float | None:
        return None if start is None or end is None else max(0.0, end - start)

    async def _dispatch_command(
        self, command: str, timeout_sec: int | None, *, operation: str = "command"
    ) -> Any:
        if self.options.mock:
            from .mock import CommandExitException
        else:
            from e2b import CommandExitException

        sandbox = self.sandbox
        if sandbox is None:
            raise RuntimeError("Sandbox is not available")

        async def start_command() -> Any:
            return await sandbox.commands.run(
                cmd=command,
                background=True,
                timeout=timeout_sec or 0,
                request_timeout=timeout_sec or 0,
                user="root",
            )

        handle = await self._api_call(operation + "_start", start_command())
        try:
            if timeout_sec:
                return await asyncio.wait_for(
                    self._api_call(operation + "_wait", handle.wait()), timeout=timeout_sec
                )
            return await self._api_call(operation + "_wait", handle.wait())
        except asyncio.TimeoutError as exc:
            try:
                await asyncio.wait_for(self._api_call("command_kill", handle.kill()), timeout=3)
            except Exception as kill_exc:
                self._event(
                    "warning",
                    f"Could not kill timed-out command: {kill_exc!r}",
                )
            raise TimeoutError(
                f"Command result did not return within {timeout_sec}s"
            ) from exc
        except CommandExitException as exc:
            # Only a confirmed process exit is a business command failure.
            # SDK/transport errors must propagate even if they contain output.
            return exc

    async def _execute_step(
        self,
        step: ReplayStep,
        total_steps: int,
        job_total_steps: int,
    ) -> dict[str, Any]:
        if self.options.mock:
            from .mock import TimeoutException
        else:
            from e2b import TimeoutException

        execution_action = normalize_str_replace_editor_action(step.action)
        command = wrap_action(
            execution_action,
            self.options.setup_command,
        )
        started_at = absolute_timestamp()
        self._job_event(
            "info",
            f"[{step.index + 1}/{job_total_steps}] COMMAND {step.action}",
            timestamp=started_at,
        )
        if execution_action != step.action:
            self._job_event(
                "info",
                f"[{step.index + 1}/{job_total_steps}] "
                "Normalized str_replace_editor boundary newlines",
                timestamp=started_at,
            )
        self._block(
            "\n"
            f"[{started_at}] [{step.index + 1}/{total_steps}] COMMAND START\n"
            f"{step.action}"
        )
        api_offset = len(self._api_timings)
        infrastructure_error = False
        timed_out = False
        try:
            raw_result = await self._dispatch_command(
                command,
                self.options.command_timeout_sec,
            )
            stdout, stderr, return_code = result_fields(raw_result)
            if return_code is None:
                infrastructure_error = True
                raise RuntimeError("Command result is missing an exit code")
            succeeded = return_code == 0
        except Exception as exc:
            stdout = ""
            stderr = f"{type(exc).__name__}: {exc}"
            return_code = None
            succeeded = False
            timed_out = isinstance(exc, (TimeoutError, TimeoutException))
            # Match replay-aenv: ordinary command exceptions are failed steps,
            # with continuation governed by stop_on_error. Recognized transient errors
            # and invalid replay results remain fatal; timeouts keep their policy.
            infrastructure_error = infrastructure_error or (
                not timed_out and is_transient_sandbox_error(exc)
            )
        command_calls = [x for x in self._api_timings[api_offset:]
                         if x["operation"] in ("command_start", "command_wait")]
        # Do not include parsing, timeout cleanup/kill, warning logs or result writes.
        elapsed_sec = sum(x["elapsed_sec"] for x in command_calls)
        command_start_sec = sum(
            x["elapsed_sec"] for x in command_calls if x["operation"] == "command_start"
        )
        command_wait_sec = sum(
            x["elapsed_sec"] for x in command_calls if x["operation"] == "command_wait"
        )
        if command_calls:
            started_at = command_calls[0]["started_at"]
            completed_at = command_calls[-1]["completed_at"]
            if self._first_command_started_monotonic is None:
                self._first_command_started_at = started_at
                self._first_command_started_monotonic = command_calls[0]["started_monotonic"]
            self._last_command_completed_at = completed_at
            self._last_command_completed_monotonic = command_calls[-1]["completed_monotonic"]
        else:
            completed_at = absolute_timestamp()
        if timed_out:
            outcome = (
                "ignored; continuing with subsequent commands"
                if self.options.command_timeout_continue
                else "stopping replay because continuation is disabled"
            )
            self._job_event(
                "warning",
                f"[{step.index + 1}/{job_total_steps}] COMMAND TIMEOUT {outcome}",
            )

        self._job_event(
            "info",
            f"[{step.index + 1}/{job_total_steps}] RESULT "
            f"exit_code={return_code} succeeded={succeeded} elapsed={elapsed_sec:.3f}s",
            timestamp=completed_at,
        )
        self._block(
            "\n"
            f"[{completed_at}] [{step.index + 1}/{total_steps}] COMMAND END\n"
            f"exit_code: {return_code}\n"
            f"elapsed: {elapsed_sec:.3f}s\n"
            "stdout:\n"
            f"{stdout.rstrip() if stdout else '<empty>'}\n\n"
            "stderr:\n"
            f"{stderr.rstrip() if stderr else '<empty>'}"
        )
        result = {
            "index": step.index,
            "action": step.action,
            "execution_action": execution_action,
            "started_at": started_at,
            "completed_at": completed_at,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "elapsed_sec": elapsed_sec,
            "command_start_sec": command_start_sec,
            "command_wait_sec": command_wait_sec,
            "succeeded": succeeded,
            "timed_out": timed_out,
            "infrastructure_error": infrastructure_error,
        }
        return result


def is_transient_sandbox_error(exc: BaseException) -> bool:
    """Return whether a failed infrastructure request is safe to retry."""

    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "502:",
            "http 502",
            "bad gateway",
            "503:",
            "http 503",
            "service unavailable",
            "504:",
            "http 504",
            "gateway timeout",
            "429:",
            "http 429",
            "too many requests",
            "rate limit",
            "timed out",
            "timeout",
            "connection reset",
            "connection refused",
            "connection aborted",
            "temporarily unavailable",
        )
    )


def is_timeout_error(exc: BaseException) -> bool:
    """Return whether an exception represents a timeout."""

    text = f"{type(exc).__name__}: {exc}".lower()
    return "timeout" in text or "timed out" in text


def result_fields(raw_result: Any) -> tuple[str, str, int | None]:
    stdout = getattr(raw_result, "stdout", "")
    stderr = getattr(raw_result, "stderr", "")
    return_code = None
    for name in ("exit_code", "return_code", "exit_status", "code"):
        value = getattr(raw_result, name, None)
        if isinstance(value, int):
            return_code = value
            break
    return str(stdout or ""), str(stderr or ""), return_code
