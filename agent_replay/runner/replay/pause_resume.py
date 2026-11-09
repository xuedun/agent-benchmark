"""Release RUNNING capacity during recorded delays; reserve before every resume."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ...report.results import absolute_timestamp

from ..models import ReplayStep
from .sandbox import SandboxRunner, is_transient_sandbox_error


class PauseResumeRunner(SandboxRunner):
    execution_mode = "pause-resume"

    async def _recover_created(self, matches: list[Any]) -> bool:
        sandbox_api = self._sandbox_api

        # Adopt one sandbox, remove any duplicates before admitting more work.
        self.sandbox_id = matches[0].sandbox_id
        for duplicate in matches[1:]:
            await asyncio.wait_for(
                self._api_call("create_cleanup_delete", sandbox_api.kill(duplicate.sandbox_id)),
                self.options.request_timeout_sec,
            )
        self.sandbox = await self._api_call("create_recovery_resume", sandbox_api.connect(
            self.sandbox_id,
            timeout=self.options.sandbox_timeout_sec,
            request_timeout=self.options.request_timeout_sec,
        ))
        self._create_state_uncertain = False
        return True

    async def _pause(self) -> dict[str, Any]:
        await self._api_call("pause", self.sandbox.beta_pause(
            request_timeout=self.options.request_timeout_sec
        ))
        timing = self._api_timings[-1]
        self._paused = True
        return {
            "pause_started_at": timing["started_at"],
            "pause_completed_at": timing["completed_at"],
            "pause_completed_monotonic": timing["completed_monotonic"],
            "pause_sec": timing["elapsed_sec"],
        }

    async def _resume(self) -> dict[str, Any]:
        start_at = absolute_timestamp()
        api_offset = len(self._api_timings)
        # Even a failed resume might have transitioned the remote sandbox.
        self._paused = False
        for attempt in range(self.options.resume_retries + 1):
            try:
                self.sandbox = await self._api_call("resume", self.sandbox.connect(
                    timeout=self.options.sandbox_timeout_sec,
                    request_timeout=self.options.request_timeout_sec,
                ))
                api_sec = sum(x["elapsed_sec"] for x in self._api_timings[api_offset:]
                              if x["operation"] == "resume")
                if attempt:
                    self._event("info", f"Retry recovered stage=resume attempts={attempt + 1}")
                return {
                    "resume_started_at": start_at,
                    "resume_completed_at": self._api_timings[-1]["completed_at"],
                    "resume_sec": api_sec,
                    "resume_api_sec": api_sec,
                    "resume_attempts": attempt + 1,
                }
            except Exception as exc:
                wait_ready = re.search(
                    r"\b500: sandbox ([0-9a-fA-F-]{36}) operation WaitReady failed:",
                    str(exc),
                )
                retryable = is_transient_sandbox_error(exc) or bool(
                    wait_ready
                    and wait_ready.group(1).lower() == str(self.sandbox_id).lower()
                )
                if not retryable or attempt >= self.options.resume_retries:
                    self._event("error", f"Retry exhausted stage=resume attempts={attempt + 1} "
                                f"retryable={str(retryable).lower()} "
                                f"error_type={type(exc).__name__} error={exc!r}")
                    raise
                self._event("warning", f"Retry stage=resume attempt={attempt + 1}/{self.options.resume_retries + 1} "
                            f"retryable=true error_type={type(exc).__name__} error={exc!r}")
        raise AssertionError("unreachable")

    async def _initialize(self) -> None:
        self._preceding_pause = await self._pause()
        await self._release_if_safe()

    async def _run_step(
        self, step: ReplayStep, total: int, trajectory_total: int
    ) -> dict[str, Any]:
        preceding = self._preceding_pause
        requested = 0.0 if self.options.skip_delays else step.delay_time_sec
        queued_at = absolute_timestamp()
        lease = await self.running_slot_scheduler.acquire(
            self.run_slot_task_id,
            ready_at=preceding["pause_completed_monotonic"] + requested,
        )
        self._running_lease = lease
        acquired_at = absolute_timestamp()
        self._job_event("info", f"Run slot acquired step={step.index + 1}/{trajectory_total}",
                        timestamp=acquired_at)
        paused_sec = max(
            0.0, lease.acquired_at - preceding["pause_completed_monotonic"]
        )
        result = None
        try:
            resume = await self._resume()
            result = await self._execute_step(step, total, trajectory_total)
            self._step_results.append(result)
            result.update(
                pause_started_at=preceding["pause_started_at"],
                pause_completed_at=preceding["pause_completed_at"],
                **resume,
                resume_total_sec=resume["resume_sec"],
                trajectory_delay_sec=step.delay_time_sec,
                requested_delay_sec=requested,
                pause_sec=preceding["pause_sec"],
                paused_sec=paused_sec,
                capacity_wait_sec=max(0.0, paused_sec - requested),
                run_queue_entered_at=queued_at,
                run_slot_acquired_at=acquired_at,
                run_queue_wait_sec=lease.queue_wait_sec,
                command_sec=result["elapsed_sec"],
                command_start_sec=result["command_start_sec"],
                command_wait_sec=result["command_wait_sec"],
                interaction_total_sec=preceding["pause_sec"]
                + paused_sec
                + resume["resume_sec"]
                + result["elapsed_sec"],
            )
            timing_message = (
                f"Agent interaction timing step={step.index + 1}/{trajectory_total} "
                f"pause={result['pause_sec']:.3f}s delay={requested:.3f}s "
                f"capacity_wait={result['capacity_wait_sec']:.3f}s "
                f"resume={result['resume_sec']:.3f}s command={result['command_sec']:.3f}s "
                f"total={result['interaction_total_sec']:.3f}s"
            )
            self._job_event("info", timing_message, timestamp=result["completed_at"])
            self._block(timing_message)
        finally:
            # A failed pause retains its lease. run() then deletes the sandbox;
            # only confirmed pause/deletion may free this capacity.
            if self.sandbox is not None and not self._paused:
                self._preceding_pause = await self._pause()
                if result is not None:
                    following = self._preceding_pause
                    result.update(
                        post_command_pause_started_at=following["pause_started_at"],
                        post_command_pause_completed_at=following["pause_completed_at"],
                        post_command_pause_sec=following["pause_sec"],
                        running_slot_held_sec=following["pause_completed_monotonic"]
                        - lease.acquired_at,
                    )
            await self._release_if_safe()
        return result

    async def _cleanup(self) -> None:
        try:
            try:
                await self._delete_sandbox()
            except Exception as exc:
                if not self._paused:
                    raise
                self._event("warning", f"Direct delete failed for sandbox {self.sandbox_id}: {exc}; "
                            "retrying cleanup under a running slot")
                # Some backends require resume before deleting a paused sandbox.
                # Resume must also reserve capacity, never exceed the cap.
                self._running_lease = await self.running_slot_scheduler.acquire(
                    self.run_slot_task_id + ":cleanup"
                )
                try:
                    await self._resume()
                finally:
                    await self._delete_sandbox()
            if self._create_state_uncertain:
                raise RuntimeError(
                    f"Create outcome is uncertain; key={self.sandbox_create_key}"
                )
        except BaseException:
            await self.running_slot_scheduler.close()
            raise
        finally:
            await self._release_if_safe()
