from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_TOOLS = (
    "python", "git", "node", "npm", "flutter", "ollama",
    "codex", "opencode", "claude", "copilot", "kilo", "qwen", "gemini", "kandev",
)
_PACKAGES = {
    "Windows": ("winget", "scoop", "choco"),
    "Darwin": ("brew", "port"),
    "Linux": ("apt", "dnf", "pacman", "zypper", "snap", "flatpak"),
}


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    name: str
    path: str | None
    version: str | None
    state: str


@dataclass(frozen=True, slots=True)
class EnvironmentReport:
    version: int
    system: str
    architecture: str
    cpu_count: int | None
    memory_bytes: int | None
    disk_free_bytes: int | None
    gpu: str
    gpu_source: str
    tools: tuple[DiscoveredTool, ...]
    package_managers: tuple[str, ...]
    project_branch: str | None = None
    project_dirty: bool | None = None


def _version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0][:120] if result.returncode == 0 and output else None


def _memory_bytes(system: str) -> int | None:
    try:
        if system == "Linux":
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        if system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                timeout=3, check=False,
            )
            return int(result.stdout.strip()) if result.returncode == 0 else None
        if system == "Windows":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            return int(status.ullTotalPhys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _gpu(which: Callable[[str], str | None]) -> tuple[str, str]:
    binary = which("nvidia-smi")
    if not binary:
        return "unknown", "no supported read-only GPU probe available"
    try:
        result = subprocess.run(
            [binary, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=4, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", "nvidia-smi probe failed"
    if result.returncode != 0:
        return "unknown", "nvidia-smi probe failed"
    return result.stdout.strip()[:500] or "unknown", "nvidia-smi"


def discover_environment(
    *, project_root: Path | None = None,
    system: str | None = None, architecture: str | None = None,
    cpu_count: int | None = None, memory_bytes: int | None = None,
    disk_free_bytes: int | None = None,
    gpu: tuple[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    version: Callable[[str], str | None] = _version,
) -> EnvironmentReport:
    system = system or platform.system()
    architecture = architecture or platform.machine()
    architecture = {
        "amd64": "x86_64", "x64": "x86_64", "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(architecture.lower(), architecture)
    cpu_count = cpu_count if cpu_count is not None else os.cpu_count()
    memory_bytes = memory_bytes if memory_bytes is not None else _memory_bytes(system)
    if disk_free_bytes is None:
        try:
            disk_free_bytes = shutil.disk_usage(project_root or Path.cwd()).free
        except OSError:
            disk_free_bytes = None
    gpu_name, gpu_source = gpu if gpu is not None else _gpu(which)
    tools = []
    for name in _TOOLS:
        binary = which(name)
        tools.append(DiscoveredTool(
            name, str(Path(binary).resolve()) if binary else None,
            version(binary) if binary else None,
            "installed" if binary else "missing",
        ))
    managers = tuple(name for name in _PACKAGES.get(system, ()) if which(name))
    branch = None
    dirty = None
    if project_root is not None:
        from ..molicoder.projectstate import read_git_state

        state = read_git_state(project_root)
        if state.available:
            branch, dirty = state.branch, state.dirty
    return EnvironmentReport(
        1, system, architecture, cpu_count, memory_bytes, disk_free_bytes,
        gpu_name, gpu_source, tuple(tools), managers, branch, dirty,
    )
