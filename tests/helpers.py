from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from models import DeploymentState
from orchestrator import Orchestrator
from terraform_runner import CommandResult, TerraformCancelled, TerraformRunner

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_DIRS = ("vpn-aws-lightsail", "vpn-digitalocean", "vpn-scaleway")


def make_resource_root(tmp_path: Path) -> Path:
    root = tmp_path / "resources"
    (root / "vpn-gui-app").mkdir(parents=True)
    shutil.copy2(ROOT / "vpn-gui-app" / "provider_catalog.json", root / "vpn-gui-app" / "provider_catalog.json")
    shutil.copytree(ROOT / "terraform-common", root / "terraform-common")
    for name in PROVIDER_DIRS:
        destination = root / name
        destination.mkdir()
        for source in (ROOT / name).glob("*.tf"):
            shutil.copy2(source, destination / source.name)
        shutil.copy2(ROOT / name / ".terraform.lock.hcl", destination / ".terraform.lock.hcl")
    return root


class FakeTerraformRunner(TerraformRunner):
    def __init__(self, fail_at: str | None = None) -> None:
        super().__init__(executable="terraform")
        self.fail_at = fail_at
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.state_by_data_dir: dict[str, Path] = {}

    def run(self, args: list[str], cwd: Path, **kwargs: object) -> CommandResult:
        env_value = kwargs.get("env")
        assert isinstance(env_value, dict)
        env = {str(key): str(value) for key, value in env_value.items()}
        self.calls.append((tuple(args), cwd, env))
        command = args[0]
        if command == "init":
            backend = next(value for value in args if value.startswith("-backend-config=path="))
            state_path = Path(backend.split("=", 2)[2])
            self.state_by_data_dir[env["TF_DATA_DIR"]] = state_path
            metadata = Path(env["TF_DATA_DIR"]) / "terraform.tfstate"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "backend": {"type": "local", "config": {"path": str(state_path.resolve())}},
                    }
                ),
                encoding="utf-8",
            )
        if command == "apply":
            state = self.state_by_data_dir[env["TF_DATA_DIR"]]
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text('{"version":4,"resources":[]}', encoding="utf-8")
        if self.fail_at == command:
            raise TerraformCancelled(f"cancelled during {command}")
        return CommandResult(("terraform", *args), 0, "", "")

    def output_json(self, cwd: Path, **kwargs: object) -> dict[str, object]:
        env_value = kwargs.get("env")
        assert isinstance(env_value, dict)
        env = {str(key): str(value) for key, value in env_value.items()}
        self.calls.append((("output", "-json"), cwd, env))
        return {
            "vpn_public_ip": {"value": "203.0.113.10"},
            "server_public_key": {"value": "server-public"},
            "readiness_hint": {"value": "/var/lib/cloud/instance/wireguard-ready"},
            "resource_ids": {"value": {"server": "resource-1"}},
        }


def make_orchestrator(
    tmp_path: Path,
    runner: FakeTerraformRunner | None = None,
    *,
    ip_detector: Callable[[], str] = lambda: "198.51.100.10/32",
) -> Orchestrator:
    root = make_resource_root(tmp_path)
    orchestrator = Orchestrator(root, tmp_path / "runtime", runner or FakeTerraformRunner(), ip_detector=ip_detector)
    for provider in orchestrator.providers.list():
        provider.validate_credentials = lambda cancel=None: (True, "ok")  # type: ignore[method-assign]

    def healthy(record, outputs, options, client_private, cancel, *, automatic_ssh_cidr):  # type: ignore[no-untyped-def]
        Path(record.runtime_directory, "client.conf").write_text("test-config", encoding="utf-8")

    orchestrator._basic_health_checks = healthy  # type: ignore[method-assign]
    return orchestrator


def add_record(orchestrator: Orchestrator, provider_id: str, deployment_id: str = "deployment"):
    from models import DeploymentRecord

    runtime = orchestrator.runtime_root / deployment_id
    runtime.mkdir(parents=True, exist_ok=True)
    record = DeploymentRecord(
        deployment_id,
        provider_id,
        "us-east-1" if provider_id == "aws-lightsail" else "nyc3",
        str(orchestrator._provider_directory(provider_id)),
        str(runtime / "terraform.tfstate"),
        str(runtime),
        "2026-01-01T00:00:00+00:00",
        state=DeploymentState.FAILED,
    )
    orchestrator.deployments.save(record)
    return record


def legacy_state(provider_id: str, server_public_key: str = "server-public") -> dict[str, object]:
    resource_type = {
        "aws-lightsail": "aws_lightsail_instance",
        "digitalocean": "digitalocean_droplet",
        "scaleway": "scaleway_instance_server",
    }[provider_id]
    return {
        "version": 4,
        "lineage": "lineage",
        "serial": 5,
        "outputs": {
            "server_public_key": {"value": server_public_key},
            "resource_ids": {"value": {"server": "one"}},
        },
        "resources": [{"type": resource_type, "name": "vpn", "instances": [{"attributes": {}}]}],
    }
