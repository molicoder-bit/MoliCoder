from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GpuTelemetry:
    name: str = "Unavailable"
    total_mib: int = 0
    used_mib: int = 0
    free_mib: int = 0
    utilization_percent: int = 0


def read_nvidia_telemetry() -> GpuTelemetry | None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=4, check=True,
            creationflags=creationflags,
        )
        first = completed.stdout.strip().splitlines()[0]
        name, total, used, free, utilization = [part.strip() for part in first.split(",")]
        return GpuTelemetry(name, int(total), int(used), int(free), int(utilization))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def format_bytes(value: int) -> str:
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.1f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.1f} MB"
    return f"{value} B"

