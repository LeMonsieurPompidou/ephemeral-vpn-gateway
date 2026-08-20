from __future__ import annotations

import json
from pathlib import Path

import orchestrator as orchestrator_module
import pytest
from helpers import FakeTerraformRunner, add_record, legacy_state, make_orchestrator
from models import DeploymentOptions, DeploymentState
from security import generate_ssh_keypair, ssh_public_key_fingerprint, verify_ssh_keypair
from terraform_runner import TerraformCancelled, TerraformError


@pytest.mark.parametrize(
    ("provider_id", "location_id"),
    [("aws-lightsail", "us-east-1"), ("digitalocean", "nyc3"), ("scaleway", "fr-par-1")],
)
def test_state_backend_is_deployment_scoped(tmp_path: Path, provider_id: str, location_id: str) -> None:
    runner = FakeTerraformRunner()
    orchestrator = make_orchestrator(tmp_path, runner)
    result = orchestrator.deploy(provider_id, location_id, DeploymentOptions())
    assert result["status"] == "success"
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    init = next(call for call in runner.calls if call[0][0] == "init")
    assert f"-backend-config=path={runtime / 'terraform.tfstate'}" in init[0]
    assert init[2]["TF_DATA_DIR"] == str(runtime / ".terraform")
    assert all(not any(arg.startswith("-state=") for arg in call[0]) for call in runner.calls)
    assert (runtime / "terraform.tfstate").is_file()
    assert not (Path(record.terraform_directory) / "terraform.tfstate").exists()
    variables = json.loads((runtime / "deployment.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "ssh_public_key" in variables
    assert "ssh_private_key" not in variables
    assert "client_private_key" not in variables
    if provider_id == "aws-lightsail":
        assert variables["user_data_payload"].startswith("#!/bin/sh\n")
    else:
        assert variables["user_data_payload"].startswith("#cloud-config\n")
    destroy = orchestrator.destroy(record.id)
    assert destroy["status"] == "success"
    assert not (runtime / "terraform.tfstate").exists()
    assert not (runtime / ".terraform").exists()
    assert not (runtime / "terraform-work").exists()
    assert not (runtime / "terraform-work-manifest.json").exists()
    assert not (runtime / "deployment.auto.tfvars.json").exists()
    assert not (runtime / "deployment.tfplan").exists()
    assert not (runtime / "client.privatekey").exists()
    assert not (runtime / "ssh.privatekey").exists()


def test_one_generated_ssh_identity_reaches_files_and_terraform_variables(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    generated = 0
    actual_generate = generate_ssh_keypair

    def generate_once() -> tuple[str, str]:
        nonlocal generated
        generated += 1
        return actual_generate()

    monkeypatch.setattr(orchestrator_module, "generate_ssh_keypair", generate_once)
    result = orchestrator.deploy("aws-lightsail", "us-east-1")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    variables = json.loads((runtime / "deployment.auto.tfvars.json").read_text(encoding="utf-8"))
    public_text = (runtime / "ssh.publickey").read_text(encoding="ascii").strip()
    assert generated == 1
    assert variables["ssh_public_key"] == public_text
    assert verify_ssh_keypair(runtime / "ssh.privatekey", runtime / "ssh.publickey") == ssh_public_key_fingerprint(
        variables["ssh_public_key"]
    )
    aws_configuration = (runtime / "terraform-work" / "vpn-aws-lightsail" / "main.tf").read_text(encoding="utf-8")
    assert "public_key = var.ssh_public_key" in aws_configuration


def test_registry_record_cannot_select_another_deployments_runtime(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    first = add_record(orchestrator, "aws-lightsail", "first")
    second = add_record(orchestrator, "aws-lightsail", "second")
    first.runtime_directory = second.runtime_directory
    first.state_path = second.state_path
    with pytest.raises(TerraformError, match="runtime identity differs"):
        orchestrator._assert_record_paths(first)


@pytest.mark.parametrize("phase", ["init", "validate", "plan"])
def test_pre_apply_cancellation_is_local_only(tmp_path: Path, phase: str) -> None:
    orchestrator = make_orchestrator(tmp_path, FakeTerraformRunner(fail_at=phase))
    result = orchestrator.deploy("digitalocean", "nyc3")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    assert record.state is DeploymentState.CANCELLED
    assert not record.resources_possible
    assert record.apply_started_at is None
    assert orchestrator.remove_local_deployment(record.id)["status"] == "success"


def test_apply_cancellation_requires_cloud_cleanup(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path, FakeTerraformRunner(fail_at="apply"))
    result = orchestrator.deploy("digitalocean", "nyc3")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    assert record.resources_possible
    assert record.apply_started_at
    with pytest.raises(TerraformError, match="blocked"):
        orchestrator.remove_local_deployment(record.id)


def test_readiness_cancellation_requires_cloud_cleanup(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)

    def cancelled(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise TerraformCancelled("cancelled during readiness")

    orchestrator._basic_health_checks = cancelled  # type: ignore[method-assign]
    result = orchestrator.deploy("digitalocean", "nyc3")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    assert record.state is DeploymentState.CANCELLED
    assert record.resources_possible
    assert record.apply_completed_at


def test_cloud_init_failure_after_apply_preserves_state_and_requires_destroy(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)

    def cloud_init_failed(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise TerraformError(
            "Cloud initialization failed during bootstrap. Cloud resources may exist and must be destroyed."
        )

    orchestrator._basic_health_checks = cloud_init_failed  # type: ignore[method-assign]
    result = orchestrator.deploy("aws-lightsail", "us-east-1")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    assert result["status"] == "error"
    assert record.state is DeploymentState.FAILED
    assert record.apply_completed_at
    assert record.resources_possible
    assert record.state_present
    assert record.cleanup_status == "required"
    assert Path(record.state_path).is_file()
    with pytest.raises(TerraformError, match="blocked"):
        orchestrator.remove_local_deployment(record.id)


def test_credential_validation_cancellation_is_local_only(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)

    def cancelled(cancel=None):  # type: ignore[no-untyped-def]
        raise TerraformCancelled("cancelled during credential validation")

    orchestrator.providers.get("digitalocean").validate_credentials = cancelled  # type: ignore[method-assign]
    result = orchestrator.deploy("digitalocean", "nyc3")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    assert record.state is DeploymentState.CANCELLED
    assert not record.resources_possible


def test_reserved_deployment_cannot_be_removed_before_operation_stops(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    record = orchestrator.reserve_deployment("digitalocean", "nyc3", DeploymentOptions())
    with pytest.raises(TerraformError, match="operation has stopped"):
        orchestrator.remove_local_deployment(record.id)
    assert orchestrator.deployments.get(record.id).state is DeploymentState.IDLE
    assert Path(record.runtime_directory).is_dir()


def test_destroy_uses_fresh_cancellation_token(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    result = orchestrator.deploy("digitalocean", "nyc3")
    deployment_id = str(result["deployment_id"])
    orchestrator.cancel(deployment_id)
    old = orchestrator._cancellations[deployment_id]
    assert old.is_set()
    assert orchestrator.destroy(deployment_id)["status"] == "success"
    assert orchestrator._cancellations[deployment_id] is not old
    assert not orchestrator._cancellations[deployment_id].is_set()


def test_legacy_active_state_migrates_only_to_exact_match(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    Path(record.runtime_directory, "deployment.auto.tfvars.json").write_text(
        json.dumps({"server_public_key": "server-public"}), encoding="utf-8"
    )
    source = Path(record.terraform_directory, "terraform.tfstate")
    source.write_text(json.dumps(legacy_state("aws-lightsail")), encoding="utf-8")
    before = source.read_bytes()
    report = next(item for item in orchestrator.list_legacy_states() if item["provider_id"] == "aws-lightsail")
    assert report["classification"] == "active"
    assert report["migration_available"]
    result = orchestrator.migrate_legacy_state("aws-lightsail")
    assert result["source_preserved"]
    assert source.read_bytes() == before
    assert Path(record.state_path).read_bytes() == before
    migrated = orchestrator.deployments.get(record.id)
    assert migrated.legacy_backup_path and Path(migrated.legacy_backup_path).is_file()


def test_legacy_empty_malformed_and_ambiguous_states(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    aws = orchestrator._provider_directory("aws-lightsail")
    aws.joinpath("terraform.tfstate").write_text('{"version":4,"resources":[]}', encoding="utf-8")
    assert orchestrator._inspect_legacy_provider("aws-lightsail")["classification"] == "empty"
    aws.joinpath("terraform.tfstate").write_text("not-json", encoding="utf-8")
    assert orchestrator._inspect_legacy_provider("aws-lightsail")["classification"] == "malformed"
    aws.joinpath("terraform.tfstate").write_text('{"version":4,"resources":[]}', encoding="utf-8")
    aws.joinpath("terraform.tfstate.backup").write_text(json.dumps(legacy_state("aws-lightsail")), encoding="utf-8")
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["classification"] == "ambiguous"
    assert report["blocking"]


def test_interrupted_apply_is_reconciled_on_restart(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.state = DeploymentState.PROVISIONING
    record.apply_started_at = "2026-01-01T00:01:00+00:00"
    orchestrator.deployments.save(record)
    orchestrator.reconcile_interrupted()
    recovered = orchestrator.deployments.get(record.id)
    assert recovered.state is DeploymentState.FAILED
    assert recovered.resources_possible
    assert recovered.cleanup_status == "required"


def test_empty_pre_apply_runtime_state_remains_locally_removable(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "digitalocean")
    record.state = DeploymentState.PLANNING
    Path(record.state_path).write_text('{"version":4,"resources":[]}', encoding="utf-8")
    orchestrator.deployments.save(record)
    orchestrator.reconcile_interrupted()
    recovered = orchestrator.deployments.get(record.id)
    assert recovered.state_present
    assert not recovered.resources_possible
    assert orchestrator.remove_local_deployment(record.id)["status"] == "success"
