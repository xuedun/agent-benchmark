"""Fetch per-sandbox guest metrics history from the AgentENV metrics API.

The AgentENV server samples each running sandbox's envd guest metrics
(cpu/mem/disk) every `metrics_interval_secs` (default 15s) and retains them
for `metrics_retention_secs` (default 3600s). `GET /sandboxes/{id}/metrics`
returns the in-memory history, so a single fetch before sandbox deletion
reconstructs the per-VM memory curve without running anything on the host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

METRICS_FILE = "sandbox-metrics.json"


def fetch_sandbox_metrics(
    sandbox_id: str,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout_s: float = 10.0,
) -> list[dict] | None:
    api_url = (api_url or os.environ.get("E2B_API_URL", "http://127.0.0.1:8000")).rstrip("/")
    api_key = api_key or os.environ.get("E2B_API_KEY", "")
    if not api_key or not sandbox_id:
        return None
    url = f"{api_url}/sandboxes/{sandbox_id}/metrics"
    try:
        try:
            resp = httpx.get(url, headers={"X-API-Key": api_key}, timeout=timeout_s)
        except ImportError:
            resp = httpx.get(url, headers={"X-API-Key": api_key}, timeout=timeout_s, trust_env=False)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError, OSError):
        return None
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    if isinstance(data, dict) and isinstance(data.get("samples"), list):
        return [s for s in data["samples"] if isinstance(s, dict)]
    return None


def write_sandbox_metrics(output_dir: Path, sandbox_id: str, samples: list[dict] | None) -> bool:
    if not samples:
        return False
    payload = {
        "sandbox_id": sandbox_id,
        "sample_count": len(samples),
        "samples": samples,
    }
    path = Path(output_dir) / METRICS_FILE
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return True
