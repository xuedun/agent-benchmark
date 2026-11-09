"""Validate trials and export stable detail tables for analysis and plotting."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from .results import SCHEMA_VERSION, read_json, write_csv, write_json

PAUSE_RESUME_TASK_COLUMNS = [
    "ratio",
    "repeat",
    "oversubscription_ratio",
    "task_index",
    "source_index",
    "cycle",
    "occurrence",
    "source_trajectory",
    "staged_trajectory",
    "instance_id",
    "template",
    "sandbox_id",
    "started_at",
    "completed_at",
    "elapsed_sec",
    "replay_completed",
    "succeeded",
    "submitted",
    "commands",
    "succeeded_commands",
    "failed_commands",
    "requested_delay_sec",
    "actual_paused_sec",
    "pause_sec_sum",
    "paused_sec_sum",
    "capacity_wait_sec_sum",
    "run_queue_wait_sec_sum",
    "resume_sec_sum",
    "resume_total_sec_sum",
    "command_sec_sum",
    "command_start_sec_sum",
    "command_wait_sec_sum",
    "running_slot_held_sec_sum",
    "interaction_total_sec_sum",
    "post_command_pause_sec_sum",
    "resume_attempts_sum",
    "step_count",
    "error",
    "output_dir",
    "result_path",
]
CONTINUOUS_TASK_COLUMNS = [
    "ratio",
    "repeat",
    "oversubscription_ratio",
    "task_index",
    "source_index",
    "cycle",
    "occurrence",
    "source_trajectory",
    "staged_trajectory",
    "instance_id",
    "template",
    "sandbox_id",
    "command_transport",
    "route_source",
    "direct_envd_url",
    "started_at",
    "completed_at",
    "elapsed_sec",
    "replay_completed",
    "succeeded",
    "submitted",
    "commands",
    "succeeded_commands",
    "failed_commands",
    "timed_out_commands",
    "requested_delay_sec",
    "actual_delay_wait_sec",
    "running_slot_queue_wait_sec",
    "sandbox_create_sec",
    "first_command_to_last_command_sec",
    "delete_cleanup_sec",
    "first_command_to_delete_sec",
    "delay_wait_sec_sum",
    "command_sec_sum",
    "command_start_sec_sum",
    "command_wait_sec_sum",
    "interaction_total_sec_sum",
    "step_count",
    "error",
    "output_dir",
    "result_path",
]
TASK_COLUMNS = {
    "pause-resume": PAUSE_RESUME_TASK_COLUMNS,
    "continuous": CONTINUOUS_TASK_COLUMNS,
}
RATIO_COLUMNS = [
    "ratio",
    "oversubscription_ratio",
    "expected_repeats",
    "attempted",
    "successful",
    "all_repeats_successful",
    "median_batch_elapsed_sec",
    "average_batch_elapsed_sec",
    "min_batch_elapsed_sec",
    "max_batch_elapsed_sec",
    "median_throughput_tasks_per_sec",
    "time_degradation_vs_1_1_pct",
    "throughput_gain_vs_1_1_pct",
]


def trajectory_record(
    item: dict, result: dict, context: dict, result_path: Path
) -> dict:
    """Export the legacy trajectory CSV contract without weakening batch success.

    The old runners omitted counters when replay raised, but kept step details.
    A normal stop-on-error return populated counters even for an incomplete replay.
    Their CSV success field described replay completion, independent of cleanup.
    """
    steps = [step for step in result.get("steps", []) if isinstance(step, dict)]
    completed = result.get("replay_completed", item.get("replay_completed"))
    aborted = not completed and not result.get("stopped_early", False)

    def total(key: str) -> float:
        return sum(float(step.get(key) or 0.0) for step in steps)

    def counter(key: str):
        return None if aborted else result.get(key, item.get(key))

    row = {
        **context,
        "task_index": item["task_index"],
        # bench stages one source per workload, just like the old drivers.
        "source_index": item.get("source_index", 1),
        "cycle": item.get("cycle", item["occurrence"]),
        "occurrence": item["occurrence"],
        "source_trajectory": item["source_trajectory"],
        "staged_trajectory": item.get("staged_trajectory", item["source_trajectory"]),
        "template": result.get("template") or item.get("template"),
        "replay_completed": completed,
        "succeeded": completed,
        "submitted": counter("submitted"),
        "commands": counter("commands"),
        "succeeded_commands": counter("succeeded_commands"),
        "failed_commands": counter("failed_commands"),
        "requested_delay_sec": counter("requested_delay_sec"),
        "step_count": len(steps),
        "error": item.get("error")
        or result.get("error")
        or result.get("cleanup_error"),
        "output_dir": str(result_path.parent),
        "result_path": str(result_path),
    }
    for key in (
        "instance_id",
        "sandbox_id",
        "started_at",
        "completed_at",
        "elapsed_sec",
    ):
        row[key] = result.get(key)
    for key in (
        "command_sec",
        "command_start_sec",
        "command_wait_sec",
        "interaction_total_sec",
    ):
        row[key + "_sum"] = total(key)
    if context["execution_mode"] == "pause-resume":
        row["actual_paused_sec"] = None if aborted else total("paused_sec")
        for key in (
            "pause_sec",
            "paused_sec",
            "capacity_wait_sec",
            "run_queue_wait_sec",
            "resume_sec",
            "resume_total_sec",
            "running_slot_held_sec",
            "post_command_pause_sec",
            "resume_attempts",
        ):
            row[key + "_sum"] = total(key)
    else:
        row.update(
            command_transport="mock" if result.get("mock") else "aenv-proxy",
            route_source="mock" if result.get("mock") else "aenv-proxy",
            direct_envd_url=None,
            timed_out_commands=sum(bool(step.get("timed_out")) for step in steps),
            actual_delay_wait_sec=None if aborted else total("delay_wait_sec"),
            running_slot_queue_wait_sec=(
                None if aborted else result.get("initial_running_slot_queue_wait_sec")
            ),
            delay_wait_sec_sum=total("delay_wait_sec"),
        )
        for key in (
            "sandbox_create_sec",
            "first_command_to_last_command_sec",
            "delete_cleanup_sec",
            "first_command_to_delete_sec",
        ):
            row[key] = result.get(key)
    return row


def collect_trial(
    summary: dict[str, Any],
    *,
    ratio: int,
    repeat: int,
    limits: dict[str, int],
    output_dir: Path,
    reused: bool = False,
) -> tuple[dict, list[dict]]:
    elapsed = float(summary["elapsed_sec"])
    running = summary["running_slots"]
    expected = limits["total_tasks"]
    items = summary["items"]
    valid = (
        summary["total"] == expected
        and len(items) == expected
        and summary["succeeded"] == expected
        and summary["failed"] == 0
        and summary["concurrency"] == limits["concurrency"]
        and summary["running_concurrency"] == limits["running_concurrency"]
        and running["maximum"] == limits["running_concurrency"]
        and running.get("complete", True)
        and type(running["peak_active"]) is int
        and 0 <= running["peak_active"] <= limits["running_concurrency"]
        and running["active"] == 0
        and not summary.get("process_errors")
        and not summary.get("interrupted")
        and not summary.get("error")
        and not summary.get("observability_error")
        and not summary.get("observability_report_error")
    )
    context = {
        "execution_mode": summary["execution_mode"],
        "ratio": ratio,
        "repeat": repeat,
        "oversubscription_ratio": f"1:{ratio}",
    }
    tasks = []
    errors = [summary.get("error") or summary.get("observability_error") or summary.get("observability_report_error")]
    errors = [error for error in errors if error]
    for item in items:
        result_path = Path(item["result_path"])
        if not result_path.is_absolute():
            result_path = output_dir / result_path
        try:
            result = read_json(result_path)
        except (OSError, ValueError) as exc:
            result = {
                "error": f"Missing/invalid task result: {exc}",
                "succeeded": False,
                "replay_completed": False,
            }
            valid = False
        if not result.get("succeeded") or not result.get("cleanup_succeeded"):
            valid = False
        error = result.get("error") or result.get("cleanup_error") or item.get("error")
        if error:
            errors.append(str(error))
        tasks.append(trajectory_record(item, result, context, result_path))
    trial = {
        **context,
        **limits,
        "target_count": expected,
        "valid": valid,
        "total": summary["total"],
        "succeeded": summary["succeeded"],
        "failed": summary["failed"],
        "batch_elapsed_sec": elapsed,
        "throughput_tasks_per_sec": summary["total"] / elapsed if elapsed > 0 else None,
        "running_peak": running["peak_active"],
        "run_queue_wait_avg_sec": running["average_queue_wait_sec"],
        "reused": reused,
        "observability_enabled": summary.get("observability_enabled", False),
        "observability_interval_sec": summary.get("observability_interval_sec"),
        "observability_dir": summary.get("observability_dir"),
        "observability_return_code": summary.get("observability_return_code"),
        "observability_error": summary.get("observability_error"),
        "observability_reports_enabled": summary.get("observability_reports_enabled", False),
        "observability_report_return_code": summary.get("observability_report_return_code"),
        "observability_report_error": summary.get("observability_report_error"),
        "observability_report_elapsed_sec": summary.get("observability_report_elapsed_sec"),
        "observability_report_log": summary.get("observability_report_log"),
        "observability_report_files": summary.get("observability_report_files", {}),
        "output_dir": str(output_dir),
        "error": errors[0]
        if errors
        else (
            None if valid else "Trial incomplete or running-capacity validation failed"
        ),
    }
    return trial, tasks


def aggregate_ratios(trials: list[dict], ratios: list[int], repeats: int) -> list[dict]:
    rows = []
    for ratio in ratios:
        attempted = [x for x in trials if x["ratio"] == ratio]
        valid = [x for x in attempted if x["valid"]]
        elapsed = [x["batch_elapsed_sec"] for x in valid]
        throughput = [
            x["throughput_tasks_per_sec"]
            for x in valid
            if x["throughput_tasks_per_sec"] is not None
        ]
        rows.append(
            {
                "ratio": ratio,
                "oversubscription_ratio": f"1:{ratio}",
                "expected_repeats": repeats,
                "attempted": len(attempted),
                "successful": len(valid),
                "all_repeats_successful": len(valid) == repeats,
                "median_batch_elapsed_sec": statistics.median(elapsed)
                if elapsed
                else None,
                "average_batch_elapsed_sec": statistics.fmean(elapsed)
                if elapsed
                else None,
                "min_batch_elapsed_sec": min(elapsed) if elapsed else None,
                "max_batch_elapsed_sec": max(elapsed) if elapsed else None,
                "median_throughput_tasks_per_sec": statistics.median(throughput)
                if throughput
                else None,
                "time_degradation_vs_1_1_pct": None,
                "throughput_gain_vs_1_1_pct": None,
            }
        )
    baseline = next((x for x in rows if x["ratio"] == 1), None)
    if baseline:
        for row in rows:
            if (
                baseline["median_batch_elapsed_sec"]
                and row["median_batch_elapsed_sec"] is not None
            ):
                row["time_degradation_vs_1_1_pct"] = (
                    row["median_batch_elapsed_sec"]
                    / baseline["median_batch_elapsed_sec"]
                    - 1
                ) * 100
            if (
                baseline["median_throughput_tasks_per_sec"]
                and row["median_throughput_tasks_per_sec"] is not None
            ):
                row["throughput_gain_vs_1_1_pct"] = (
                    row["median_throughput_tasks_per_sec"]
                    / baseline["median_throughput_tasks_per_sec"]
                    - 1
                ) * 100
    return rows


def write_outputs(
    output_dir: Path,
    configuration: dict,
    trials: list[dict],
    tasks: list[dict],
) -> None:
    ratios = aggregate_ratios(trials, configuration["ratios"], configuration["repeats"])
    write_csv(output_dir / "ratio-summary.csv", RATIO_COLUMNS, ratios)
    write_csv(
        output_dir / "trajectory-detail.csv",
        TASK_COLUMNS[configuration["execution_mode"]],
        tasks,
    )
    write_json(
        output_dir / "benchmark-report.json",
        {
            "schema_version": SCHEMA_VERSION,
            "configuration": configuration,
            "trials": trials,
            "ratio_summary": ratios,
        },
    )
