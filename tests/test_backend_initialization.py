from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import FakeTerraformRunner, make_orchestrator
from models import DeploymentState
from terraform_runner import TerraformError

PROVIDERS = [
    ("aws-lightsail", "us-east-1"),
    ("digitalocean", "nyc3"),
    ("scaleway", "fr-par-1"),
]


class PromptOnWorkingDirectoryStateRunner(FakeTerraformRunner):
    """Model Terraform's legacy-local-state prompt without invoking Terraform."""

    def run(self, args: list[str], cwd: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        if args[0] == "init" and (cwd / "terraform.tfstate").exists():
            raise TerraformError("Can't ask approval for state migration when interactive input is disabled")
        return super().run(args, cwd, **kwargs)


class FailFirstInitRunner(FakeTerraformRunner):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def run(self, args: list[str], cwd: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        if args[0] == "init" and not self.failed:
            self.failed = True
            env_value = kwargs.get("env")
            assert isinstance(env_value, dict)
            self.calls.append((tuple(args), cwd, {str(key): str(value) for key, value in env_value.items()}))
            raise TerraformError("Can't ask approval for state migration when interactive input is disabled")
        return super().run(args, cwd, **kwargs)


def backend_metadata(record) -> Path:  # type: ignore[no-untyped-def]
    return Path(record.runtime_directory) / ".terraform" / "terraform.tfstate"


@pytest.mark.parametrize(("provider_id", "location_id"), PROVIDERS)
def test_fresh_backend_uses_isolated_working_copy_and_reconfigure(
    tmp_path: Path, provider_id: str, location_id: str
) -> None:
    runner = PromptOnWorkingDirectoryStateRunner()
    orchestrator = make_orchestrator(tmp_path, runner)
    source = orchestrator._provider_directory(provider_id) / "terraform.tfstate"
    source.write_text(
        json.dumps({"version": 4, "lineage": "legacy", "serial": 1, "resources": []}),
        encoding="utf-8",
    )
    source_before = source.read_bytes()

    result = orchestrator.deploy(provider_id, location_id)

    assert result["status"] == "success"
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    init = next(call for call in runner.calls if call[0][0] == "init")
    assert "-input=false" in init[0]
    assert "-reconfigure" in init[0]
    assert f"-backend-config=path={Path(record.state_path).resolve()}" in init[0]
    assert init[1] == runtime / "terraform-work" / Path(record.terraform_directory).name
    assert not (init[1] / "terraform.tfstate").exists()
    assert init[2]["TF_DATA_DIR"] == str(runtime / ".terraform")
    assert source.read_bytes() == source_before
    metadata = json.loads(backend_metadata(record).read_text(encoding="utf-8"))
    assert Path(metadata["backend"]["config"]["path"]).resolve() == Path(record.state_path).resolve()


@pytest.mark.parametrize(("provider_id", "location_id"), PROVIDERS)
def test_recovery_backend_preserves_existing_state_without_reconfigure(
    tmp_path: Path, provider_id: str, location_id: str
) -> None:
    runner = FakeTerraformRunner()
    orchestrator = make_orchestrator(tmp_path, runner)
    result = orchestrator.deploy(provider_id, location_id)
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    resource_type = {
        "aws-lightsail": "aws_lightsail_instance",
        "digitalocean": "digitalocean_droplet",
        "scaleway": "scaleway_instance_server",
    }[provider_id]
    state = Path(record.state_path)
    state.write_text(
        json.dumps(
            {
                "version": 4,
                "lineage": "deployment",
                "serial": 2,
                "resources": [{"type": resource_type, "name": "vpn", "instances": [{"attributes": {}}]}],
            }
        ),
        encoding="utf-8",
    )
    state_before = state.read_bytes()
    call_count = len(runner.calls)

    destroyed = orchestrator.destroy(record.id)

    assert destroyed["status"] == "success"
    recovery_calls = runner.calls[call_count:]
    recovery_init = next(call for call in recovery_calls if call[0][0] == "init")
    assert "-input=false" in recovery_init[0]
    assert "-reconfigure" not in recovery_init[0]
    assert f"-backend-config=path={state.resolve()}" in recovery_init[0]
    destroy_call = next(call for call in recovery_calls if call[0][0] == "destroy")
    assert recovery_init[1] == destroy_call[1]
    assert state_before  # State was present until confirmed destroy cleanup.


@pytest.mark.parametrize("metadata_change", ["different-path", "malformed", "missing", "wrong-backend"])
def test_recovery_backend_metadata_disagreement_fails_without_destroy(tmp_path: Path, metadata_change: str) -> None:
    runner = FakeTerraformRunner()
    orchestrator = make_orchestrator(tmp_path, runner)
    result = orchestrator.deploy("aws-lightsail", "us-east-1")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    state = Path(record.state_path)
    state_before = state.read_bytes()
    metadata_path = backend_metadata(record)
    if metadata_change == "missing":
        metadata_path.unlink()
    elif metadata_change == "malformed":
        metadata_path.write_text("not-json", encoding="utf-8")
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_change == "different-path":
            metadata["backend"]["config"]["path"] = str(Path(record.runtime_directory) / "other.tfstate")
        else:
            metadata["backend"]["type"] = "remote"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    calls_before = len(runner.calls)

    outcome = orchestrator.destroy(record.id)

    assert outcome["status"] == "error"
    assert state.read_bytes() == state_before
    assert not any(call[0][0] == "destroy" for call in runner.calls[calls_before:])
    recovered = orchestrator.deployments.get(record.id)
    assert recovered.resources_possible
    assert recovered.cleanup_status == "failed"


def test_recovery_missing_runtime_state_after_apply_fails_before_init(tmp_path: Path) -> None:
    runner = FakeTerraformRunner()
    orchestrator = make_orchestrator(tmp_path, runner)
    result = orchestrator.deploy("aws-lightsail", "us-east-1")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    Path(record.state_path).unlink()
    metadata_before = backend_metadata(record).read_bytes()
    calls_before = len(runner.calls)

    with pytest.raises(TerraformError, match="state is missing"):
        orchestrator.destroy(record.id)

    assert backend_metadata(record).read_bytes() == metadata_before
    assert runner.calls[calls_before:] == []


@pytest.mark.parametrize(("provider_id", "location_id"), PROVIDERS)
def test_pre_apply_init_failure_is_local_only_and_next_uuid_is_clean(
    tmp_path: Path, provider_id: str, location_id: str
) -> None:
    runner = FailFirstInitRunner()
    orchestrator = make_orchestrator(tmp_path, runner)

    failed = orchestrator.deploy(provider_id, location_id)
    failed_record = orchestrator.deployments.get(str(failed["deployment_id"]))
    failed_runtime = Path(failed_record.runtime_directory)
    assert failed_record.state is DeploymentState.FAILED
    assert failed_record.apply_started_at is None
    assert failed_record.apply_completed_at is None
    assert not failed_record.resources_possible
    assert not failed_record.state_present
    assert not Path(failed_record.state_path).exists()
    with pytest.raises(TerraformError, match="No cloud resources"):
        orchestrator.destroy(failed_record.id)

    assert orchestrator.remove_local_deployment(failed_record.id)["status"] == "success"
    assert not failed_runtime.exists()
    succeeding = orchestrator.deploy(provider_id, location_id)
    assert succeeding["status"] == "success"
    assert succeeding["deployment_id"] != failed["deployment_id"]
