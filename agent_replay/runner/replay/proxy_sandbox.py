"""Proxy-sandbox mode: read-only slices never leave the paused state.

Extends the pause-resume runner: before executing a step, the action is
classified against a conservative read-only whitelist. Eligible steps are
served through AgentENV's resident proxy sandbox (POST
/sandboxes/{id}/proxy-read) -- no running slot is acquired, the target
sandbox is never resumed, and the post-command pause is skipped. Every other
action keeps the baseline resume -> command -> pause slice so the workload
mix and lifecycle semantics stay comparable with `--mode pause-resume`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ...report.results import absolute_timestamp

from ..models import ReplayStep
from .pause_resume import PauseResumeRunner

# ── Command classification ───────────────────────────────────────────────────
#
# The proxy executes `busybox chroot <target-rootfs> /bin/sh -c '<command>'`:
# the target image's own /bin/sh and binaries run with full flag
# compatibility.  Classification is intentionally conservative — a mis-routed
# write would silently diverge from the source trajectory, while a mis-routed
# read only costs one extra resume slice.

#: Read-only commands safe to execute against the paused rootfs.  Everything
#: here must be side-effect free on the filesystem.
READONLY_COMMANDS = frozenset(
    {
        "ls", "find", "cat", "head", "tail", "grep", "egrep", "fgrep",
        "stat", "wc", "du", "df", "which", "whereis", "basename", "dirname",
        "readlink", "realpath", "diff", "cmp", "sort", "uniq", "cut", "tr",
        "nl", "tac", "rev", "md5sum", "sha1sum", "sha256sum", "cksum",
        "sum", "pwd", "echo", "cd", "true", "false", "type",
    }
)

#: find primaries with filesystem side effects.
FIND_MUTATING_PRIMARIES = (
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
)

#: Segment separators inside a shell command line.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||\||;|\n")


def proxy_read_eligible(action: str | None) -> tuple[bool, str]:
    """Return (eligible, reason) for executing `action` via proxy-read."""
    if not action or not action.strip():
        return False, "empty"
    text = action
    if "$" in text:
        # Environment/positional variables resolve against the proxy's
        # environment, not the paused sandbox's shell state.
        return False, "env-var"
    if "`" in text or "$(" in text:
        return False, "command-substitution"
    if ">" in text or "<" in text:
        # Redirections, heredocs and process substitution.  Conservative:
        # even `2>/dev/null` is routed to the resume path.
        return False, "redirection"
    for segment in _SEGMENT_SPLIT.split(text):
        segment = segment.strip()
        if not segment:
            continue
        tokens = segment.split()
        if not tokens:
            continue
        head = tokens[0]
        if head not in READONLY_COMMANDS:
            return False, f"command:{head}"
        if head == "find":
            for primary in FIND_MUTATING_PRIMARIES:
                if any(token == primary or token.startswith(primary) for token in tokens[1:]):
                    return False, f"find:{primary}"
    return True, "ok"


# ── Proxy-read HTTP client ────────────────────────────────────────────────────

#: Server-side hard cap for ProxyReadRequest.timeoutSecs.
SERVER_TIMEOUT_CEILING_SEC = 300


class ProxyReadError(RuntimeError):
    """Infrastructure-level proxy-read failure (fallback eligible)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


#: AgentENV lives on localhost (or a routed interaction address); never
#: honor inherited http_proxy/https_proxy environment variables.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _proxy_read_http(
    api_url: str,
    api_key: str,
    sandbox_id: str,
    command: str,
    timeout_sec: float,
) -> dict[str, Any]:
    url = f"{api_url.rstrip('/')}/sandboxes/{sandbox_id}/proxy-read"
    body = json.dumps({"command": command, "timeoutSecs": int(timeout_sec)}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with _NO_PROXY_OPENER.open(request, timeout=timeout_sec + 30.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise ProxyReadError(
            f"proxy-read HTTP {exc.code}: {detail}", status=exc.code
        ) from exc
    except Exception as exc:
        raise ProxyReadError(f"proxy-read request failed: {exc!r}") from exc


class ProxySandboxRunner(PauseResumeRunner):
    execution_mode = "proxy-sandbox"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        # The very first action always takes the resume path: the initial
        # pause-triggered BIND may still be in flight, and the target rootfs
        # is not guaranteed to be mounted in the proxy sandbox yet.
        self._proxy_slices_started = False

    async def _run_step(
        self, step: ReplayStep, total: int, trajectory_total: int
    ) -> dict[str, Any]:
        first_slice = not self._proxy_slices_started
        self._proxy_slices_started = True
        if first_slice:
            eligible, reason = False, "first-action"
        elif self.options.proxy_read_enabled:
            eligible, reason = proxy_read_eligible(step.action)
        else:
            eligible, reason = False, "proxy-disabled"
        if not eligible:
            result = await super()._run_step(step, total, trajectory_total)
            result["transport"] = "resume"
            result["proxy_eligible"] = False
            result["proxy_skip_reason"] = reason
            return result

        self._job_event(
            "info",
            f"[{step.index + 1}/{trajectory_total}] PROXY-READ {step.action}",
            timestamp=absolute_timestamp(),
        )
        try:
            result = await self._proxy_read_step(step, total, trajectory_total)
            result["transport"] = "proxy_read"
            result["proxy_eligible"] = True
            result["proxy_skip_reason"] = None
            return result
        except ProxyReadError as exc:
            if not self.options.proxy_read_fallback:
                raise
            self._job_event(
                "warning",
                f"[{step.index + 1}/{trajectory_total}] Proxy read failed; "
                f"falling back to resume slice: {exc}",
            )
            result = await super()._run_step(step, total, trajectory_total)
            result["transport"] = "resume_fallback"
            result["proxy_eligible"] = True
            result["proxy_skip_reason"] = None
            result["proxy_fallback_reason"] = str(exc)
            return result

    async def _proxy_read_step(
        self, step: ReplayStep, total: int, trajectory_total: int
    ) -> dict[str, Any]:
        """Read one step against the paused sandbox; no slot, no resume."""
        preceding = self._preceding_pause
        requested = 0.0 if self.options.skip_delays else step.delay_time_sec
        started_wall = time.monotonic()
        # Consume the trajectory delay without holding a running slot so the
        # phase relationship between steps matches the pause-resume baseline.
        if requested > 0:
            await asyncio.sleep(requested)
        started_at = absolute_timestamp()
        response = await self._proxy_read_with_retries(step.action)
        elapsed_sec = time.monotonic() - started_wall
        return_code = response.get("exitCode")
        stdout = response.get("stdout") or ""
        stderr = response.get("stderr") or ""
        succeeded = return_code == 0
        completed_at = absolute_timestamp()
        result = {
            "index": step.index,
            "action": step.action,
            "execution_action": step.action,
            "started_at": started_at,
            "completed_at": completed_at,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "elapsed_sec": elapsed_sec,
            "command_start_sec": 0.0,
            "command_wait_sec": 0.0,
            "succeeded": succeeded,
            "timed_out": False,
            "infrastructure_error": False,
            "transport": "proxy_read",
            "proxy_duration_ms": response.get("durationMs"),
            "binding_generation": response.get("bindingGeneration"),
            "pause_started_at": preceding["pause_started_at"],
            "pause_completed_at": preceding["pause_completed_at"],
            "pause_sec": preceding["pause_sec"],
            "paused_sec": 0.0,
            "capacity_wait_sec": 0.0,
            "resume_sec": 0.0,
            "resume_api_sec": 0.0,
            "resume_total_sec": 0.0,
            "resume_attempts": 0,
            "trajectory_delay_sec": step.delay_time_sec,
            "requested_delay_sec": requested,
            "running_slot_saved": True,
            "interaction_total_sec": preceding["pause_sec"] + elapsed_sec,
        }
        self._job_event(
            "info",
            f"[{step.index + 1}/{trajectory_total}] RESULT "
            f"transport=proxy_read exit_code={return_code} succeeded={succeeded} "
            f"elapsed={elapsed_sec:.3f}s binding_gen={response.get('bindingGeneration')}",
            timestamp=completed_at,
        )
        return result

    async def _proxy_read_with_retries(self, command: str) -> dict[str, Any]:
        """One proxy-read with rebind_in_progress retries (503 backoff)."""
        api_url = os.environ.get("E2B_API_URL", "http://127.0.0.1:8000")
        api_key = os.environ.get("E2B_API_KEY", "")
        timeout_sec = min(float(self.options.command_timeout_sec), SERVER_TIMEOUT_CEILING_SEC)
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + self.options.proxy_read_retry_timeout_sec
        attempt = 0
        while True:
            attempt += 1
            try:
                return await loop.run_in_executor(
                    None,
                    lambda: _proxy_read_http(
                        api_url,
                        api_key,
                        str(self.sandbox_id),
                        command,
                        timeout_sec,
                    ),
                )
            except ProxyReadError as exc:
                retryable = exc.status == 503  # rebind_in_progress / proxy_unavailable
                if retryable and time.monotonic() < deadline:
                    await asyncio.sleep(min(0.5 * attempt, 5.0))
                    continue
                raise
