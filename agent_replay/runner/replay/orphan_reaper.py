"""Scan and reap orphaned sandboxes left by crashed or killed benchmark processes."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ...report.results import absolute_timestamp, append_text, job_log_line, write_json

RUNNER_METADATA_KEY = "runner"
RUNNER_METADATA_VALUE = "agent-benchmark"


@dataclass
class SandboxInfo:
    sandbox_id: str
    started_at: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ReapResult:
    scanned: list[SandboxInfo] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    scan_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "scan_error": self.scan_error,
            "scanned_count": len(self.scanned),
            "reaped_count": len(self.reaped),
            "failed_count": len(self.failed),
            "skipped_count": len(self.skipped),
            "reaped": self.reaped,
            "failed": [{"sandbox_id": sid, "error": err} for sid, err in self.failed],
            "skipped": self.skipped,
            "scanned": [
                {"sandbox_id": s.sandbox_id, "started_at": s.started_at}
                for s in self.scanned
            ],
        }


def _write_orphan_cleanup_json(log_dir: Path, phase: str, data: dict[str, Any]) -> None:
    """Merge phase result into orphan-cleanup.json, preserving the other phase."""
    path = log_dir / "orphan-cleanup.json"
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing[phase] = data
    write_json(path, existing)


def _default_api() -> Any:
    from e2b import AsyncSandbox

    return AsyncSandbox


async def _list_by_metadata(
    api: Any, metadata: dict[str, str], request_timeout_sec: int
) -> list[Any]:
    if hasattr(api, "find_by_metadata"):
        return await asyncio.wait_for(
            api.find_by_metadata(metadata), request_timeout_sec
        )
    from e2b.sandbox.sandbox_api import SandboxQuery

    paginator = api.list(
        query=SandboxQuery(metadata=metadata),
        limit=100,
    )
    matches: list[Any] = []
    while paginator.has_next:
        matches.extend(
            await asyncio.wait_for(paginator.next_items(), request_timeout_sec)
        )
    return matches


async def list_runner_sandboxes(
    *, api: Any | None = None, request_timeout_sec: int = 120
) -> list[SandboxInfo]:
    """List all sandboxes created by this benchmark suite (by runner metadata)."""
    if api is None:
        api = _default_api()
    raw = await _list_by_metadata(
        api, {RUNNER_METADATA_KEY: RUNNER_METADATA_VALUE}, request_timeout_sec
    )
    items: list[SandboxInfo] = []
    for item in raw:
        started_at_raw = getattr(item, "started_at", None)
        if isinstance(started_at_raw, datetime):
            started_at = started_at_raw.isoformat()
        elif started_at_raw is not None:
            started_at = str(started_at_raw)
        else:
            started_at = None
        items.append(SandboxInfo(
            sandbox_id=item.sandbox_id,
            started_at=started_at,
            metadata=dict(getattr(item, "metadata", {}) or {}),
        ))
    return items


def _parse_started_at(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if not isinstance(value, str) or not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    return None


async def reap_orphan_sandboxes(
    sandboxes: list[SandboxInfo],
    *,
    api: Any | None = None,
    request_timeout_sec: int = 120,
    max_age_sec: float = 0,
    concurrency: int = 8,
    log_callback: Callable[[str, str], None] | None = None,
) -> ReapResult:
    """Kill orphaned sandboxes with per-item timeout and fault tolerance."""
    result = ReapResult(started_at=absolute_timestamp())
    if not sandboxes:
        result.completed_at = absolute_timestamp()
        return result

    now = time.time()
    if api is None:
        api = _default_api()

    targets: list[SandboxInfo] = []
    for sb in sandboxes:
        if max_age_sec > 0:
            age = _parse_started_at(sb.started_at)
            if age is not None and (now - age) < max_age_sec:
                result.skipped.append(sb.sandbox_id)
                if log_callback:
                    log_callback(
                        "info",
                        f"Skipped sandbox {sb.sandbox_id}: younger than {max_age_sec}s",
                    )
                continue
        targets.append(sb)

    semaphore = asyncio.Semaphore(concurrency)

    async def kill_one(sb: SandboxInfo) -> None:
        async with semaphore:
            try:
                await asyncio.wait_for(api.kill(sb.sandbox_id), request_timeout_sec)
                result.reaped.append(sb.sandbox_id)
                if log_callback:
                    log_callback("info", f"Reaped orphan sandbox {sb.sandbox_id}")
            except Exception as exc:
                result.failed.append((sb.sandbox_id, f"{type(exc).__name__}: {exc}"))
                if log_callback:
                    log_callback(
                        "warning", f"Failed to reap sandbox {sb.sandbox_id}: {exc!r}"
                    )

    await asyncio.gather(*(kill_one(sb) for sb in targets))
    result.completed_at = absolute_timestamp()
    return result


async def scan_and_reap(
    *,
    phase: str = "before",
    api: Any | None = None,
    request_timeout_sec: int = 120,
    max_age_sec: float = 0,
    concurrency: int = 8,
    log_dir: Path | None = None,
) -> ReapResult:
    """List then reap orphaned sandboxes, with logging and JSON report."""

    def log(level: str, message: str) -> None:
        prefix = f"[orphan-reaper/{phase}] "
        line = prefix + message
        if log_dir is not None:
            append_text(log_dir / "job.log", job_log_line(level, line))
        else:
            print(line, flush=True)

    log("info", f"Scanning for orphaned sandboxes (runner={RUNNER_METADATA_VALUE})")
    result = ReapResult(started_at=absolute_timestamp())
    try:
        sandboxes = await list_runner_sandboxes(
            api=api, request_timeout_sec=request_timeout_sec
        )
    except Exception as exc:
        result.completed_at = absolute_timestamp()
        result.scan_error = f"{type(exc).__name__}: {exc}"
        log("error", f"Orphan scan failed: {exc!r}")
        if log_dir is not None:
            _write_orphan_cleanup_json(log_dir, phase, result.to_dict())
        return result

    result.scanned = sandboxes
    log("info", f"Found {len(sandboxes)} orphaned sandbox(es)")
    reap = await reap_orphan_sandboxes(
        sandboxes,
        api=api,
        request_timeout_sec=request_timeout_sec,
        max_age_sec=max_age_sec,
        concurrency=concurrency,
        log_callback=lambda level, msg: log(level, msg),
    )
    result.reaped = reap.reaped
    result.failed = reap.failed
    result.skipped = reap.skipped
    result.completed_at = reap.completed_at
    if log_dir is not None:
        _write_orphan_cleanup_json(log_dir, phase, result.to_dict())
    log(
        "info",
        f"Orphan cleanup done: reaped={len(result.reaped)} "
        f"failed={len(result.failed)} skipped={len(result.skipped)}",
    )
    return result
