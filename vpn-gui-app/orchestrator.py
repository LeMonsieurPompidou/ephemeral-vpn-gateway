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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator

from catalog import ProviderCatalog
from client_peers import (
    CLIENT_SCHEMA_VERSION,
    ClientPeer,
    client_tunnel_ipv4,
    generate_client_peers,
    resolve_client_path,
    validate_client_count,
)
from cloud_init import FAILURE_MARKER, READINESS_MARKER, bootstrap_source_sha256, render_user_data
from config_export import ConfigExportError, regular_file_sha256, remove_tracked_config
from file_lock import FileLock
from legacy_cloud_verification import LegacyCloudVerifier
from legacy_reconciliation import LegacyReconciliationStore, LegacySourceStore
from models import (
    ClientExportMetadata,
    ClientMetadata,
    DeploymentOptions,
    DeploymentRecord,
    DeploymentState,
    StatusEvent,
    now_iso,
)
from networking import PublicIpDetectionError, detect_public_ipv4, normalize_public_ipv4_cidr
from output_contract import ProviderOutputs, validate_provider_outputs
from providers import ProviderRegistry
from runtime_registry import DeploymentRegistry
from security import (
    PrivateFileSecurityError,
    SshIdentityError,
    append_redacted_log,
    generate_ssh_keypair,
    generate_wireguard_keypair,
    redact,
    verify_private_file,
    verify_ssh_keypair,
    write_secret,
    write_secret_bytes,
)
from ssh_probe import (
    SshAuthenticationFailure,
    SshProbeError,
    SshProbeTimeout,
    SshTransientError,
    SshTransportFailure,
    classify_ssh_failure,
)
from terraform_runner import CommandResult, TerraformCancelled, TerraformError, TerraformRunner
from validation import validate_options

EventCallback = Callable[[StatusEvent], None]
LogCallback = Callable[[str, str], None]
IpDetector = Callable[[], str]
LEGACY_STATE_NAMES = ("terraform.tfstate", "terraform.tfstate.backup")
TERRAFORM_WORK_ROOT = "terraform-work"
TERRAFORM_WORK_MANIFEST = "terraform-work-manifest.json"
USER_DATA_MANIFEST = "user-data-manifest.json"
SSH_READINESS_TIMEOUT_SECONDS = 120.0
CLOUD_INIT_HARD_TIMEOUT_SECONDS = 900.0
CLOUD_INIT_INACTIVITY_TIMEOUT_SECONDS = 180.0
WIREGUARD_READINESS_TIMEOUT_SECONDS = 120.0
READINESS_POLL_INTERVAL_SECONDS = 5.0
CLOUD_INIT_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
SSH_CONNECT_TIMEOUT_SECONDS = 10
SSH_SERVER_ALIVE_INTERVAL_SECONDS = 5
SSH_SERVER_ALIVE_COUNT_MAX = 2
SSH_PROCESS_TIMEOUT_SECONDS = 25.0
SSH_PROBE_OUTAGE_ALLOWANCE_SECONDS = 90.0
LEGACY_VERIFICATION_MAX_AGE_SECONDS = 15 * 60
CLOUD_INIT_PROGRESS_COMMAND = (
    "status=/var/lib/ephemeral-vpn/bootstrap-status; "
    "output=/var/log/cloud-init-output.log; "
    'if [ -r "$status" ]; then cat "$status"; '
    'elif [ -r "$output" ]; then '
    "sed -n 's/^ephemeral-vpn bootstrap phase: \\([A-Za-z0-9 /_-]\\{1,80\\}\\)$/phase=\\1/p' "
    '"$output" | tail -n 1; '
    "sed -n 's/^ephemeral-vpn bootstrap build: \\([0-9a-f]\\{12\\}\\)$/build=\\1/p' "
    '"$output" | tail -n 1; '
    "fi; "
    'if [ -r "$output" ]; then '
    "stat -c 'activity_epoch=%Y' \"$output\"; "
    "stat -c 'activity_size=%s' \"$output\"; "
    "fi; "
    f"if [ -f {READINESS_MARKER} ]; then echo ready=1; else echo ready=0; fi"
)
PROVIDER_RESOURCE_PREFIXES = {
    "aws-lightsail": "aws_lightsail_",
    "digitalocean": "digitalocean_",
    "scaleway": "scaleway_",
}


@dataclass(frozen=True)
class CloudInitProgress:
    phase: str | None
    build: str | None
    activity_epoch: int | None
    activity_size: int | None
    ready: bool

    @property
    def token(self) -> tuple[str | None, int | None, int | None]:
        return (self.phase, self.activity_epoch, self.activity_size)


class Orchestrator:
    def __init__(
        self,
        resource_root: Path,
        runtime_root: Path,
        runner: TerraformRunner | None = None,
        *,
        ip_detector: IpDetector = detect_public_ipv4,
        readiness_timeout: float | None = None,
        ssh_readiness_timeout: float = SSH_READINESS_TIMEOUT_SECONDS,
        cloud_init_hard_timeout: float = CLOUD_INIT_HARD_TIMEOUT_SECONDS,
        cloud_init_inactivity_timeout: float = CLOUD_INIT_INACTIVITY_TIMEOUT_SECONDS,
        wireguard_readiness_timeout: float = WIREGUARD_READINESS_TIMEOUT_SECONDS,
        readiness_poll_interval: float = READINESS_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.resource_root = resource_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.catalog = ProviderCatalog(self.resource_root / "vpn-gui-app" / "provider_catalog.json")
        self.providers = ProviderRegistry(self.catalog)
        self.deployments = DeploymentRegistry(self.runtime_root / "deployments.json")
        self.legacy_reconciliations = LegacyReconciliationStore(self.runtime_root / "legacy-reconciliations")
        self.legacy_verifications = LegacyReconciliationStore(self.runtime_root / "legacy-verifications")
        self.legacy_sources = LegacySourceStore(self.runtime_root / "legacy-source-roots.json")
        self.legacy_cloud_verifier = LegacyCloudVerifier()
        self.runner = runner or TerraformRunner()
        self.ip_detector = ip_detector
        if readiness_timeout is not None:
            cloud_init_hard_timeout = readiness_timeout
        timeouts = (
            ssh_readiness_timeout,
            cloud_init_hard_timeout,
            cloud_init_inactivity_timeout,
            wireguard_readiness_timeout,
            readiness_poll_interval,
        )
        if any(value <= 0 for value in timeouts):
            raise ValueError("Readiness timeouts must be positive")
        if cloud_init_inactivity_timeout >= cloud_init_hard_timeout:
            raise ValueError("Cloud-init inactivity timeout must be shorter than its hard timeout")
        self.ssh_readiness_timeout = ssh_readiness_timeout
        self.cloud_init_hard_timeout = cloud_init_hard_timeout
        self.cloud_init_inactivity_timeout = cloud_init_inactivity_timeout
        self.wireguard_readiness_timeout = wireguard_readiness_timeout
        self.readiness_poll_interval = readiness_poll_interval
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
        result = self.providers.get(provider_id).credential_details()
        return {"provider_id": provider_id, **result.to_dict()}

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
        location = self.catalog.get_location(provider_id, location_id)
        if not provider.info.terraform_root:
            raise ValueError("The selected provider is not available for deployment")
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
                estimated_hourly_cost_usd=location.estimated_hourly_cost_usd,
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
            raise ValueError("The selected provider is not available for deployment")
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
            if not valid:
                raise ValueError(message)

            server_private, server_public = generate_wireguard_keypair()
            client_peers = generate_client_peers(options.client_count, generate_wireguard_keypair)
            ssh_private, ssh_public = generate_ssh_keypair()
            for peer in client_peers:
                private_path = resolve_client_path(runtime, peer.private_key_relative_path)
                private_path.parent.mkdir(parents=True, exist_ok=True)
                write_secret(private_path, peer.private_key + "\n")
            write_secret(runtime / "ssh.privatekey", ssh_private)
            write_secret(runtime / "ssh.publickey", ssh_public + "\n")
            record.client_schema_version = CLIENT_SCHEMA_VERSION
            record.clients = [peer.metadata() for peer in client_peers]
            self.deployments.save(record)
            self._raise_if_cancelled(cancel)

            self._stage_terraform_configuration(record, include_legacy_tfvars=True)
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
                    client_peers=[peer.terraform_peer() for peer in client_peers],
                )
                self._write_user_data_manifest(record, staged_common, user_data_payload)
                variables = provider.terraform_variables(location, effective_options)
                variables.update(
                    {
                        "deployment_id": identifier,
                        "expires_at": expires,
                        "server_private_key": server_private,
                        "server_public_key": server_public,
                        "client_peers": [peer.terraform_peer() for peer in client_peers],
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
                client_peers,
                cancel,
                automatic_ssh_cidr=not manual_ssh_cidr,
            )
            self._transition(record, DeploymentState.READY, "WireGuard gateway is ready")
            return {
                "status": "success",
                "deployment_id": identifier,
                "state": record.state.value,
                "ip": record.public_ip,
                "clients": self.list_client_configs(identifier),
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
        client_peers: list[ClientPeer],
        cancel: threading.Event,
        *,
        automatic_ssh_cidr: bool,
    ) -> None:
        self._raise_if_cancelled(cancel)
        runtime = Path(record.runtime_directory)
        for peer in client_peers:
            config = self._render_client_config(
                record.public_ip or "",
                outputs.server_public_key,
                peer.private_key,
                peer.tunnel_ipv4,
                options,
            )
            self._validate_client_config(config)
            write_secret(resolve_client_path(runtime, peer.config_relative_path), config)
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
        user = "ubuntu" if record.provider_id == "aws-lightsail" else "root"
        runtime = Path(record.runtime_directory)
        identity = runtime / "ssh.privatekey"
        known_hosts = runtime / "known_hosts"
        try:
            verify_private_file(identity)
        except PrivateFileSecurityError as exc:
            raise TerraformError(f"Local SSH private-key security check failed: {exc}") from exc
        try:
            verify_ssh_keypair(identity, runtime / "ssh.publickey")
        except SshIdentityError as exc:
            raise TerraformError(f"Local SSH deployment identity check failed: {exc}") from exc
        overall_started = time.monotonic()
        record.readiness_started_at = record.readiness_started_at or now_iso()
        record.provisioning_elapsed_seconds = 0
        self.deployments.save(record)
        self._wait_for_ssh(
            record,
            identity,
            known_hosts,
            user,
            options,
            cancel,
            overall_started=overall_started,
            automatic_ssh_cidr=automatic_ssh_cidr,
        )
        self._transition(
            record, DeploymentState.WAITING_FOR_CLOUD_INIT, "SSH is ready; waiting for cloud initialization"
        )
        self._wait_for_cloud_init(
            record,
            identity,
            known_hosts,
            user,
            cancel,
            overall_started=overall_started,
        )
        self._transition(
            record,
            DeploymentState.CHECKING_WIREGUARD,
            "Cloud initialization succeeded; checking WireGuard readiness",
        )
        self._wait_for_wireguard(
            record,
            identity,
            known_hosts,
            user,
            options,
            cancel,
            readiness_marker=readiness_marker,
            overall_started=overall_started,
        )

    def _wait_for_ssh(
        self,
        record: DeploymentRecord,
        identity: Path,
        known_hosts: Path,
        user: str,
        options: DeploymentOptions,
        cancel: threading.Event,
        *,
        overall_started: float,
        automatic_ssh_cidr: bool,
    ) -> None:
        assert record.public_ip
        started = time.monotonic()
        last_error = "SSH did not become ready"
        attempts = 0
        ip_refresh_attempted = False
        while True:
            self._raise_if_cancelled(cancel)
            attempts += 1
            try:
                with socket.create_connection((record.public_ip, 22), timeout=3):
                    pass
                args = self._ssh_command(identity, known_hosts, user, record.public_ip, "true")
                self._run_ssh_probe(args, cancel, allowed_returncodes={0})
                self._persist_provisioning_elapsed(record, overall_started)
                return
            except TerraformCancelled:
                raise
            except SshProbeError as exc:
                if not isinstance(exc, SshTransientError):
                    raise TerraformError(f"{exc} Cloud resources may exist and must be destroyed.") from exc
                last_error = str(exc)
            except OSError:
                last_error = "SSH transport is not ready."

            if automatic_ssh_cidr and not ip_refresh_attempted and attempts >= 3:
                ip_refresh_attempted = True
                try:
                    detected = self.ip_detector()
                    if detected != options.ssh_cidr:
                        self._refresh_ssh_firewall(record, detected, cancel)
                        options = replace(options, ssh_cidr=detected)
                except (PublicIpDetectionError, TerraformError) as exc:
                    last_error = redact(str(exc))
            elapsed = time.monotonic() - started
            self._persist_provisioning_elapsed(record, overall_started)
            if elapsed >= self.ssh_readiness_timeout:
                raise TerraformError(f"SSH readiness timed out after {self._duration_text(elapsed)}: {last_error}")
            if cancel.wait(self.readiness_poll_interval):
                raise TerraformCancelled("Deployment cancelled during SSH readiness checks")

    def _wait_for_cloud_init(
        self,
        record: DeploymentRecord,
        identity: Path,
        known_hosts: Path,
        user: str,
        cancel: threading.Event,
        *,
        overall_started: float,
    ) -> None:
        assert record.public_ip
        started = time.monotonic()
        last_progress = started
        last_log = started - CLOUD_INIT_PROGRESS_LOG_INTERVAL_SECONDS
        last_token: tuple[str | None, int | None, int | None] | None = None
        probe_outage_started: float | None = None
        probe_failures = 0
        expected_build = self._expected_bootstrap_build(record)
        while True:
            self._raise_if_cancelled(cancel)
            poll_started = time.monotonic()
            try:
                args = self._ssh_command(identity, known_hosts, user, record.public_ip, "cloud-init status --long")
                returncode, stdout, stderr = self._run_ssh_probe(
                    args,
                    cancel,
                    allowed_returncodes={0, 1, 2},
                    timeout=self._cloud_init_probe_timeout(started, probe_outage_started),
                )
                combined = (stdout + "\n" + stderr).strip()
                status = self._cloud_init_status(returncode, combined)
                if status == "error":
                    failure_args = self._ssh_command(
                        identity,
                        known_hosts,
                        user,
                        record.public_ip,
                        f"if [ -r {FAILURE_MARKER} ]; then cat {FAILURE_MARKER}; fi",
                    )
                    try:
                        _failure_code, failure_stdout, failure_stderr = self._run_ssh_probe(
                            failure_args,
                            cancel,
                            allowed_returncodes={0},
                            timeout=self._cloud_init_probe_timeout(started, probe_outage_started),
                        )
                    except SshProbeError:
                        failure_stdout = failure_stderr = ""
                    diagnostic = self._cloud_init_failure_diagnostic(
                        "\n".join((combined, failure_stdout, failure_stderr))
                    )
                    if diagnostic:
                        raise TerraformError(
                            f"Cloud initialization failed during {diagnostic}. "
                            "Cloud resources may exist and must be destroyed."
                        )
                    raise TerraformError(
                        "Cloud initialization failed during bootstrap. Cloud resources may exist and must be destroyed."
                    )

                progress_args = self._ssh_command(
                    identity, known_hosts, user, record.public_ip, CLOUD_INIT_PROGRESS_COMMAND
                )
                _progress_code, progress_stdout, _progress_stderr = self._run_ssh_probe(
                    progress_args,
                    cancel,
                    allowed_returncodes={0},
                    timeout=self._cloud_init_probe_timeout(started, probe_outage_started),
                )
            except TerraformCancelled:
                raise
            except SshTransientError as exc:
                now = time.monotonic()
                if probe_outage_started is None:
                    probe_outage_started = poll_started
                    probe_failures = 0
                probe_failures += 1
                elapsed = now - started
                outage = now - probe_outage_started
                self._persist_provisioning_elapsed(record, overall_started)
                if elapsed >= self.cloud_init_hard_timeout:
                    raise TerraformError(
                        f"Cloud initialization exceeded the maximum provisioning time of "
                        f"{self._duration_text(self.cloud_init_hard_timeout)} while SSH monitoring was unavailable. "
                        "Cloud resources may exist and must be destroyed."
                    ) from exc
                if outage >= SSH_PROBE_OUTAGE_ALLOWANCE_SECONDS:
                    raise TerraformError(
                        "Server became unreachable during cloud initialization for "
                        f"{self._duration_text(outage)}. Cloud resources may exist and must be destroyed."
                    ) from exc
                probe_description = (
                    "temporarily timed out" if isinstance(exc, SshProbeTimeout) else "is temporarily unavailable"
                )
                self._log(
                    record.id,
                    f"SSH readiness probe {probe_description}; retrying "
                    f"({probe_failures}, outage {self._duration_text(outage)}/"
                    f"{self._duration_text(SSH_PROBE_OUTAGE_ALLOWANCE_SECONDS)})",
                )
                if cancel.wait(self.readiness_poll_interval):
                    raise TerraformCancelled("Deployment cancelled while waiting for cloud initialization") from exc
                continue
            except SshProbeError as exc:
                raise TerraformError(f"{exc} Cloud resources may exist and must be destroyed.") from exc

            now = time.monotonic()
            elapsed = now - started
            if elapsed > self.cloud_init_hard_timeout:
                raise TerraformError(
                    "Cloud initialization is progressing but exceeded the maximum provisioning time of "
                    f"{self._duration_text(self.cloud_init_hard_timeout)}. "
                    "Cloud resources may exist and must be destroyed."
                )
            if probe_outage_started is not None:
                outage = now - probe_outage_started
                if outage > SSH_PROBE_OUTAGE_ALLOWANCE_SECONDS:
                    raise TerraformError(
                        "Server became unreachable during cloud initialization for "
                        f"{self._duration_text(outage)}. Cloud resources may exist and must be destroyed."
                    )
                last_progress += outage
                self._log(
                    record.id,
                    f"SSH readiness monitoring recovered after {self._duration_text(outage)}; "
                    "cloud-init monitoring continues",
                )
                probe_outage_started = None
                probe_failures = 0

            progress = self._cloud_init_progress(progress_stdout)
            if expected_build and progress.build and progress.build != expected_build:
                raise TerraformError(
                    "Remote bootstrap build fingerprint differs from the deployment runtime manifest; "
                    "cloud resources may exist and must be destroyed."
                )

            meaningful_progress = any(value is not None for value in progress.token)
            changed = meaningful_progress and progress.token != last_token
            previous_phase = record.bootstrap_phase
            if changed:
                last_progress = now
                last_token = progress.token
                record.bootstrap_last_progress_at = now_iso()
            if progress.phase:
                record.bootstrap_phase = progress.phase
            elif not record.bootstrap_phase:
                record.bootstrap_phase = "cloud-init startup"
            phase_changed = record.bootstrap_phase != previous_phase
            inactivity = now - last_progress
            self._persist_provisioning_elapsed(record, overall_started)

            if status == "done":
                if changed:
                    self._log(record.id, f"Cloud init completed ({self._duration_text(elapsed)})")
                return
            if phase_changed:
                self._log(record.id, f"Cloud init: {record.bootstrap_phase} ({self._duration_text(elapsed)})")
                last_log = now
            elif now - last_log >= CLOUD_INIT_PROGRESS_LOG_INTERVAL_SECONDS:
                self._log(
                    record.id,
                    f"Cloud init still active during {record.bootstrap_phase}; "
                    f"latest progress {self._duration_text(inactivity)} ago; "
                    f"elapsed {self._duration_text(elapsed)}",
                )
                last_log = now

            if inactivity >= self.cloud_init_inactivity_timeout:
                raise TerraformError(
                    f"Cloud initialization stalled during {record.bootstrap_phase}; "
                    f"no safe progress for {self._duration_text(inactivity)}. "
                    "Cloud resources may exist and must be destroyed."
                )
            if elapsed >= self.cloud_init_hard_timeout:
                raise TerraformError(
                    f"Cloud initialization is progressing during {record.bootstrap_phase} but exceeded the "
                    f"maximum provisioning time of {self._duration_text(self.cloud_init_hard_timeout)}. "
                    "Cloud resources may exist and must be destroyed."
                )
            if cancel.wait(self.readiness_poll_interval):
                raise TerraformCancelled("Deployment cancelled while waiting for cloud initialization")

    def _wait_for_wireguard(
        self,
        record: DeploymentRecord,
        identity: Path,
        known_hosts: Path,
        user: str,
        options: DeploymentOptions,
        cancel: threading.Event,
        *,
        readiness_marker: str,
        overall_started: float,
    ) -> None:
        assert record.public_ip
        started = time.monotonic()
        last_error = "WireGuard is not ready"
        wireguard_check = (
            f"if ! test -f {readiness_marker}; then echo readiness=marker-missing; exit 1; fi; "
            "if ! systemctl is-active --quiet wg-quick@wg0; "
            "then echo readiness=service-inactive; exit 1; fi; "
            "if ! ip link show wg0 >/dev/null; then echo readiness=interface-missing; exit 1; fi; "
            'if ! test "$(sysctl -n net.ipv4.ip_forward)" = 1; '
            "then echo readiness=forwarding-disabled; exit 1; fi; "
            f"if ! ss -H -lun 'sport = :{options.wireguard_port}' | grep -q .; "
            "then echo readiness=udp-listener-missing; exit 1; fi; "
            "echo readiness=ready"
        )
        while True:
            self._raise_if_cancelled(cancel)
            try:
                args = self._ssh_command(identity, known_hosts, user, record.public_ip, wireguard_check)
                returncode, stdout, stderr = self._run_ssh_probe(args, cancel, allowed_returncodes={0, 1})
                if returncode == 0:
                    self._persist_provisioning_elapsed(record, overall_started)
                    return
                last_error = redact((stderr or stdout).strip())[-500:] or "WireGuard readiness check failed"
            except TerraformCancelled:
                raise
            except SshProbeError as exc:
                if not isinstance(exc, SshTransientError):
                    if isinstance(exc, SshAuthenticationFailure):
                        raise TerraformError(
                            "Deployment SSH identity rejected after bootstrap. "
                            "Cloud resources may exist and must be destroyed."
                        ) from exc
                    raise TerraformError(f"{exc} Cloud resources may exist and must be destroyed.") from exc
                last_error = str(exc)
            elapsed = time.monotonic() - started
            self._persist_provisioning_elapsed(record, overall_started)
            if elapsed >= self.wireguard_readiness_timeout:
                raise TerraformError(
                    f"WireGuard readiness timed out after {self._duration_text(elapsed)}: {last_error}. "
                    "Cloud resources may exist and must be destroyed."
                )
            if cancel.wait(self.readiness_poll_interval):
                raise TerraformCancelled("Deployment cancelled during WireGuard readiness checks")

    def _persist_provisioning_elapsed(self, record: DeploymentRecord, started: float) -> None:
        record.provisioning_elapsed_seconds = max(0, int(time.monotonic() - started))
        self.deployments.save(record)

    def _expected_bootstrap_build(self, record: DeploymentRecord) -> str | None:
        manifest_path = Path(record.runtime_directory) / USER_DATA_MANIFEST
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        build = manifest.get("bootstrap_build") if isinstance(manifest, dict) else None
        return build.lower() if isinstance(build, str) and re.fullmatch(r"[0-9a-fA-F]{12}", build) else None

    @staticmethod
    def _cloud_init_progress(output: str) -> CloudInitProgress:
        phase_match = re.search(r"(?m)^phase=([A-Za-z0-9 /_-]{1,80})$", output)
        build_match = re.search(r"(?m)^build=([0-9a-f]{12})$", output, flags=re.IGNORECASE)
        epoch_match = re.search(r"(?m)^(?:activity|updated)_epoch=([0-9]{1,12})$", output)
        size_match = re.search(r"(?m)^activity_size=([0-9]{1,12})$", output)
        return CloudInitProgress(
            phase_match.group(1).strip() if phase_match else None,
            build_match.group(1).lower() if build_match else None,
            int(epoch_match.group(1)) if epoch_match else None,
            int(size_match.group(1)) if size_match else None,
            bool(re.search(r"(?m)^ready=1$", output)),
        )

    @staticmethod
    def _duration_text(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, remainder = divmod(total, 60)
        return f"{minutes}m{remainder:02d}s" if minutes else f"{remainder}s"

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
            "BatchMode=yes",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
            "-o",
            f"ServerAliveInterval={SSH_SERVER_ALIVE_INTERVAL_SECONDS}",
            "-o",
            f"ServerAliveCountMax={SSH_SERVER_ALIVE_COUNT_MAX}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            f"{user}@{public_ip}",
            remote_command,
        ]

    def _run_ssh_probe(
        self,
        args: list[str],
        cancel: threading.Event,
        *,
        allowed_returncodes: set[int],
        timeout: float = SSH_PROCESS_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        try:
            returncode, stdout, stderr = self._run_cancellable_process(args, cancel, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SshProbeTimeout("Temporary SSH readiness probe timeout.") from exc
        except OSError as exc:
            raise SshTransportFailure("SSH transport is temporarily unavailable.") from exc
        if returncode not in allowed_returncodes:
            raise classify_ssh_failure(returncode, "\n".join((stdout, stderr)))
        return returncode, stdout, stderr

    def _cloud_init_probe_timeout(self, started: float, outage_started: float | None) -> float:
        now = time.monotonic()
        hard_remaining = self.cloud_init_hard_timeout - (now - started)
        if hard_remaining <= 0:
            context = " while SSH monitoring was unavailable" if outage_started is not None else ""
            raise TerraformError(
                "Cloud initialization is progressing but exceeded the maximum provisioning time of "
                f"{self._duration_text(self.cloud_init_hard_timeout)}{context}. "
                "Cloud resources may exist and must be destroyed."
            )
        timeout = min(SSH_PROCESS_TIMEOUT_SECONDS, hard_remaining)
        if outage_started is not None:
            outage_remaining = SSH_PROBE_OUTAGE_ALLOWANCE_SECONDS - (now - outage_started)
            if outage_remaining <= 0:
                raise TerraformError(
                    "Server became unreachable during cloud initialization for "
                    f"{self._duration_text(SSH_PROBE_OUTAGE_ALLOWANCE_SECONDS)}. "
                    "Cloud resources may exist and must be destroyed."
                )
            timeout = min(timeout, outage_remaining)
        return timeout

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
        detailed = re.search(
            r"ephemeral-vpn bootstrap failure: "
            r"phase=([A-Za-z0-9 /_-]{1,80}) "
            r"category=([a-z0-9-]{1,64}) "
            r"exit=([0-9]{1,3}) "
            r"line=([0-9]{1,5}) "
            r"build=([0-9a-f]{12})",
            output,
            flags=re.IGNORECASE,
        )
        if detailed:
            phase, category, exit_status, line, build = detailed.groups()
            return f"{phase.strip()} ({category.lower()}, exit {exit_status}, line {line}, build {build.lower()})"
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
        public_ip: str,
        server_public: str,
        client_private: str,
        tunnel_ipv4: str,
        options: DeploymentOptions,
    ) -> str:
        return "\n".join(
            [
                "[Interface]",
                f"PrivateKey = {client_private}",
                f"Address = {tunnel_ipv4}/32",
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
                self._stage_terraform_configuration(record, include_legacy_tfvars=True)
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
            self._cleanup_exported_configs(record)
            self._cleanup_after_destroy(record, preserve_config=preserve_config)
            record.public_ip = None
            record.resource_ids = {}
            record.last_error = None
            record.legacy_backup_path = None
            message = "Deployment destroyed and sensitive artifacts removed"
            if record.local_cleanup_warnings:
                message = "Deployment destroyed; one or more exported configurations require manual deletion"
            self._transition(record, DeploymentState.DESTROYED, message)
            return {
                "status": "success",
                "deployment_id": deployment_id,
                "local_cleanup_warnings": list(record.local_cleanup_warnings),
            }
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

    def list_client_configs(self, deployment_id: str) -> list[dict[str, object]]:
        record = self.deployments.get(deployment_id)
        self._assert_record_paths(record)
        return [
            {
                "id": client.id,
                "index": client.index,
                "display_name": client.display_name,
                "tunnel_ipv4": client.tunnel_ipv4,
            }
            for client in self._client_metadata(record)
        ]

    def get_client_config(self, deployment_id: str, client_id: str | None = None) -> str:
        return self.client_config_path(deployment_id, client_id).read_text(encoding="utf-8")

    def record_client_export(self, deployment_id: str, client_id: str, path: Path, sha256: str) -> None:
        record = self.deployments.get(deployment_id)
        self._assert_record_paths(record)
        if record.state != DeploymentState.READY:
            raise RuntimeError("Client configurations can be exported only while the deployment is ready")
        source = self.client_config_path(deployment_id, client_id)
        authoritative_sha256 = regular_file_sha256(source)
        if sha256 != authoritative_sha256 or not path.is_absolute():
            raise RuntimeError("Exported configuration provenance could not be verified")
        resolved = path.parent.resolve(strict=True) / path.name
        if regular_file_sha256(resolved) != sha256:
            raise RuntimeError("Exported configuration changed before it could be recorded")
        record.client_exports = [item for item in record.client_exports if Path(item.path) != resolved]
        record.client_exports.append(
            ClientExportMetadata(
                client_id=client_id,
                path=str(resolved),
                sha256=sha256,
                exported_at=now_iso(),
            )
        )
        record.local_cleanup_status = "pending"
        record.local_cleanup_warnings = []
        self.deployments.save(record)

    def client_config_path(self, deployment_id: str, client_id: str | None = None) -> Path:
        record = self.deployments.get(deployment_id)
        self._assert_record_paths(record)
        clients = self._client_metadata(record)
        selected_id = client_id or clients[0].id
        matches = [client for client in clients if client.id == selected_id]
        if len(matches) != 1:
            raise RuntimeError("Unknown deployment client")
        path = resolve_client_path(Path(record.runtime_directory), matches[0].config_relative_path)
        if record.state != DeploymentState.READY and not path.is_file():
            raise RuntimeError("Client configuration is not ready")
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Runtime client configuration is missing or unsafe")
        return path

    @staticmethod
    def _client_metadata(record: DeploymentRecord) -> list[ClientMetadata]:
        if not record.clients:
            return [
                ClientMetadata(
                    id="client-1",
                    index=1,
                    display_name="Client 1",
                    tunnel_ipv4="10.8.0.2",
                    config_relative_path="client.conf",
                    private_key_relative_path="client.privatekey",
                )
            ]
        ids = {client.id for client in record.clients}
        indexes = {client.index for client in record.clients}
        addresses = {client.tunnel_ipv4 for client in record.clients}
        if not (
            len(ids) == len(record.clients)
            and len(indexes) == len(record.clients)
            and len(addresses) == len(record.clients)
        ):
            raise RuntimeError("Deployment client metadata is ambiguous")
        ordered = sorted(record.clients, key=lambda client: client.index)
        validate_client_count(len(ordered))
        for expected_index, client in enumerate(ordered, start=1):
            if (
                client.index != expected_index
                or client.id != f"client-{expected_index}"
                or client.tunnel_ipv4 != client_tunnel_ipv4(expected_index)
            ):
                raise RuntimeError("Deployment client metadata is invalid")
            resolve_client_path(Path(record.runtime_directory), client.config_relative_path)
            resolve_client_path(Path(record.runtime_directory), client.private_key_relative_path)
        return ordered

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
                    "source_kind": report["source_kind"],
                    "source_path": report["source_path"],
                    "source_sha256": report["source_sha256"],
                    "source_lineage": report["source_lineage"],
                    "source_serial": report["source_serial"],
                    "primary_sha256": report["primary_sha256"],
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

    def verify_legacy_cloud_state(self, provider_id: str) -> dict[str, object]:
        directory = self._legacy_provider_directory(provider_id)
        with self._provider_operation_lock(provider_id, directory):
            report = self._inspect_legacy_provider(provider_id)
            if not report["cloud_verification_available"]:
                raise TerraformError("Legacy state is not eligible for read-only cloud verification")
            source = Path(str(report["source_path"]))
            before = self._legacy_state_fingerprints(report)
            result = self.legacy_cloud_verifier.verify(provider_id, source, directory)
            refreshed = self._inspect_legacy_provider(provider_id)
            if self._legacy_state_fingerprints(refreshed) != before:
                raise TerraformError("Legacy state changed during cloud verification; verify it again")
            receipt = {
                "version": 1,
                "provider_id": provider_id,
                "status": result.get("status"),
                "message": result.get("message"),
                "verified_at": now_iso(),
                "source_kind": report["source_kind"],
                "source_path": report["source_path"],
                "source_sha256": report["source_sha256"],
                "terraform_lineage": report["source_lineage"],
                "terraform_serial": report["source_serial"],
                "primary_sha256": report["primary_sha256"],
                "backup_sha256": report["backup_sha256"],
                "resources": result.get("resources", []),
            }
            self.legacy_verifications.write(provider_id, receipt)
            verified = self._inspect_legacy_provider(provider_id)
            return {
                "status": str(result.get("status")),
                "provider_id": provider_id,
                "message": str(result.get("message")),
                "resources": result.get("resources", []),
                "legacy_state": verified,
            }

    def reconcile_stale_legacy_state(self, provider_id: str, confirmed: bool) -> dict[str, object]:
        if confirmed is not True:
            raise TerraformError("Explicit confirmation of independently verified cloud absence is required")
        directory = self._legacy_provider_directory(provider_id)
        with self._legacy_snapshot_lock:
            displayed = self._legacy_confirmation_snapshots.pop(provider_id, None)
        if displayed is None:
            raise TerraformError("Legacy state must be refreshed and reviewed before reconciliation")

        with self._provider_operation_lock(provider_id, directory):
            report = self._inspect_legacy_provider(provider_id)
            if not report["stale_reconciliation_available"]:
                raise TerraformError("Legacy state is not eligible for stale-state reconciliation")
            current = {
                "source_kind": report["source_kind"],
                "source_path": report["source_path"],
                "source_sha256": report["source_sha256"],
                "source_lineage": report["source_lineage"],
                "source_serial": report["source_serial"],
                "primary_sha256": report["primary_sha256"],
                "backup_sha256": report["backup_sha256"],
            }
            if current != displayed:
                raise TerraformError("Legacy state changed after it was displayed; refresh and review it again")

            cloud_verification: dict[str, Any] | None = None
            if report["classification"] == "ambiguous":
                cloud_verification, verification_reason = self._verified_cloud_absence(report)
                if cloud_verification is None:
                    raise TerraformError(f"Cloud-absence verification is invalid: {verification_reason}")

            source = Path(str(report["source_path"]))
            primary = Path(str(report["primary_path"]))
            backup = Path(str(report["backup_path"]))
            source_snapshot = self._source_state_snapshot(directory)
            fingerprint = str(report["source_sha256"])
            if source_snapshot.get(source.name) != fingerprint:
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
                for original in (primary, backup):
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
                "version": 2,
                "provider_id": provider_id,
                "source_kind": report["source_kind"],
                "source_path": str(source.resolve()),
                "source_sha256": fingerprint,
                "primary_sha256": report["primary_sha256"],
                "backup_sha256": report["backup_sha256"],
                "terraform_lineage": report["source_lineage"],
                "terraform_serial": report["source_serial"],
                "resource_count": report["source_resources"],
                "resources": report["resource_summary"],
                "outputs": report["outputs_summary"],
                "reconciled_at": now_iso(),
                "reason": "cloud_absence_confirmed",
                "quarantine_path": str(quarantine),
                "quarantine_files": quarantine_files,
                "status": "reconciled-stale",
            }
            if cloud_verification is not None:
                receipt["cloud_verification"] = {
                    "verified_at": cloud_verification["verified_at"],
                    "source_sha256": cloud_verification["source_sha256"],
                    "status": "all_absent",
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
        directory = self._legacy_provider_directory(provider_id)
        primary = directory / "terraform.tfstate"
        backup = directory / "terraform.tfstate.backup"
        primary_info = self._inspect_state_file(primary)
        backup_info = self._inspect_state_file(backup)
        receipt, receipt_error = self.legacy_reconciliations.read(provider_id)
        classification = "none"
        reason = "No provider-root state files detected"
        migration_available = False
        stale_reconciliation_available = False
        cloud_verification_available = False
        cloud_verification: dict[str, object] | None = None
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
        source_kind = "backup" if classification == "ambiguous" else "primary"
        source = backup if source_kind == "backup" else primary
        source_info = backup_info if source_kind == "backup" else primary_info
        fingerprint = source_info.get("sha256")
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

        if classification in {"active", "ambiguous"} and isinstance(fingerprint, str):
            if receipt_error:
                reason = f"Stale-state reconciliation is invalid: {receipt_error}"
            elif receipt is not None:
                valid, receipt_reason, receipt_details = self._verify_stale_receipt(
                    provider_id,
                    source_kind,
                    source,
                    primary,
                    backup,
                    source_info,
                    primary_info,
                    backup_info,
                    receipt,
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
            identity_complete = (
                isinstance(source_info.get("lineage"), str)
                and bool(source_info.get("lineage"))
                and isinstance(source_info.get("serial"), int)
                and not isinstance(source_info.get("serial"), bool)
            )
            if classification == "active":
                stale_reconciliation_available = bool(not migration_available and identity_complete)
            elif classification == "ambiguous":
                cloud_verification_available = bool(identity_complete)
                verification_report = {
                    "provider_id": provider_id,
                    "source_kind": source_kind,
                    "source_path": str(source),
                    "source_sha256": source_info.get("sha256"),
                    "source_lineage": source_info.get("lineage"),
                    "source_serial": source_info.get("serial"),
                    "primary_sha256": primary_info.get("sha256"),
                    "backup_sha256": backup_info.get("sha256"),
                }
                verification, verification_reason = self._cloud_verification(verification_report)
                if verification is not None:
                    cloud_verification = {
                        "status": verification.get("status"),
                        "message": verification.get("message"),
                        "verified_at": verification.get("verified_at"),
                        "resources": verification.get("resources", []),
                    }
                    stale_reconciliation_available = verification.get("status") == "all_absent"
                elif verification_reason:
                    cloud_verification = {"status": "invalid", "message": verification_reason}
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
            "cloud_verification_available": cloud_verification_available,
            "cloud_verification": cloud_verification,
            "source_kind": source_kind,
            "source_path": str(source),
            "source_sha256": source_info.get("sha256"),
            "source_resources": source_info.get("resources", 0),
            "source_lineage": source_info.get("lineage"),
            "source_serial": source_info.get("serial"),
            "primary_path": str(primary),
            "primary_exists": primary_info["exists"],
            "primary_parseable": primary_info["parseable"],
            "primary_sha256": primary_info.get("sha256"),
            "primary_resources": primary_info["resources"],
            "primary_lineage": primary_info.get("lineage"),
            "primary_serial": primary_info.get("serial"),
            "resource_summary": source_info.get("resource_summary", []),
            "outputs_summary": source_info.get("outputs_summary", []),
            "backup_path": str(backup),
            "backup_exists": backup_info["exists"],
            "backup_parseable": backup_info["parseable"],
            "backup_sha256": backup_info.get("sha256"),
            "backup_resources": backup_info["resources"],
            "backup_lineage": backup_info.get("lineage"),
            "backup_serial": backup_info.get("serial"),
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

    def _cloud_verification(self, report: dict[str, object]) -> tuple[dict[str, Any] | None, str | None]:
        provider_id = str(report.get("provider_id", ""))
        receipt, error = self.legacy_verifications.read(provider_id)
        if error:
            return None, error
        if receipt is None:
            return None, None
        expected = {
            "provider_id": provider_id,
            "source_kind": report.get("source_kind"),
            "source_path": report.get("source_path"),
            "source_sha256": report.get("source_sha256"),
            "terraform_lineage": report.get("source_lineage"),
            "terraform_serial": report.get("source_serial"),
            "primary_sha256": report.get("primary_sha256"),
            "backup_sha256": report.get("backup_sha256"),
        }
        if receipt.get("version") != 1 or any(receipt.get(key) != value for key, value in expected.items()):
            return None, "Cloud verification no longer matches the exact legacy state fingerprint."
        verified_at = receipt.get("verified_at")
        if not isinstance(verified_at, str):
            return None, "Cloud verification timestamp is missing."
        try:
            timestamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except ValueError:
            return None, "Cloud verification timestamp is malformed."
        if timestamp.tzinfo is None:
            return None, "Cloud verification timestamp has no timezone."
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > LEGACY_VERIFICATION_MAX_AGE_SECONDS:
            return None, "Cloud verification expired; run it again before reconciliation."
        resources = receipt.get("resources")
        if not isinstance(resources, list):
            return None, "Cloud verification evidence is malformed."
        return receipt, None

    def _verified_cloud_absence(self, report: dict[str, object]) -> tuple[dict[str, Any] | None, str | None]:
        receipt, error = self._cloud_verification(report)
        if receipt is None:
            return None, error
        if receipt.get("status") != "all_absent":
            return None, "Cloud verification did not confirm that every exact resource is absent."
        if any(
            not isinstance(item, dict) or item.get("status") not in {"absent", "local_only"}
            for item in receipt["resources"]
        ):
            return None, "Cloud verification evidence contains an unresolved resource."
        return receipt, None

    @staticmethod
    def _legacy_state_fingerprints(report: dict[str, object]) -> dict[str, object]:
        return {
            "source_kind": report.get("source_kind"),
            "source_path": report.get("source_path"),
            "source_sha256": report.get("source_sha256"),
            "source_lineage": report.get("source_lineage"),
            "source_serial": report.get("source_serial"),
            "primary_sha256": report.get("primary_sha256"),
            "backup_sha256": report.get("backup_sha256"),
        }

    def _verify_stale_receipt(
        self,
        provider_id: str,
        source_kind: str,
        source: Path,
        primary: Path,
        backup: Path,
        source_info: dict[str, object],
        primary_info: dict[str, object],
        backup_info: dict[str, object],
        receipt: dict[str, Any],
    ) -> tuple[bool, str, dict[str, object] | None]:
        version = receipt.get("version")
        if version not in {1, 2}:
            return False, "unsupported receipt version", None
        if receipt.get("provider_id") != provider_id:
            return False, "receipt belongs to another provider", None
        if receipt.get("status") != "reconciled-stale":
            return False, "receipt status is not reconciled-stale", None
        if receipt.get("reason") != "cloud_absence_confirmed":
            return False, "receipt reason is invalid", None
        if receipt.get("source_path") != str(source.resolve()):
            return False, "source path differs from the receipt", None
        if receipt.get("source_sha256") != source_info.get("sha256"):
            return False, "source fingerprint differs from the receipt", None
        if version == 1:
            if source_kind != "primary":
                return False, "version 1 receipt cannot reconcile a resource-bearing backup", None
            if receipt.get("source_backup_sha256") != backup_info.get("sha256"):
                return False, "provider-root backup fingerprint differs from the receipt", None
        else:
            if receipt.get("source_kind") != source_kind:
                return False, "resource-bearing state location differs from the receipt", None
            if receipt.get("primary_sha256") != primary_info.get("sha256"):
                return False, "provider-root primary fingerprint differs from the receipt", None
            if receipt.get("backup_sha256") != backup_info.get("sha256"):
                return False, "provider-root backup fingerprint differs from the receipt", None
            if source_kind == "backup":
                verification = receipt.get("cloud_verification")
                if (
                    not isinstance(verification, dict)
                    or verification.get("status") != "all_absent"
                    or verification.get("source_sha256") != source_info.get("sha256")
                ):
                    return False, "cloud-absence verification proof is missing or mismatched", None
        if receipt.get("terraform_lineage") != source_info.get("lineage"):
            return False, "Terraform lineage differs from the receipt", None
        if receipt.get("terraform_serial") != source_info.get("serial"):
            return False, "Terraform serial differs from the receipt", None
        if receipt.get("resource_count") != source_info.get("resources"):
            return False, "managed resource count differs from the receipt", None
        if receipt.get("resources") != source_info.get("resource_summary"):
            return False, "managed resource summary differs from the receipt", None
        if receipt.get("outputs") != source_info.get("outputs_summary"):
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
        expected: dict[str, object] = {}
        if primary_info.get("sha256") is not None:
            expected[primary.name] = primary_info.get("sha256")
        if backup_info.get("sha256") is not None:
            expected[backup.name] = backup_info.get("sha256")
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
            "fingerprint": str(source_info.get("sha256", "")),
            "quarantine_path": str(quarantine),
            "resource_count": source_info.get("resources", 0),
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

    def _legacy_provider_directory(self, provider_id: str) -> Path:
        recorded = self.legacy_sources.get(provider_id)
        if recorded is not None:
            return recorded
        current = self._provider_directory(provider_id)
        if any((current / name).is_file() for name in LEGACY_STATE_NAMES):
            self.legacy_sources.remember(provider_id, current)
        return current

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
        if runtime != (self.runtime_root / record.id).resolve():
            raise TerraformError("Deployment runtime identity differs from its registry ID")
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

    def _stage_terraform_configuration(
        self,
        record: DeploymentRecord,
        *,
        include_legacy_tfvars: bool = False,
    ) -> Path:
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
        if include_legacy_tfvars and legacy_tfvars.is_file():
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

    def _write_user_data_manifest(self, record: DeploymentRecord, common_root: Path, payload: str) -> None:
        """Persist non-secret proof of the exact staged source and Terraform payload."""
        runtime = Path(record.runtime_directory).resolve()
        self._assert_scoped_path(runtime, Path(record.state_path).resolve())
        manifest_path = (runtime / USER_DATA_MANIFEST).resolve()
        if manifest_path.parent != runtime or manifest_path.exists():
            raise TerraformError("Deployment user-data manifest already exists or is unsafe")
        self._verify_terraform_work_manifest(record)
        source_sha256 = bootstrap_source_sha256(common_root)
        build = source_sha256[:12]
        if f"BOOTSTRAP_BUILD={build}" not in payload:
            raise TerraformError("Rendered user-data does not match the staged bootstrap source")
        payload_bytes = payload.encode("utf-8")
        manifest = {
            "version": 1,
            "provider_id": record.provider_id,
            "bootstrap_source_sha256": source_sha256,
            "bootstrap_build": build,
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "payload_byte_length": len(payload_bytes),
            "transport": (
                "lightsail-posix-to-bash-trampoline"
                if record.provider_id == "aws-lightsail"
                else "cloud-config-shared-bootstrap"
            ),
            "created_at": now_iso(),
        }
        temporary = manifest_path.with_suffix(".tmp")
        write_secret(temporary, json.dumps(manifest, indent=2, sort_keys=True))
        temporary.replace(manifest_path)
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        if persisted != manifest:
            raise TerraformError("Deployment user-data manifest verification failed")

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
        clients_root = runtime / "clients"
        if clients_root.is_dir():
            if preserve_config:
                for private_key in clients_root.rglob("*.privatekey"):
                    if private_key.is_file():
                        private_key.unlink()
            else:
                shutil.rmtree(clients_root)
        terraform_data = runtime / ".terraform"
        if terraform_data.is_dir():
            shutil.rmtree(terraform_data)
        terraform_work = runtime / TERRAFORM_WORK_ROOT
        if terraform_work.is_dir():
            shutil.rmtree(terraform_work)
        (runtime / TERRAFORM_WORK_MANIFEST).unlink(missing_ok=True)

    def _cleanup_exported_configs(self, record: DeploymentRecord) -> None:
        """Best-effort cleanup bound to exact client content and recorded destinations."""
        if not record.client_exports:
            record.local_cleanup_status = "not_required"
            record.local_cleanup_warnings = []
            return
        warnings: list[str] = []
        authoritative: dict[str, str] = {}
        cleaned_at = now_iso()
        for exported in record.client_exports:
            if exported.cleanup_status in {"deleted", "missing"}:
                continue
            try:
                if exported.client_id not in authoritative:
                    source = self.client_config_path(record.id, exported.client_id)
                    authoritative[exported.client_id] = regular_file_sha256(source)
                result = remove_tracked_config(
                    Path(exported.path),
                    exported.sha256,
                    authoritative[exported.client_id],
                )
                exported.cleanup_status = result
                exported.cleaned_at = cleaned_at
            except (ConfigExportError, OSError, RuntimeError) as exc:
                exported.cleanup_status = "warning"
                exported.cleaned_at = cleaned_at
                safe_path = redact(exported.path).replace("\r", "?").replace("\n", "?")
                warnings.append(f"Delete exported VPN configuration manually: {safe_path} ({redact(str(exc))})")
        record.local_cleanup_warnings = warnings
        record.local_cleanup_status = "warning" if warnings else "complete"

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
