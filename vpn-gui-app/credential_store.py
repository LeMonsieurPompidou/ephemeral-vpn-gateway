from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any, Protocol

DIGITALOCEAN_TOKEN_TARGET = "HeresVPN/DigitalOcean/token"
SCALEWAY_ACCESS_KEY_TARGET = "HeresVPN/Scaleway/access-key"
SCALEWAY_SECRET_KEY_TARGET = "HeresVPN/Scaleway/secret-key"


class CredentialStoreError(RuntimeError):
    """A secret could not be read or changed in the platform credential store."""


class CredentialStore(Protocol):
    def save(self, target: str, secret: str) -> None: ...

    def get(self, target: str) -> str | None: ...

    def delete(self, target: str) -> bool: ...


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Per-user generic credentials backed by Windows Credential Manager."""

    _GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168
    _MAX_BLOB_BYTES = 2560

    def __init__(self, api: Any | None = None) -> None:
        if api is None:
            if os.name != "nt":
                raise CredentialStoreError("Secure persistent credentials require Windows Credential Manager")
            api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._api = api
        self._configure_api()

    def _configure_api(self) -> None:
        for name, argtypes, restype in (
            (
                "CredWriteW",
                (ctypes.POINTER(_CredentialW), wintypes.DWORD),
                wintypes.BOOL,
            ),
            (
                "CredReadW",
                (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CredentialW))),
                wintypes.BOOL,
            ),
            ("CredDeleteW", (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD), wintypes.BOOL),
            ("CredFree", (ctypes.c_void_p,), None),
        ):
            function = getattr(self._api, name)
            try:
                function.argtypes = argtypes
                function.restype = restype
            except AttributeError:
                # Test doubles intentionally use ordinary Python callables.
                pass

    @staticmethod
    def _validate_target(target: str) -> str:
        value = target.strip()
        if not value or len(value) > 256 or "\x00" in value or not value.startswith("HeresVPN/"):
            raise CredentialStoreError("Credential target is invalid")
        return value

    def save(self, target: str, secret: str) -> None:
        target = self._validate_target(target)
        if not secret or "\x00" in secret:
            raise CredentialStoreError("Credential value is empty or invalid")
        blob = secret.encode("utf-16-le")
        if len(blob) > self._MAX_BLOB_BYTES:
            raise CredentialStoreError("Credential value exceeds the Windows Credential Manager limit")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _CredentialW()
        credential.Type = self._GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.UserName = "HeresVPN"
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreError(f"Windows Credential Manager write failed (error {ctypes.get_last_error()})")

    def get(self, target: str) -> str | None:
        target = self._validate_target(target)
        pointer = ctypes.POINTER(_CredentialW)()
        if not self._api.CredReadW(target, self._GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(f"Windows Credential Manager read failed (error {error})")
        try:
            credential = pointer.contents
            if not credential.CredentialBlob or credential.CredentialBlobSize == 0:
                return ""
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-16-le")
        except (UnicodeError, ValueError) as exc:
            raise CredentialStoreError("Windows Credential Manager returned an invalid credential") from exc
        finally:
            self._api.CredFree(pointer)

    def delete(self, target: str) -> bool:
        target = self._validate_target(target)
        if self._api.CredDeleteW(target, self._GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == self._ERROR_NOT_FOUND:
            return False
        raise CredentialStoreError(f"Windows Credential Manager delete failed (error {error})")


class UnavailableCredentialStore:
    """Non-Windows fallback that preserves environment/tfvars operation."""

    def save(self, target: str, secret: str) -> None:
        raise CredentialStoreError("Secure persistent credential storage is unavailable on this platform")

    def get(self, target: str) -> str | None:
        return None

    def delete(self, target: str) -> bool:
        return False


def platform_credential_store() -> CredentialStore:
    return WindowsCredentialStore() if os.name == "nt" else UnavailableCredentialStore()
