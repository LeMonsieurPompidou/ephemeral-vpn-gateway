from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config_export import (
    ConfigExportError,
    copy_config_bytes,
    normalize_export_destination,
    proposed_export_path,
    resolve_desktop_directory,
)
from credential_store import CredentialStore, platform_credential_store
from file_lock import FileLock
from models import DeploymentOptions, DeploymentState
from orchestrator import Orchestrator

SaveDialog = Callable[[Path, str], Path | None]


def native_save_dialog(directory: Path, filename: str) -> Path | None:
    try:
        import webview

        window = webview.windows[0]
        selection = window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(directory),
            save_filename=filename,
            file_types=("WireGuard configuration (*.conf)",),
        )
    except (AttributeError, ImportError, IndexError, RuntimeError) as exc:
        raise ConfigExportError("Native Save As dialog is unavailable") from exc
    if not selection:
        return None
    selected = selection if isinstance(selection, str) else selection[0]
    return Path(selected)


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
    def __init__(
        self,
        resource_root: Path,
        runtime_root: Path | None = None,
        *,
        start_expiration_monitor: bool = True,
        acquire_app_lock: bool = True,
        save_dialog: SaveDialog = native_save_dialog,
        credential_store: CredentialStore | None = None,
    ) -> None:
        resolved_runtime = (runtime_root or default_runtime_root()).resolve()
        self._app_lock: FileLock | None = None
        if acquire_app_lock:
            self._app_lock = FileLock(resolved_runtime / "application.lock", timeout=0.2)
            self._app_lock.__enter__()
        self.orchestrator = Orchestrator(
            resource_root,
            resolved_runtime,
            credential_store=credential_store or platform_credential_store(),
        )
        self._operations: dict[str, threading.Thread] = {}
        self._operation_deployments: dict[str, str] = {}
        self._operation_results: dict[str, dict[str, object]] = {}
        self._logs: dict[str, list[str]] = {}
        self._lock = threading.RLock()
        self._save_dialog = save_dialog
        self.orchestrator.on_log = self._capture_log
        self.orchestrator.reconcile_interrupted()
        self._expiration_thread: threading.Thread | None = None
        if start_expiration_monitor:
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

    def credential_status(self, provider_id: str) -> dict[str, object]:
        return self.orchestrator.credential_status(provider_id)

    def save_provider_credentials(self, provider_id: str, values: dict[str, object]) -> dict[str, object]:
        return self.orchestrator.save_provider_credentials(provider_id, values)

    def remove_provider_credentials(self, provider_id: str, confirmed_active: bool = False) -> dict[str, object]:
        return self.orchestrator.remove_provider_credentials(provider_id, confirmed_active)

    def login_aws(self) -> dict[str, object]:
        return self.orchestrator.login_aws()

    def deploy(
        self,
        provider_id: str,
        location_id: str,
        options: dict[str, Any] | None = None,
        deployment_id: str | None = None,
    ) -> dict[str, object]:
        return self.orchestrator.deploy(provider_id, location_id, parse_options(options), deployment_id)

    def start_deploy(self, provider_id: str, location_id: str, options: dict[str, Any] | None = None) -> dict[str, str]:
        parsed_options = parse_options(options)
        try:
            record = self.orchestrator.reserve_deployment(provider_id, location_id, parsed_options)
        except Exception as exc:
            from security import redact

            return {"status": "error", "message": redact(str(exc))}
        operation_id = str(uuid.uuid4())
        deployment_id = record.id

        def target() -> None:
            try:
                result = self.orchestrator.deploy_reserved(deployment_id, parsed_options)
            except Exception as exc:
                from security import redact

                result = {
                    "status": "error",
                    "deployment_id": deployment_id,
                    "state": "failed",
                    "message": redact(str(exc)),
                }
            with self._lock:
                self._operation_results[operation_id] = result

        thread = threading.Thread(target=target, name=operation_id, daemon=True)
        with self._lock:
            self._operations[operation_id] = thread
            self._operation_deployments[operation_id] = deployment_id
        try:
            thread.start()
        except Exception as exc:
            from security import redact

            message = redact(f"Deployment worker could not start: {exc}")
            record = self.orchestrator.deployments.get(deployment_id)
            self.orchestrator._transition(record, DeploymentState.CANCELLED, message, error=message)
            with self._lock:
                self._operations.pop(operation_id, None)
                self._operation_deployments.pop(operation_id, None)
            return {"status": "error", "message": message, "deployment_id": deployment_id}
        return {"status": "started", "operation_id": operation_id, "deployment_id": deployment_id}

    def operation_status(self, operation_id: str) -> dict[str, object]:
        with self._lock:
            thread = self._operations.get(operation_id)
            if not thread:
                return {"status": "error", "message": "Unknown operation"}
            result = self._operation_results.get(operation_id)
            deployment_id = self._operation_deployments[operation_id]
        try:
            deployment = self.orchestrator.get_status(deployment_id)
        except KeyError:
            if result is None and thread.is_alive():
                return {
                    "status": "running",
                    "message": "Deployment reservation is becoming visible; retry status shortly",
                }
            return {"status": "error", "message": "Deployment record is unavailable; refresh Recovery"}
        if result is not None:
            return {"status": "complete", "result": result, "deployment": deployment}
        return {"status": "running", "deployment": deployment}

    def destroy(self, deployment_id: str, preserve_config: bool = False) -> dict[str, object]:
        # Compatibility: old clients passed a provider. Resolve only if unambiguous.
        if deployment_id in {provider["id"] for provider in self.list_providers()}:
            matches = [item for item in self.orchestrator.recovery_candidates() if item["provider_id"] == deployment_id]
            if len(matches) != 1:
                return {"status": "error", "message": "Legacy provider destroy is ambiguous; choose a deployment"}
            deployment_id = str(matches[0]["id"])
        if self._deployment_operation_is_running(deployment_id):
            return {
                "status": "error",
                "message": "Deployment is still running; cancel it and wait for the operation to stop before destroy",
            }
        return self.orchestrator.destroy(deployment_id, preserve_config)

    def cancel(self, deployment_id: str) -> dict[str, str]:
        return self.orchestrator.cancel(deployment_id)

    def remove_local_deployment(self, deployment_id: str) -> dict[str, str]:
        if self._deployment_operation_is_running(deployment_id):
            raise RuntimeError("Deployment is still running; cancel it and wait before removing local data")
        return self.orchestrator.remove_local_deployment(deployment_id)

    def get_status(self, deployment_id: str) -> dict[str, object]:
        return self.orchestrator.get_status(deployment_id)

    def get_client_configs(self, deployment_id: str) -> list[dict[str, object]]:
        return self.orchestrator.list_client_configs(deployment_id)

    def get_client_config(self, deployment_id: str, client_id: str | None = None) -> str:
        return self.orchestrator.get_client_config(deployment_id, client_id)

    def list_recovery_deployments(self) -> list[dict[str, object]]:
        return self.orchestrator.recovery_candidates()

    def list_legacy_states(self) -> list[dict[str, object]]:
        return self.orchestrator.list_legacy_states()

    def migrate_legacy_state(self, provider_id: str) -> dict[str, object]:
        return self.orchestrator.migrate_legacy_state(provider_id)

    def reconcile_stale_legacy_state(self, provider_id: str, confirmed: bool) -> dict[str, object]:
        return self.orchestrator.reconcile_stale_legacy_state(provider_id, confirmed)

    def verify_legacy_cloud_state(self, provider_id: str) -> dict[str, object]:
        return self.orchestrator.verify_legacy_cloud_state(provider_id)

    def get_logs(self, deployment_id: str) -> list[str]:
        persisted = self.orchestrator.get_logs(deployment_id)
        if persisted:
            return persisted
        with self._lock:
            return self._logs.get(deployment_id, [])[-250:]

    def get_client_config_export(self, deployment_id: str, client_id: str | None = None) -> dict[str, str]:
        clients = self.orchestrator.list_client_configs(deployment_id)
        selected_id = client_id or str(clients[0]["id"])
        if selected_id not in {str(client["id"]) for client in clients}:
            raise RuntimeError("Unknown deployment client")
        self.orchestrator.client_config_path(deployment_id, selected_id)
        desktop = resolve_desktop_directory()
        proposed = proposed_export_path(desktop, selected_id)
        return {
            "status": "ready",
            "deployment_id": deployment_id,
            "client_id": selected_id,
            "directory": str(desktop),
            "filename": proposed.name,
            "path": str(proposed),
        }

    def save_client_config(self, deployment_id: str, client_id: str | None = None) -> dict[str, str]:
        proposal = self.get_client_config_export(deployment_id, client_id)
        source = self.orchestrator.client_config_path(deployment_id, proposal["client_id"])
        selected = self._save_dialog(Path(proposal["directory"]), proposal["filename"])
        if selected is None:
            return {"status": "cancelled", "path": proposal["path"]}
        destination_path = normalize_export_destination(selected)
        digest = copy_config_bytes(source, destination_path)
        self.orchestrator.record_client_export(deployment_id, proposal["client_id"], destination_path, digest)
        return {"status": "success", "path": str(destination_path)}

    def _capture_log(self, deployment_id: str, line: str) -> None:
        with self._lock:
            self._logs.setdefault(deployment_id, []).append(line)

    def _expiration_monitor(self) -> None:
        """Destroy only deployments whose user explicitly enabled auto-expiration."""
        while True:
            self._expiration_pass(datetime.now(timezone.utc))
            time.sleep(30)

    def _expiration_pass(self, now: datetime) -> None:
        for record in self.orchestrator.deployments.list():
            if not record.auto_expire or not record.expires_at or record.state.value == "destroyed":
                continue
            try:
                expires = datetime.fromisoformat(record.expires_at)
                if expires > now:
                    continue
                if self._deployment_operation_is_running(record.id):
                    continue
                if (
                    record.resources_possible
                    or record.apply_started_at
                    or self.orchestrator.state_contains_resources(record)
                ):
                    result = self.orchestrator.destroy(record.id)
                    if result.get("status") != "success":
                        record.cleanup_status = "failed"
                        self.orchestrator.deployments.save(record)
                else:
                    self.orchestrator.remove_local_deployment(record.id)
            except Exception as exc:
                from security import redact

                record.cleanup_status = "failed"
                record.last_error = redact(str(exc))
                self.orchestrator.deployments.save(record)
                self.orchestrator._log(record.id, f"Automatic cleanup failed: {record.last_error}")

    def _deployment_operation_is_running(self, deployment_id: str) -> bool:
        with self._lock:
            return any(
                self._operation_deployments.get(operation_id) == deployment_id and thread.is_alive()
                for operation_id, thread in self._operations.items()
            )


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
