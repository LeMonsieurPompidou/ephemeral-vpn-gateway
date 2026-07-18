from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from catalog import ProviderCatalog
from models import DeploymentOptions, DeploymentRecord, DeploymentState, StatusEvent, now_iso
from providers import ProviderRegistry
from runtime_registry import DeploymentRegistry
from security import generate_wireguard_keypair, redact, write_secret
from terraform_runner import TerraformCancelled, TerraformError, TerraformRunner, output_value
from validation import validate_options

EventCallback = Callable[[StatusEvent], None]
LogCallback = Callable[[str, str], None]


class Orchestrator:
    def __init__(self, resource_root: Path, runtime_root: Path, runner: TerraformRunner | None = None) -> None:
        self.resource_root = resource_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.catalog = ProviderCatalog(self.resource_root / "vpn-gui-app" / "provider_catalog.json")
        self.providers = ProviderRegistry(self.catalog)
        self.deployments = DeploymentRegistry(self.runtime_root / "deployments.json")
        self.runner = runner or TerraformRunner()
        self._cancellations: dict[str, threading.Event] = {}
        self._events: list[StatusEvent] = []
        self.on_event: EventCallback | None = None
        self.on_log: LogCallback | None = None

    def list_providers(self) -> list[dict[str, object]]:
        return [provider.info.to_dict() for provider in self.providers.list()]

    def list_locations(self, provider_id: str) -> list[dict[str, object]]:
        return [location.to_dict() for location in self.providers.get(provider_id).list_locations()]

    def validate_credentials(self, provider_id: str) -> dict[str, object]:
        valid, message = self.providers.get(provider_id).validate_credentials()
        return {"valid": valid, "message": message}

    def initialize(self, provider_id: str, deployment_id: str | None = None) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not provider.info.terraform_root:
            raise ValueError(f"Provider {provider_id} is not Terraform-provisioned")
        directory = self.resource_root / provider.info.terraform_root
        if not directory.is_dir() or directory.resolve().parent != self.resource_root:
            raise TerraformError("Provider Terraform directory is missing or outside the application resources")
        runtime = self.runtime_root / (deployment_id or f"init-{provider_id}")
        runtime.mkdir(parents=True, exist_ok=True)
        result = self.runner.run(
            ["init", "-input=false", "-backend=false"], directory, env={"TF_DATA_DIR": str(runtime / ".terraform")}
        )
        return {"status": "success", "output": result.stdout}

    def plan(self, provider_id: str, location_id: str, options: DeploymentOptions | None = None) -> dict[str, object]:
        return self._create_and_run(provider_id, location_id, options or DeploymentOptions(), apply=False)

    def deploy(self, provider_id: str, location_id: str, options: DeploymentOptions | None = None) -> dict[str, object]:
        return self._create_and_run(provider_id, location_id, options or DeploymentOptions(), apply=True)

    def _create_and_run(
        self, provider_id: str, location_id: str, options: DeploymentOptions, *, apply: bool
    ) -> dict[str, object]:
        validate_options(options)
        provider = self.providers.get(provider_id)
        location = self.catalog.get_location(provider_id, location_id)
        if not provider.info.terraform_root:
            raise ValueError("Residential node import is not implemented yet")
        terraform_directory = (self.resource_root / provider.info.terraform_root).resolve()
        if terraform_directory.parent != self.resource_root or not terraform_directory.is_dir():
            raise TerraformError("Unsafe or missing Terraform working directory")
        deployment_id = str(uuid.uuid4())
        runtime = self.runtime_root / deployment_id
        runtime.mkdir(parents=True, exist_ok=False)
        state_path = runtime / "terraform.tfstate"
        expires = None
        if options.expiration_minutes:
            expires = (datetime.now(timezone.utc) + timedelta(minutes=options.expiration_minutes)).isoformat()
        record = DeploymentRecord(
            deployment_id,
            provider_id,
            location_id,
            str(terraform_directory),
            str(state_path),
            str(runtime),
            now_iso(),
            expires_at=expires,
            auto_expire=options.automatic_expiration,
        )
        self.deployments.save(record)
        cancel = self._cancellations.setdefault(deployment_id, threading.Event())
        try:
            self._transition(record, DeploymentState.VALIDATING_CREDENTIALS, "Checking provider credentials")
            valid, message = provider.validate_credentials()
            legacy_tfvars = terraform_directory / "terraform.tfvars"
            if not valid and not legacy_tfvars.is_file():
                raise ValueError(message)
            if not valid:
                self._log(
                    deployment_id,
                    "Using legacy terraform.tfvars credentials; migrate to provider environment variables",
                )
            server_private, server_public = generate_wireguard_keypair()
            client_private, client_public = generate_wireguard_keypair()
            variables = provider.terraform_variables(location, options)
            variables.update(
                {
                    "server_private_key": server_private,
                    "server_public_key": server_public,
                    "client_public_key": client_public,
                }
            )
            var_file = runtime / "deployment.auto.tfvars.json"
            write_secret(var_file, json.dumps(variables))
            client_key_file = runtime / "client.privatekey"
            write_secret(client_key_file, client_private + "\n")
            env = {"TF_DATA_DIR": str(runtime / ".terraform"), "TF_IN_AUTOMATION": "1"}

            def progress(line: str) -> None:
                self._log(deployment_id, line)

            with self.runner.lock_for(terraform_directory):
                self._transition(record, DeploymentState.INITIALIZING, "Initializing Terraform providers")
                self.runner.run(
                    ["init", "-input=false", "-backend=false"],
                    terraform_directory,
                    env=env,
                    cancel=cancel,
                    progress=progress,
                )
                self.runner.run(
                    ["validate", "-no-color"], terraform_directory, env=env, cancel=cancel, progress=progress
                )
                self._transition(record, DeploymentState.PLANNING, "Creating an explicit Terraform plan")
                plan_path = runtime / "deployment.tfplan"
                self.runner.run(
                    [
                        "plan",
                        "-input=false",
                        "-no-color",
                        f"-state={state_path}",
                        f"-var-file={var_file}",
                        f"-out={plan_path}",
                    ],
                    terraform_directory,
                    env=env,
                    cancel=cancel,
                    progress=progress,
                )
                if not apply:
                    return {"status": "success", "deployment_id": deployment_id, "state": record.state.value}
                self._transition(record, DeploymentState.PROVISIONING, "Applying the reviewed plan")
                self.runner.run(
                    ["apply", "-input=false", "-no-color", str(plan_path)],
                    terraform_directory,
                    env=env,
                    cancel=cancel,
                    progress=progress,
                )
                outputs = self.runner.output_json(
                    terraform_directory, state_path, env=env, cancel=cancel, progress=progress
                )
            record.public_ip = str(output_value(outputs, "vpn_public_ip"))
            resource_ids = outputs.get("resource_ids", {})
            if isinstance(resource_ids, dict) and isinstance(resource_ids.get("value"), dict):
                record.resource_ids = {str(k): str(v) for k, v in resource_ids["value"].items()}
            self.deployments.save(record)
            self._transition(
                record, DeploymentState.WAITING_FOR_CLOUD_INIT, "Cloud-init applied; starting bounded health checks"
            )
            self._basic_health_checks(record, outputs, options, client_private)
            self._transition(record, DeploymentState.READY, "WireGuard gateway is ready")
            return {
                "status": "success",
                "deployment_id": deployment_id,
                "state": record.state.value,
                "ip": record.public_ip,
                "config": self.get_client_config(deployment_id),
                "expires_at": record.expires_at,
            }
        except TerraformCancelled as exc:
            self._transition(record, DeploymentState.CANCELLED, str(exc), error=str(exc))
            return {"status": "error", "deployment_id": deployment_id, "state": record.state.value, "message": str(exc)}
        except Exception as exc:
            message = redact(str(exc))
            self._transition(record, DeploymentState.FAILED, message, error=message)
            return {"status": "error", "deployment_id": deployment_id, "state": record.state.value, "message": message}

    def _basic_health_checks(
        self, record: DeploymentRecord, outputs: dict[str, object], options: DeploymentOptions, client_private: str
    ) -> None:
        if not record.public_ip:
            raise TerraformError("The provider did not allocate a public IP")
        server_public = str(output_value(outputs, "server_public_key"))
        config = self._render_client_config(record.public_ip, server_public, client_private, options)
        self._validate_client_config(config)
        write_secret(Path(record.runtime_directory) / "client.conf", config)
        ready = outputs.get("readiness_hint")
        if not isinstance(ready, dict) or not ready.get("value"):
            raise TerraformError("Terraform did not provide a cloud-init readiness hint")
        self._transition(
            record,
            DeploymentState.CHECKING_WIREGUARD,
            "Client configuration validated; checking the server over SSH",
        )
        self._ssh_health_checks(record, options)
        if options.verify_egress:
            self._transition(
                record,
                DeploymentState.VERIFYING_EGRESS,
                "Egress verification requires a connected client and remains pending",
            )

    def _ssh_health_checks(self, record: DeploymentRecord, options: DeploymentOptions) -> None:
        """Bounded checks for cloud-init, WireGuard, forwarding, NAT, and UDP."""
        assert record.public_ip
        deadline = time.monotonic() + 300
        last_error = "SSH did not become ready"
        user = "ubuntu" if record.provider_id == "aws-lightsail" else "root"
        remote_check = (
            "cloud-init status --wait >/dev/null && "
            "test -f /var/lib/cloud/instance/wireguard-ready && "
            "systemctl is-active --quiet wg-quick@wg0 && "
            "ip link show wg0 >/dev/null && "
            'test "$(sysctl -n net.ipv4.ip_forward)" = 1 && '
            "iptables -t nat -C POSTROUTING -j MASQUERADE && "
            f"ss -H -lun 'sport = :{options.wireguard_port}' | grep -q ."
        )
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((record.public_ip, 22), timeout=5):
                    pass
                result = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=10",
                        "-o",
                        "StrictHostKeyChecking=accept-new",
                        f"{user}@{record.public_ip}",
                        remote_check,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                if result.returncode == 0:
                    return
                last_error = redact((result.stderr or result.stdout).strip())[-500:]
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = redact(str(exc))
            time.sleep(5)
        raise TerraformError(f"Server readiness checks timed out: {last_error}")

    @staticmethod
    def _render_client_config(
        public_ip: str, server_public: str, client_private: str, options: DeploymentOptions
    ) -> str:
        return "\n".join(
            [
                "[Interface]",
                f"PrivateKey = {client_private}",
                "Address = 10.8.0.2/32",
                f"DNS = {', '.join(options.dns_servers)}",
                f"MTU = {options.client_mtu}",
                "",
                "[Peer]",
                f"PublicKey = {server_public}",
                f"Endpoint = {public_ip}:{options.wireguard_port}",
                f"AllowedIPs = {', '.join(options.allowed_ips)}",
                f"PersistentKeepalive = {options.persistent_keepalive}",
                "",
            ]
        )

    @staticmethod
    def _validate_client_config(config: str) -> None:
        required = ("[Interface]", "PrivateKey = ", "[Peer]", "PublicKey = ", "Endpoint = ", "AllowedIPs = ")
        if not all(value in config for value in required):
            raise ValueError("Generated WireGuard client configuration is malformed")

    def destroy(self, deployment_id: str, preserve_config: bool = False) -> dict[str, object]:
        record = self.deployments.get(deployment_id)
        directory = Path(record.terraform_directory).resolve()
        runtime = Path(record.runtime_directory).resolve()
        if directory.parent != self.resource_root or not directory.is_dir() or runtime.parent != self.runtime_root:
            raise TerraformError("Refusing destroy: deployment paths are missing or ambiguous")
        state_path = Path(record.state_path)
        if not state_path.is_file():
            raise TerraformError("Refusing destroy: recorded Terraform state is missing")
        self._transition(record, DeploymentState.DESTROYING, "Destroying deployment resources")
        cancel = self._cancellations.setdefault(deployment_id, threading.Event())
        env = {"TF_DATA_DIR": str(runtime / ".terraform"), "TF_IN_AUTOMATION": "1"}
        var_file = runtime / "deployment.auto.tfvars.json"
        try:
            with self.runner.lock_for(directory):
                self.runner.run(
                    [
                        "destroy",
                        "-auto-approve",
                        "-input=false",
                        "-no-color",
                        f"-state={state_path}",
                        f"-var-file={var_file}",
                    ],
                    directory,
                    env=env,
                    cancel=cancel,
                    progress=lambda line: self._log(deployment_id, line),
                )
            if not preserve_config:
                for name in ("client.conf", "client.privatekey"):
                    (runtime / name).unlink(missing_ok=True)
            self._transition(record, DeploymentState.DESTROYED, "Deployment destroyed")
            return {"status": "success", "deployment_id": deployment_id}
        except Exception as exc:
            message = redact(str(exc))
            self._transition(record, DeploymentState.FAILED, message, error=message)
            return {"status": "error", "deployment_id": deployment_id, "message": message}

    def cancel(self, deployment_id: str) -> dict[str, str]:
        self.deployments.get(deployment_id)
        self._cancellations.setdefault(deployment_id, threading.Event()).set()
        return {"status": "success", "deployment_id": deployment_id}

    def get_status(self, deployment_id: str) -> dict[str, object]:
        return self.deployments.get(deployment_id).to_dict()

    def get_client_config(self, deployment_id: str) -> str:
        record = self.deployments.get(deployment_id)
        path = Path(record.runtime_directory) / "client.conf"
        if record.state != DeploymentState.READY and not path.exists():
            raise RuntimeError("Client configuration is not ready")
        return path.read_text(encoding="utf-8")

    def recovery_candidates(self) -> list[dict[str, object]]:
        return [
            record.to_dict()
            for record in self.deployments.list()
            if record.state not in {DeploymentState.DESTROYED, DeploymentState.IDLE}
        ]

    def _transition(
        self, record: DeploymentRecord, state: DeploymentState, message: str, error: str | None = None
    ) -> None:
        record.state = state
        record.last_error = error
        self.deployments.save(record)
        event = StatusEvent(record.id, state, message)
        self._events.append(event)
        if self.on_event:
            self.on_event(event)

    def _log(self, deployment_id: str, line: str) -> None:
        if self.on_log:
            self.on_log(deployment_id, redact(line))
