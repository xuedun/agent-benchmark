"""Spawn execution shards; batch.py remains the sole monitor/report owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import heapq
import json
import logging
import multiprocessing
from pathlib import Path
import os
import signal
import time

from ..report.results import absolute_timestamp, read_json, write_json
from .execution import LocalExecution, task_directory
from .models import ReplayOptions, ReplayTask

TOPOLOGY = "process-per-ratio-v1"
STARTUP_TIMEOUT_SEC = 120.0
CLEANUP_TIMEOUT_SEC = 300.0
FLUSH_TIMEOUT_SEC = 300.0
EXIT_TIMEOUT_SEC = 5.0


def shard_tasks(tasks: list[ReplayTask], process_count: int) -> list[list[ReplayTask]]:
    if process_count < 1:
        raise ValueError("process_count must be positive")
    size, extra = divmod(len(tasks), process_count)
    shards, offset = [], 0
    for index in range(process_count):
        end = offset + size + (index < extra)
        shards.append(tasks[offset:end])
        offset = end
    return shards


def _write_running_events(path: Path, values):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for offset in range(0, len(values), 2):
            stream.write(f"[{values[offset]},{values[offset + 1]}]\n")
    os.replace(temporary, path)


async def _run_child(connection, tasks, config):
    loop = asyncio.get_running_loop()
    start, flush = asyncio.Event(), asyncio.Event()
    permits = {}
    execution_task = None
    stopping = False
    disconnected = False

    def send(kind, **data):
        nonlocal disconnected
        if disconnected:
            return
        try:
            connection.send({"kind": kind, **data})
        except (BrokenPipeError, EOFError, OSError):
            disconnected = True
            stop()
            flush.set()

    def stop():
        nonlocal stopping
        if not stopping:
            stopping = True
            if execution_task is not None and not execution_task.done():
                execution_task.cancel()
        start.set()

    def receive():
        nonlocal disconnected
        try:
            message = connection.recv()
        except (EOFError, OSError):
            disconnected = True
            loop.remove_reader(connection.fileno())
            stop()
            flush.set()
            return
        kind = message["kind"]
        if kind == "START":
            start.set()
        elif kind == "FLUSH":
            flush.set()
        elif kind == "STOP":
            stop()
        elif kind == "PERMIT":
            future = permits.get(message["task_index"])
            if future is not None and not future.done():
                future.set_result(None)

    launch_lock = asyncio.Lock()

    async def launch(task_index):
        async with launch_lock:
            future = loop.create_future()
            permits[task_index] = future
            try:
                send("LAUNCH", task_index=task_index)
                await future
                # This is the task admission boundary, before logging/runner construction.
                send("ADMITTED", task_index=task_index, timestamp_ns=time.monotonic_ns())
            finally:
                permits.pop(task_index, None)

    process_dir = Path(config["process_dir"])
    process_dir.mkdir(parents=True, exist_ok=True)
    local = LocalExecution(
        tasks, mode=config["mode"], concurrency=config["capacity"],
        running_concurrency=config["capacity"], options=config["options"],
        output_dir=Path(config["output_dir"]), total_tasks=config["total_tasks"],
        emit=lambda name, line: send("LOG", name=name, line=line),
        launch_gate=launch if config["launch_interval_sec"] else None,
        record_running_events=True,
    )
    loop.add_reader(connection.fileno(), receive)
    try:
        send("READY", pid=os.getpid(), worker_count=min(config["capacity"], len(tasks)),
             assigned_tasks=len(tasks), capacity=config["capacity"], **local.loop_metadata)
        await start.wait()
        started_ns = time.monotonic_ns()
        if not stopping:
            execution_task = asyncio.create_task(local.run())
            try:
                await execution_task
            except BaseException as exc:
                local.failure = exc
        else:
            local.failure = asyncio.CancelledError()
            await local.scheduler.close()
        error = None
        if local.failure is not None:
            error = ("Execution cancelled" if isinstance(local.failure, asyncio.CancelledError)
                     else f"{type(local.failure).__name__}: {local.failure}")
            if not stopping:
                send("ERROR", error=error)
        send("QUIESCED", snapshot=local.snapshot, started_ns=started_ns,
             stopped_ns=time.monotonic_ns(), error=error,
             sample_count=local.recorder.sample_count if local.recorder else 0)
        await flush.wait()
        scheduling = await local.flush(process_dir)
        await asyncio.to_thread(_write_running_events, process_dir / "running-events.jsonl",
                                local.running_events)
        write_json(process_dir / "process-summary.json", {
            "pid": os.getpid(), **local.loop_metadata, "items": local.items,
            "running_slots": local.snapshot, "loop_scheduling": scheduling, "error": error,
        })
        send("FLUSHED", error=scheduling["error"] if scheduling else None)
    finally:
        loop.remove_reader(connection.fileno())


def _child_entry(connection, tasks, config):
    # Only the coordinator handles terminal Ctrl-C; children receive one STOP.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=config["log_level"])
    for name, level in config["logger_levels"].items():
        logging.getLogger(name).setLevel(level)
    try:
        if config["options"].event_loop == "uvloop":
            import uvloop
            uvloop.run(_run_child(connection, tasks, config))
        else:
            asyncio.run(_run_child(connection, tasks, config))
    except BaseException as exc:
        try:
            connection.send({"kind": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
        except (EOFError, OSError):
            pass
        raise
    finally:
        connection.close()


def merge_running_events(paths: list[Path], snapshots: list[dict], capacity: int) -> dict:
    """Merge same-host monotonic clocks; ties preserve each process's event order."""
    def events(path, index):
        previous = -1
        with path.open(encoding="utf-8") as stream:
            for sequence, line in enumerate(stream):
                timestamp, delta = json.loads(line)
                if type(timestamp) is not int or timestamp < previous or delta not in (-1, 1):
                    raise ValueError(f"Invalid running event in {path}")
                previous = timestamp
                yield timestamp, index, sequence, delta

    active = [0] * len(paths)
    grants = [0] * len(paths)
    local_peaks = [0] * len(paths)
    peak = current = 0
    for _, index, _, delta in heapq.merge(*(events(p, i) for i, p in enumerate(paths))):
        active[index] += delta
        grants[index] += delta == 1
        local_peaks[index] = max(local_peaks[index], active[index])
        if not 0 <= active[index] <= snapshots[index]["maximum"]:
            raise ValueError("Unbalanced running slot events")
        current += delta
        peak = max(peak, current)
    for index, snapshot in enumerate(snapshots):
        if (active[index] != snapshot["active"] or grants[index] != snapshot["granted"]
                or local_peaks[index] != snapshot["peak_active"]):
            raise ValueError("Running events disagree with process snapshot")
    granted = sum(grants)
    return {
        "maximum": capacity, "active": sum(active), "peak_active": peak,
        "waiting": sum(s["waiting"] for s in snapshots), "granted": granted,
        "average_queue_wait_sec": (
            sum(s["average_queue_wait_sec"] * s["granted"] for s in snapshots) / granted
            if granted else 0.0
        ),
        "peak_method": "merged_events", "complete": True,
        "process_peak_sum": sum(s["peak_active"] for s in snapshots),
        "clock": "monotonic_ns", "clock_implementation": time.get_clock_info("monotonic").implementation,
        "tie_order": "timestamp,process_index,sequence",
    }


@dataclass
class _Child:
    process: object
    connection: object
    tasks: list[ReplayTask]
    directory: Path
    ready: dict | None = None
    quiesced: dict | None = None
    flushed: bool = False
    exited: bool = False
    forced: bool = False
    eof: bool = False
    errors: list[str] = field(default_factory=list)


class ProcessExecution:
    def __init__(self, tasks, *, process_count, workers_per_process, mode, options,
                 output_dir, launch_interval_sec, emit):
        self.tasks = tasks
        self.process_count = process_count
        self.capacity = workers_per_process
        self.mode = mode
        self.options = options
        self.output_dir = output_dir
        self.interval = launch_interval_sec
        self.emit = emit
        self.children: list[_Child] = []
        self.errors = []
        self.changed = asyncio.Event()
        self.launch_requests = asyncio.Queue()
        self.admission = None
        self.admissions = []
        self.launch_task = None
        self.stopping = False
        self.loop_metadata = {"event_loop": options.event_loop, "event_loop_class": None}
        self.startup_sec = 0.0
        self.flush_started_ns = None
        self.started_ns = None
        self.finished_ns = None
        self.completed_at = None
        self._loop = asyncio.get_running_loop()

    def _error(self, child, message, *, notify=True):
        if message not in child.errors:
            child.errors.append(message)
            self.errors.append(f"Process {child.process.pid}: {message}")
        if notify:
            self.changed.set()

    def _send(self, child, kind, **data):
        if child.exited or child.eof:
            return
        try:
            child.connection.send({"kind": kind, **data})
        except (EOFError, OSError) as exc:
            self._error(child, f"Control channel failed: {exc}")

    def _receive(self, child):
        try:
            message = child.connection.recv()
        except (EOFError, OSError):
            child.eof = True
            self._loop.remove_reader(child.connection.fileno())
            if not child.quiesced:
                self._error(child, "Control channel closed before QUIESCED")
            return
        kind = message["kind"]
        if kind == "LOG":
            try:
                self.emit(message["name"], message["line"])
            except OSError as exc:
                self._error(child, f"Batch log write failed: {exc}")
        elif kind == "READY":
            child.ready = message
        elif kind == "QUIESCED":
            child.quiesced = message
        elif kind == "FLUSHED":
            child.flushed = True
            if message.get("error"):
                self._error(child, message["error"])
        elif kind == "ERROR":
            self._error(child, message["error"])
        elif kind == "LAUNCH":
            self.launch_requests.put_nowait((child, message["task_index"]))
        elif kind == "ADMITTED":
            if self.admission is not None:
                owner, task_index, future = self.admission
                if owner is child and task_index == message["task_index"] and not future.done():
                    self.admissions.append({"pid": child.process.pid, "task_index": task_index,
                                            "timestamp_ns": message["timestamp_ns"]})
                    future.set_result(None)
        self.changed.set()

    def _exited(self, child):
        child.process.join(timeout=0)
        child.exited = True
        self._loop.remove_reader(child.process.sentinel)
        # The write end is closed, so recv drains every buffered message then EOF.
        while not child.eof:
            self._receive(child)
        # exitcode can still be None right after the sentinel fires; that is a
        # clean exit in progress, not a failure. Only a real nonzero code is fatal.
        if child.process.exitcode not in (None, 0) or not child.flushed:
            self._error(child, f"Execution exited before successful collection (code={child.process.exitcode})")
        self.changed.set()

    async def _wait(self, predicate, timeout=None, *, fail_fast=True):
        async def wait():
            while True:
                self.changed.clear()
                if fail_fast and self.errors:
                    raise RuntimeError(self.errors[0])
                if predicate():
                    return
                await self.changed.wait()
        if timeout is None:
            await wait()
        else:
            await asyncio.wait_for(wait(), timeout)

    async def prepare(self):
        started = time.perf_counter()
        context = multiprocessing.get_context("spawn")
        for index, shard in enumerate(shard_tasks(self.tasks, self.process_count)):
            parent_conn, child_conn = context.Pipe()
            directory = self.output_dir / "processes" / f"{index:02d}"
            config = {
                "mode": self.mode, "capacity": self.capacity, "options": self.options,
                "output_dir": str(self.output_dir), "process_dir": str(directory),
                "total_tasks": len(self.tasks), "launch_interval_sec": self.interval,
                "log_level": logging.getLogger().getEffectiveLevel(),
                "logger_levels": {name: logging.getLogger(name).level
                                  for name in ("httpx", "httpcore", "e2b")},
            }
            process = context.Process(target=_child_entry, args=(child_conn, shard, config),
                                      name=f"replay-{index:02d}")
            try:
                process.start()
            except BaseException:
                parent_conn.close()
                raise
            finally:
                child_conn.close()
            child = _Child(process, parent_conn, shard, directory)
            self.children.append(child)
            self._loop.add_reader(parent_conn.fileno(), self._receive, child)
            self._loop.add_reader(process.sentinel, self._exited, child)
        try:
            await self._wait(lambda: all(c.ready for c in self.children), STARTUP_TIMEOUT_SEC)
            metadata = [{key: c.ready[key] for key in ("event_loop", "event_loop_class")}
                        for c in self.children]
            if any(m != metadata[0] or m["event_loop"] != self.options.event_loop for m in metadata):
                raise RuntimeError("Execution processes disagree with the requested event loop")
            self.loop_metadata = metadata[0]
        finally:
            self.startup_sec = time.perf_counter() - started

    async def _launch(self):
        while True:
            child, task_index = await self.launch_requests.get()
            if child.exited or child.quiesced:
                continue
            acknowledgement = self._loop.create_future()
            self.admission = child, task_index, acknowledgement
            self._send(child, "PERMIT", task_index=task_index)
            await acknowledgement
            self.admission = None
            # Receipt follows actual admission, so a delayed child cannot cause catch-up bursts.
            await asyncio.sleep(self.interval)

    async def run(self):
        self.started_ns = time.monotonic_ns()
        if self.interval:
            self.launch_task = asyncio.create_task(self._launch())
        for child in self.children:
            self._send(child, "START")
        await self._wait(lambda: all(c.quiesced for c in self.children))
        self.finished_ns = time.monotonic_ns()
        self.completed_at = absolute_timestamp()

    async def stop(self):
        self.stopping = True
        if self.launch_task is not None:
            self.launch_task.cancel()
            await asyncio.gather(self.launch_task, return_exceptions=True)
        for child in self.children:
            if not child.quiesced:
                self._send(child, "STOP")
        try:
            await self._wait(lambda: all(c.quiesced or c.exited for c in self.children),
                             CLEANUP_TIMEOUT_SEC, fail_fast=False)
        except TimeoutError:
            for child in self.children:
                if not child.quiesced and not child.exited:
                    child.forced = True
                    self._error(child, "Cleanup timed out; sandbox cleanup is unconfirmed")
                    child.process.terminate()
            await self._reap_unresponsive()
        if self.finished_ns is None:
            self.finished_ns = time.monotonic_ns()
            self.completed_at = absolute_timestamp()

    async def _reap_unresponsive(self):
        try:
            await self._wait(lambda: all(c.exited or (c.quiesced and not c.forced)
                                         for c in self.children),
                             EXIT_TIMEOUT_SEC, fail_fast=False)
        except TimeoutError:
            for child in self.children:
                if not child.exited and (child.forced or not child.quiesced):
                    child.process.kill()
            await self._wait(lambda: all(c.exited or (c.quiesced and not c.forced)
                                         for c in self.children),
                             EXIT_TIMEOUT_SEC, fail_fast=False)

    async def finish(self):
        """Called under batch cleanup shielding, after run() or any failure."""
        try:
            await self.stop()
            self.flush_started_ns = time.monotonic_ns()
            for child in self.children:
                if child.quiesced:
                    self._send(child, "FLUSH")
            try:
                await self._wait(lambda: all(c.exited for c in self.children),
                                 FLUSH_TIMEOUT_SEC, fail_fast=False)
            except TimeoutError:
                for child in self.children:
                    if not child.exited:
                        child.forced = True
                        self._error(child, "Flush/exit timed out")
                        child.process.terminate()
                await self._reap_unresponsive()
            try:
                return await asyncio.to_thread(self._collect)
            except Exception as exc:
                self.errors.append(f"Result collection failed: {type(exc).__name__}: {exc}")
                return {
                    "items": [], "running_slots": {
                        "maximum": self.process_count * self.capacity, "active": None,
                        "peak_active": None, "waiting": None, "granted": None,
                        "average_queue_wait_sec": None, "complete": False,
                        "peak_method": "incomplete",
                    },
                    "loop_scheduling": None, "execution_topology": TOPOLOGY,
                    "process_count": self.process_count,
                    "workers_per_process": self.capacity, "startup_sec": self.startup_sec,
                    "processes": [], "process_errors": self.errors,
                    "launch_admissions": self.admissions,
                    "flush_started_ns": self.flush_started_ns,
                }
        finally:
            for child in self.children:
                self._loop.remove_reader(child.connection.fileno())
                self._loop.remove_reader(child.process.sentinel)
                if child.process.is_alive():
                    child.process.kill()
                child.process.join(timeout=EXIT_TIMEOUT_SEC)
                child.connection.close()

    def _collect(self):
        summaries = []
        for child in self.children:
            try:
                summary = read_json(child.directory / "process-summary.json")
                if not child.ready or any(summary.get(key) != child.ready[key]
                                          for key in ("pid", "event_loop", "event_loop_class")):
                    raise ValueError("Process identity/loop metadata mismatch")
                if not child.quiesced or summary["running_slots"] != child.quiesced["snapshot"]:
                    raise ValueError("Process running snapshot mismatch")
                if sorted(item["task_index"] for item in summary["items"]) != sorted(
                    task.task_index for task in child.tasks
                ):
                    self._error(child, "Process result identities are incomplete or duplicated",
                                notify=False)
                if summary.get("error"):
                    self._error(child, summary["error"], notify=False)
                summaries.append(summary)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._error(child, f"Missing/invalid process summary: {exc}", notify=False)
                summaries.append(None)
        items = []
        for task in self.tasks:
            path = task_directory(self.output_dir, task) / "replay-result.json"
            try:
                result = read_json(path)
                if not isinstance(result, dict):
                    raise ValueError("task result is not an object")
            except (OSError, ValueError) as exc:
                result = {"error": f"Missing/invalid task result: {exc}"}
            items.append({
                **task.record(), "result_path": str(path),
                "succeeded": bool(result.get("succeeded")),
                "replay_completed": bool(result.get("replay_completed")),
                "elapsed_sec": result.get("elapsed_sec"),
                "error": result.get("error") or result.get("cleanup_error"),
            })
        running = {"maximum": self.process_count * self.capacity, "active": None,
                   "peak_active": None, "waiting": None, "granted": None,
                   "average_queue_wait_sec": None, "complete": False, "peak_method": "incomplete"}
        try:
            if len(summaries) != self.process_count or any(s is None for s in summaries):
                raise ValueError("Not all execution snapshots were collected")
            running = merge_running_events(
                [c.directory / "running-events.jsonl" for c in self.children],
                [s["running_slots"] for s in summaries], running["maximum"],
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.errors.append(f"Running statistics incomplete: {exc}")
        scheduling = self._merge_samples(summaries) if self.options.mock else None
        processes = [{
            "process_index": index, "pid": child.process.pid,
            "assigned_task_indices": [t.task_index for t in child.tasks],
            "worker_count": min(self.capacity, len(child.tasks)), "capacity": self.capacity,
            "event_loop": (child.ready or {}).get("event_loop"),
            "event_loop_class": (child.ready or {}).get("event_loop_class"),
            "exitcode": child.process.exitcode, "forced_cleanup": child.forced,
            "ready": child.ready is not None, "quiesced": child.quiesced is not None,
            "flushed": child.flushed, "errors": child.errors,
            "started_ns": (child.quiesced or {}).get("started_ns"),
            "stopped_ns": (child.quiesced or {}).get("stopped_ns"),
            "running_slots": (summary or {}).get("running_slots"),
        } for index, (child, summary) in enumerate(zip(self.children, summaries))]
        return {
            "items": items, "running_slots": running, "loop_scheduling": scheduling,
            "execution_topology": TOPOLOGY, "process_count": self.process_count,
            "workers_per_process": self.capacity, "startup_sec": self.startup_sec,
            "processes": processes, "process_errors": self.errors,
            "launch_admissions": self.admissions, "flush_started_ns": self.flush_started_ns,
        }

    def _merge_samples(self, summaries):
        path = self.output_dir / "loop-scheduling.jsonl"
        known_count = sum((c.quiesced or {}).get("sample_count", 0) for c in self.children)
        all_stopped = len(self.children) == self.process_count and all(c.quiesced for c in self.children)
        status = {"path": str(path), "metric": "yield_to_resume", "clock": "perf_counter_ns",
                  "sample_count": known_count if all_stopped else None,
                  "known_sample_count": known_count, "written_samples": 0,
                  "complete": True, "error": None, "flush_sec": None}
        errors = [] if all_stopped else ["Not all sampling processes stopped with a sample count"]
        temporary = path.with_suffix(".jsonl.tmp")
        try:
            with temporary.open("wb") as output:
                for child, summary in zip(self.children, summaries):
                    count = 0
                    try:
                        if summary is None:
                            raise ValueError("Missing flushed process summary")
                        shard_status = summary["loop_scheduling"]
                        task_ids = {task_directory(self.output_dir, task).name for task in child.tasks}
                        with (child.directory / "loop-scheduling.jsonl").open("rb") as source:
                            for line in source:
                                row = json.loads(line)
                                if (not isinstance(row, dict) or row.get("schema_version") != "1.0"
                                        or row.get("mock") is not True
                                        or row.get("skip_delays") is not self.options.skip_delays
                                        or row.get("task_id") not in task_ids
                                        or row.get("operation") not in ("command_start", "command_wait")
                                        or any(row.get(key) != value for key, value in self.loop_metadata.items())):
                                    raise ValueError("Scheduling shard metadata mismatch")
                                start, end, wait = (row.get(key) for key in (
                                    "yield_started_ns", "resumed_ns", "schedule_wait_ns"))
                                if (any(type(value) is not int or value < 0 for value in (start, end, wait))
                                        or end - start != wait):
                                    raise ValueError("Invalid scheduling duration")
                                output.write(line)
                                count += 1
                        if (count != shard_status["sample_count"] or count != shard_status["written_samples"]
                                or count != (child.quiesced or {}).get("sample_count") or shard_status["error"]):
                            raise ValueError("Scheduling shard count/error mismatch")
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        errors.append(f"Process {child.process.pid}: {exc}")
                    status["written_samples"] += count
            os.replace(temporary, path)
        except OSError as exc:
            errors.append(str(exc))
        finally:
            temporary.unlink(missing_ok=True)
        status["flush_sec"] = (time.monotonic_ns() - self.flush_started_ns) / 1e9
        if errors:
            status["complete"] = False
            status["error"] = "Loop scheduling merge failed: " + "; ".join(errors)
        return status
