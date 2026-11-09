"""Local success-only sandbox operations; never execute commands or open sockets."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from ..loop_scheduling import LoopSchedulingRecorder


class CommandExitException(Exception):
    """Local exception type for the shared command result handling."""


class TimeoutException(TimeoutError):
    """Local exception type for the shared timeout handling."""


@dataclass(frozen=True, slots=True)
class MockCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class MockCommandHandle:
    def __init__(
        self, recorder: LoopSchedulingRecorder | None = None,
        task_key: int = 0, command_index: int = 0,
    ):
        self._recorder = recorder
        self._task_key = task_key
        self._command_index = command_index

    async def wait(self) -> MockCommandResult:
        if self._recorder is None:
            await asyncio.sleep(0)
        else:
            await self._recorder.yield_once(
                self._task_key, self._command_index, LoopSchedulingRecorder.COMMAND_WAIT
            )
        return MockCommandResult()

    async def kill(self) -> bool:
        await asyncio.sleep(0)
        return True


class MockCommands:
    def __init__(self, recorder: LoopSchedulingRecorder | None = None, task_key: int = 0):
        self._recorder = recorder
        self._task_key = task_key
        self._next_command_index = 0

    async def run(
        self, cmd: str, *, background: bool = False, **kwargs: Any
    ) -> MockCommandHandle | MockCommandResult:
        index = self._next_command_index
        self._next_command_index += 1
        if self._recorder is None:
            await asyncio.sleep(0)
        else:
            await self._recorder.yield_once(
                self._task_key, index, LoopSchedulingRecorder.COMMAND_START
            )
        handle = MockCommandHandle(self._recorder, self._task_key, index)
        return handle if background else await handle.wait()


class MockSandbox:
    def __init__(self, api: MockSandboxAPI, metadata: dict[str, str]):
        self.sandbox_id = "mock-" + uuid.uuid4().hex
        self.metadata = dict(metadata)
        self.commands = MockCommands(api.recorder, api.task_key)
        self._api = api

    async def connect(self, **kwargs: Any) -> MockSandbox:
        await asyncio.sleep(0)
        return self

    async def beta_pause(self, **kwargs: Any) -> bool:
        await asyncio.sleep(0)
        return True

    async def kill(self) -> bool:
        await asyncio.sleep(0)
        return self._api.sandboxes.pop(self.sandbox_id, None) is not None


class MockSandboxAPI:
    """One registry per runner, including its create-reconciliation operations."""

    def __init__(self, recorder: LoopSchedulingRecorder | None = None, task_id: str = ""):
        self.sandboxes: dict[str, MockSandbox] = {}
        self.recorder = recorder
        self.task_key = recorder.register_task(task_id) if recorder is not None else 0

    async def create(self, *, metadata: dict[str, str], **kwargs: Any) -> MockSandbox:
        await asyncio.sleep(0)
        sandbox = MockSandbox(self, metadata)
        self.sandboxes[sandbox.sandbox_id] = sandbox
        return sandbox

    async def find_by_create_key(self, key: str) -> list[MockSandbox]:
        await asyncio.sleep(0)
        return [sandbox for sandbox in self.sandboxes.values()
                if sandbox.metadata.get("replay_create_key") == key]

    async def find_by_metadata(self, metadata: dict[str, str]) -> list[MockSandbox]:
        await asyncio.sleep(0)
        return [sandbox for sandbox in self.sandboxes.values()
                if all(sandbox.metadata.get(k) == v for k, v in metadata.items())]

    async def connect(self, sandbox_id: str, **kwargs: Any) -> MockSandbox:
        return await self.sandboxes[sandbox_id].connect(**kwargs)

    async def kill(self, sandbox_id: str) -> bool:
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is not None:
            return await sandbox.kill()
        await asyncio.sleep(0)
        return False
