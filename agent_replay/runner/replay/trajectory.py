from __future__ import annotations

import json
import logging
import math
import shlex
from pathlib import Path
from typing import Any

from ..models import ReplayStep

LOGGER = logging.getLogger(__name__)
SUBMIT_ACTIONS = {"submit", "finish", "done"}


def load_trajectory(path: str | Path) -> tuple[str, str, list[ReplayStep]]:
    trajectory_path = Path(path).expanduser().resolve()
    if not trajectory_path.is_file():
        raise FileNotFoundError(f"Trajectory not found: {trajectory_path}")

    try:
        data: Any = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid trajectory JSON: {trajectory_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Trajectory root must be a JSON object")

    environment_name = data.get("environment", "main")
    if not isinstance(environment_name, str):
        environment_name = "main"

    # 处理后的 replay JSON 通常没有单独的 instance_id 字段，因此优先读字段，
    # 缺失时再从 `<instance_id>.replay.json` 文件名恢复完整任务 ID。
    instance_id = data.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        instance_id = trajectory_path.name
        for suffix in (".replay.json", ".json", ".traj"):
            if instance_id.endswith(suffix):
                instance_id = instance_id[: -len(suffix)]
                break
    instance_id = instance_id.strip()
    if not instance_id:
        raise ValueError(f"Cannot determine instance_id from {trajectory_path}")

    raw_steps = data.get("trajectory")
    if not isinstance(raw_steps, list):
        raise ValueError("trajectory must be a JSON array")

    steps: list[ReplayStep] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            continue
        action = raw_step.get("action")
        if not isinstance(action, str) or not action.strip():
            continue

        raw_delay = raw_step.get("delay_time", 0.0)
        if (
            isinstance(raw_delay, bool)
            or not isinstance(raw_delay, (int, float))
            or not math.isfinite(raw_delay)
            or raw_delay < 0
        ):
            LOGGER.warning(
                "Invalid delay_time=%r for action %r; using 0 seconds",
                raw_delay,
                action,
            )
            delay_time_sec = 0.0
        else:
            delay_time_sec = float(raw_delay)

        steps.append(
            ReplayStep(
                index=len(steps),
                action=action.strip(),
                delay_time_sec=delay_time_sec,
            )
        )

    if not steps:
        raise ValueError(f"No executable actions found in {trajectory_path}")
    return environment_name, instance_id, steps


def _remove_one_boundary_newline(value: str) -> str:
    """Remove one presentation newline from each edge of a tool argument."""

    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]

    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value


def normalize_str_replace_editor_action(action: str) -> str:
    """Match the original SWE structured-tool handling for multiline edits.

    Recorded ``str_replace_editor`` actions render multiline ``old_str`` and
    ``new_str`` values with one presentation newline at each edge.  The
    original structured tool call removes those edge newlines before matching,
    while invoking the rendered CLI command directly preserves them.  Rebuild
    only ``str_replace`` commands with the same boundary normalization; all
    other actions remain byte-for-byte unchanged.
    """

    try:
        arguments = shlex.split(action)
    except ValueError:
        return action

    if (
        len(arguments) < 3
        or arguments[0] != "str_replace_editor"
        or arguments[1] != "str_replace"
    ):
        return action

    flag_indexes: dict[str, int] = {}
    for flag in ("--old_str", "--new_str"):
        try:
            index = arguments.index(flag)
        except ValueError:
            return action
        if index + 1 >= len(arguments):
            return action
        flag_indexes[flag] = index + 1

    normalized_old_str = _remove_one_boundary_newline(
        arguments[flag_indexes["--old_str"]]
    )
    if not normalized_old_str:
        return action

    arguments[flag_indexes["--old_str"]] = normalized_old_str
    arguments[flag_indexes["--new_str"]] = _remove_one_boundary_newline(
        arguments[flag_indexes["--new_str"]]
    )
    return shlex.join(arguments)


def wrap_action(
    action: str,
    setup_command: str | None,
) -> str:
    commands = [
        # The original SWE bash tool reports the status of the final pipeline
        # command.  Keep the replay shell consistent with that behavior.
        "set +o pipefail",
        "export PAGER=cat",
        "export MANPAGER=cat",
        "export GIT_PAGER=cat",
        "export LESS=-FRX",
    ]
    if setup_command:
        commands.append(setup_command)
    commands.append(action)
    # The SDK already executes cmd through /bin/bash -l -c.
    return "; ".join(commands)
