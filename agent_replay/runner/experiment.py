"""One experiment: stable base selection, multiple ratios, repeated fresh trials."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..report.results import SCHEMA_VERSION, TIMING_CONTRACT, read_json, write_json
from ..report.summary import collect_trial, write_outputs
from .prepare import (
    build_task_plan,
    filter_selection,
    load_excluded_cases,
    prepare_selection,
)

from .batch import run_batch
from .models import ReplayOptions, trial_limits
from .multiprocess import TOPOLOGY
from ..observability.monitor import validate_interval
from .replay.trajectory import load_trajectory


async def run_experiment(
    *,
    mode: str,
    base_count: int,
    ratios: list[int],
    repeats: int,
    total_multiplier: int,
    trajectory_dir: Path,
    image_map: Path | None,
    options: ReplayOptions,
    api_url: str,
    output_dir: Path,
    launch_interval_sec: float = 0,
    cooldown_sec: float = 0,
    dry_run: bool = False,
    reuse: bool = False,
    stop_on_failure: bool = False,
    observability: bool = True,
    observability_interval_sec: float = 1.0,
    early_stop_rounds: int | None = None,
    exclude_cases_file: Path | None = None,
) -> int:
    validate_interval(observability_interval_sec)
    observability = observability and not options.mock
    if early_stop_rounds is not None and total_multiplier != 1:
        print(f"WARNING: --early-stop-rounds set; ignoring --total-multiplier={total_multiplier}",
              flush=True)
    if early_stop_rounds is not None:
        # Tolerant cleanup: early-stop aborts on the first failed task anyway,
        # so a failed delete must not also cascade-kill the other workers.
        options = replace(options, early_stop_tolerant_cleanup=True)
    # The requested loop belongs to the execution children, not the coordinator.
    output_dir = output_dir.expanduser().resolve()
    selection = prepare_selection(trajectory_dir, base_count, image_map)
    excluded_stems: list[str] = []
    if early_stop_rounds is not None:
        # Early-stop aborts on any failed task, so known-bad cases must be
        # dropped from the plan instead of failing the whole experiment.
        path = exclude_cases_file
        if path is None:
            default = Path("skiplist.txt")
            path = default if default.is_file() else None
        elif str(path).lower() in ("none", "-"):
            path = None
        if path is not None:
            selection, excluded_stems = filter_selection(selection, load_excluded_cases(path))
            if not selection:
                raise ValueError("exclusion file removed every selected case")
            if excluded_stems:
                print(f"early-stop: excluded {len(excluded_stems)} selection entries "
                      f"via {path} (e.g. {sorted(set(excluded_stems))[:3]})", flush=True)
    # Validate every selected trajectory before creating any sandbox.
    for source in {x["source_trajectory"] for x in selection}:
        load_trajectory(source)
    configuration: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timing_contract": TIMING_CONTRACT,
        "execution_mode": mode,
        "execution_topology": TOPOLOGY,
        "sharding": "contiguous-balanced-v1",
        "api_url": api_url,
        "base_count": base_count,
        "ratios": ratios,
        "repeats": repeats,
        "total_multiplier": total_multiplier,
        "early_stop_rounds": early_stop_rounds,
        "excluded_cases": sorted(set(excluded_stems)),
        "trajectory_dir": str(trajectory_dir.expanduser().resolve()),
        "launch_interval_sec": launch_interval_sec,
        "cooldown_sec": cooldown_sec,
        "observability": observability,
        "observability_interval_sec": observability_interval_sec,
        "replay_options": asdict(options),
        "selection": selection,
    }
    manifest = output_dir / "trajectory-selection.json"
    selection_record = {
        "natural_sort": True,
        "selected": selection,
        "configuration": {key: value for key, value in configuration.items() if key != "selection"},
    }
    if manifest.exists():
        if not reuse:
            raise ValueError(
                f"Output already contains an experiment; choose a new directory or --reuse: {output_dir}"
            )
        if read_json(manifest) != selection_record:
            raise ValueError(
                "Existing experiment configuration or trajectory contents differ; use a new output directory"
            )
    elif output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(
            f"Refusing to overwrite nonempty output directory: {output_dir}"
        )
    write_json(manifest, selection_record)
    trials, task_records = [], []
    executed = 0
    failed = False
    for ratio in ratios:
        limits = trial_limits(mode, base_count, ratio, total_multiplier,
                              early_stop_rounds=early_stop_rounds)
        unbounded = bool(limits["unbounded"])
        tasks = None if unbounded else build_task_plan(selection, limits["total_tasks"])
        for repeat in range(1, repeats + 1):
            trial_dir = output_dir / f"ratio-{ratio:02d}" / f"repeat-{repeat:02d}"
            planned = {
                "execution_mode": mode,
                "ratio": ratio,
                "repeat": repeat,
                "execution_topology": TOPOLOGY,
                "process_count": ratio,
                "workers_per_process": base_count,
                **limits,
                "tasks": ([task.record() for task in tasks] if tasks is not None else None),
                "task_generation": ({"kind": "unbounded-stride", "selection_size": len(selection)}
                                    if unbounded else None),
            }
            plan_path = trial_dir / "task-plan.json"
            if plan_path.exists() and read_json(plan_path) != planned:
                raise ValueError(f"Existing task plan differs: {plan_path}")
            summary_path = trial_dir / "batch-summary.json"
            if not summary_path.exists() and any(
                (trial_dir / name).exists() for name in ("tasks", "processes")
            ):
                raise ValueError(
                    f"Incomplete trial contains task artifacts; preserve it and use a new output directory: {trial_dir}"
                )
            write_json(plan_path, planned)
            plan_label = (f"unbounded(rounds={early_stop_rounds}, slots={len(selection)})"
                          if unbounded else f"{len(tasks)}")
            print(
                f"mode={mode} ratio=1:{ratio} repeat={repeat}/{repeats} "
                f"tasks={plan_label} concurrency={limits['concurrency']} running={limits['running_concurrency']} "
                f"processes={ratio} workers_per_process={base_count}",
                flush=True,
            )
            if dry_run:
                continue
            reused = reuse and summary_path.exists()
            cancelled = False
            if reused:
                summary = read_json(summary_path)
            else:
                if executed and cooldown_sec:
                    await asyncio.sleep(cooldown_sec)
                try:
                    summary = await run_batch(
                        tasks,
                        mode=mode,
                        concurrency=limits["concurrency"],
                        running_concurrency=limits["running_concurrency"],
                        options=options,
                        output_dir=trial_dir,
                        launch_interval_sec=launch_interval_sec,
                        observability=observability,
                        observability_interval_sec=observability_interval_sec,
                        process_count=ratio,
                        workers_per_process=base_count,
                        selection=selection,
                        early_stop_rounds=early_stop_rounds,
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    summary = read_json(summary_path)
                executed += 1
            if summary["execution_mode"] != mode:
                raise ValueError(f"Summary execution mode differs: {summary_path}")
            if (summary.get("execution_topology") != TOPOLOGY
                    or summary.get("process_count") != ratio
                    or summary.get("workers_per_process") != base_count):
                raise ValueError(f"Summary execution topology differs: {summary_path}")
            trial, details = collect_trial(
                summary,
                ratio=ratio,
                repeat=repeat,
                limits=limits,
                output_dir=trial_dir,
                reused=reused,
            )
            trials.append(trial)
            task_records.extend(details)
            write_outputs(output_dir, configuration, trials, task_records)
            failed |= not trial["valid"]
            print(
                f"valid={trial['valid']} elapsed={trial['batch_elapsed_sec']:.3f}s output={trial_dir}",
                flush=True,
            )
            if cancelled:
                raise asyncio.CancelledError
            if failed and stop_on_failure:
                return 1
    return 1 if failed else 0
