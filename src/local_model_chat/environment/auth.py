from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import re
from ctypes import wintypes
from pathlib import Path

from ..inference.registry import ProviderRegistry


class CredentialUnavailable(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(value: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    return _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_protect(value: bytes) -> bytes:
    if platform.system() != "Windows":
        raise CredentialUnavailable("Windows DPAPI is unavailable on this platform")
    source, source_buffer = _input_blob(value)
    output = _Blob()
    crypt = ctypes.windll.crypt32.CryptProtectData
    crypt.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.restype = wintypes.BOOL
    if not crypt(ctypes.byref(source), "MoliCoder account secret", None, None, None, 1, ctypes.byref(output)):
        raise CredentialUnavailable(f"DPAPI protection failed with Windows error {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(output.pbData, ctypes.c_void_p))


def _dpapi_unprotect(value: bytes) -> bytes:
    if platform.system() != "Windows":
        raise CredentialUnavailable("Windows DPAPI is unavailable on this platform")
    source, source_buffer = _input_blob(value)
    output = _Blob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.restype = wintypes.BOOL
    if not crypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise CredentialUnavailable(f"DPAPI unprotection failed with Windows error {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(output.pbData, ctypes.c_void_p))


class ProtectedCredentialStore:
    """User-scoped Windows DPAPI blobs; SQLite stores only opaque references."""

    def __init__(self, root: Path) -> None:
        if platform.system() != "Windows":
            raise CredentialUnavailable("this credential backend currently requires Windows")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, reference: str) -> Path:
        if not re.fullmatch(r"encrypted-local:[0-9a-f]{64}", reference):
            raise ValueError("invalid credential reference")
        return self.root / (reference.split(":", 1)[1] + ".dpapi")

    def put(self, account_id: str, secret: str) -> str:
        if not account_id or not secret or len(secret) > 16384:
            raise ValueError("account and bounded secret are required")
        reference = "encrypted-local:" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        path = self._path(reference)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(_dpapi_protect(secret.encode("utf-8")))
        os.replace(temporary, path)
        return reference

    def get(self, reference: str) -> str:
        return _dpapi_unprotect(self._path(reference).read_bytes()).decode("utf-8")

    def delete(self, reference: str) -> None:
        self._path(reference).unlink(missing_ok=True)


def store_provider_key(
    registry: ProviderRegistry, protected: ProtectedCredentialStore,
    provider_id: str, secret: str,
) -> str:
    if provider_id in {"ollama", "codex", "claude", "copilot"}:
        raise ValueError("this provider uses local or official CLI sign-in, not an API key here")
    if provider_id not in {item[0] for item in registry.list_providers()}:
        raise ValueError("provider is not registered")
    account_id = f"manual-key:{provider_id}"
    reference = protected.put(account_id, secret)
    registry.update_account_secret(account_id, provider_id, reference)
    return reference
