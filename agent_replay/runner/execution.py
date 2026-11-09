"""Process-local task execution, shared by single runs and spawned workers."""

from __future__ import annotations

import asyncio
from array import array
import logging
from pathlib import Path
import re
import time
from dataclasses import replace
from typing import Callable

from ..report.results import append_text, job_log_line, read_json, replay_outcome
from .loop_scheduling import LoopSchedulingRecorder
from .models import ReplayOptions, ReplayTask, event_loop_metadata
from .replay.continuous import ContinuousRunner
from .replay.pause_resume import PauseResumeRunner
from .running_scheduler import RunningSlotScheduler

LOGGER = logging.getLogger(__name__)


async def finish_cleanup(task: asyncio.Task):
    """Finish owned cleanup even if its caller is cancelled repeatedly."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def task_directory(output_dir: Path, task: ReplayTask) -> Path:
    name = task.source_trajectory.stem.removesuffix(".replay")
    name = re.sub(r"[^\w.-]+", "-", name).strip("._-") or "task"
    name = name.encode("utf-8")[:200].decode("utf-8", errors="ignore")
    return output_dir / "tasks" / f"{task.task_index:06d}-{name}"


class LocalExecution:
    def __init__(
        self, tasks: list[ReplayTask], *, mode: str, concurrency: int,
        running_concurrency: int, options: ReplayOptions, output_dir: Path,
        launch_interval_sec: float = 0, total_tasks: int | None = None,
        emit: Callable[[str, str], None] | None = None, launch_gate=None,
        record_running_events: bool = False,
    ):
        self.tasks = tasks
        self.mode = mode
        self.concurrency = concurrency
        self.options = options
        self.output_dir = output_dir
        self.total_tasks = len(tasks) if total_tasks is None else total_tasks
        self.emit = emit or (lambda name, line: append_text(output_dir / name, line))
        self.launch_gate = launch_gate
        self.launch_interval_sec = launch_interval_sec
        self.loop_metadata = event_loop_metadata()
        self.recorder = (
            LoopSchedulingRecorder(self.loop_metadata, skip_delays=options.skip_delays)
            if options.mock else None
        )
        self.running_events = array("q") if record_running_events else None
        self.scheduler = RunningSlotScheduler(running_concurrency, events=self.running_events)
        self.items: list[dict] = []
        self.snapshot = self.scheduler.snapshot()
        self.failure: BaseException | None = None
        self._pending = iter(tasks)
        self._launch_lock = asyncio.Lock()
        self._next_launch = 0.0

    def job_event(self, level: str, message: str, *, timestamp: str | None = None):
        self.emit("job.log", job_log_line(level, message, timestamp=timestamp))

    async def _worker(self):
        runner_type = PauseResumeRunner if self.mode == "pause-resume" else ContinuousRunner
        for task in self._pending:
            task_dir = task_directory(self.output_dir, task)
            result_path = task_dir / "replay-result.json"
            result = {}
            try:
                if self.launch_gate is not None:
                    await self.launch_gate(task.task_index)
                elif self.launch_interval_sec:
                    async with self._launch_lock:
                        await asyncio.sleep(max(0.0, self._next_launch - time.monotonic()))
                        self._next_launch = time.monotonic() + self.launch_interval_sec
                LOGGER.info("Starting %d/%d: %s cycle=%d", task.task_index,
                            self.total_tasks, task.source_trajectory.name, task.occurrence)
                self.job_event("info", f"[{task.task_index}/{self.total_tasks}] Replay job started "
                               f"workload=slot-{task.selection_index:04d} template={task.template} "
                               f"trajectory={task.source_trajectory} "
                               f"source_index=1 cycle={task.occurrence} occurrence={task.occurrence} "
                               f"output_dir={task_dir}")
                runner = runner_type(
                    task.source_trajectory, task_dir, replace(self.options, template=task.template),
                    running_slot_scheduler=self.scheduler, console_output=False,
                    loop_scheduling_recorder=self.recorder,
                )
                result = await runner.run()
                LOGGER.info("Completed %d/%d: %s succeeded=%s commands=%s failed=%s",
                            task.task_index, self.total_tasks, task.source_trajectory.name,
                            result.get("succeeded"), result.get("commands"), result.get("failed_commands"))
            except asyncio.CancelledError:
                result = read_json(result_path) if result_path.exists() else {
                    "succeeded": False, "replay_completed": False, "error": "Cancelled before replay",
                }
                raise
            except Exception as exc:
                LOGGER.error("Replay failed for %s: %s: %r",
                             task.source_trajectory.name, type(exc).__name__, exc)
                result = {"succeeded": False, "replay_completed": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            finally:
                item = {
                    **task.record(), "result_path": str(result_path),
                    "succeeded": bool(result.get("succeeded")),
                    "replay_completed": bool(result.get("replay_completed")),
                    "elapsed_sec": result.get("elapsed_sec"),
                    "error": result.get("error") or result.get("cleanup_error"),
                }
                self.items.append(item)
                if not item["error"]:
                    status, explanation = replay_outcome(result)
                    self.job_event("info" if item["succeeded"] else "warning",
                                   f"[{task.task_index}/{self.total_tasks}] Replay job completed "
                                   f"status={status} commands={result.get('commands')} "
                                   f"succeeded={result.get('succeeded_commands')} "
                                   f"failed={result.get('failed_commands')} "
                                   f"submitted={str(bool(result.get('submitted'))).lower()} "
                                   f"replay_completed={str(item['replay_completed']).lower()} "
                                   f"summary={explanation} result={result_path}",
                                   timestamp=result.get("completed_at"))
                else:
                    self.job_event("error", f"[{task.task_index}/{self.total_tasks}] Replay job failed "
                                   f"trajectory={task.source_trajectory} "
                                   f"error={item['error']} result={result_path}")
                self.emit("run.log", job_log_line(
                    "info" if item["succeeded"] else "error",
                    f"task={task.task_index} succeeded={item['succeeded']} error={item['error']}",
                ))
            # Uncertain cleanup closes admission for the entire trial, not just this shard.
            if self.scheduler.closed:
                raise RuntimeError("Running scheduler closed after unsafe sandbox cleanup")

    async def run(self):
        workers = [asyncio.create_task(self._worker())
                   for _ in range(min(self.concurrency, len(self.tasks)))]
        group = asyncio.gather(*workers)
        try:
            await asyncio.shield(group)
        except BaseException as exc:
            self.failure = exc
        finally:
            async def cleanup():
                for worker in workers:
                    if not worker.done() and not worker.cancelling():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if group.done() and not group.cancelled():
                    group.exception()
                self.snapshot = self.scheduler.snapshot()
                await self.scheduler.close()

            _, cancelled = await finish_cleanup(asyncio.create_task(cleanup()))
            if cancelled:
                self.failure = asyncio.CancelledError()
        return self

    async def flush(self, output_dir: Path) -> dict | None:
        if self.recorder is None:
            return None
        log_path = output_dir / "loop-scheduling.jsonl"
        status = {
            "path": str(log_path), "metric": "yield_to_resume", "clock": "perf_counter_ns",
            "sample_count": self.recorder.sample_count, "written_samples": 0,
            "flush_sec": None, "error": None,
        }
        LOGGER.info("Writing %d loop scheduling samples: %s", self.recorder.sample_count, log_path)
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self.recorder.write_jsonl, log_path)
            status["written_samples"] = self.recorder.sample_count
        except Exception as exc:
            status["error"] = f"Loop scheduling log failed: {type(exc).__name__}: {exc}"
        finally:
            status["flush_sec"] = time.perf_counter() - started
        return status
