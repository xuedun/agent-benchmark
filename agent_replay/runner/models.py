from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReplayStep:
    index: int
    action: str
    delay_time_sec: float


@dataclass(frozen=True, slots=True)
class ReplayTask:
    task_index: int
    source_trajectory: Path
    template: str
    selection_index: int = 1
    occurrence: int = 1

    def record(self) -> dict:
        return {
            **asdict(self),
            "source_trajectory": str(self.source_trajectory),
            "workload": f"slot-{self.selection_index:04d}",
            "source_index": 1,
            "cycle": self.occurrence,
        }


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    template: str
    command_timeout_sec: int = 300
    command_timeout_continue: bool = False
    stop_on_error: bool = False
    setup_command: str | None = None
    sandbox_timeout_sec: int = 86400
    request_timeout_sec: int = 120
    create_retries: int = 1
    resume_retries: int = 2
    mock: bool = False
    event_loop: str = "uvloop"
    skip_delays: bool = False


def event_loop_metadata() -> dict[str, str]:
    loop_type = type(asyncio.get_running_loop())
    return {
        "event_loop": "uvloop" if loop_type.__module__.split(".")[0] == "uvloop" else "asyncio",
        "event_loop_class": f"{loop_type.__module__}.{loop_type.__qualname__}",
    }


def trial_limits(
    mode: str, base_count: int, ratio: int, total_multiplier: int = 1
) -> dict[str, int]:
    if mode not in ("pause-resume", "continuous"):
        raise ValueError(f"Unknown execution mode: {mode}")
    if base_count < 1 or ratio < 1 or total_multiplier < 1:
        raise ValueError("base-count, ratio and total-multiplier must be positive")
    concurrency = base_count * ratio
    return {
        "concurrency": concurrency,
        "running_concurrency": concurrency,
        "total_tasks": total_multiplier * concurrency,
        "total_multiplier": total_multiplier,
    }
