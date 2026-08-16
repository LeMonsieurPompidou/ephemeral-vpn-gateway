from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from file_lock import FileLock
from models import DeploymentRecord, DeploymentState, now_iso


class DeploymentRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def list(self) -> list[DeploymentRecord]:
        with self._lock:
            with FileLock(self.path.with_suffix(".lock")):
                values = list(self._read().values())
            return sorted((DeploymentRecord.from_dict(item) for item in values), key=lambda item: item.created_at)

    def get(self, deployment_id: str) -> DeploymentRecord:
        with self._lock:
            with FileLock(self.path.with_suffix(".lock")):
                try:
                    return DeploymentRecord.from_dict(self._read()[deployment_id])
                except KeyError as exc:
                    raise KeyError(f"Unknown deployment: {deployment_id}") from exc

    def save(self, record: DeploymentRecord) -> None:
        with self._lock:
            with FileLock(self.path.with_suffix(".lock")):
                records = self._read()
                record.updated_at = now_iso()
                records[record.id] = record.to_dict()
                self._write(records)

    def remove(self, deployment_id: str) -> None:
        with self._lock:
            with FileLock(self.path.with_suffix(".lock")):
                records = self._read()
                if deployment_id not in records:
                    raise KeyError(f"Unknown deployment: {deployment_id}")
                del records[deployment_id]
                self._write(records)

    def transition(self, deployment_id: str, state: DeploymentState, *, error: str | None = None) -> DeploymentRecord:
        record = self.get(deployment_id)
        record.state = state
        record.last_error = error
        self.save(record)
        return record

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read deployment registry: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Deployment registry is malformed")
        return value

    def _write(self, value: dict[str, object] | dict[str, dict[str, object]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)
