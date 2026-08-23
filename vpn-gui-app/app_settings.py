from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    version: int = 1
    aws_profile: str | None = None
    scaleway_project_id: str | None = None


class SettingsStore:
    """Atomic persistence for non-secret application preferences only."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def load(self) -> AppSettings:
        if not self.path.is_file():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                return AppSettings()
            profile = raw.get("aws_profile")
            project = raw.get("scaleway_project_id")
            return AppSettings(
                aws_profile=profile.strip() if isinstance(profile, str) and profile.strip() else None,
                scaleway_project_id=project.strip() if isinstance(project, str) and project.strip() else None,
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        payload = asdict(settings)
        forbidden = {"token", "secret", "access_key", "private_key"}
        if forbidden & payload.keys():
            raise ValueError("Secret fields are forbidden in application settings")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def update(
        self, *, aws_profile: str | None | object = ..., scaleway_project_id: str | None | object = ...
    ) -> AppSettings:
        current = self.load()
        updated = AppSettings(
            aws_profile=current.aws_profile if aws_profile is ... else _optional_string(aws_profile),
            scaleway_project_id=(
                current.scaleway_project_id if scaleway_project_id is ... else _optional_string(scaleway_project_id)
            ),
        )
        self.save(updated)
        return updated


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Setting value must be text or null")
    stripped = value.strip()
    return stripped or None
