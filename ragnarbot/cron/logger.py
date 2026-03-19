"""Cron execution logger — JSONL per job."""

import json
import time
from pathlib import Path

from ragnarbot.cron.types import CronJob
from ragnarbot.instance import ensure_instance_root


def get_cron_logs_dir() -> Path:
    """Return (and create) the cron logs directory."""
    logs_dir = ensure_instance_root().cron_logs_path
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def log_execution(
    job: CronJob,
    output: str | None,
    status: str,
    duration_s: float,
    error: str | None = None,
) -> None:
    """Append one JSONL execution record for *job*."""
    logs_dir = get_cron_logs_dir()
    log_file = logs_dir / f"{job.id}.jsonl"

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "job_id": job.id,
        "job_name": job.name,
        "mode": job.payload.mode,
        "input": job.payload.message,
        "output": output,
        "status": status,
        "duration_s": round(duration_s, 2),
        "error": error,
    }

    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
