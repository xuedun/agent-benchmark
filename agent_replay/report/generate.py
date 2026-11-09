"""Generate observability reports after collection and sandbox cleanup finish."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import shlex
import shutil
import sys
import time

LOGGER = logging.getLogger(__name__)
REPORT_SCRIPTS = Path(__file__).resolve().parents[1] / "observability"
REPORT_TIMEOUT_SEC = 300.0
TERMINATE_TIMEOUT_SEC = 5.0


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


async def _stop(process: asyncio.subprocess.Process) -> None:
    try:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT_SEC)
        except TimeoutError:
            pass
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _run(command: list[str], log, cwd: Path) -> int:
    process = None
    try:
        environment = {**os.environ, "MPLBACKEND": "Agg", "PYTHONUNBUFFERED": "1"}
        process, cancelled = await finish_cleanup(asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command, cwd=cwd, env=environment,
                stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
        ))
        if cancelled:
            raise asyncio.CancelledError
        return await asyncio.wait_for(process.wait(), REPORT_TIMEOUT_SEC)
    finally:
        if process is not None and process.returncode is None:
            _, cancelled = await finish_cleanup(asyncio.create_task(_stop(process)))
            if cancelled:
                raise asyncio.CancelledError


async def generate_reports(
    output_dir: Path, *, mode: str, result_dirs: list[Path],
) -> dict:
    """Run all applicable exporters; preserve raw data and report every failure."""
    started = time.perf_counter()
    output_dir = output_dir.resolve()
    obs = output_dir / "observability"
    log_path = obs / "report-generation.log"
    errors = []
    commands = []
    expected = {}
    interpreter = sys.executable
    for name in ("metrics_1s.jsonl", "sandbox_events.jsonl"):
        path = obs / name
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing monitoring data: {path}")
    directories = sorted({path.resolve() for path in result_dirs
                          if (path / "replay-result.json").is_file()})
    if not directories:
        errors.append("no replay-result.json files found for reports")
    if not errors:
        for label, script in (("xlsx", "make_obs_xlsx.py"), ("markdown", "make_obs_report.py")):
            commands.append((label, [interpreter, str(REPORT_SCRIPTS / script),
                                    str(obs), *(str(path) for path in directories)]))
        expected = {"xlsx": obs / "report.xlsx", "markdown": obs / "report.md"}
        if mode == "pause-resume":
            expected["gantt"] = obs / "gantt.png"
            # Concatenate complete task logs, not interleaved batch progress lines.
            # This also supports legacy results nested under slot-* directories.
            runlog = obs / "report-phase-input.log"
            with runlog.open("w", encoding="utf-8") as combined:
                for directory in directories:
                    source = directory / "job.log"
                    if not source.is_file():
                        source = directory / "run.log"
                    if source.is_file():
                        with source.open(encoding="utf-8", errors="replace") as handle:
                            shutil.copyfileobj(handle, combined)
                        combined.write("\n")
            commands.append(("pause-resume", [interpreter, str(REPORT_SCRIPTS / "analyze_pause_resume.py"),
                                              str(obs), "--runlog", str(runlog), "--plot"]))
    obs.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Generating observability reports: %s", obs)
    with log_path.open("w", encoding="utf-8") as log:
        for label, command in commands:
            log.write(f"[{label}] {shlex.join(command)}\n")
            log.flush()
            try:
                code = await _run(command, log, output_dir)
                if code:
                    errors.append(f"{label} report exited with code {code}")
            except TimeoutError:
                errors.append(f"{label} report timed out after {REPORT_TIMEOUT_SEC:g}s")
            except OSError as exc:
                errors.append(f"{label} report could not start: {exc}")
        for label, path in expected.items():
            if not path.is_file() or not path.stat().st_size:
                errors.append(f"{label} output was not produced: {path.name}")
        if commands and mode == "pause-resume":
            for label, candidates in {
                "phase-xlsx": ["pause_resume_analysis.xlsx"],
                "phase-markdown": ["pause_resume_analysis.md", "pause_resume_analysis_multi.md"],
                "phase-plot": ["pause_resume_cycle.png", "pause_resume_multi.png"],
            }.items():
                found = next((obs / name for name in candidates
                              if (obs / name).is_file() and (obs / name).stat().st_size), None)
                if found is None:
                    errors.append(f"{label} output was not produced")
                else:
                    expected[label] = found
        if errors:
            log.write("\n" + "\n".join(errors) + "\n")
    return {
        "observability_report_return_code": 1 if errors else 0,
        "observability_report_error": "; ".join(errors) + f"; see {log_path}" if errors else None,
        "observability_report_elapsed_sec": time.perf_counter() - started,
        "observability_report_log": str(log_path),
        "observability_report_files": {name: str(path) for name, path in expected.items()
                                       if path.is_file() and path.stat().st_size},
    }
