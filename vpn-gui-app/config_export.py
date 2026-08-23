from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

_DESKTOP_FOLDER_ID = uuid.UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641")
_CLIENT_ID = re.compile(r"client-([1-9]|10)\Z")


class ConfigExportError(RuntimeError):
    """Raised when a configuration cannot be exported safely."""


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> _Guid:
        return cls(value.time_low, value.time_mid, value.time_hi_version, (ctypes.c_ubyte * 8)(*value.bytes[8:]))


def _platform_is_windows() -> bool:
    return os.name == "nt"


def _windows_known_desktop() -> Path | None:
    """Resolve FOLDERID_Desktop with the Windows Known Folder API."""
    if not _platform_is_windows():
        return None
    path_pointer = ctypes.c_wchar_p()
    ole32: Any = None
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        folder_id = _Guid.from_uuid(_DESKTOP_FOLDER_ID)
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id),
            0,
            None,
            ctypes.byref(path_pointer),
        )
        if result != 0 or not path_pointer.value:
            return None
        return Path(path_pointer.value)
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if path_pointer.value and ole32 is not None:
            ole32.CoTaskMemFree(ctypes.cast(path_pointer, ctypes.c_void_p))


def resolve_desktop_directory() -> Path:
    """Resolve a user-facing Desktop independently of the process working directory."""
    known = _windows_known_desktop()
    if known is not None and known.is_dir():
        return known.resolve()

    home = Path.home()
    candidates: list[Path] = []
    if _platform_is_windows():
        onedrive = os.getenv("OneDrive") or os.getenv("OneDriveConsumer")
        if onedrive:
            candidates.append(Path(onedrive) / "Desktop")
        profile = os.getenv("USERPROFILE")
        if profile:
            candidates.append(Path(profile) / "Desktop")
    candidates.append(home / "Desktop")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return home.resolve()


def default_config_filename(client_id: str | None = None) -> str:
    match = _CLIENT_ID.fullmatch(client_id or "client-1")
    if match is None:
        raise ConfigExportError("VPN client identity is invalid")
    return f"HeresVPN{match.group(1)}.conf"


def proposed_export_path(
    desktop: Path,
    client_id: str | None = None,
) -> Path:
    if not desktop.is_absolute():
        raise ConfigExportError("Desktop directory must be absolute")
    return desktop / default_config_filename(client_id)


def normalize_export_destination(value: str | Path) -> Path:
    destination = Path(value).expanduser()
    if not destination.is_absolute():
        raise ConfigExportError("Configuration export destination must be absolute")
    if destination.name in {"", ".", ".."}:
        raise ConfigExportError("Configuration export filename is invalid")
    if destination.suffix.lower() != ".conf":
        destination = destination.with_suffix(".conf")
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise ConfigExportError("Configuration export directory does not exist") from exc
    if not parent.is_dir():
        raise ConfigExportError("Configuration export parent is not a directory")
    normalized = parent / destination.name
    if normalized.exists() and (normalized.is_symlink() or not normalized.is_file()):
        raise ConfigExportError("Configuration export destination is not a regular file")
    return normalized


def copy_config_bytes(source: Path, destination: Path) -> str:
    """Atomically copy the authoritative runtime bytes to an explicit destination."""
    try:
        source_identity = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise ConfigExportError("Runtime client configuration is missing or unsafe") from exc
    if not stat.S_ISREG(source_identity.st_mode) or source.is_symlink():
        raise ConfigExportError("Runtime client configuration is missing or unsafe")
    try:
        if destination.exists() and destination.samefile(source):
            raise ConfigExportError("The secure runtime configuration cannot be overwritten during export")
    except OSError as exc:
        raise ConfigExportError("Configuration export destination could not be verified") from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
        with os.fdopen(source_fd, "rb") as source_file:
            opened_identity = os.fstat(source_file.fileno())
            if (source_identity.st_dev, source_identity.st_ino) != (opened_identity.st_dev, opened_identity.st_ino):
                raise ConfigExportError("Runtime client configuration changed before it could be read")
            payload = source_file.read()
    except ConfigExportError:
        raise
    except OSError as exc:
        raise ConfigExportError("Runtime client configuration could not be read safely") from exc

    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        write_flags |= os.O_BINARY
    try:
        output_fd = os.open(temporary, write_flags, 0o600)
        with os.fdopen(output_fd, "wb") as output_file:
            output_file.write(payload)
            output_file.flush()
            os.fsync(output_file.fileno())
        if not _platform_is_windows():
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ConfigExportError("Configuration could not be saved to the selected destination") from exc
    return hashlib.sha256(payload).hexdigest()


def regular_file_sha256(path: Path) -> str:
    """Hash one exact regular file without following a symbolic link."""
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ConfigExportError("Tracked configuration is missing or unsafe") from exc
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ConfigExportError("Tracked configuration is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ConfigExportError("Tracked configuration changed before verification")
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
    except ConfigExportError:
        raise
    except OSError as exc:
        raise ConfigExportError("Tracked configuration could not be verified") from exc
    after = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ConfigExportError("Tracked configuration changed during verification")
    return digest.hexdigest()


def remove_tracked_config(path: Path, recorded_sha256: str, authoritative_sha256: str) -> str:
    """Remove a proven export, refusing paths whose content or type changed."""
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", recorded_sha256):
        raise ConfigExportError("Tracked configuration provenance is invalid")
    if not hmac.compare_digest(recorded_sha256, authoritative_sha256):
        raise ConfigExportError("Tracked configuration no longer matches its deployment client")
    try:
        current_sha256 = regular_file_sha256(path)
    except ConfigExportError as exc:
        if not path.exists() and not path.is_symlink():
            return "missing"
        raise exc
    if not hmac.compare_digest(current_sha256, recorded_sha256):
        raise ConfigExportError("Tracked configuration content changed after export")
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ConfigExportError("Tracked configuration is not a regular file")
    path.unlink()
    return "deleted"
