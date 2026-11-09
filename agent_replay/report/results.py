"""Small runtime writers; importing these functions never imports plotting tools."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
TIMING_CONTRACT = "api-await-v1"


def absolute_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text.rstrip() + "\n")


def job_log_line(level: str, message: str, *, timestamp: str | None = None) -> str:
    safe = message.replace("\r", "\\r").replace("\n", "\\n")
    return f"{timestamp or absolute_timestamp()} {level.upper()} {safe}"


def replay_outcome(result: dict[str, Any]) -> tuple[str, str]:
    """Keep the legacy batch job.log outcome and human-readable summary."""
    if result.get("succeeded"):
        failed = int(result.get("failed_commands", 0))
        if not failed:
            return "success", "全部已执行命令成功。"
        completion = "已到达终止 action" if result.get("submitted") else "已完整执行全部可回放命令"
        return "success", (
            f"{completion}；{failed}/{result.get('commands', 0)} "
            "条命令返回非零，已按命令结果记录，不影响回放完成状态。"
        )
    if result.get("stopped_early"):
        return "stopped_early", "遇到失败命令后提前停止，尚未到达终止 action。"
    return "failed", str(result.get("error") or result.get("cleanup_error") or "回放未成功完成。")


def write_csv(path: Path, columns: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)
