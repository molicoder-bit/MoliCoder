"""Process birth identities used only to reconcile local run ownership."""
import os
from pathlib import Path
import subprocess


def process_birth(pid):
    """Return a birth identity, empty for gone, or None when it cannot be checked."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return '' if ctypes.get_last_error() == 87 else None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    proc = Path(f'/proc/{int(pid)}/stat')
    if Path('/proc').is_dir():
        try:
            return proc.read_text().rsplit(')', 1)[1].split()[19]
        except FileNotFoundError:
            return ''
        except OSError:
            return None
    try:
        result = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'lstart='], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode in (0, 1) else None
    except (OSError, subprocess.TimeoutExpired):
        return None
