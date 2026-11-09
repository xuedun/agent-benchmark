"""Compare two batches of mock scheduling samples without runtime dependencies."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

OPERATIONS = ("command_start", "command_wait")
METRICS = ("avg", "p50", "p90", "p95", "p99", "max")
NS_PER_MS = 1_000_000
# Batch settings that must match for a fair comparison; differences become warnings.
COMPARED_SETTINGS = ("execution_mode", "concurrency", "running_concurrency", "requested_total_tasks")


@dataclass
class Samples:
    path: Path
    values: dict[str, list[int]]
    skip_delays: bool | None
    event_loop: str | None
    event_loop_class: str | None
    summary: dict[str, Any] | None
    warnings: list[str]


def _nanoseconds(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(f"{field} must be a nonnegative 64-bit integer")
    return value


def load_samples(path: Path, expected_loop: str | None = None) -> Samples:
    """Read one batch side. Each log must use a single consistent loop, but the
    two compared sides may differ (asyncio vs uvloop, or single- vs multi-process)."""
    path = path.expanduser().resolve()
    if path.is_dir():
        path = path / "loop-scheduling.jsonl"
    values: dict[str, list[int]] = {operation: [] for operation in OPERATIONS}
    skip_delays = None
    event_loop = None
    loop_class = None
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("schema_version") != "1.0":
                    raise ValueError("expected a scheduling record with schema_version=1.0")
                if row.get("mock") is not True:
                    raise ValueError("expected mock=true")
                operation = row.get("operation")
                if operation not in OPERATIONS:
                    raise ValueError(f"unknown operation: {operation!r}")
                skipped = row.get("skip_delays")
                if type(skipped) is not bool:
                    raise ValueError("skip_delays must be a boolean")
                if skip_delays is not None and skip_delays != skipped:
                    raise ValueError("mixed skip_delays settings in one log")
                row_loop = row.get("event_loop")
                if not isinstance(row_loop, str) or not row_loop:
                    raise ValueError("event_loop is required")
                if expected_loop is not None and row_loop != expected_loop:
                    raise ValueError(f"expected event_loop={expected_loop}")
                if event_loop is not None and event_loop != row_loop:
                    raise ValueError("mixed event loops in one log")
                actual_class = row.get("event_loop_class")
                if not isinstance(actual_class, str) or not actual_class:
                    raise ValueError("event_loop_class is required")
                if loop_class is not None and loop_class != actual_class:
                    raise ValueError("mixed event loop classes in one log")
                start = _nanoseconds(row.get("yield_started_ns"), "yield_started_ns")
                resumed = _nanoseconds(row.get("resumed_ns"), "resumed_ns")
                waited = _nanoseconds(row.get("schedule_wait_ns"), "schedule_wait_ns")
                if resumed - start != waited:
                    raise ValueError("schedule_wait_ns does not match resumed_ns - yield_started_ns")
                skip_delays, event_loop, loop_class = skipped, row_loop, actual_class
                values[operation].append(waited)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc

    count = sum(len(items) for items in values.values())
    warnings = []
    summary_path = path.with_name("batch-summary.json")
    summary = None
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            raise ValueError(f"{summary_path}: expected an object")
        if summary.get("mock") is not True:
            raise ValueError(f"{summary_path}: mock metadata does not match the log")
        summary_loop = summary.get("event_loop")
        if not isinstance(summary_loop, str) or not summary_loop:
            raise ValueError(f"{summary_path}: event_loop is required")
        if event_loop is not None and summary_loop != event_loop:
            raise ValueError(f"{summary_path}: event_loop does not match the log")
        summary_skip = summary.get("skip_delays")
        if type(summary_skip) is not bool or (skip_delays is not None and summary_skip != skip_delays):
            raise ValueError(f"{summary_path}: skip_delays does not match the log")
        skip_delays, event_loop = summary_skip, summary_loop
        status = summary.get("loop_scheduling")
        if not isinstance(status, dict):
            raise ValueError(f"{summary_path}: missing loop_scheduling status")
        if status.get("sample_count") != count or status.get("written_samples") != count:
            raise ValueError(f"{summary_path}: recorded/written sample counts do not match {count} log rows")
        if summary.get("interrupted") or summary.get("error") or summary.get("failed") or status.get("error"):
            warnings.append("batch was interrupted or has errors/failed tasks; samples may be incomplete")
    else:
        warnings.append("batch-summary.json not found; batch completion and workload settings were not verified")
    if not count:
        warnings.append("no samples; statistics are N/A, not zero")
    if len(values["command_start"]) != len(values["command_wait"]):
        warnings.append("command_start and command_wait sample counts differ")
    return Samples(path, values, skip_delays, event_loop, loop_class, summary, warnings)


def statistics_ms(values: list[int]) -> dict[str, int | float | None]:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return {"count": 0, **dict.fromkeys(METRICS)}

    def percentile(p: int) -> float:
        # Linear interpolation at (n - 1) * p / 100, including the n=1 case.
        position = (count - 1) * p / 100
        lower = int(position)
        upper = min(lower + 1, count - 1)
        return (ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)) / NS_PER_MS

    return {
        "count": count,
        "avg": sum(ordered) / count / NS_PER_MS,
        **{f"p{p}": percentile(p) for p in (50, 90, 95, 99)},
        "max": ordered[-1] / NS_PER_MS,
    }


def side_label(path: Path) -> str:
    """Default a side's name to its experiment directory (…/<experiment>/ratio-XX/repeat-YY)."""
    path = path.expanduser().resolve()
    trial_dir = path.parent if path.name == "loop-scheduling.jsonl" else path
    if trial_dir.parent.name.startswith("ratio-"):
        return trial_dir.parent.parent.name
    return trial_dir.name


def compare(a_path: Path, b_path: Path, *, label_a: str | None = None, label_b: str | None = None) -> dict[str, Any]:
    """Side A is the baseline; delta = B - A and reduction = (A - B) / A."""
    left = load_samples(a_path)
    right = load_samples(b_path)
    labels = {"a": label_a or side_label(a_path), "b": label_b or side_label(b_path)}
    if left.skip_delays is not None and right.skip_delays is not None and left.skip_delays != right.skip_delays:
        raise ValueError("Cannot compare different skip_delays settings; run both sides with the same workload")
    warnings = [f"[{labels[side]}] {warning}"
                for side, samples in (("a", left), ("b", right))
                for warning in samples.warnings]
    if left.summary is not None and right.summary is not None:
        for field in COMPARED_SETTINGS:
            if left.summary.get(field) != right.summary.get(field):
                warnings.append(f"Batch setting {field} differs: "
                                f"{labels['a']}={left.summary.get(field)!r}, {labels['b']}={right.summary.get(field)!r}")
    groups = {}
    for operation in (*OPERATIONS, "all"):
        if operation == "all":
            a_values = [value for values in left.values.values() for value in values]
            u_values = [value for values in right.values.values() for value in values]
        else:
            a_values, u_values = left.values[operation], right.values[operation]
        a, u = statistics_ms(a_values), statistics_ms(u_values)
        if operation != "all" and a["count"] != u["count"]:
            warnings.append(f"{operation}: sample counts differ: {labels['a']}={a['count']}, {labels['b']}={u['count']}")
        comparisons = {}
        for metric in METRICS:
            baseline, candidate = a[metric], u[metric]
            comparisons[metric] = {
                "a_ms": baseline,
                "b_ms": candidate,
                "delta_ms": None if baseline is None or candidate is None else candidate - baseline,
                "reduction_pct": None if not baseline or candidate is None else (baseline - candidate) / baseline * 100,
            }
        groups[operation] = {"a_count": a["count"], "b_count": u["count"], "metrics": comparisons}
    return {
        "metric": "yield_to_resume",
        "unit": "ms",
        "percentile_method": "linear interpolation at (n - 1) * p / 100",
        "labels": labels,
        "paths": {"a": str(left.path), "b": str(right.path)},
        "event_loops": {"a": left.event_loop, "b": right.event_loop},
        "event_loop_classes": {"a": left.event_loop_class, "b": right.event_loop_class},
        "process_counts": {"a": left.summary.get("process_count") if left.summary else None,
                           "b": right.summary.get("process_count") if right.summary else None},
        "skip_delays": left.skip_delays if left.skip_delays is not None else right.skip_delays,
        "warnings": warnings,
        "groups": groups,
    }


def print_comparison(report: dict[str, Any]) -> None:
    labels = report["labels"]
    print("Yield-to-resume scheduling latency (milliseconds)")
    for side in ("a", "b"):
        info = f"event_loop={report['event_loops'][side]}"
        if report["process_counts"][side] is not None:
            info += f" process_count={report['process_counts'][side]}"
        print(f"[{labels[side]}] {report['paths'][side]}  {info}")
    print(f"skip_delays: {report['skip_delays']}; percentiles: linear interpolation")
    print(f"delta = {labels['b']} - {labels['a']} (negative is lower); "
          f"reduction = ({labels['a']} - {labels['b']}) / {labels['a']}")
    for operation, group in report["groups"].items():
        print(f"\n[{operation}] samples: {labels['a']}={group['a_count']}, {labels['b']}={group['b_count']}")
        width = max(14, len(labels["a"]) + 4, len(labels["b"]) + 4)
        header_a, header_b = f"{labels['a']}(ms)", f"{labels['b']}(ms)"
        print(f"{'metric':<8} {header_a:>{width}} {header_b:>{width}} {'delta(ms)':>15} {'reduction(%)':>15}")
        for metric, row in group["metrics"].items():
            cells = ["N/A" if row[key] is None else f"{row[key]:.6f}" for key in ("a_ms", "b_ms", "delta_ms")]
            reduction = "N/A" if row["reduction_pct"] is None else f"{row['reduction_pct']:.2f}"
            print(f"{metric:<8} {cells[0]:>{width}} {cells[1]:>{width}} {cells[2]:>15} {reduction:>15}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side_a", type=Path, help="Baseline batch directory or loop-scheduling.jsonl")
    parser.add_argument("side_b", type=Path, help="Candidate batch directory or loop-scheduling.jsonl")
    parser.add_argument("--label-a", help="Display name for side A (default: experiment directory name)")
    parser.add_argument("--label-b", help="Display name for side B (default: experiment directory name)")
    parser.add_argument("--output-json", type=Path, help="Optionally save the comparison; refuses existing files")
    args = parser.parse_args(argv)
    try:
        report = compare(args.side_a, args.side_b, label_a=args.label_a, label_b=args.label_b)
        if args.output_json is not None:
            with args.output_json.expanduser().open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        print_comparison(report)
        return 1 if report["warnings"] else 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
