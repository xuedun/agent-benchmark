#!/usr/bin/env python3
"""Build AENV templates from local images and the bundled trajectory mapping."""

import argparse
import csv
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHES = {"arm64": "arm64 linux", "x86": "amd64 linux"}


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def detect_arch():
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x86"
    raise ValueError(f"Unknown host architecture {machine!r}; specify --arch")


def load_cases(arch, registry):
    mapping = json.loads((ROOT / "artifacts/traces/image-map.json").read_text(
        encoding="utf-8"))
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("image-map.json must be a nonempty object")
    with (ROOT / "artifacts/images/manifest.csv").open(
            encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"instance_id", "replay_file", "image_arm64", "image_x86"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Manifest requires columns: {sorted(required)}")
        rows = list(reader)
    cases = []
    replays, templates, targets, instances = set(), set(), set(), set()
    for row in rows:
        replay = (row.get("replay_file") or "").strip()
        instance = (row.get("instance_id") or "").strip()
        if not instance or instance in instances:
            raise ValueError(f"Empty or duplicate instance_id: {instance!r}")
        if not replay or Path(replay).name != replay or replay in replays:
            raise ValueError(f"Invalid or duplicate replay_file: {replay!r}")
        if not (ROOT / "artifacts/traces/replays" / replay).is_file():
            raise ValueError(f"Missing trajectory: {replay}")
        name = mapping.get(replay)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"Missing or invalid template name for {replay}")
        if name in templates:
            raise ValueError(f"Duplicate template name: {name}")
        remote = (row.get(f"image_{arch}") or "").strip()
        # Keep the architecture in the repository name to avoid tag collisions.
        basename = remote.rsplit("/", 1)[-1]
        if (not remote or remote.startswith("-") or
                any(c.isspace() for c in remote) or
                not re.fullmatch(r"[a-z0-9][a-z0-9._-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*",
                                 basename)):
            raise ValueError(f"Invalid tagged image for {replay}: {remote!r}")
        target = f"{registry}/{basename}"
        if target in targets:
            raise ValueError(f"Duplicate registry target: {target}")
        cases.append({"replay": replay, "name": name,
                      "remote": remote, "target": target})
        replays.add(replay)
        instances.add(instance)
        templates.add(name)
        targets.add(target)
    if replays != set(mapping):
        raise ValueError("Manifest and image-map.json must contain the same trajectories")
    if not cases:
        raise ValueError("Manifest is empty")
    return cases


def command(args, capture=False):
    print(f"$ {shlex.join(args)}", flush=True)
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            timeout=60 if capture else None)
    return result.stdout.strip() if capture else None


def existing_templates():
    records = json.loads(command(
        ["aenv", "template", "list", "--output", "json"], capture=True))
    if not isinstance(records, list):
        raise ValueError("Unexpected AENV template list format (expected JSON array)")
    states = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Unexpected AENV template record")
        names = record.get("names")
        status = record.get("buildStatus")
        if (not isinstance(names, list) or
                not all(isinstance(name, str) for name in names) or
                not isinstance(status, str)):
            raise ValueError("AENV template records require names and buildStatus")
        for name in names:
            if name in states:
                raise ValueError(f"Ambiguous existing template name: {name}")
            states[name] = status
    return states


def build(case, arch, timeout, dry_run):
    expected = ARCHES[arch]
    commands = [
        ["docker", "image", "inspect", "--format",
         "{{.Architecture}} {{.Os}}", case["remote"]],
        ["docker", "tag", case["remote"], case["target"]],
        ["docker", "push", case["target"]],
        ["aenv", "pull", case["target"], "--name", case["name"],
         "--timeout", str(timeout)],
    ]
    for index, args in enumerate(commands):
        if dry_run:
            print(f"$ {shlex.join(args)}")
        elif index == 0:
            try:
                actual = command(args, capture=True)
            except subprocess.CalledProcessError as error:
                raise ValueError(
                    f"Cannot inspect local image {case['remote']}; check Docker "
                    "and run bash artifacts/images/download-images.sh first "
                    "if the image is missing") from error
            if actual != expected:
                raise ValueError(f"{case['remote']}: expected {expected}, got {actual}")
        else:
            command(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=ARCHES,
                        help="Target AENV architecture; defaults to this host")
    parser.add_argument("--registry", default="127.0.0.1:5000/swerebench",
                        help="Writable registry/repository prefix reachable by AENV")
    parser.add_argument("--timeout", type=positive_int, default=1800,
                        help="AENV build wait timeout in seconds (default: 1800)")
    parser.add_argument("--template", action="append", default=[], metavar="NAME",
                        help="Build only this mapped name; repeat to select several")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help="Print mappings without invoking Docker or AENV")
    mode.add_argument("--dry-run", action="store_true",
                      help="Print all selected commands without invoking Docker or AENV")
    args = parser.parse_args()
    try:
        arch = args.arch or detect_arch()
        registry = args.registry.rstrip("/")
        if (not registry or registry.startswith(("-", "/")) or
                "://" in registry or any(c.isspace() for c in registry)):
            raise ValueError("--registry must be an image prefix without a URL scheme")
        cases = load_cases(arch, registry)
        if args.template:
            requested = set(args.template)
            missing = requested - {case["name"] for case in cases}
            if missing:
                raise ValueError(f"Unknown template names: {sorted(missing)}")
            cases = [case for case in cases if case["name"] in requested]
        print(f"Architecture: {arch}; selected templates: {len(cases)}", flush=True)
        if args.list:
            for case in cases:
                print(f"{case['replay']} -> {case['name']} -> {case['remote']}")
            return 0
        states = {}
        if not args.dry_run:
            for executable in ("docker", "aenv"):
                if shutil.which(executable) is None:
                    raise ValueError(f"Required command not found: {executable}")
            states = existing_templates()
            # Check all names before making changes; never overwrite non-ready builds.
            for case in cases:
                status = states.get(case["name"])
                if status is not None and status != "ready":
                    raise ValueError(
                        f"{case['name']} already exists with status {status!r}; "
                        "resolve it in AENV before retrying")
        built = skipped = 0
        for index, case in enumerate(cases, 1):
            print(f"[{index}/{len(cases)}] {case['name']}", flush=True)
            if states.get(case["name"]) == "ready":
                print("  Reusing existing ready template (architecture not rechecked).")
                skipped += 1
                continue
            build(case, arch, args.timeout, args.dry_run)
            built += 1
        label = "Planned" if args.dry_run else "Built"
        print(f"{label}: {built}; reused: {skipped}")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; check AENV build status before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
