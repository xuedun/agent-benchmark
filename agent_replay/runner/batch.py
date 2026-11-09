"""Execute one task plan; experiment-level repetition lives in experiment.py."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..report.results import (
    SCHEMA_VERSION,
    absolute_timestamp,
    append_text,
    job_log_line,
    write_json,
)
from .execution import LocalExecution, finish_cleanup
from .models import ReplayOptions, ReplayTask
from .multiprocess import ProcessExecution
from ..observability.monitor import BatchMonitor, validate_interval
from ..report.generate import generate_reports

LOGGER = logging.getLogger(__name__)


async def run_batch(
    tasks: list[ReplayTask],
    *,
    mode: str,
    concurrency: int,
    running_concurrency: int,
    options: ReplayOptions,
    output_dir: Path,
    launch_interval_sec: float = 0,
    observability: bool = True,
    observability_interval_sec: float = 1.0,
    process_count: int | None = None,
    workers_per_process: int | None = None,
) -> dict[str, Any]:
    if not tasks or concurrency < 1 or not 1 <= running_concurrency <= concurrency:
        raise ValueError(
            "A nonempty plan and 1 <= running-concurrency <= concurrency are required"
        )
    if mode not in ("pause-resume", "continuous"):
        raise ValueError(f"Unknown mode: {mode}")
    if process_count is not None:
        if (process_count < 1 or workers_per_process is None or workers_per_process < 1
                or concurrency != process_count * workers_per_process
                or running_concurrency != concurrency):
            raise ValueError("Process capacity must match task and RUNNING concurrency")
    elif workers_per_process is not None:
        raise ValueError("workers-per-process requires process-count")
    validate_interval(observability_interval_sec)
    observability = observability and not options.mock
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = absolute_timestamp()
    started_ns: int | None = None
    interrupted = False
    monitor = BatchMonitor(output_dir, observability_interval_sec) if observability else None
    failure: BaseException | None = None
    startup_failed = False

    def emit(name: str, line: str) -> None:
        append_text(output_dir / name, line)

    def job_event(level: str, message: str, *, timestamp: str | None = None) -> None:
        emit("job.log", job_log_line(level, message, timestamp=timestamp))

    if process_count is None:
        execution = LocalExecution(
            tasks, mode=mode, concurrency=concurrency, running_concurrency=running_concurrency,
            options=options, output_dir=output_dir, launch_interval_sec=launch_interval_sec,
            emit=emit,
        )
    else:
        execution = ProcessExecution(
            tasks, process_count=process_count, workers_per_process=workers_per_process,
            mode=mode, options=options, output_dir=output_dir,
            launch_interval_sec=launch_interval_sec, emit=emit,
        )
    try:
        if process_count is not None:
            await execution.prepare()
        if monitor is not None:
            try:
                await monitor.start()
            except Exception as exc:
                startup_failed = True
                monitor.record_error(f"cannot start observability monitor: {type(exc).__name__}: {exc}")
                raise
            LOGGER.info("Observability started: %s", monitor.output_dir)
        started_at = absolute_timestamp()
        started_ns = time.monotonic_ns()
        job_event("info", f"Batch job started trajectories={len(tasks)} "
                  f"mock={str(options.mock).lower()} skip_delays={str(options.skip_delays).lower()} "
                  f"event_loop={execution.loop_metadata['event_loop_class']} "
                  f"source_trajectories={len({t.selection_index for t in tasks})} "
                  f"target_count={len(tasks)} concurrency={concurrency} "
                  f"running_concurrency={running_concurrency} create_admission=running_slots "
                  f"process_count={process_count or 1} workers_per_process={workers_per_process or concurrency} "
                  f"launch_interval_sec={launch_interval_sec:.3f} "
                  f"template={'mixed' if len({t.template for t in tasks}) > 1 else tasks[0].template} "
                  f"output_dir={output_dir}")
        await execution.run()
        if process_count is None:
            failure = execution.failure
            interrupted = isinstance(failure, asyncio.CancelledError)
    except BaseException as exc:
        failure = exc
        interrupted = isinstance(exc, asyncio.CancelledError)
    finally:
        async def cleanup():
            # Monitoring covers sandbox cleanup and diagnostic flushing, not just replay.
            try:
                if process_count is not None:
                    result = await execution.finish()
                    elapsed = ((execution.finished_ns - started_ns) / 1e9
                               if started_ns is not None else 0.0)
                    completed_at = execution.completed_at or absolute_timestamp()
                else:
                    await execution.scheduler.close()
                    elapsed = ((time.monotonic_ns() - started_ns) / 1e9
                               if started_ns is not None else 0.0)
                    completed_at = absolute_timestamp()
                    result = {
                        "items": execution.items, "running_slots": execution.snapshot,
                        "loop_scheduling": await execution.flush(output_dir),
                    }
                return result, elapsed, completed_at
            finally:
                if monitor is not None:
                    try:
                        await monitor.stop()
                    except Exception as exc:
                        monitor.record_error(f"monitor cleanup failed: {type(exc).__name__}: {exc}")

        (result, elapsed, completed_at), cancelled = await finish_cleanup(
            asyncio.create_task(cleanup())
        )
        if cancelled:
            interrupted = True
            failure = asyncio.CancelledError()
        items = result["items"]
        scheduling_status = result["loop_scheduling"]
        monitor_status = monitor.status() if monitor else {
            "observability_dir": None, "observability_return_code": None,
            "observability_error": None, "observability_started_at": None,
            "observability_stopped_at": None,
        }
        errors = [monitor_status["observability_error"]]
        if scheduling_status:
            errors.append(scheduling_status["error"])
        errors.extend(result.get("process_errors", []))
        if failure is not None and not startup_failed:
            errors.append("Batch cancelled" if interrupted else f"{type(failure).__name__}: {failure}")
        error = "; ".join(dict.fromkeys(e for e in errors if e)) or None
        succeeded = sum(x["succeeded"] for x in items)
        summary = {
            "schema_version": SCHEMA_VERSION,
            "execution_mode": mode,
            "mock": options.mock,
            "skip_delays": options.skip_delays,
            **execution.loop_metadata,
            **result,
            "coordinator_pid": os.getpid(),
            "started_at": started_at,
            "completed_at": completed_at,
            "elapsed_sec": elapsed,
            "concurrency": concurrency,
            "running_concurrency": running_concurrency,
            "requested_total_tasks": len(tasks),
            "total": len(items),
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
            "interrupted": interrupted,
            "error": error,
            "observability_enabled": observability,
            "observability_interval_sec": observability_interval_sec,
            **monitor_status,
            "observability_reports_enabled": observability,
            "observability_report_return_code": None,
            "observability_report_error": None,
            "observability_report_elapsed_sec": None,
            "observability_report_log": None,
            "observability_report_files": {},
            "items": sorted(items, key=lambda x: x["task_index"]),
        }
        write_json(output_dir / "batch-summary.json", summary)
        if observability and not startup_failed and not interrupted:
            try:
                summary.update(await generate_reports(
                    output_dir, mode=mode,
                    result_dirs=[Path(item["result_path"]).parent for item in items],
                ))
            except asyncio.CancelledError:
                interrupted = True
                failure = asyncio.CancelledError()
                summary["interrupted"] = True
                summary["observability_report_error"] = "Report generation cancelled"
                summary["observability_report_return_code"] = 1
            except Exception as exc:
                summary["observability_report_error"] = f"Report generation failed: {type(exc).__name__}: {exc}"
                summary["observability_report_return_code"] = 1
            report_error = summary["observability_report_error"]
            if report_error:
                error = f"{error}; {report_error}" if error else report_error
                summary["error"] = error
            write_json(output_dir / "batch-summary.json", summary)
        batch_succeeded = succeeded == len(tasks) and not interrupted and not error
        job_event("info" if batch_succeeded else "error",
                  f"Batch job completed status={'success' if batch_succeeded else 'failed'} "
                  f"total={summary['total']} succeeded={succeeded} failed={summary['failed']} "
                  f"elapsed={summary['elapsed_sec']:.3f}s error={error} result={output_dir / 'batch-summary.json'}")
        status = "interrupted" if interrupted else (
            "success" if batch_succeeded else "completed_with_errors"
        )
        print("\n".join([
            "",
            f"[{summary['completed_at']}] [BATCH RESULT]",
            f"started_at: {summary['started_at']}",
            f"finished_at: {summary['completed_at']}",
            f"status: {status}",
            f"total: {summary['total']}",
            f"succeeded: {summary['succeeded']}",
            f"failed: {summary['failed']}",
            f"concurrency: {summary['concurrency']}",
            f"observability_error: {summary['observability_error']}",
            f"observability_report_error: {summary['observability_report_error']}",
            f"result: {output_dir / 'batch-summary.json'}",
        ]), flush=True)
    if failure is not None and (interrupted or (process_count is None and not startup_failed)):
        raise failure
    return summary
