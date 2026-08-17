from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bridge import BridgeService
from helpers import add_record, make_resource_root
from models import DeploymentState


def test_operation_status_is_bound_to_its_deployment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    barrier = threading.Barrier(2)

    def deploy(provider_id, location_id, options=None, deployment_id=None):  # type: ignore[no-untyped-def]
        barrier.wait(timeout=5)
        return {"status": "success", "deployment_id": deployment_id, "state": "ready", "config": "config"}

    monkeypatch.setattr(bridge, "deploy", deploy)
    first = bridge.start_deploy("digitalocean", "nyc3")
    second = bridge.start_deploy("scaleway", "fr-par-1")
    deadline = time.monotonic() + 5
    results: dict[str, dict[str, object]] = {}
    while len(results) < 2 and time.monotonic() < deadline:
        for item in (first, second):
            status = bridge.operation_status(item["operation_id"])
            if status["status"] == "complete":
                result = status["result"]
                assert isinstance(result, dict)
                results[item["operation_id"]] = result
        time.sleep(0.01)
    assert results[first["operation_id"]]["deployment_id"] == first["deployment_id"]
    assert results[second["operation_id"]]["deployment_id"] == second["deployment_id"]
    assert first["deployment_id"] != second["deployment_id"]


def test_operation_status_and_logs_reconcile_from_persisted_deployment(tmp_path: Path) -> None:
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    record = add_record(bridge.orchestrator, "aws-lightsail", "active")
    record.state = DeploymentState.PLANNING
    bridge.orchestrator.deployments.save(record)
    Path(record.runtime_directory, "deployment.log").write_text("Initializing\nPlanning\n", encoding="utf-8")
    operation_id = "operation"
    bridge._operations[operation_id] = threading.Thread()
    bridge._operation_deployments[operation_id] = record.id

    first = bridge.operation_status(operation_id)
    assert first["status"] == "running"
    assert first["deployment"]["state"] == "planning"  # type: ignore[index]
    assert bridge.get_logs(record.id) == ["Initializing", "Planning"]

    record.state = DeploymentState.PROVISIONING
    bridge.orchestrator.deployments.save(record)
    second = bridge.operation_status(operation_id)
    assert second["deployment"]["state"] == "provisioning"  # type: ignore[index]


def test_expiration_failure_does_not_stop_other_cleanup(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    first = add_record(bridge.orchestrator, "aws-lightsail", "first")
    second = add_record(bridge.orchestrator, "digitalocean", "second")
    for record in (first, second):
        record.auto_expire = True
        record.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        record.resources_possible = True
        record.apply_started_at = record.created_at
        record.state = DeploymentState.FAILED
        Path(record.state_path).write_text("{}", encoding="utf-8")
        bridge.orchestrator.deployments.save(record)
    calls: list[str] = []

    def destroy(deployment_id: str):
        calls.append(deployment_id)
        if deployment_id == "first":
            return {"status": "error", "message": "expired credentials"}
        return {"status": "success"}

    monkeypatch.setattr(bridge.orchestrator, "destroy", destroy)
    bridge._expiration_pass(datetime.now(timezone.utc))
    assert calls == ["first", "second"]
    assert bridge.orchestrator.deployments.get("first").cleanup_status == "failed"


def test_overdue_deployment_survives_registry_restart(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = make_resource_root(tmp_path)
    runtime = tmp_path / "runtime"
    first_bridge = BridgeService(root, runtime, start_expiration_monitor=False, acquire_app_lock=False)
    record = add_record(first_bridge.orchestrator, "aws-lightsail", "overdue")
    record.auto_expire = True
    record.expires_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    record.resources_possible = True
    record.apply_started_at = record.created_at
    Path(record.state_path).write_text("{}", encoding="utf-8")
    first_bridge.orchestrator.deployments.save(record)
    restarted = BridgeService(root, runtime, start_expiration_monitor=False, acquire_app_lock=False)
    calls: list[str] = []

    def destroy(deployment_id: str) -> dict[str, str]:
        calls.append(deployment_id)
        return {"status": "success"}

    monkeypatch.setattr(restarted.orchestrator, "destroy", destroy)
    restarted._expiration_pass(datetime.now(timezone.utc))
    assert calls == ["overdue"]


def test_recovery_list_reflects_record_removed_between_refreshes(tmp_path: Path) -> None:
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    record = add_record(bridge.orchestrator, "aws-lightsail", "cancelled")
    record.state = DeploymentState.CANCELLED
    record.resources_possible = False
    record.apply_started_at = None
    bridge.orchestrator.deployments.save(record)

    assert [item["id"] for item in bridge.list_recovery_deployments()] == ["cancelled"]
    bridge.orchestrator.deployments.remove(record.id)

    assert bridge.list_recovery_deployments() == []
    with pytest.raises(KeyError, match="Unknown deployment"):
        bridge.get_status(record.id)
    with pytest.raises(KeyError, match="Unknown deployment"):
        bridge.remove_local_deployment(record.id)
