from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator

from catalog import ProviderCatalog
from cloud_init import render_user_data
from file_lock import FileLock
from legacy_reconciliation import LegacyReconciliationStore
from models import DeploymentOptions, DeploymentRecord, DeploymentState, StatusEvent, now_iso
from networking import PublicIpDetectionError, detect_public_ipv4, normalize_public_ipv4_cidr
from output_contract import ProviderOutputs, validate_provider_outputs
from providers import ProviderRegistry
from runtime_registry import DeploymentRegistry
from security import (
    PrivateFileSecurityError,
    append_redacted_log,
    generate_ssh_keypair,
    generate_wireguard_keypair,
    redact,
    verify_private_file,
    write_secret,
    write_secret_bytes,
)
from terraform_runner import CommandResult, TerraformCancelled, TerraformError, TerraformRunner
from validation import validate_options

EventCallback = Callable[[StatusEvent], None]
LogCallback = Callable[[str, str], None]
IpDetector = Callable[[], str]
LEGACY_STATE_NAMES = ("terraform.tfstate", "terraform.tfstate.backup")
TERRAFORM_WORK_ROOT = "terraform-work"
TERRAFORM_WORK_MANIFEST = "terraform-work-manifest.json"
PROVIDER_RESOURCE_PREFIXES = {
    "aws-lightsail": "aws_lightsail_",
    "digitalocean": "digitalocean_",
    "scaleway": "scaleway_",
}


class Orchestrator:
    def __init__(
        self,
        resource_root: Path,
        runtime_root: Path,
        runner: TerraformRunner | None = None,
        *,
        ip_detector: IpDetector = detect_public_ipv4,
        readiness_timeout: float = 300,
    ) -> None:
        self.resource_root = resource_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.catalog = ProviderCatalog(self.resource_root / "vpn-gui-app" / "provider_catalog.json")
        self.providers = ProviderRegistry(self.catalog)
        self.deployments = DeploymentRegistry(self.runtime_root / "deployments.json")
        self.legacy_reconciliations = LegacyReconciliationStore(self.runtime_root / "legacy-reconciliations")
        self.runner = runner or TerraformRunner()
        self.ip_detector = ip_detector
        self.readiness_timeout = readiness_timeout
        self._cancellations: dict[str, threading.Event] = {}
        self._events: list[StatusEvent] = []
        self._legacy_snapshot_lock = threading.RLock()
        self._deployment_creation_lock = threading.RLock()
        self._legacy_confirmation_snapshots: dict[str, dict[str, object]] = {}
        self.on_event: EventCallback | None = None
        self.on_log: LogCallback | None = None

    def new_deployment_id(self) -> str:
        return str(uuid.uuid4())

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
        directory = self._provider_directory(provider_id)
        identifier = deployment_id or f"init-{provider_id}-{self.new_deployment_id()}"
        runtime = (self.runtime_root / identifier).resolve()
        runtime.mkdir(parents=True, exist_ok=False)
        state_path = runtime / "terraform.tfstate"
        record = DeploymentRecord(
            identifier,
            provider_id,
            "initialization-only",
            str(directory),
            str(state_path),
            str(runtime),
            now_iso(),
        )
        self._assert_record_paths(record)
        self._stage_terraform_configuration(record)
        env = self._terraform_env(record)
        with self._provider_operation_lock(provider_id, directory):
            result = self._initialize_terraform_backend(record, env=env, cancel=threading.Event(), fresh=True)
        return {"status": "success", "output": result.stdout}

    def plan(
        self,
        provider_id: str,
        location_id: str,
        options: DeploymentOptions | None = None,
        deployment_id: str | None = None,
    ) -> dict[str, object]:
        return self._create_and_run(
            provider_id, location_id, options or DeploymentOptions(), apply=False, deployment_id=deployment_id
        )

    def reserve_deployment(
        self,
        provider_id: str,
        location_id: str,
        options: DeploymentOptions,
        deployment_id: str | None = None,
    ) -> DeploymentRecord:
        """Atomically reserve and persist a deployment before exposing its ID."""
        validate_options(options)
        provider = self.providers.get(provider_id)
        self.catalog.get_location(provider_id, location_id)
        if not provider.info.terraform_root:
            raise ValueError("Residential node import is not implemented yet")
        terraform_directory = self._provider_directory(provider_id)
        with self._deployment_creation_transaction():
            blocking = [record for record in self.deployments.list() if record.state != DeploymentState.DESTROYED]
            if blocking:
                active = blocking[0]
                raise TerraformError(f"Another deployment operation is already active: {active.id}")
            self._assert_provider_ready_for_new_deployment(provider_id)
            identifier = deployment_id or self.new_deployment_id()
            runtime = (self.runtime_root / identifier).resolve()
            runtime.mkdir(parents=True, exist_ok=False)
            state_path = runtime / "terraform.tfstate"
            expires = None
            if options.expiration_minutes:
                expires = (datetime.now(timezone.utc) + timedelta(minutes=options.expiration_minutes)).isoformat()
            record = DeploymentRecord(
                identifier,
                provider_id,
                location_id,
                str(terraform_directory),
                str(state_path),
                str(runtime),
                now_iso(),
                expires_at=expires,
                auto_expire=options.automatic_expiration,
            )
            self._assert_record_paths(record)
            try:
                self.deployments.save(record)
            except Exception:
                runtime.rmdir()
                raise
            return record

    def deploy_reserved(self, deployment_id: str, options: DeploymentOptions) -> dict[str, object]:
        record = self.deployments.get(deployment_id)
        return self._create_and_run(
            record.provider_id,
            record.location_id,
            options,
            apply=True,
            deployment_id=deployment_id,
            reserved=True,
        )

    def deploy(
        self,
        provider_id: str,
        location_id: str,
        options: DeploymentOptions | None = None,
        deployment_id: str | None = None,
    ) -> dict[str, object]:
        return self._create_and_run(
            provider_id, location_id, options or DeploymentOptions(), apply=True, deployment_id=deployment_id
        )

    def _create_and_run(
        self,
        provider_id: str,
        location_id: str,
        options: DeploymentOptions,
        *,
        apply: bool,
        deployment_id: str | None,
        reserved: bool = False,
    ) -> dict[str, object]:
        validate_options(options)
        provider = self.providers.get(provider_id)
        location = self.catalog.get_location(provider_id, location_id)
        if not provider.info.terraform_root:
            raise ValueError("Residential node import is not implemented yet")
        record = (
            self.deployments.get(str(deployment_id))
            if reserved
            else self.reserve_deployment(provider_id, location_id, options, deployment_id)
        )
        if (
            record.provider_id != provider_id
            or record.location_id != location_id
            or record.state != DeploymentState.IDLE
        ):
            raise TerraformError("Reserved deployment identity or lifecycle state is invalid")
        self._assert_record_paths(record)
        identifier = record.id
        runtime = Path(record.runtime_directory)
        state_path = Path(record.state_path)
        terraform_directory = Path(record.terraform_directory)
        expires = record.expires_at
        cancel = threading.Event()
        self._cancellations[identifier] = cancel
        try:
            self._transition(record, DeploymentState.VALIDATING_CREDENTIALS, "Checking provider credentials")
            self._raise_if_cancelled(cancel)
            valid, message = provider.validate_credentials(cancel)
            self._raise_if_cancelled(cancel)
            legacy_tfvars = terraform_directory / "terraform.tfvars"
            if not valid and (provider_id == "aws-lightsail" or not legacy_tfvars.is_file()):
                raise ValueError(message)
            if not valid:
                self._log(
                    identifier, "Using legacy terraform.tfvars credentials; migrate to provider environment variables"
                )

            server_private, server_public = generate_wireguard_keypair()
            client_private, client_public = generate_wireguard_keypair()
            ssh_private, ssh_public = generate_ssh_keypair()
            write_secret(runtime / "client.privatekey", client_private + "\n")
            write_secret(runtime / "ssh.privatekey", ssh_private)
            write_secret(runtime / "ssh.publickey", ssh_public + "\n")
            self._raise_if_cancelled(cancel)

            self._stage_terraform_configuration(record)
            env = self._terraform_env(record)
            with self._provider_operation_lock(provider_id, terraform_directory):
                self._transition(
                    record, DeploymentState.INITIALIZING, "Initializing deployment-scoped Terraform backend"
                )
                self._initialize_terraform_backend(record, env=env, cancel=cancel, fresh=True)
                self._run_terraform(record, ["validate", "-no-color"], env=env, cancel=cancel)
                self._raise_if_cancelled(cancel)

                manual_ssh_cidr = bool(options.ssh_cidr)
                ssh_cidr = normalize_public_ipv4_cidr(options.ssh_cidr) if options.ssh_cidr else self.ip_detector()
                effective_options = replace(options, ssh_cidr=ssh_cidr)
                staged_common = Path(record.runtime_directory) / TERRAFORM_WORK_ROOT / "terraform-common"
                user_data_payload = render_user_data(
                    provider_id,
                    staged_common,
                    wireguard_port=effective_options.wireguard_port,
                    server_private_key=server_private,
                    client_public_key=client_public,
                )
                variables = provider.terraform_variables(location, effective_options)
                variables.update(
                    {
                        "deployment_id": identifier,
                        "expires_at": expires,
                        "server_private_key": server_private,
                        "server_public_key": server_public,
                        "client_public_key": client_public,
                        "ssh_public_key": ssh_public,
                        "user_data_payload": user_data_payload,
                    }
                )
                var_file = runtime / "deployment.auto.tfvars.json"
                write_secret(var_file, json.dumps(variables))

                record.plan_started_at = now_iso()
                self.deployments.save(record)
                self._transition(record, DeploymentState.PLANNING, "Creating an explicit Terraform plan")
                plan_path = runtime / "deployment.tfplan"
                self._run_terraform(
                    record,
                    ["plan", "-input=false", "-no-color", f"-var-file={var_file}", f"-out={plan_path}"],
                    env=env,
                    cancel=cancel,
                )
                record.plan_completed_at = now_iso()
                self.deployments.save(record)
                if not apply:
                    return {"status": "success", "deployment_id": identifier, "state": record.state.value}

                record.apply_started_at = now_iso()
                record.resources_possible = True
                record.cleanup_status = "required"
                self.deployments.save(record)
                self._transition(record, DeploymentState.PROVISIONING, "Applying the reviewed plan")
                self._run_terraform(
                    record,
                    ["apply", "-input=false", "-no-color", str(plan_path)],
                    env=env,
                    cancel=cancel,
                )
                record.apply_completed_at = now_iso()
                record.state_present = state_path.is_file()
                self.deployments.save(record)
                if not record.state_present:
                    raise TerraformError(f"Terraform apply completed without deployment state at {state_path}")
                outputs = self._terraform_outputs(record, env=env, cancel=cancel)

            contract = validate_provider_outputs(outputs, provider_id=provider_id, deployment_id=identifier)
            record.public_ip = contract.vpn_public_ip
            record.resource_ids = {str(key): str(value) for key, value in contract.resource_ids.items()}
            self.deployments.save(record)
            self._transition(
                record, DeploymentState.WAITING_FOR_CLOUD_INIT, "Cloud resources exist; starting health checks"
            )
            self._basic_health_checks(
                record,
                contract,
                effective_options,
                client_private,
                cancel,
                automatic_ssh_cidr=not manual_ssh_cidr,
            )
            self._transition(record, DeploymentState.READY, "WireGuard gateway is ready")
            return {
                "status": "success",
                "deployment_id": identifier,
                "state": record.state.value,
                "ip": record.public_ip,
                "config": self.get_client_config(identifier),
                "expires_at": record.expires_at,
            }
        except TerraformCancelled as exc:
            record.state_present = Path(record.state_path).is_file()
            record.cleanup_status = "required" if record.resources_possible else "not_required"
            self._transition(record, DeploymentState.CANCELLED, str(exc), error=str(exc))
            return {"status": "error", "deployment_id": identifier, "state": record.state.value, "message": str(exc)}
        except Exception as exc:
            record.state_present = Path(record.state_path).is_file()
            record.cleanup_status = "required" if record.resources_possible else "not_required"
            message = redact(str(exc))
            self._transition(record, DeploymentState.FAILED, message, error=message)
            return {"status": "error", "deployment_id": identifier, "state": record.state.value, "message": message}

    def _basic_health_checks(
        self,
        record: DeploymentRecord,
        outputs: ProviderOutputs,
        options: DeploymentOptions,
        client_private: str,
        cancel: threading.Event,
        *,
        automatic_ssh_cidr: bool,
    ) -> None:
        self._raise_if_cancelled(cancel)
        config = self._render_client_config(record.public_ip or "", outputs.server_public_key, client_private, options)
        self._validate_client_config(config)
        write_secret(Path(record.runtime_directory) / "client.conf", config)
        self._ssh_health_checks(
            record,
            options,
            cancel,
            automatic_ssh_cidr=automatic_ssh_cidr,
            readiness_marker=outputs.readiness_hint,
        )
        if options.verify_egress:
            self._raise_if_cancelled(cancel)
            self._transition(
                record,
                DeploymentState.VERIFYING_EGRESS,
                "Egress verification requires a connected client and remains pending",
            )

    def _ssh_health_checks(
        self,
        record: DeploymentRecord,
        options: DeploymentOptions,
        cancel: threading.Event,
        *,
        automatic_ssh_cidr: bool,
        readiness_marker: str = "/var/lib/ephemeral-vpn/ready",
    ) -> None:
        assert record.public_ip
        self._raise_if_cancelled(cancel)
        deadline = time.monotonic() + self.readiness_timeout
        last_error = "SSH did not become ready"
        user = "ubuntu" if record.provider_id == "aws-lightsail" else "root"
        runtime = Path(record.runtime_directory)
        identity = runtime / "ssh.privatekey"
        known_hosts = runtime / "known_hosts"
        try:
            verify_private_file(identity)
        except PrivateFileSecurityError as exc:
            raise TerraformError(f"Local SSH private-key security check failed: {exc}") from exc
        wireguard_check = (
            f"test -f {readiness_marker} && "
            "systemctl is-enabled --quiet wg-quick@wg0 && "
            "systemctl is-active --quiet wg-quick@wg0 && "
            "ip link show wg0 >/dev/null && "
            'test "$(sysctl -n net.ipv4.ip_forward)" = 1 && '
            "iptables -t nat -C POSTROUTING -j MASQUERADE && "
            f"ss -H -lun 'sport = :{options.wireguard_port}' | grep -q ."
        )
        attempts = 0
        ip_refresh_attempted = False
        cloud_init_complete = False
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel)
            attempts += 1
            try:
                with socket.create_connection((record.public_ip, 22), timeout=3):
                    pass
                if not cloud_init_complete:
                    args = self._ssh_command(identity, known_hosts, user, record.public_ip, "cloud-init status --long")
                    returncode, stdout, stderr = self._run_cancellable_process(args, cancel, timeout=20)
                    combined = (stdout + "\n" + stderr).strip()
                    status = self._cloud_init_status(returncode, combined)
                    if status == "error":
                        diagnostic = self._cloud_init_failure_diagnostic(combined)
                        detail = f" Failed phase: {diagnostic}." if diagnostic else ""
                        raise TerraformError(
                            "Cloud initialization failed during bootstrap. "
                            f"Cloud resources may exist and must be destroyed.{detail}"
                        )
                    if status != "done":
                        last_error = "Cloud initialization is still running"
                    else:
                        cloud_init_complete = True
                        self._transition(
                            record,
                            DeploymentState.CHECKING_WIREGUARD,
                            "Cloud initialization succeeded; checking WireGuard readiness",
                        )
                if cloud_init_complete:
                    args = self._ssh_command(identity, known_hosts, user, record.public_ip, wireguard_check)
                    returncode, stdout, stderr = self._run_cancellable_process(args, cancel, timeout=20)
                    if returncode == 0:
                        return
                    last_error = redact((stderr or stdout).strip())[-500:] or "WireGuard is not ready"
            except TerraformCancelled:
                raise
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = redact(str(exc))

            if automatic_ssh_cidr and not ip_refresh_attempted and attempts >= 3:
                ip_refresh_attempted = True
                try:
                    detected = self.ip_detector()
                    if detected != options.ssh_cidr:
                        self._refresh_ssh_firewall(record, detected, cancel)
                        options = replace(options, ssh_cidr=detected)
                except (PublicIpDetectionError, TerraformError) as exc:
                    last_error = redact(str(exc))
            if cancel.wait(5):
                raise TerraformCancelled("Deployment cancelled during server readiness checks")
        raise TerraformError(f"Server readiness checks timed out: {last_error}")

    @staticmethod
    def _ssh_command(identity: Path, known_hosts: Path, user: str, public_ip: str, remote_command: str) -> list[str]:
        return [
            "ssh",
            "-i",
            str(identity),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            f"{user}@{public_ip}",
            remote_command,
        ]

    @staticmethod
    def _cloud_init_status(returncode: int, output: str) -> str:
        match = re.search(r"(?im)^status:\s*([a-z_-]+)", output)
        status = match.group(1).lower() if match else ""
        if status == "error" or (returncode not in {0, 255} and "error" in output.lower()):
            return "error"
        if status == "done" and returncode == 0:
            return "done"
        if status in {"running", "not-run", "not_run"} or returncode == 255:
            return "running"
        return "error" if returncode else "running"

    @staticmethod
    def _cloud_init_failure_diagnostic(output: str) -> str | None:
        match = re.search(
            r"ephemeral-vpn bootstrap failed in phase ([A-Za-z0-9 /_-]{1,80})", output, flags=re.IGNORECASE
        )
        return match.group(1).strip() if match else None

    def _refresh_ssh_firewall(self, record: DeploymentRecord, cidr: str, cancel: threading.Event) -> None:
        runtime = Path(record.runtime_directory)
        var_file = runtime / "deployment.auto.tfvars.json"
        variables = json.loads(var_file.read_text(encoding="utf-8"))
        variables["ssh_allowed_cidr"] = cidr
        write_secret(var_file, json.dumps(variables))
        plan_path = runtime / "ssh-cidr-refresh.tfplan"
        directory = Path(record.terraform_directory)
        env = self._terraform_env(record)
        with self._provider_operation_lock(record.provider_id, directory):
            self._run_terraform(
                record,
                ["plan", "-input=false", "-no-color", f"-var-file={var_file}", f"-out={plan_path}"],
                env=env,
                cancel=cancel,
            )
            self._run_terraform(
                record,
                ["apply", "-input=false", "-no-color", str(plan_path)],
                env=env,
                cancel=cancel,
            )

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
        required = (
            "[Interface]",
            "PrivateKey = ",
            "DNS = ",
            "[Peer]",
            "PublicKey = ",
            "Endpoint = ",
            "AllowedIPs = ",
        )
        if not all(value in config for value in required) or "DNS = \n" in config:
            raise ValueError("Generated WireGuard client configuration is malformed")

    def destroy(self, deployment_id: str, preserve_config: bool = False) -> dict[str, object]:
        record = self.deployments.get(deployment_id)
        self._assert_record_paths(record)
        runtime = Path(record.runtime_directory)
        state_path = Path(record.state_path)
        if not record.resources_possible and not self.state_contains_resources(record):
            raise TerraformError(
                "No cloud resources can be established for this deployment; remove local deployment instead"
            )
        if not state_path.is_file():
            record.cleanup_status = "failed"
            self.deployments.save(record)
            raise TerraformError("Cannot destroy safely: deployment Terraform state is missing")
        record.state_present = True
        record.cleanup_status = "destroying"
        self._transition(record, DeploymentState.DESTROYING, "Destroying deployment resources")
        cancel = threading.Event()
        self._cancellations[deployment_id] = cancel
        env = self._terraform_env(record)
        var_file = runtime / "deployment.auto.tfvars.json"
        try:
            directory = Path(record.terraform_directory)
            with self._provider_operation_lock(record.provider_id, directory):
                self._validate_recovery_backend(record, env)
                self._stage_terraform_configuration(record)
                self._initialize_terraform_backend(record, env=env, cancel=cancel, fresh=False)
                self._run_terraform(
                    record,
                    ["destroy", "-auto-approve", "-input=false", "-no-color", f"-var-file={var_file}"],
                    env=env,
                    cancel=cancel,
                )
            record.resources_possible = False
            record.state_present = False
            record.cleanup_status = "destroyed"
            record.destroyed_at = now_iso()
            self._cleanup_after_destroy(record, preserve_config=preserve_config)
            record.public_ip = None
            record.resource_ids = {}
            record.last_error = None
            record.legacy_backup_path = None
            self._transition(record, DeploymentState.DESTROYED, "Deployment destroyed and sensitive artifacts removed")
            return {"status": "success", "deployment_id": deployment_id}
        except Exception as exc:
            record.state_present = state_path.is_file()
            record.resources_possible = True
            record.cleanup_status = "failed"
            message = redact(str(exc))
            self._transition(record, DeploymentState.FAILED, message, error=message)
            return {"status": "error", "deployment_id": deployment_id, "message": message}

    def remove_local_deployment(self, deployment_id: str) -> dict[str, str]:
        record = self.deployments.get(deployment_id)
        self._assert_record_paths(record)
        if record.state not in {DeploymentState.CANCELLED, DeploymentState.FAILED}:
            raise TerraformError("Local removal is allowed only after the deployment operation has stopped")
        if record.apply_started_at or record.resources_possible or self.state_contains_resources(record):
            raise TerraformError(
                "Local removal is blocked because cloud resources may exist; reconcile or destroy first"
            )
        runtime = Path(record.runtime_directory)
        shutil.rmtree(runtime)
        self.deployments.remove(deployment_id)
        self._cancellations.pop(deployment_id, None)
        return {"status": "success", "deployment_id": deployment_id}

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

    def get_logs(self, deployment_id: str) -> list[str]:
        record = self.deployments.get(deployment_id)
        path = Path(record.runtime_directory) / "deployment.log"
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-250:]

    def recovery_candidates(self) -> list[dict[str, object]]:
        return [record.to_dict() for record in self.deployments.list() if record.state != DeploymentState.DESTROYED]

    def reconcile_interrupted(self) -> None:
        pre_apply = {
            DeploymentState.IDLE,
            DeploymentState.VALIDATING_CREDENTIALS,
            DeploymentState.INITIALIZING,
            DeploymentState.PLANNING,
        }
        post_apply = {
            DeploymentState.PROVISIONING,
            DeploymentState.WAITING_FOR_CLOUD_INIT,
            DeploymentState.CHECKING_WIREGUARD,
            DeploymentState.VERIFYING_EGRESS,
            DeploymentState.DESTROYING,
        }
        for record in self.deployments.list():
            state_file = Path(record.state_path)
            record.state_present = state_file.is_file()
            state_resources = self._state_resource_count(state_file)
            if state_resources > 0:
                record.resources_possible = True
                record.cleanup_status = "required"
            if record.state in pre_apply and not record.apply_started_at and state_resources == 0:
                record.cleanup_status = "not_required"
                self._transition(record, DeploymentState.CANCELLED, "Previous local operation was interrupted")
            elif record.state in post_apply or state_resources > 0:
                record.resources_possible = True
                record.cleanup_status = "required"
                self._transition(
                    record,
                    DeploymentState.FAILED,
                    "Previous operation was interrupted after apply may have started; cloud cleanup may be required",
                    error="Interrupted operation requires reconciliation",
                )
            else:
                self.deployments.save(record)

    def list_legacy_states(self) -> list[dict[str, object]]:
        reports = [self._inspect_legacy_provider(provider_id) for provider_id in PROVIDER_RESOURCE_PREFIXES]
        snapshots: dict[str, dict[str, object]] = {}
        for report in reports:
            if report["stale_reconciliation_available"]:
                snapshots[str(report["provider_id"])] = {
                    "primary_path": report["primary_path"],
                    "primary_sha256": report["primary_sha256"],
                    "primary_lineage": report["primary_lineage"],
                    "primary_serial": report["primary_serial"],
                    "backup_sha256": report["backup_sha256"],
                }
        with self._legacy_snapshot_lock:
            self._legacy_confirmation_snapshots = snapshots
        return reports

    def migrate_legacy_state(self, provider_id: str) -> dict[str, object]:
        report = self._inspect_legacy_provider(provider_id)
        if report["classification"] != "active" or not report["migration_available"]:
            raise TerraformError("Legacy state identity is not sufficiently certain for automatic migration")
        source = Path(str(report["primary_path"]))
        fingerprint = str(report["primary_sha256"])
        candidates = self._legacy_identity_candidates(provider_id, source)
        if len(candidates) != 1:
            raise TerraformError("Legacy state does not match exactly one deployment registry record")
        record = candidates[0]
        self._assert_record_paths(record)
        destination = Path(record.state_path)
        if destination.exists():
            raise TerraformError("Migration refused because the deployment already has runtime state")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = Path(record.runtime_directory) / f"legacy-state-backup-{timestamp}.tfstate"
        shutil.copy2(source, backup)
        shutil.copy2(source, destination)
        if self._sha256(source) != fingerprint or self._sha256(destination) != fingerprint:
            destination.unlink(missing_ok=True)
            raise TerraformError("Legacy state changed during migration; the original was preserved")
        record.state_present = True
        record.resources_possible = True
        record.cleanup_status = "required"
        record.legacy_source_path = str(source)
        record.legacy_source_sha256 = fingerprint
        record.legacy_backup_path = str(backup)
        self.deployments.save(record)
        return {"status": "success", "deployment_id": record.id, "source_preserved": True}

    def reconcile_stale_legacy_state(self, provider_id: str, confirmed: bool) -> dict[str, object]:
        if confirmed is not True:
            raise TerraformError("Explicit confirmation of independently verified cloud absence is required")
        directory = self._provider_directory(provider_id)
        with self._legacy_snapshot_lock:
            displayed = self._legacy_confirmation_snapshots.pop(provider_id, None)
        if displayed is None:
            raise TerraformError("Legacy state must be refreshed and reviewed before reconciliation")

        with self._provider_operation_lock(provider_id, directory):
            report = self._inspect_legacy_provider(provider_id)
            if not report["stale_reconciliation_available"]:
                raise TerraformError("Legacy state is not eligible for stale-state reconciliation")
            current = {
                "primary_path": report["primary_path"],
                "primary_sha256": report["primary_sha256"],
                "primary_lineage": report["primary_lineage"],
                "primary_serial": report["primary_serial"],
                "backup_sha256": report["backup_sha256"],
            }
            if current != displayed:
                raise TerraformError("Legacy state changed after it was displayed; refresh and review it again")

            source = Path(str(report["primary_path"]))
            backup = Path(str(report["backup_path"]))
            source_snapshot = self._source_state_snapshot(directory)
            fingerprint = str(report["primary_sha256"])
            if (
                source_snapshot.get(source.name) != fingerprint
                or source_snapshot.get(backup.name) != report["backup_sha256"]
            ):
                raise TerraformError("Legacy state changed before quarantine creation; refresh and review it again")
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            quarantine_root = (self.runtime_root / "legacy-quarantine" / provider_id).resolve()
            expected_root = (self.runtime_root / "legacy-quarantine").resolve()
            if quarantine_root.parent != expected_root:
                raise TerraformError("Unsafe legacy quarantine provider path")
            quarantine = (quarantine_root / f"{timestamp}-{fingerprint[:12]}").resolve()
            if quarantine.parent != quarantine_root:
                raise TerraformError("Unsafe legacy quarantine path")
            quarantine.mkdir(parents=True, exist_ok=False)
            quarantine_files: list[dict[str, object]] = []
            try:
                for original in (source, backup):
                    expected_sha = source_snapshot.get(original.name)
                    if expected_sha is None:
                        continue
                    destination = quarantine / original.name
                    self._copy_quarantine_file(original, destination)
                    copied_sha = self._sha256(destination)
                    if copied_sha != expected_sha:
                        raise TerraformError(f"Quarantine verification failed for {original.name}")
                    quarantine_files.append(
                        {
                            "source_name": original.name,
                            "path": str(destination),
                            "sha256": copied_sha,
                            "size": destination.stat().st_size,
                        }
                    )
                if self._source_state_snapshot(directory) != source_snapshot:
                    raise TerraformError("Legacy state changed while quarantine copies were being created")
            except Exception:
                shutil.rmtree(quarantine, ignore_errors=True)
                raise

            receipt: dict[str, Any] = {
                "version": 1,
                "provider_id": provider_id,
                "source_path": str(source.resolve()),
                "source_sha256": fingerprint,
                "source_backup_sha256": report["backup_sha256"],
                "terraform_lineage": report["primary_lineage"],
                "terraform_serial": report["primary_serial"],
                "resource_count": report["primary_resources"],
                "resources": report["resource_summary"],
                "outputs": report["outputs_summary"],
                "reconciled_at": now_iso(),
                "reason": "cloud_absence_confirmed",
                "quarantine_path": str(quarantine),
                "quarantine_files": quarantine_files,
                "status": "reconciled-stale",
            }
            receipt_path = self.legacy_reconciliations.write(provider_id, receipt)
            verified = self._inspect_legacy_provider(provider_id)
            if verified["classification"] != "reconciled-stale" or verified["blocking"]:
                raise TerraformError(f"Reconciliation receipt verification failed: {verified['reason']}")
            return {
                "status": "success",
                "provider_id": provider_id,
                "classification": verified["classification"],
                "source_preserved": True,
                "receipt_path": str(receipt_path),
                "quarantine_path": str(quarantine),
            }

    def _inspect_legacy_provider(self, provider_id: str) -> dict[str, object]:
        directory = self._provider_directory(provider_id)
        primary = directory / "terraform.tfstate"
        backup = directory / "terraform.tfstate.backup"
        primary_info = self._inspect_state_file(primary)
        backup_info = self._inspect_state_file(backup)
        receipt, receipt_error = self.legacy_reconciliations.read(provider_id)
        classification = "none"
        reason = "No provider-root state files detected"
        migration_available = False
        stale_reconciliation_available = False
        blocking = False
        if primary_info["exists"] and not primary_info["parseable"]:
            classification, reason, blocking = "malformed", "Primary provider-root state is malformed", True
        elif backup_info["exists"] and not backup_info["parseable"]:
            classification, reason, blocking = "malformed", "Provider-root state backup is malformed", True
        elif isinstance(primary_info["resources"], int) and primary_info["resources"] > 0:
            classification, reason, blocking = "active", "Primary provider-root state contains managed resources", True
            candidates = self._legacy_identity_candidates(provider_id, primary)
            migration_available = bool(primary_info["identity_valid"] and len(candidates) == 1)
        elif isinstance(backup_info["resources"], int) and backup_info["resources"] > 0:
            classification = "ambiguous"
            reason = "Primary state is empty or missing while its backup contains managed resources"
            blocking = True
        elif primary_info["exists"] or backup_info["exists"]:
            classification, reason = "empty", "Provider-root state files contain no managed resources"
        fingerprint = primary_info.get("sha256")
        receipt_details: dict[str, object] | None = None
        if classification == "active" and isinstance(fingerprint, str):
            migrated_records = [
                record
                for record in self.deployments.list()
                if record.legacy_source_sha256 == fingerprint and record.provider_id == provider_id
            ]
            if migrated_records:
                migrated, migration_reason = self._verify_migrated_legacy_state(
                    provider_id, primary, fingerprint, migrated_records
                )
                if migrated:
                    classification, reason, blocking = (
                        "migrated",
                        "Legacy state has a verified identical runtime state",
                        False,
                    )
                    migration_available = False
                else:
                    reason = f"Recorded legacy migration is invalid: {migration_reason}"

            if classification == "active":
                if receipt_error:
                    reason = f"Stale-state reconciliation is invalid: {receipt_error}"
                elif receipt is not None:
                    valid, receipt_reason, receipt_details = self._verify_stale_receipt(
                        provider_id, primary, backup, primary_info, backup_info, receipt
                    )
                    if valid:
                        classification, reason, blocking = (
                            "reconciled-stale",
                            "Historical state preserved; cloud absence was explicitly confirmed",
                            False,
                        )
                        migration_available = False
                    else:
                        reason = f"Stale-state reconciliation is invalid: {receipt_reason}"
                stale_reconciliation_available = (
                    classification == "active"
                    and not migration_available
                    and isinstance(primary_info.get("lineage"), str)
                    and bool(primary_info.get("lineage"))
                    and isinstance(primary_info.get("serial"), int)
                    and not isinstance(primary_info.get("serial"), bool)
                )
        if classification not in {"active", "migrated", "reconciled-stale"} and (
            receipt is not None or receipt_error is not None
        ):
            classification = "unverified-stale"
            blocking = True
            detail = receipt_error or "the provider-root state no longer matches the recorded reconciliation"
            reason = f"Stale-state reconciliation is invalid: {detail}"
        return {
            "provider_id": provider_id,
            "classification": classification,
            "reason": reason,
            "blocking": blocking,
            "migration_available": migration_available,
            "stale_reconciliation_available": stale_reconciliation_available,
            "primary_path": str(primary),
            "primary_sha256": primary_info.get("sha256"),
            "primary_resources": primary_info["resources"],
            "primary_lineage": primary_info.get("lineage"),
            "primary_serial": primary_info.get("serial"),
            "resource_summary": primary_info.get("resource_summary", []),
            "outputs_summary": primary_info.get("outputs_summary", []),
            "backup_path": str(backup),
            "backup_sha256": backup_info.get("sha256"),
            "backup_resources": backup_info["resources"],
            "reconciliation": receipt_details,
        }

    def _inspect_state_file(self, path: Path) -> dict[str, object]:
        if not path.is_file():
            return {"exists": False, "parseable": False, "resources": 0, "identity_valid": False}
        try:
            payload = path.read_bytes()
        except OSError:
            return {"exists": True, "parseable": False, "resources": 0, "identity_valid": False}
        result: dict[str, object] = {
            "exists": True,
            "parseable": False,
            "resources": 0,
            "identity_valid": False,
            "resource_summary": [],
            "outputs_summary": [],
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        try:
            state = json.loads(payload.decode("utf-8"))
            if not isinstance(state, dict):
                return result
            resources = self._summarize_state_resources(state)
            outputs = self._summarize_state_outputs(state)
            result["parseable"] = True
            result["resources"] = len(resources)
            result["resource_summary"] = resources
            result["outputs_summary"] = outputs
            result["lineage"] = state.get("lineage")
            result["serial"] = state.get("serial")
            resource_ids = state.get("outputs", {}).get("resource_ids", {}).get("value")
            result["identity_valid"] = bool(isinstance(resource_ids, dict) and resource_ids)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        return result

    @staticmethod
    def _summarize_state_resources(state: dict[str, Any]) -> list[dict[str, object]]:
        summary: list[dict[str, object]] = []
        raw_resources = state.get("resources", [])
        if not isinstance(raw_resources, list):
            return summary
        for resource in raw_resources:
            if not isinstance(resource, dict) or resource.get("mode", "managed") != "managed":
                continue
            resource_type = resource.get("type")
            resource_name = resource.get("name")
            instances = resource.get("instances")
            if (
                not isinstance(resource_type, str)
                or not isinstance(resource_name, str)
                or not isinstance(instances, list)
            ):
                continue
            module = resource.get("module")
            base_address = f"{resource_type}.{resource_name}"
            if isinstance(module, str) and module:
                base_address = f"{module}.{base_address}"
            for index, instance in enumerate(instances):
                if not isinstance(instance, dict):
                    continue
                index_key = instance.get("index_key")
                address = base_address
                if index_key is not None:
                    address += f"[{json.dumps(index_key, ensure_ascii=True)}]"
                elif len(instances) > 1:
                    address += f"[{index}]"
                identifiers: list[str] = []
                attributes = instance.get("attributes")
                if isinstance(attributes, dict):
                    for key in ("id", "name", "urn"):
                        value = attributes.get(key)
                        if isinstance(value, (str, int)) and not isinstance(value, bool):
                            safe = redact(str(value)).replace("\n", " ")[:256]
                            if safe and safe not in identifiers:
                                identifiers.append(safe)
                summary.append(
                    {
                        "address": address,
                        "type": resource_type,
                        "identifiers": identifiers,
                    }
                )
        return summary

    @staticmethod
    def _summarize_state_outputs(state: dict[str, Any]) -> list[dict[str, object]]:
        outputs = state.get("outputs", {})
        if not isinstance(outputs, dict):
            return []
        summary: list[dict[str, object]] = []
        for name in sorted(outputs):
            item = outputs[name]
            if not isinstance(name, str) or not isinstance(item, dict):
                continue
            value = item.get("value")
            value_type = (
                "null"
                if value is None
                else "boolean"
                if isinstance(value, bool)
                else "number"
                if isinstance(value, (int, float))
                else "string"
                if isinstance(value, str)
                else "list"
                if isinstance(value, list)
                else "object"
                if isinstance(value, dict)
                else "unknown"
            )
            summary.append({"name": name, "sensitive": bool(item.get("sensitive", False)), "type": value_type})
        return summary

    def _verify_migrated_legacy_state(
        self,
        provider_id: str,
        source: Path,
        fingerprint: str,
        records: list[DeploymentRecord],
    ) -> tuple[bool, str]:
        if len(records) != 1:
            return False, "the source fingerprint is associated with multiple deployment records"
        record = records[0]
        try:
            self._assert_record_paths(record)
        except (TerraformError, ValueError) as exc:
            return False, str(exc)
        if record.provider_id != provider_id:
            return False, "deployment provider does not match"
        if not record.legacy_source_path or Path(record.legacy_source_path).resolve() != source.resolve():
            return False, "recorded legacy source path does not match"
        runtime_state = Path(record.state_path)
        if not runtime_state.is_file():
            return False, "matching runtime state is missing"
        runtime_info = self._inspect_state_file(runtime_state)
        if not runtime_info["parseable"]:
            return False, "matching runtime state is malformed"
        if runtime_info.get("sha256") != fingerprint:
            return False, "matching runtime state fingerprint differs"
        prefix = PROVIDER_RESOURCE_PREFIXES[provider_id]
        resources = runtime_info.get("resource_summary")
        if (
            not isinstance(resources, list)
            or not resources
            or any(not isinstance(item, dict) or not str(item.get("type", "")).startswith(prefix) for item in resources)
        ):
            return False, "matching runtime state provider identity differs"
        return True, "verified"

    def _verify_stale_receipt(
        self,
        provider_id: str,
        source: Path,
        backup: Path,
        primary_info: dict[str, object],
        backup_info: dict[str, object],
        receipt: dict[str, Any],
    ) -> tuple[bool, str, dict[str, object] | None]:
        if receipt.get("version") != 1:
            return False, "unsupported receipt version", None
        if receipt.get("provider_id") != provider_id:
            return False, "receipt belongs to another provider", None
        if receipt.get("status") != "reconciled-stale":
            return False, "receipt status is not reconciled-stale", None
        if receipt.get("reason") != "cloud_absence_confirmed":
            return False, "receipt reason is invalid", None
        if receipt.get("source_path") != str(source.resolve()):
            return False, "source path differs from the receipt", None
        if receipt.get("source_sha256") != primary_info.get("sha256"):
            return False, "source fingerprint differs from the receipt", None
        if receipt.get("source_backup_sha256") != backup_info.get("sha256"):
            return False, "provider-root backup fingerprint differs from the receipt", None
        if receipt.get("terraform_lineage") != primary_info.get("lineage"):
            return False, "Terraform lineage differs from the receipt", None
        if receipt.get("terraform_serial") != primary_info.get("serial"):
            return False, "Terraform serial differs from the receipt", None
        if receipt.get("resource_count") != primary_info.get("resources"):
            return False, "managed resource count differs from the receipt", None
        if receipt.get("resources") != primary_info.get("resource_summary"):
            return False, "managed resource summary differs from the receipt", None
        if receipt.get("outputs") != primary_info.get("outputs_summary"):
            return False, "output summary differs from the receipt", None

        quarantine_root = (self.runtime_root / "legacy-quarantine" / provider_id).resolve()
        quarantine_value = receipt.get("quarantine_path")
        if not isinstance(quarantine_value, str):
            return False, "quarantine path is missing", None
        quarantine = Path(quarantine_value).resolve()
        if quarantine.parent != quarantine_root or not quarantine.is_dir():
            return False, "quarantine directory is missing or unsafe", None
        files = receipt.get("quarantine_files")
        if not isinstance(files, list):
            return False, "quarantine file manifest is missing", None
        expected = {"terraform.tfstate": primary_info.get("sha256")}
        if backup_info.get("sha256") is not None:
            expected["terraform.tfstate.backup"] = backup_info.get("sha256")
        if len(files) != len(expected):
            return False, "quarantine file manifest is incomplete", None
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                return False, "quarantine file manifest is malformed", None
            name = item.get("source_name")
            path_value = item.get("path")
            if not isinstance(name, str) or name not in expected or name in seen:
                return False, "quarantine file manifest has an unexpected entry", None
            if not isinstance(path_value, str):
                return False, "quarantine file path is missing", None
            path = Path(path_value).resolve()
            if path.parent != quarantine or path.name != name or not path.is_file():
                return False, f"quarantine copy for {name} is missing or unsafe", None
            try:
                actual_sha = self._sha256(path)
            except OSError as exc:
                return False, f"quarantine copy for {name} cannot be read: {exc}", None
            if item.get("sha256") != expected[name] or actual_sha != expected[name]:
                return False, f"quarantine copy for {name} failed fingerprint verification", None
            seen.add(name)

        reconciled_at = receipt.get("reconciled_at")
        if not isinstance(reconciled_at, str) or not reconciled_at:
            return False, "reconciliation timestamp is missing", None
        details = {
            "reconciled_at": reconciled_at,
            "fingerprint": str(primary_info.get("sha256", "")),
            "quarantine_path": str(quarantine),
            "resource_count": primary_info.get("resources", 0),
        }
        return True, "verified", details

    @staticmethod
    def _copy_quarantine_file(source: Path, destination: Path) -> None:
        shutil.copy2(source, destination)
        if not destination.is_file():
            raise TerraformError(f"Quarantine copy was not created for {source.name}")

    def _legacy_identity_candidates(self, provider_id: str, state_path: Path) -> list[DeploymentRecord]:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            resources = state.get("resources", [])
            prefix = PROVIDER_RESOURCE_PREFIXES[provider_id]
            if not resources or any(not str(item.get("type", "")).startswith(prefix) for item in resources):
                return []
            server_public = state.get("outputs", {}).get("server_public_key", {}).get("value")
            if not isinstance(server_public, str) or not server_public:
                return []
        except (OSError, json.JSONDecodeError, AttributeError):
            return []
        matches: list[DeploymentRecord] = []
        for record in self.deployments.list():
            if record.provider_id != provider_id or Path(record.state_path).exists():
                continue
            var_file = Path(record.runtime_directory) / "deployment.auto.tfvars.json"
            try:
                values = json.loads(var_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if values.get("server_public_key") == server_public:
                matches.append(record)
        return matches

    def _assert_provider_ready_for_new_deployment(self, provider_id: str) -> None:
        report = self._inspect_legacy_provider(provider_id)
        if report["blocking"]:
            raise TerraformError(
                f"New {provider_id} deployment blocked by {report['classification']} legacy state: {report['reason']}"
            )
        active = [
            record
            for record in self.deployments.list()
            if record.provider_id == provider_id
            and record.state != DeploymentState.DESTROYED
            and (record.resources_possible or record.apply_started_at or self.state_contains_resources(record))
        ]
        if active:
            raise TerraformError(
                f"New {provider_id} deployment blocked until existing cloud-capable deployment is reconciled"
            )

    def _provider_directory(self, provider_id: str) -> Path:
        provider = self.providers.get(provider_id)
        if not provider.info.terraform_root:
            raise TerraformError(f"Provider {provider_id} has no Terraform root")
        directory = (self.resource_root / provider.info.terraform_root).resolve()
        if directory.parent != self.resource_root or not directory.is_dir():
            raise TerraformError("Provider Terraform directory is missing or outside application resources")
        return directory

    def _assert_record_paths(self, record: DeploymentRecord) -> None:
        directory = Path(record.terraform_directory).resolve()
        runtime = Path(record.runtime_directory).resolve()
        state = Path(record.state_path).resolve()
        if directory != self._provider_directory(record.provider_id):
            raise TerraformError("Recorded Terraform source directory is unsafe or inconsistent")
        self._assert_scoped_path(runtime, state)

    def _assert_scoped_path(self, runtime: Path, state: Path) -> None:
        if runtime.parent != self.runtime_root or state.parent != runtime or state.name != "terraform.tfstate":
            raise TerraformError("Terraform state must be the deployment runtime terraform.tfstate")
        if runtime == self.runtime_root or self.resource_root in (runtime, *runtime.parents):
            raise TerraformError("Deployment runtime must not be inside application resources")

    def _terraform_working_directory(self, record: DeploymentRecord) -> Path:
        runtime = Path(record.runtime_directory).resolve()
        source = Path(record.terraform_directory).resolve()
        work_root = (runtime / TERRAFORM_WORK_ROOT).resolve()
        working = (work_root / source.name).resolve()
        if work_root.parent != runtime or working.parent != work_root:
            raise TerraformError("Terraform working directory is outside the deployment runtime")
        return working

    def _stage_terraform_configuration(self, record: DeploymentRecord) -> Path:
        self._assert_record_paths(record)
        runtime = Path(record.runtime_directory).resolve()
        source = Path(record.terraform_directory).resolve()
        work_root = (runtime / TERRAFORM_WORK_ROOT).resolve()
        working = self._terraform_working_directory(record)
        manifest_path = runtime / TERRAFORM_WORK_MANIFEST
        if work_root.exists() or manifest_path.exists():
            self._verify_terraform_work_manifest(record)
            return working

        source_files = sorted(source.glob("*.tf"))
        lock_file = source / ".terraform.lock.hcl"
        if not lock_file.is_file():
            raise TerraformError("Terraform provider dependency lockfile is missing")
        source_files.append(lock_file)
        legacy_tfvars = source / "terraform.tfvars"
        if legacy_tfvars.is_file():
            source_files.append(legacy_tfvars)
        common_source = self.resource_root / "terraform-common"
        common_files = sorted(path for path in common_source.rglob("*") if path.is_file())
        if not source_files or not common_files:
            raise TerraformError("Terraform source configuration is incomplete")

        entries: list[dict[str, str]] = []
        work_root.mkdir(parents=True, exist_ok=False)
        try:
            working.mkdir()
            common_destination = work_root / "terraform-common"
            common_destination.mkdir()
            for original in source_files:
                destination = working / original.name
                before = self._sha256(original)
                if original.name == "terraform.tfvars":
                    write_secret_bytes(destination, original.read_bytes())
                else:
                    shutil.copy2(original, destination)
                if self._sha256(original) != before or self._sha256(destination) != before:
                    raise TerraformError(f"Terraform source changed while staging {original.name}")
                entries.append({"path": str(destination.relative_to(work_root)), "sha256": before})
            for original in common_files:
                relative = original.relative_to(common_source)
                destination = common_destination / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                before = self._sha256(original)
                shutil.copy2(original, destination)
                if self._sha256(original) != before or self._sha256(destination) != before:
                    raise TerraformError(f"Terraform common source changed while staging {relative}")
                entries.append({"path": str(destination.relative_to(work_root)), "sha256": before})
            manifest = {
                "version": 1,
                "provider_id": record.provider_id,
                "source_directory": str(source),
                "working_directory": str(working),
                "files": entries,
            }
            temporary = manifest_path.with_suffix(".tmp")
            write_secret(temporary, json.dumps(manifest, indent=2, sort_keys=True))
            temporary.replace(manifest_path)
            self._verify_terraform_work_manifest(record)
            return working
        except Exception:
            shutil.rmtree(work_root, ignore_errors=True)
            manifest_path.unlink(missing_ok=True)
            raise

    def _verify_terraform_work_manifest(self, record: DeploymentRecord) -> None:
        runtime = Path(record.runtime_directory).resolve()
        work_root = (runtime / TERRAFORM_WORK_ROOT).resolve()
        working = self._terraform_working_directory(record)
        manifest_path = runtime / TERRAFORM_WORK_MANIFEST
        if not work_root.is_dir() or not working.is_dir() or not manifest_path.is_file():
            raise TerraformError("Deployment Terraform working copy is missing or incomplete")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TerraformError(f"Deployment Terraform working manifest is invalid: {exc}") from exc
        if not isinstance(manifest, dict):
            raise TerraformError("Deployment Terraform working manifest is invalid")
        if manifest.get("version") != 1 or manifest.get("provider_id") != record.provider_id:
            raise TerraformError("Deployment Terraform working manifest identity differs")
        if manifest.get("source_directory") != str(Path(record.terraform_directory).resolve()):
            raise TerraformError("Deployment Terraform source identity differs")
        if manifest.get("working_directory") != str(working):
            raise TerraformError("Deployment Terraform working path differs")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise TerraformError("Deployment Terraform working manifest contains no files")
        for entry in entries:
            if not isinstance(entry, dict):
                raise TerraformError("Deployment Terraform working manifest is malformed")
            relative = entry.get("path")
            fingerprint = entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(fingerprint, str):
                raise TerraformError("Deployment Terraform working manifest entry is malformed")
            path = (work_root / relative).resolve()
            if work_root not in path.parents or not path.is_file():
                raise TerraformError("Deployment Terraform working file is missing or unsafe")
            if self._sha256(path) != fingerprint:
                raise TerraformError(f"Deployment Terraform working file changed: {relative}")
        if any(working.glob("terraform.tfstate*")):
            raise TerraformError("Terraform state must not exist in the deployment working copy")

    def _backend_metadata_path(self, record: DeploymentRecord) -> Path:
        runtime = Path(record.runtime_directory).resolve()
        metadata = (runtime / ".terraform" / "terraform.tfstate").resolve()
        if metadata.parent.parent != runtime:
            raise TerraformError("Terraform backend metadata path is outside the deployment runtime")
        return metadata

    def _backend_state_path(self, record: DeploymentRecord) -> Path:
        metadata_path = self._backend_metadata_path(record)
        if not metadata_path.is_file():
            raise TerraformError("Deployment backend metadata is missing")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            backend = metadata.get("backend")
            if not isinstance(backend, dict) or backend.get("type") != "local":
                raise TerraformError("Deployment backend metadata is not a local backend")
            config = backend.get("config")
            path_value = config.get("path") if isinstance(config, dict) else None
            if not isinstance(path_value, str) or not Path(path_value).is_absolute():
                raise TerraformError("Deployment backend metadata has no absolute state path")
            return Path(path_value).resolve()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise TerraformError(f"Deployment backend metadata is malformed: {exc}") from exc

    def _assert_terraform_data_dir(self, record: DeploymentRecord, env: dict[str, str]) -> None:
        runtime = Path(record.runtime_directory).resolve()
        expected = (runtime / ".terraform").resolve()
        configured = env.get("TF_DATA_DIR")
        if not configured or Path(configured).resolve() != expected or expected.parent != runtime:
            raise TerraformError("TF_DATA_DIR must be the deployment runtime .terraform directory")

    def _assert_backend_invariants(self, record: DeploymentRecord, env: dict[str, str]) -> None:
        self._assert_record_paths(record)
        self._assert_terraform_data_dir(record, env)
        self._verify_terraform_work_manifest(record)
        expected_state = Path(record.state_path).resolve()
        if self._backend_state_path(record) != expected_state:
            raise TerraformError("Deployment backend metadata points to a different Terraform state")

    def _initialize_terraform_backend(
        self,
        record: DeploymentRecord,
        *,
        env: dict[str, str],
        cancel: threading.Event,
        fresh: bool,
    ) -> CommandResult:
        self._assert_terraform_data_dir(record, env)
        state = Path(record.state_path)
        metadata = self._backend_metadata_path(record)
        if fresh:
            if (
                record.apply_started_at
                or record.apply_completed_at
                or record.resources_possible
                or state.exists()
                or metadata.exists()
            ):
                raise TerraformError("Fresh backend initialization refused because deployment state may already exist")
            args = [
                "init",
                "-input=false",
                "-reconfigure",
                "-lockfile=readonly",
                "-no-color",
                f"-backend-config=path={state.resolve()}",
            ]
        else:
            self._validate_recovery_backend(record, env)
            args = [
                "init",
                "-input=false",
                "-lockfile=readonly",
                "-no-color",
                f"-backend-config=path={state.resolve()}",
            ]
        result = self._run_terraform(record, args, env=env, cancel=cancel)
        self._assert_backend_invariants(record, env)
        return result

    def _validate_recovery_backend(self, record: DeploymentRecord, env: dict[str, str]) -> None:
        self._assert_record_paths(record)
        self._assert_terraform_data_dir(record, env)
        state = Path(record.state_path)
        if not state.is_file():
            raise TerraformError("Recovery backend initialization refused because deployment state is missing")
        state_info = self._inspect_state_file(state)
        if not state_info["parseable"]:
            raise TerraformError("Recovery backend initialization refused because deployment state is malformed")
        managed_resources = state_info.get("resources")
        if not (
            record.apply_started_at
            or record.apply_completed_at
            or record.resources_possible
            or isinstance(managed_resources, int)
            and managed_resources > 0
        ):
            raise TerraformError("Recovery backend initialization refused because lifecycle ownership is unproven")
        expected_state = state.resolve()
        if self._backend_state_path(record) != expected_state:
            raise TerraformError("Recovery backend metadata disagrees with the recorded deployment state path")

    def _terraform_env(self, record: DeploymentRecord) -> dict[str, str]:
        runtime = Path(record.runtime_directory)
        return {"TF_DATA_DIR": str(runtime / ".terraform"), "TF_IN_AUTOMATION": "1"}

    def _run_terraform(
        self,
        record: DeploymentRecord,
        args: list[str],
        *,
        env: dict[str, str],
        cancel: threading.Event,
    ) -> CommandResult:
        self._assert_record_paths(record)
        self._assert_terraform_data_dir(record, env)
        source_directory = Path(record.terraform_directory)
        working_directory = self._terraform_working_directory(record)
        self._verify_terraform_work_manifest(record)
        snapshot = self._source_state_snapshot(source_directory)
        try:
            result = self.runner.run(
                args,
                working_directory,
                env=env,
                cancel=cancel,
                progress=lambda line: self._log(record.id, line),
            )
        finally:
            self._assert_source_state_unchanged(source_directory, snapshot)
        self._assert_backend_invariants(record, env)
        return result

    def _terraform_outputs(
        self, record: DeploymentRecord, *, env: dict[str, str], cancel: threading.Event
    ) -> dict[str, object]:
        self._assert_record_paths(record)
        self._assert_backend_invariants(record, env)
        source_directory = Path(record.terraform_directory)
        working_directory = self._terraform_working_directory(record)
        snapshot = self._source_state_snapshot(source_directory)
        try:
            return self.runner.output_json(
                working_directory,
                env=env,
                cancel=cancel,
                progress=lambda line: self._log(record.id, line),
            )
        finally:
            self._assert_source_state_unchanged(source_directory, snapshot)
            self._assert_backend_invariants(record, env)

    @contextmanager
    def _deployment_creation_transaction(self) -> Iterator[None]:
        with self._deployment_creation_lock:
            with FileLock(self.runtime_root / "deployment-operation.lock", timeout=60):
                yield

    def _provider_operation_lock(self, provider_id: str, directory: Path) -> ContextManager[None]:
        thread_lock = self.runner.lock_for(directory)
        file_lock = FileLock(self.runtime_root / "locks" / f"{provider_id}.lock", timeout=60)

        class CombinedLock:
            def __enter__(self_nonlocal) -> None:
                thread_lock.acquire()
                try:
                    file_lock.__enter__()
                except Exception:
                    thread_lock.release()
                    raise

            def __exit__(self_nonlocal, exc_type: object, exc: object, traceback: object) -> None:
                try:
                    file_lock.__exit__(exc_type, exc, traceback)  # type: ignore[arg-type]
                finally:
                    thread_lock.release()

        return CombinedLock()

    def _source_state_snapshot(self, directory: Path) -> dict[str, str | None]:
        return {
            name: self._sha256(directory / name) if (directory / name).is_file() else None
            for name in LEGACY_STATE_NAMES
        }

    def _assert_source_state_unchanged(self, directory: Path, before: dict[str, str | None]) -> None:
        after = self._source_state_snapshot(directory)
        if before != after:
            raise TerraformError(f"Safety invariant failed: Terraform modified provider-root state in {directory}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def state_contains_resources(self, record: DeploymentRecord) -> bool:
        return self._state_resource_count(Path(record.state_path)) > 0

    @staticmethod
    def _state_resource_count(path: Path) -> int:
        if not path.is_file():
            return 0
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            return sum(
                1
                for item in state.get("resources", [])
                if isinstance(item, dict) and isinstance(item.get("instances"), list) and item["instances"]
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return 0

    @staticmethod
    def _raise_if_cancelled(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise TerraformCancelled("Deployment operation cancelled")

    @staticmethod
    def _run_cancellable_process(args: list[str], cancel: threading.Event, *, timeout: float) -> tuple[int, str, str]:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel.wait(0.1):
                process.terminate()
                try:
                    process.wait(3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise TerraformCancelled("Deployment cancelled during SSH readiness checks")
            if time.monotonic() >= deadline:
                process.kill()
                raise subprocess.TimeoutExpired(args, timeout)
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def _cleanup_after_destroy(self, record: DeploymentRecord, *, preserve_config: bool) -> None:
        runtime = Path(record.runtime_directory)
        sensitive = [
            "deployment.auto.tfvars.json",
            "deployment.tfplan",
            "ssh-cidr-refresh.tfplan",
            "terraform.tfstate",
            "terraform.tfstate.backup",
            "client.privatekey",
            "ssh.privatekey",
            "ssh.publickey",
            "known_hosts",
        ]
        if not preserve_config:
            sensitive.append("client.conf")
        for name in sensitive:
            (runtime / name).unlink(missing_ok=True)
        for pattern in ("*.tfstate", "*.tfstate.backup", "*.tfplan", "*.privatekey"):
            for path in runtime.glob(pattern):
                if path.is_file():
                    path.unlink()
        terraform_data = runtime / ".terraform"
        if terraform_data.is_dir():
            shutil.rmtree(terraform_data)
        terraform_work = runtime / TERRAFORM_WORK_ROOT
        if terraform_work.is_dir():
            shutil.rmtree(terraform_work)
        (runtime / TERRAFORM_WORK_MANIFEST).unlink(missing_ok=True)

    def _transition(
        self, record: DeploymentRecord, state: DeploymentState, message: str, error: str | None = None
    ) -> None:
        record.state = state
        record.last_error = error
        self.deployments.save(record)
        event = StatusEvent(record.id, state, message)
        self._events.append(event)
        self._log(record.id, message)
        if self.on_event:
            self.on_event(event)

    def _log(self, deployment_id: str, line: str) -> None:
        try:
            record = self.deployments.get(deployment_id)
            append_redacted_log(Path(record.runtime_directory) / "deployment.log", line)
        except (KeyError, OSError, RuntimeError):
            pass
        if self.on_log:
            self.on_log(deployment_id, redact(line))
