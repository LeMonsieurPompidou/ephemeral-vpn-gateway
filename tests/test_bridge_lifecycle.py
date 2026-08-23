from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bridge import BridgeService
from helpers import add_record, make_resource_root
from models import DeploymentState


def deployment_runtime_names(bridge: BridgeService) -> list[str]:
    infrastructure = {"legacy-reconciliations", "legacy-verifications", "legacy-quarantine", "locks"}
    return sorted(
        path.name
        for path in bridge.orchestrator.runtime_root.iterdir()
        if path.is_dir() and path.name not in infrastructure
    )


def test_two_simultaneous_deploy_calls_create_one_durable_record(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    callers = threading.Barrier(3)
    release_operation = threading.Event()

    def deploy_reserved(deployment_id, options):  # type: ignore[no-untyped-def]
        release_operation.wait(timeout=5)
        return {"status": "success", "deployment_id": deployment_id, "state": "ready", "config": "config"}

    monkeypatch.setattr(bridge.orchestrator, "deploy_reserved", deploy_reserved)
    results: list[dict[str, str]] = []

    def start(provider_id: str, location_id: str) -> None:
        callers.wait(timeout=5)
        results.append(bridge.start_deploy(provider_id, location_id))

    threads = [
        threading.Thread(target=start, args=("digitalocean", "nyc3")),
        threading.Thread(target=start, args=("scaleway", "fr-par-1")),
    ]
    for thread in threads:
        thread.start()
    callers.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
    release_operation.set()

    started = [item for item in results if item["status"] == "started"]
    rejected = [item for item in results if item["status"] == "error"]
    assert len(started) == len(rejected) == 1
    assert "Another deployment operation is already active" in rejected[0]["message"]
    records = bridge.orchestrator.deployments.list()
    assert [record.id for record in records] == [started[0]["deployment_id"]]
    assert deployment_runtime_names(bridge) == [started[0]["deployment_id"]]


def test_file_lock_serializes_reservations_across_bridge_instances(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = make_resource_root(tmp_path)
    runtime = tmp_path / "runtime"
    first = BridgeService(root, runtime, start_expiration_monitor=False, acquire_app_lock=False)
    second = BridgeService(root, runtime, start_expiration_monitor=False, acquire_app_lock=False)
    callers = threading.Barrier(3)
    release_operation = threading.Event()

    def deploy_reserved(deployment_id, options):  # type: ignore[no-untyped-def]
        release_operation.wait(timeout=5)
        return {"status": "success", "deployment_id": deployment_id}

    monkeypatch.setattr(first.orchestrator, "deploy_reserved", deploy_reserved)
    monkeypatch.setattr(second.orchestrator, "deploy_reserved", deploy_reserved)
    results: list[dict[str, str]] = []

    def start(bridge: BridgeService) -> None:
        callers.wait(timeout=5)
        results.append(bridge.start_deploy("aws-lightsail", "us-east-1"))

    threads = [threading.Thread(target=start, args=(bridge,)) for bridge in (first, second)]
    for thread in threads:
        thread.start()
    callers.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
    release_operation.set()

    assert sum(result["status"] == "started" for result in results) == 1
    assert sum(result["status"] == "error" for result in results) == 1
    assert len(first.orchestrator.deployments.list()) == 1
    assert len(deployment_runtime_names(first)) == 1


def test_immediate_operation_poll_observes_durable_registry_record(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    release_operation = threading.Event()
    monkeypatch.setattr(
        bridge.orchestrator,
        "deploy_reserved",
        lambda deployment_id, options: (
            release_operation.wait(timeout=5) and {"status": "success", "deployment_id": deployment_id}
        ),
    )
    started = bridge.start_deploy("digitalocean", "nyc3")
    status = bridge.operation_status(started["operation_id"])
    release_operation.set()
    assert status["status"] == "running"
    assert status["deployment"]["id"] == started["deployment_id"]  # type: ignore[index]


def test_transient_status_read_miss_does_not_report_unknown_deployment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    release_operation = threading.Event()

    def deploy_reserved(deployment_id, options):  # type: ignore[no-untyped-def]
        release_operation.wait(timeout=5)
        return {"status": "success", "deployment_id": deployment_id}

    monkeypatch.setattr(bridge.orchestrator, "deploy_reserved", deploy_reserved)
    started = bridge.start_deploy("digitalocean", "nyc3")
    original_get_status = bridge.orchestrator.get_status
    monkeypatch.setattr(bridge.orchestrator, "get_status", lambda deployment_id: (_ for _ in ()).throw(KeyError()))
    transient = bridge.operation_status(started["operation_id"])
    assert transient["status"] == "running"
    assert "Unknown deployment" not in str(transient.get("message", ""))

    monkeypatch.setattr(bridge.orchestrator, "get_status", original_get_status)
    visible = bridge.operation_status(started["operation_id"])
    assert visible["deployment"]["id"] == started["deployment_id"]  # type: ignore[index]
    release_operation.set()


def test_registry_write_completes_before_deployment_id_is_exposed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    original_save = bridge.orchestrator.deployments.save
    write_entered = threading.Event()
    permit_write = threading.Event()
    returned: list[dict[str, str]] = []

    def delayed_save(record):  # type: ignore[no-untyped-def]
        write_entered.set()
        assert permit_write.wait(timeout=5)
        original_save(record)

    monkeypatch.setattr(bridge.orchestrator.deployments, "save", delayed_save)
    monkeypatch.setattr(
        bridge.orchestrator,
        "deploy_reserved",
        lambda deployment_id, options: {"status": "success", "deployment_id": deployment_id},
    )
    caller = threading.Thread(target=lambda: returned.append(bridge.start_deploy("digitalocean", "nyc3")))
    caller.start()
    assert write_entered.wait(timeout=5)
    assert returned == []
    assert bridge._operation_deployments == {}
    permit_write.set()
    caller.join(timeout=5)
    assert returned[0]["status"] == "started"
    assert bridge.get_status(returned[0]["deployment_id"])["id"] == returned[0]["deployment_id"]


def test_rapid_second_deploy_is_rejected_without_a_second_runtime(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    release_operation = threading.Event()

    def deploy_reserved(deployment_id, options):  # type: ignore[no-untyped-def]
        release_operation.wait(timeout=5)
        return {"status": "success", "deployment_id": deployment_id}

    monkeypatch.setattr(bridge.orchestrator, "deploy_reserved", deploy_reserved)
    first = bridge.start_deploy("aws-lightsail", "us-east-1")
    second = bridge.start_deploy("aws-lightsail", "us-east-1")
    release_operation.set()
    assert first["status"] == "started"
    assert second["status"] == "error"
    assert len(bridge.orchestrator.deployments.list()) == 1
    assert len(deployment_runtime_names(bridge)) == 1


def test_active_deployment_cannot_be_destroyed_or_removed_concurrently(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def deploy_reserved(deployment_id, options):  # type: ignore[no-untyped-def]
        worker_entered.set()
        release_worker.wait(timeout=5)
        return {"status": "success", "deployment_id": deployment_id}

    monkeypatch.setattr(bridge.orchestrator, "deploy_reserved", deploy_reserved)
    started = bridge.start_deploy("aws-lightsail", "us-east-1")
    assert worker_entered.wait(timeout=5)
    deployment_id = started["deployment_id"]

    with pytest.raises(RuntimeError, match="still running"):
        bridge.remove_local_deployment(deployment_id)
    destroy = bridge.destroy(deployment_id)
    assert destroy["status"] == "error"
    assert "still running" in str(destroy["message"])
    assert bridge.get_status(deployment_id)["id"] == deployment_id
    assert deployment_runtime_names(bridge) == [deployment_id]
    release_worker.set()


@pytest.mark.parametrize(
    ("state", "resources_possible", "apply_started"),
    [
        (DeploymentState.CANCELLED, False, False),
        (DeploymentState.FAILED, True, True),
    ],
)
def test_unreconciled_record_blocks_deploy_after_restart(
    tmp_path: Path,
    state: DeploymentState,
    resources_possible: bool,
    apply_started: bool,
) -> None:
    root = make_resource_root(tmp_path)
    runtime = tmp_path / "runtime"
    initial = BridgeService(root, runtime, start_expiration_monitor=False, acquire_app_lock=False)
    record = add_record(initial.orchestrator, "aws-lightsail", "unfinished")
    record.state = state
    record.resources_possible = resources_possible
    record.apply_started_at = record.created_at if apply_started else None
    initial.orchestrator.deployments.save(record)

    restarted = BridgeService(root, runtime, start_expiration_monitor=False, acquire_app_lock=False)
    result = restarted.start_deploy("digitalocean", "nyc3")
    assert result["status"] == "error"
    assert "unfinished" in result["message"]
    assert [item["id"] for item in restarted.list_recovery_deployments()] == ["unfinished"]
    assert len(restarted.orchestrator.deployments.list()) == 1


def test_destroyed_record_does_not_block_next_deployment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    old = add_record(bridge.orchestrator, "aws-lightsail", "destroyed")
    old.state = DeploymentState.DESTROYED
    old.resources_possible = False
    bridge.orchestrator.deployments.save(old)
    monkeypatch.setattr(
        bridge.orchestrator,
        "deploy_reserved",
        lambda deployment_id, options: {"status": "success", "deployment_id": deployment_id},
    )
    result = bridge.start_deploy("digitalocean", "nyc3")
    assert result["status"] == "started"
    assert len(bridge.orchestrator.deployments.list()) == 2


def test_explicit_cleanup_of_terminal_pre_apply_record_allows_next_deployment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
    )
    abandoned = add_record(bridge.orchestrator, "aws-lightsail", "abandoned")
    abandoned.state = DeploymentState.CANCELLED
    abandoned.resources_possible = False
    abandoned.apply_started_at = None
    bridge.orchestrator.deployments.save(abandoned)
    assert bridge.start_deploy("digitalocean", "nyc3")["status"] == "error"

    assert bridge.remove_local_deployment(abandoned.id)["status"] == "success"
    monkeypatch.setattr(
        bridge.orchestrator,
        "deploy_reserved",
        lambda deployment_id, options: {"status": "success", "deployment_id": deployment_id},
    )
    result = bridge.start_deploy("digitalocean", "nyc3")
    assert result["status"] == "started"
    assert [record.id for record in bridge.orchestrator.deployments.list()] == [result["deployment_id"]]


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
