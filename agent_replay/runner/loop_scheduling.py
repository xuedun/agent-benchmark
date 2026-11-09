"""Buffer mock yield-to-resume samples; write only after all workers stop."""

from __future__ import annotations

import asyncio
from array import array
import json
import os
from pathlib import Path
import time


class LoopSchedulingRecorder:
    COMMAND_START = 0
    COMMAND_WAIT = 1
    OPERATIONS = ("command_start", "command_wait")
    RECORD_WIDTH = 5

    def __init__(self, loop_metadata: dict[str, str], *, skip_delays: bool = False):
        self.loop_metadata = dict(loop_metadata)
        self.skip_delays = skip_delays
        self.task_ids: list[str] = []
        # Five 64-bit integers per sample, instead of a dict and strings per yield.
        self._samples = array("q")

    def register_task(self, task_id: str) -> int:
        key = len(self.task_ids)
        self.task_ids.append(task_id)
        return key

    @property
    def sample_count(self) -> int:
        return len(self._samples) // self.RECORD_WIDTH

    async def yield_once(self, task_key: int, command_index: int, operation: int) -> None:
        started_ns = time.perf_counter_ns()
        await asyncio.sleep(0)
        resumed_ns = time.perf_counter_ns()
        # No formatting, file I/O or result construction inside the timed interval.
        self._samples.extend((task_key, command_index, operation, started_ns, resumed_ns))

    def write_jsonl(self, path: Path) -> None:
        """Called in a thread after worker cleanup; never alongside producers."""
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for offset in range(0, len(self._samples), self.RECORD_WIDTH):
                task_key, index, operation, started, resumed = self._samples[
                    offset:offset + self.RECORD_WIDTH
                ]
                record = {
                    "schema_version": "1.0",
                    "mock": True,
                    "skip_delays": self.skip_delays,
                    **self.loop_metadata,
                    "task_id": self.task_ids[task_key],
                    "command_index": index,
                    "operation": self.OPERATIONS[operation],
                    "yield_started_ns": started,
                    "resumed_ns": resumed,
                    "schedule_wait_ns": resumed - started,
                }
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
