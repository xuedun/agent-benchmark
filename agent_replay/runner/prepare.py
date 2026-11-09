"""Select base workloads, bind images and expand a deterministic task plan."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..report.results import read_json
from .models import ReplayTask


def natural_key(value: str) -> list[tuple[int, int | str]]:
    """Return a deterministic case-insensitive natural-sort key."""

    parts: list[tuple[int, int | str]] = []
    for part in re.split(r"(\d+)", value.casefold()):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return parts


def find_trajectories(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise NotADirectoryError(f"trajectory directory not found: {directory}")
    trajectories = sorted(
        (
            path.resolve()
            for path in directory.iterdir()
            if path.is_file() and path.name.endswith((".json", ".traj"))
        ),
        key=lambda path: natural_key(path.name),
    )
    if not trajectories:
        raise ValueError(f"no .json or .traj files found in {directory}")
    return trajectories


def load_image_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"image map not found: {resolved}")
    mapping: dict[str, str] = {}
    if resolved.suffix.casefold() == ".csv":
        with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                key = str(
                    row.get("trajectory")
                    or row.get("trajectory_path")
                    or row.get("filename")
                    or ""
                ).strip()
                image = str(row.get("image") or row.get("template") or "").strip()
                if not key or not image:
                    raise ValueError(
                        f"image map CSV row {row_number} requires trajectory and image"
                    )
                mapping[key] = image
    else:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("images"), dict):
            raw = raw["images"]
        if isinstance(raw, dict):
            for key, value in raw.items():
                image = (
                    value.get("image") or value.get("template")
                    if isinstance(value, dict)
                    else value
                )
                if not isinstance(image, str) or not image.strip():
                    raise ValueError(f"invalid image mapping for {key!r}")
                mapping[str(key)] = image.strip()
        elif isinstance(raw, list):
            for index, item in enumerate(raw, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"image map item {index} must be an object")
                key = (
                    item.get("trajectory")
                    or item.get("trajectory_path")
                    or item.get("filename")
                )
                image = item.get("image") or item.get("template")
                if (
                    not isinstance(key, str)
                    or not isinstance(image, str)
                    or not key.strip()
                    or not image.strip()
                ):
                    raise ValueError(
                        f"image map item {index} requires trajectory and image"
                    )
                mapping[key.strip()] = image.strip()
        else:
            raise ValueError("image map JSON must be an object or array")
    return mapping


def image_from_trajectory(path: Path, mapping: dict[str, str]) -> str | None:
    for key in (str(path), path.name, path.stem):
        if key in mapping:
            return mapping[key]
    raw = read_json(path)
    candidates = [
        raw.get("image"),
        raw.get("template"),
        (raw.get("replay_config") or {}).get("image")
        if isinstance(raw.get("replay_config"), dict)
        else None,
        (raw.get("replay_config") or {}).get("template")
        if isinstance(raw.get("replay_config"), dict)
        else None,
        (raw.get("info") or {}).get("image")
        if isinstance(raw.get("info"), dict)
        else None,
        (raw.get("info") or {}).get("template")
        if isinstance(raw.get("info"), dict)
        else None,
    ]
    return next(
        (
            value.strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        ),
        None,
    )


def prepare_selection(
    directory: Path, base_count: int, image_map: Path | None = None
) -> list[dict[str, Any]]:
    if base_count < 1:
        raise ValueError("base-count must be positive")
    trajectories = find_trajectories(directory.expanduser().resolve())
    pool = trajectories[:base_count]
    mapping = load_image_map(image_map)
    selection = []
    for offset in range(base_count):
        path = pool[offset % len(pool)]
        template = image_from_trajectory(path, mapping)
        if not template:
            raise ValueError(f"No image/template mapping for {path}")
        selection.append(
            {
                "selection_index": offset + 1,
                "source_trajectory": str(path),
                "template": template,
                "source_cycle": offset // len(pool) + 1,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return selection


def build_task_plan(
    selection: list[dict[str, Any]], total_tasks: int
) -> list[ReplayTask]:
    if not selection or total_tasks < 1:
        raise ValueError("selection must not be empty and total-tasks must be positive")
    return [
        ReplayTask(
            task_index=index + 1,
            source_trajectory=Path(
                selection[index % len(selection)]["source_trajectory"]
            ),
            template=selection[index % len(selection)]["template"],
            selection_index=selection[index % len(selection)]["selection_index"],
            occurrence=index // len(selection) + 1,
        )
        for index in range(total_tasks)
    ]
