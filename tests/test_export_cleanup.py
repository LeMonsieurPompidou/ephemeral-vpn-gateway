from __future__ import annotations

from pathlib import Path

import pytest
from config_export import copy_config_bytes
from helpers import FakeTerraformRunner, make_orchestrator
from models import DeploymentOptions, DeploymentRecord
from orchestrator import Orchestrator


def deployed_with_exports(
    tmp_path: Path,
    *,
    clients: int = 1,
    runner: FakeTerraformRunner | None = None,
) -> tuple[Orchestrator, DeploymentRecord]:
    orchestrator = make_orchestrator(tmp_path, runner)
    result = orchestrator.deploy(
        "aws-lightsail",
        "us-east-1",
        DeploymentOptions(client_count=clients),
    )
    return orchestrator, orchestrator.deployments.get(str(result["deployment_id"]))


def export(orchestrator: Orchestrator, record: DeploymentRecord, client_id: str, destination: Path) -> str:
    deployment_id = str(record.id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = copy_config_bytes(orchestrator.client_config_path(deployment_id, client_id), destination)
    orchestrator.record_client_export(deployment_id, client_id, destination, digest)
    return digest


def test_confirmed_destroy_removes_one_tracked_export_and_preserves_unrelated_file(tmp_path: Path) -> None:
    orchestrator, record = deployed_with_exports(tmp_path)
    desktop = tmp_path / "Desktop"
    tracked = desktop / "HeresVPN1.conf"
    unrelated = desktop / "other.conf"
    export(orchestrator, record, "client-1", tracked)
    unrelated.write_text("unrelated", encoding="utf-8")

    result = orchestrator.destroy(str(record.id))

    assert result["status"] == "success"
    assert not tracked.exists()
    assert unrelated.read_text(encoding="utf-8") == "unrelated"
    final = orchestrator.deployments.get(str(record.id))
    assert final.local_cleanup_status == "complete"
    assert final.client_exports[0].cleanup_status == "deleted"
    assert final.local_cleanup_warnings == []


def test_multiple_clients_and_repeated_exports_are_all_tracked_and_removed(tmp_path: Path) -> None:
    orchestrator, record = deployed_with_exports(tmp_path, clients=2)
    destinations = [
        tmp_path / "Desktop" / "HeresVPN1.conf",
        tmp_path / "Alternate folder" / "phone.conf",
        tmp_path / "Desktop" / "HeresVPN2.conf",
    ]
    export(orchestrator, record, "client-1", destinations[0])
    export(orchestrator, record, "client-1", destinations[1])
    export(orchestrator, record, "client-2", destinations[2])
    persisted = orchestrator.deployments.get(str(record.id))
    assert {item.path for item in persisted.client_exports} == {str(path.resolve()) for path in destinations}

    assert orchestrator.destroy(str(record.id))["status"] == "success"
    assert all(not path.exists() for path in destinations)


def test_export_tracking_survives_restart_before_destroy(tmp_path: Path) -> None:
    runner = FakeTerraformRunner()
    orchestrator, record = deployed_with_exports(tmp_path, runner=runner)
    destination = tmp_path / "Desktop" / "HeresVPN1.conf"
    export(orchestrator, record, "client-1", destination)

    restarted = Orchestrator(orchestrator.resource_root, orchestrator.runtime_root, runner)
    recovered = restarted.deployments.get(str(record.id))

    assert len(recovered.client_exports) == 1
    assert recovered.client_exports[0].path == str(destination.resolve())
    assert "test-config" not in restarted.deployments.path.read_text(encoding="utf-8")


def test_destroy_failure_does_not_remove_export(tmp_path: Path) -> None:
    runner = FakeTerraformRunner(fail_at="destroy")
    orchestrator, record = deployed_with_exports(tmp_path, runner=runner)
    destination = tmp_path / "Desktop" / "HeresVPN1.conf"
    before = orchestrator.client_config_path(str(record.id), "client-1").read_bytes()
    export(orchestrator, record, "client-1", destination)

    result = orchestrator.destroy(str(record.id))

    assert result["status"] == "error"
    assert destination.read_bytes() == before
    assert orchestrator.deployments.get(str(record.id)).client_exports[0].cleanup_status == "tracked"


def test_absent_tracked_file_is_finalized_without_warning(tmp_path: Path) -> None:
    orchestrator, record = deployed_with_exports(tmp_path)
    destination = tmp_path / "Desktop" / "HeresVPN1.conf"
    export(orchestrator, record, "client-1", destination)
    destination.unlink()

    assert orchestrator.destroy(str(record.id))["status"] == "success"
    final = orchestrator.deployments.get(str(record.id))
    assert final.client_exports[0].cleanup_status == "missing"
    assert final.local_cleanup_status == "complete"


def test_changed_export_is_refused_without_failing_cloud_destroy(tmp_path: Path) -> None:
    orchestrator, record = deployed_with_exports(tmp_path)
    destination = tmp_path / "Desktop" / "HeresVPN1.conf"
    export(orchestrator, record, "client-1", destination)
    destination.write_text("replacement owned by the user", encoding="utf-8")

    result = orchestrator.destroy(str(record.id))

    assert result["status"] == "success"
    assert destination.read_text(encoding="utf-8") == "replacement owned by the user"
    final = orchestrator.deployments.get(str(record.id))
    assert final.local_cleanup_status == "warning"
    assert str(destination.resolve()) in final.local_cleanup_warnings[0]
    assert "replacement owned by the user" not in final.local_cleanup_warnings[0]


def test_symlinked_export_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator, record = deployed_with_exports(tmp_path)
    destination = tmp_path / "Desktop" / "HeresVPN1.conf"
    export(orchestrator, record, "client-1", destination)
    unrelated = tmp_path / "unrelated.conf"
    unrelated.write_text("do not delete", encoding="utf-8")
    destination.unlink()
    try:
        destination.symlink_to(unrelated)
    except OSError:
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == destination or original(self))

    result = orchestrator.destroy(str(record.id))

    assert result["status"] == "success"
    assert unrelated.read_text(encoding="utf-8") == "do not delete"
    final = orchestrator.deployments.get(str(record.id))
    assert final.local_cleanup_status == "warning"


def test_registry_path_manipulation_cannot_delete_different_content(tmp_path: Path) -> None:
    orchestrator, record = deployed_with_exports(tmp_path)
    destination = tmp_path / "Desktop" / "HeresVPN1.conf"
    digest = export(orchestrator, record, "client-1", destination)
    unrelated = tmp_path / "valuable.conf"
    unrelated.write_text("valuable unrelated data", encoding="utf-8")
    manipulated = orchestrator.deployments.get(str(record.id))
    manipulated.client_exports[0].path = str(unrelated.resolve())
    manipulated.client_exports[0].sha256 = digest
    orchestrator.deployments.save(manipulated)

    assert orchestrator.destroy(str(record.id))["status"] == "success"
    assert unrelated.read_text(encoding="utf-8") == "valuable unrelated data"
    assert destination.exists()
