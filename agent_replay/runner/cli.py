"""CLI for single replay, multi-round benchmarks and read-only service checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .batch import run_batch
from .experiment import run_experiment
from .models import ReplayOptions, ReplayTask


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def parse_ratios(value: str) -> list[int]:
    try:
        values = sorted(
            {positive_int(item.strip()) for item in value.split(",") if item.strip()}
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "ratios must be comma-separated positive integers"
        ) from exc
    if not values:
        raise argparse.ArgumentTypeError("at least one ratio is required")
    return values


def positive_float(value: str) -> float:
    number = nonnegative_float(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url", default=os.environ.get("E2B_API_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=Path(
            os.environ.get("E2B_API_KEY_FILE", "/var/lib/aenv/secrets/api-key")
        ),
    )
    parser.add_argument("--verbose", action="store_true")


def replay_arguments(parser: argparse.ArgumentParser) -> None:
    connection_arguments(parser)
    parser.add_argument("--mode", choices=("pause-resume", "continuous"), required=True)
    parser.add_argument(
        "--mock", action="store_true",
        help="Replay real trajectories against a local success-only sandbox; disables monitoring",
    )
    parser.add_argument(
        "--event-loop", choices=("asyncio", "uvloop"), default="uvloop",
        help="Event loop for replay (default: uvloop)",
    )
    parser.add_argument(
        "--skip-delays", action="store_true",
        help="Skip recorded trajectory delays without changing commands or source files",
    )
    parser.add_argument(
        "--command-timeout-sec",
        type=nonnegative_int,
        default=300,
        help="0 disables command timeout",
    )
    parser.add_argument("--command-timeout-continue", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument(
        "--setup-command",
        help="Shell setup prefix executed before each trajectory command",
    )
    parser.add_argument("--request-timeout-sec", type=positive_int, default=120)
    parser.add_argument("--sandbox-timeout-sec", type=positive_int, default=86400)
    parser.add_argument("--create-retries", type=nonnegative_int, default=1)
    parser.add_argument(
        "--resume-retries",
        type=nonnegative_int,
        default=None,
        help="pause-resume only; default 2",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--observability", action=argparse.BooleanOptionalAction, default=True,
        help="Collect host and sandbox metrics for each executed batch (default: enabled)",
    )
    parser.add_argument(
        "--observability-interval-sec", type=positive_float, default=1.0,
        help="Host metrics sampling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--skip-orphan-cleanup",
        action="store_true",
        default=False,
        help="Skip orphan sandbox scan and reap before/after real runs (default: cleanup enabled)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_replay.runner", allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser(
        "bench", help="Run mixed workloads over ratios and repeats", allow_abbrev=False
    )
    replay_arguments(bench)
    bench.add_argument("--base-count", type=positive_int, required=True)
    bench.add_argument("--trajectory-dir", type=Path, required=True)
    bench.add_argument("--image-map", type=Path)
    bench.add_argument("--ratios", type=parse_ratios, default=[1])
    bench.add_argument("--repeats", type=positive_int, default=1)
    bench.add_argument(
        "--total-multiplier",
        type=positive_int,
        default=1,
        metavar="M",
        help="Task waves per trial: total=M*concurrency, concurrency=ratio*base-count (default: 1)",
    )
    bench.add_argument("--launch-interval-sec", type=nonnegative_float, default=0)
    bench.add_argument("--cooldown-sec", type=nonnegative_float, default=0)
    bench.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and save plans without connecting to AgentENV",
    )
    bench.add_argument(
        "--reuse",
        action="store_true",
        help="Reuse results only if configuration, plans and source hashes match",
    )
    bench.add_argument("--stop-on-failure", action="store_true")
    single = commands.add_parser(
        "run", help="Replay a single trajectory", allow_abbrev=False
    )
    replay_arguments(single)
    single.add_argument("--trajectory", type=Path, required=True)
    single.add_argument("--template", required=True)
    check = commands.add_parser(
        "check", help="Read service health and template existence", allow_abbrev=False
    )
    connection_arguments(check)
    check.add_argument("--template", required=True)
    export = commands.add_parser(
        "export-timings", help="Export offline task and command timing workbooks", allow_abbrev=False
    )
    export.add_argument("--experiment-dir", type=Path, required=True)
    export.add_argument("--output-dir", type=Path, required=True)
    export.add_argument("--trajectory-dir", type=Path, help="Relocate original source trajectories (SHA-256 checked)")
    export.add_argument("--verbose", action="store_true")
    return parser


def configure_environment(args: argparse.Namespace) -> None:
    os.environ["E2B_API_URL"] = args.api_url
    os.environ["E2B_SANDBOX_URL"] = args.api_url
    if not os.environ.get("E2B_API_KEY"):
        if not args.api_key_file.is_file():
            raise ValueError("Set E2B_API_KEY or supply an existing --api-key-file")
        key = args.api_key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("API key file is empty")
        os.environ["E2B_API_KEY"] = key
    os.environ.setdefault("E2B_ACCESS_TOKEN", "dummy")
    _patch_e2b_auth_header()


def _patch_e2b_auth_header() -> None:
    from e2b.api.client.client import AuthenticatedClient

    AuthenticatedClient.auth_header_name = "X-API-Key"
    AuthenticatedClient.prefix = ""


async def check_service(args: argparse.Namespace) -> int:
    from e2b import AsyncTemplate

    def health():
        try:
            with urllib.request.urlopen(
                args.api_url.rstrip("/") + "/health", timeout=5
            ) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    status = await asyncio.to_thread(health)
    exists = await AsyncTemplate.alias_exists(args.template)
    succeeded = 200 <= status < 300 and exists
    print(
        json.dumps(
            {
                "health_status": status,
                "template": args.template,
                "template_exists": exists,
                "succeeded": succeeded,
            }
        )
    )
    return 0 if succeeded else 1


def options_from_args(args: argparse.Namespace) -> ReplayOptions:
    return ReplayOptions(
        template=getattr(args, "template", ""),
        command_timeout_sec=args.command_timeout_sec,
        command_timeout_continue=args.command_timeout_continue,
        stop_on_error=args.stop_on_error,
        setup_command=args.setup_command,
        request_timeout_sec=args.request_timeout_sec,
        sandbox_timeout_sec=args.sandbox_timeout_sec,
        create_retries=args.create_retries,
        resume_retries=2 if args.resume_retries is None else args.resume_retries,
        mock=args.mock,
        event_loop=args.event_loop,
        skip_delays=args.skip_delays,
    )


async def dispatch(args: argparse.Namespace) -> int:
    if args.command == "export-timings":
        from ..report.export_timings import export_timings

        return export_timings(args.experiment_dir, args.output_dir, args.trajectory_dir)
    if args.command == "check":
        return await check_service(args)
    options = options_from_args(args)
    output_dir = (
        (
            args.output_dir
            or Path(__file__).resolve().parents[1]
            / "report"
            / "runs"
            / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{args.mode}"
        )
        .expanduser()
        .resolve()
    )
    skip_orphan_cleanup = getattr(args, "skip_orphan_cleanup", False)
    is_real_run = (
        not options.mock
        and not getattr(args, "dry_run", False)
        and args.command in ("run", "bench")
    )
    before_result = None
    if is_real_run and not skip_orphan_cleanup:
        from .replay.orphan_reaper import scan_and_reap

        before_result = await scan_and_reap(
            phase="before",
            request_timeout_sec=options.request_timeout_sec,
            max_age_sec=30,
            log_dir=None,
        )
    try:
        if args.command == "run":
            if output_dir.exists() and any(output_dir.iterdir()):
                raise ValueError("Single replay output directory must be empty")
            tasks = [ReplayTask(1, args.trajectory.expanduser().resolve(), args.template)]
            summary = await run_batch(
                tasks,
                mode=args.mode,
                concurrency=1,
                running_concurrency=1,
                options=options,
                output_dir=output_dir,
                observability=args.observability,
                observability_interval_sec=args.observability_interval_sec,
            )
            print(
                f"succeeded={summary['succeeded']} failed={summary['failed']} output={output_dir}"
            )
            if summary.get("error"):
                logging.error("%s", summary["error"])
            return 0 if summary["succeeded"] == 1 and not summary.get("error") else 1
        return await run_experiment(
            mode=args.mode,
            base_count=args.base_count,
            ratios=args.ratios,
            repeats=args.repeats,
            total_multiplier=args.total_multiplier,
            trajectory_dir=args.trajectory_dir,
            image_map=args.image_map,
            options=options,
            api_url=args.api_url,
            output_dir=output_dir,
            launch_interval_sec=args.launch_interval_sec,
            cooldown_sec=args.cooldown_sec,
            dry_run=args.dry_run,
            reuse=args.reuse,
            stop_on_failure=args.stop_on_failure,
            observability=args.observability,
            observability_interval_sec=args.observability_interval_sec,
        )
    finally:
        if is_real_run and not skip_orphan_cleanup:
            from .replay.orphan_reaper import _write_orphan_cleanup_json, scan_and_reap

            if before_result is not None:
                _write_orphan_cleanup_json(output_dir, "before", before_result.to_dict())
            try:
                await scan_and_reap(
                    phase="after",
                    request_timeout_sec=options.request_timeout_sec,
                    max_age_sec=0,
                    log_dir=output_dir,
                )
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "mode", None) == "continuous" and args.resume_retries is not None:
        parser.error("--resume-retries applies only to pause-resume mode")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if not args.verbose:
        for name in ("httpx", "httpcore", "e2b"):
            logging.getLogger(name).setLevel(logging.WARNING)
    try:
        if args.command == "export-timings" or getattr(args, "dry_run", False):
            return asyncio.run(dispatch(args))
        if getattr(args, "mock", False):
            logging.info("Mock replay: no sandbox connections; host monitoring and monitoring reports disabled")
        if getattr(args, "event_loop", "uvloop") == "asyncio":
            if not args.mock:
                configure_environment(args)
            return asyncio.run(dispatch(args))
        try:
            import uvloop
        except ImportError as exc:
            raise RuntimeError(
                "Replay requires uvloop; install dependencies with "
                "python -m pip install -r agent_replay/requirements.txt"
            ) from exc
        if not getattr(args, "mock", False):
            configure_environment(args)
        return uvloop.run(dispatch(args))
    except KeyboardInterrupt:
        print("Interrupted; completed and partial trial results were preserved")
        return 130
    except Exception as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        return 2
