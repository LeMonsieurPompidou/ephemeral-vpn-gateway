from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from file_lock import FileLock

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class LegacyReconciliationStore:
    """Atomic, provider-scoped storage for stale legacy-state receipts."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def read(self, provider_id: str) -> tuple[dict[str, Any] | None, str | None]:
        path = self._path(provider_id)
        with self._lock:
            with FileLock(path.with_suffix(".lock")):
                if not path.is_file():
                    return None, None
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return None, f"receipt cannot be read: {exc}"
        if not isinstance(value, dict):
            return None, "receipt root must be an object"
        return value, None

    def write(self, provider_id: str, receipt: dict[str, Any]) -> Path:
        path = self._path(provider_id)
        if receipt.get("provider_id") != provider_id:
            raise ValueError("Reconciliation receipt provider does not match its storage scope")
        payload = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8")
        temporary = path.with_suffix(".tmp")
        with self._lock:
            with FileLock(path.with_suffix(".lock")):
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                fd = os.open(temporary, flags, 0o600)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
        return path

    def _path(self, provider_id: str) -> Path:
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid provider ID for reconciliation receipt")
        path = (self.root / f"{provider_id}.json").resolve()
        if path.parent != self.root:
            raise ValueError("Unsafe reconciliation receipt path")
        return path


class LegacySourceStore:
    """Persist canonical historical provider roots across source and packaged runs."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def get(self, provider_id: str) -> Path | None:
        values = self._read()
        value = values.get(provider_id)
        if not isinstance(value, str):
            return None
        path = Path(value)
        return path.resolve() if path.is_absolute() else None

    def remember(self, provider_id: str, directory: Path) -> None:
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid provider ID for legacy source path")
        resolved = directory.resolve()
        if not resolved.is_absolute():
            raise ValueError("Legacy source path must be absolute")
        with self._lock:
            with FileLock(self.path.with_suffix(".lock")):
                values = self._read_unlocked()
                if values.get(provider_id) == str(resolved):
                    return
                values[provider_id] = str(resolved)
                self._write_unlocked(values)

    def _read(self) -> dict[str, str]:
        with self._lock:
            with FileLock(self.path.with_suffix(".lock")):
                return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}

    def _write_unlocked(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = json.dumps(values, indent=2, sort_keys=True).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
