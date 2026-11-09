"""Keep one RUNNING reservation throughout create, replay delays and deletion."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ...report.results import absolute_timestamp

from ..models import ReplayStep
from .sandbox import SandboxRunner


class ContinuousRunner(SandboxRunner):
    execution_mode = "continuous"

    async def _recover_created(self, matches: list[Any]) -> bool:
        # Preserve the old continuous-mode policy: delete uncertain creates,
        # fail this replay, and never reconnect/resume a matching sandbox.
        sandbox_api = self._sandbox_api

        self.sandbox_id = matches[0].sandbox_id
        for match in matches:
            await asyncio.wait_for(
                self._api_call("create_cleanup_delete", sandbox_api.kill(match.sandbox_id)),
                self.options.request_timeout_sec
            )
        self._create_state_uncertain = False
        raise RuntimeError(
            f"Create response lost; matching sandbox(s) deleted: {self.sandbox_id}"
        )

    async def _initialize(self) -> None:
        pass

    async def _run_step(
        self, step: ReplayStep, total: int, trajectory_total: int
    ) -> dict[str, Any]:
        requested = 0.0 if self.options.skip_delays else step.delay_time_sec
        started_at = absolute_timestamp()
        started = time.perf_counter()
        await asyncio.sleep(requested)
        waited = time.perf_counter() - started
        completed_at = absolute_timestamp()
        result = await self._execute_step(step, total, trajectory_total)
        self._step_results.append(result)
        result.update(
            trajectory_delay_sec=step.delay_time_sec,
            requested_delay_sec=requested,
            delay_wait_started_at=started_at,
            delay_wait_completed_at=completed_at,
            delay_wait_sec=waited,
            command_sec=result["elapsed_sec"],
            command_start_sec=result["command_start_sec"],
            command_wait_sec=result["command_wait_sec"],
            interaction_total_sec=waited + result["elapsed_sec"],
        )
        timing_message = (
            f"Agent interaction timing step={step.index + 1}/{trajectory_total} "
            f"delay={requested:.3f}s delay_wait={waited:.3f}s "
            f"command={result['command_sec']:.3f}s total={result['interaction_total_sec']:.3f}s"
        )
        self._job_event("info", timing_message, timestamp=result["completed_at"])
        self._block(timing_message)
        return result
