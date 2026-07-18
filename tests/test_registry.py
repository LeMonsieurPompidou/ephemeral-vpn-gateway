from pathlib import Path

from models import DeploymentRecord, DeploymentState
from runtime_registry import DeploymentRegistry


def record(tmp_path: Path) -> DeploymentRecord:
    return DeploymentRecord(
        "id",
        "digitalocean",
        "nyc3",
        str(tmp_path),
        str(tmp_path / "state"),
        str(tmp_path / "run"),
        "2026-01-01T00:00:00+00:00",
    )


def test_registry_persists_and_transitions(tmp_path: Path) -> None:
    registry = DeploymentRegistry(tmp_path / "deployments.json")
    registry.save(record(tmp_path))
    registry.transition("id", DeploymentState.PROVISIONING)
    reloaded = DeploymentRegistry(tmp_path / "deployments.json")
    assert reloaded.get("id").state is DeploymentState.PROVISIONING


def test_interrupted_deployment_remains_recoverable(tmp_path: Path) -> None:
    registry = DeploymentRegistry(tmp_path / "deployments.json")
    item = record(tmp_path)
    item.state = DeploymentState.FAILED
    item.last_error = "cloud-init failed"
    registry.save(item)
    assert registry.get("id").last_error == "cloud-init failed"
