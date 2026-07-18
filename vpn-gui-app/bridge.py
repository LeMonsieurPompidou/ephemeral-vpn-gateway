from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import DeploymentOptions
from orchestrator import Orchestrator


def default_runtime_root() -> Path:
    configured = os.getenv("EPHEMERAL_VPN_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "EphemeralVpnGateway"


class BridgeService:
    def __init__(self, resource_root: Path, runtime_root: Path | None = None) -> None:
        self.orchestrator = Orchestrator(resource_root, runtime_root or default_runtime_root())
        self._operations: dict[str, threading.Thread] = {}
        self._operation_results: dict[str, dict[str, object]] = {}
        self._logs: dict[str, list[str]] = {}
        self._lock = threading.RLock()
        self.orchestrator.on_log = self._capture_log
        self._expiration_thread = threading.Thread(
            target=self._expiration_monitor, name="automatic-expiration", daemon=True
        )
        self._expiration_thread.start()

    def list_providers(self) -> list[dict[str, object]]:
        return self.orchestrator.list_providers()

    def list_locations(self, provider_id: str) -> list[dict[str, object]]:
        return self.orchestrator.list_locations(provider_id)

    def validate_credentials(self, provider_id: str) -> dict[str, object]:
        return self.orchestrator.validate_credentials(provider_id)

    def deploy(self, provider_id: str, location_id: str, options: dict[str, Any] | None = None) -> dict[str, object]:
        return self.orchestrator.deploy(provider_id, location_id, parse_options(options))

    def start_deploy(self, provider_id: str, location_id: str, options: dict[str, Any] | None = None) -> dict[str, str]:
        operation_id = f"operation-{len(self._operations) + 1}"

        def target() -> None:
            result = self.deploy(provider_id, location_id, options)
            with self._lock:
                self._operation_results[operation_id] = result

        thread = threading.Thread(target=target, name=operation_id, daemon=True)
        with self._lock:
            self._operations[operation_id] = thread
        thread.start()
        return {"status": "started", "operation_id": operation_id}

    def operation_status(self, operation_id: str) -> dict[str, object]:
        with self._lock:
            thread = self._operations.get(operation_id)
            if not thread:
                return {"status": "error", "message": "Unknown operation"}
            result = self._operation_results.get(operation_id)
            if result is not None:
                return {"status": "complete", "result": result}
            candidates = self.orchestrator.recovery_candidates()
            latest = candidates[-1] if candidates else None
            return {"status": "running", "deployment": latest}

    def destroy(self, deployment_id: str, preserve_config: bool = False) -> dict[str, object]:
        # Compatibility: old clients passed a provider. Resolve only if unambiguous.
        if deployment_id in {provider["id"] for provider in self.list_providers()}:
            matches = [item for item in self.orchestrator.recovery_candidates() if item["provider_id"] == deployment_id]
            if len(matches) != 1:
                return {"status": "error", "message": "Legacy provider destroy is ambiguous; choose a deployment"}
            deployment_id = str(matches[0]["id"])
        return self.orchestrator.destroy(deployment_id, preserve_config)

    def cancel(self, deployment_id: str) -> dict[str, str]:
        return self.orchestrator.cancel(deployment_id)

    def get_status(self, deployment_id: str) -> dict[str, object]:
        return self.orchestrator.get_status(deployment_id)

    def get_client_config(self, deployment_id: str) -> str:
        return self.orchestrator.get_client_config(deployment_id)

    def list_recovery_deployments(self) -> list[dict[str, object]]:
        return self.orchestrator.recovery_candidates()

    def get_logs(self, deployment_id: str) -> list[str]:
        with self._lock:
            return self._logs.get(deployment_id, [])[-250:]

    def save_client_config(self, deployment_id: str, destination: str) -> dict[str, str]:
        destination_path = Path(destination).expanduser().resolve()
        content = self.get_client_config(deployment_id)
        from security import write_secret

        write_secret(destination_path, content)
        return {"status": "success", "path": str(destination_path)}

    def _capture_log(self, deployment_id: str, line: str) -> None:
        with self._lock:
            self._logs.setdefault(deployment_id, []).append(line)

    def _expiration_monitor(self) -> None:
        """Destroy only deployments whose user explicitly enabled auto-expiration."""
        while True:
            now = datetime.now(timezone.utc)
            for record in self.orchestrator.deployments.list():
                if not record.auto_expire or not record.expires_at:
                    continue
                if record.state.value not in {"ready", "failed", "cancelled"}:
                    continue
                try:
                    expires = datetime.fromisoformat(record.expires_at)
                except ValueError:
                    continue
                if expires <= now:
                    self.orchestrator.destroy(record.id)
            time.sleep(30)


def parse_options(value: dict[str, Any] | None) -> DeploymentOptions:
    if not value:
        return DeploymentOptions()
    allowed = {field.name for field in __import__("dataclasses").fields(DeploymentOptions)}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown deployment options: {', '.join(sorted(unknown))}")
    converted = dict(value)
    for key in ("allowed_ips", "dns_servers"):
        if key in converted:
            raw = converted[key]
            if isinstance(raw, str):
                converted[key] = tuple(item.strip() for item in raw.split(",") if item.strip())
            elif isinstance(raw, list):
                converted[key] = tuple(raw)
    return DeploymentOptions(**converted)
