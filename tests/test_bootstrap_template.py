from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import orchestrator as orchestrator_module
import pytest
import yaml
from cloud_init import (
    LIGHTSAIL_BASH_TRAMPOLINE,
    LIGHTSAIL_BOOTSTRAP_BOUNDARY,
    UserDataValidationError,
    bootstrap_source_sha256,
    render_user_data,
    validate_user_data_payload,
)
from helpers import FakeTerraformRunner, make_orchestrator

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "terraform-common"
SERVER_PRIVATE = base64.b64encode(bytes(range(32))).decode("ascii")
CLIENT_PUBLIC = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")
CLIENT_PEERS: list[dict[str, object]] = [{"id": "client-1", "public_key": CLIENT_PUBLIC, "tunnel_ipv4": "10.8.0.2"}]
CLIENT_PUBLIC_2 = base64.b64encode(bytes((value + 1) % 256 for value in reversed(range(32)))).decode("ascii")


def rendered(provider_id: str) -> str:
    return render_user_data(
        provider_id,
        COMMON,
        wireguard_port=51820,
        server_private_key=SERVER_PRIVATE,
        client_peers=CLIENT_PEERS,
    )


def bootstrap_from_payload(provider_id: str, payload: str) -> str:
    if provider_id == "aws-lightsail":
        assert payload.startswith(LIGHTSAIL_BASH_TRAMPOLINE)
        assert payload.endswith(LIGHTSAIL_BOOTSTRAP_BOUNDARY + "\n")
        return payload[len(LIGHTSAIL_BASH_TRAMPOLINE) : -len(LIGHTSAIL_BOOTSTRAP_BOUNDARY + "\n")]
    document = yaml.safe_load(payload)
    return next(item["content"] for item in document["write_files"] if item["path"].endswith("bootstrap"))


def simulate_ssh_phase(
    bootstrap: str,
    load_states: dict[str, list[str]],
    *,
    restart_succeeds: bool = True,
) -> tuple[str | None, list[str], int, bool, bool]:
    """Exercise the rendered resolver's declared candidates and bounded attempts."""
    candidates_match = re.search(r"for candidate in ([^;]+); do", bootstrap)
    attempts_match = re.search(r"for attempt in ([^;]+); do", bootstrap)
    assert candidates_match and attempts_match
    candidates = candidates_match.group(1).split()
    attempts = attempts_match.group(1).split()
    selected = None
    probes = 0
    for attempt_index, _attempt in enumerate(attempts):
        for candidate in candidates:
            values = load_states[candidate]
            state = values[min(attempt_index, len(values) - 1)]
            probes += 1
            if state == "loaded":
                selected = candidate
                break
        if selected:
            break
    restarts = [selected] if selected else []
    ssh_ready = bool(selected and restart_succeeds)
    return selected, restarts, probes, ssh_ready, ssh_ready


def simulate_prerequisite_phase(
    bootstrap: str,
    *,
    apt_status: int,
    needrestart_present: bool,
    needrestart_would_restart_sshd: bool,
) -> tuple[bool, bool, bool]:
    """Model the APT hook boundary declared by the final rendered script."""
    hook_suspended = "NEEDRESTART_SUSPEND=1 apt-get install -y iptables wireguard" in bootstrap
    hidden_restart_attempted = needrestart_present and needrestart_would_restart_sshd and not hook_suspended
    package_succeeded = apt_status == 0 and not hidden_restart_attempted
    ssh_phase_entered = package_succeeded
    readiness_marker_created = False
    return hidden_restart_attempted, ssh_phase_entered, readiness_marker_created


def test_aws_lightsail_receives_an_explicit_shell_launch_script() -> None:
    payload = rendered("aws-lightsail")
    bootstrap = bootstrap_from_payload("aws-lightsail", payload)
    assert payload.encode("utf-8").startswith(b"#!/bin/sh\n")
    assert "exec /usr/bin/env bash -s -- <<'EPHEMERAL_VPN_BOOTSTRAP'\n" in payload
    assert bootstrap.encode("utf-8").startswith(b"#!/usr/bin/env bash\nset -Eeuo pipefail\n")
    assert "#cloud-config" not in payload
    assert "\npackage_update:" not in payload
    assert "\nwrite_files:" not in payload
    assert "\nruncmd:" not in payload


@pytest.mark.parametrize("provider_id", ["digitalocean", "scaleway"])
def test_cloud_config_providers_receive_valid_yaml_with_bootstrap(provider_id: str) -> None:
    payload = rendered(provider_id)
    assert payload.encode("utf-8").startswith(b"#cloud-config\nwrite_files:\n")
    document = yaml.safe_load(payload)
    assert set(("write_files", "runcmd")).issubset(document)
    assert "packages" not in document
    assert "package_update" not in document
    script = next(item["content"] for item in document["write_files"] if item["path"].endswith("bootstrap"))
    assert script.startswith("#!/usr/bin/env bash\n")
    assert document["runcmd"] == [["/usr/local/sbin/ephemeral-vpn-bootstrap"]]


@pytest.mark.parametrize("provider_id", ["aws-lightsail", "digitalocean", "scaleway"])
def test_final_payload_has_literal_shell_expansion_and_complete_wireguard_bootstrap(provider_id: str) -> None:
    payload = rendered(provider_id)
    assert 'systemctl restart "${ssh_service}"' in payload
    assert 'systemctl is-active --quiet "${ssh_service}"' in payload
    assert "for candidate in ssh.service sshd.service; do" in payload
    assert "systemctl daemon-reload" in payload
    assert 'systemctl show --property=LoadState --value "${candidate}"' in payload
    assert "systemctl cat" not in payload
    assert "NEEDRESTART_SUSPEND=1 apt-get install -y iptables wireguard" in payload
    assert "NEEDRESTART_MODE=" not in payload
    assert "$$" not in payload
    assert "$${ssh_service}" not in payload
    assert "@@" not in payload
    for required in (
        "apt-get install -y iptables wireguard",
        "[Interface]",
        "ListenPort = 51820",
        f"PrivateKey = {SERVER_PRIVATE}",
        "PostUp = OUTBOUND_IFACE=",
        "[Peer]",
        f"PublicKey = {CLIENT_PUBLIC}",
        "AllowedIPs = 10.8.0.2/32",
        "sysctl --system",
        "systemctl enable --now wg-quick@wg0",
        "/var/lib/ephemeral-vpn/ready",
        "/var/lib/ephemeral-vpn/bootstrap-status",
        "record_bootstrap_status",
        "updated_epoch=%s",
    ):
        assert required in payload
    assert payload.index("systemctl is-active --quiet wg-quick@wg0") < payload.index('touch "${READY_MARKER}"')


@pytest.mark.parametrize("provider_id", ["aws-lightsail", "digitalocean", "scaleway"])
def test_final_payload_contains_one_server_peer_block_per_client(provider_id: str) -> None:
    payload = render_user_data(
        provider_id,
        COMMON,
        wireguard_port=51820,
        server_private_key=SERVER_PRIVATE,
        client_peers=[
            {"id": "client-1", "public_key": CLIENT_PUBLIC, "tunnel_ipv4": "10.8.0.2"},
            {"id": "client-2", "public_key": CLIENT_PUBLIC_2, "tunnel_ipv4": "10.8.0.3"},
        ],
    )
    bootstrap = bootstrap_from_payload(provider_id, payload)
    assert bootstrap.count("[Peer]") == 2
    assert f"PublicKey = {CLIENT_PUBLIC}\nAllowedIPs = 10.8.0.2/32" in bootstrap
    assert f"PublicKey = {CLIENT_PUBLIC_2}\nAllowedIPs = 10.8.0.3/32" in bootstrap


@pytest.mark.parametrize(
    "peers",
    [
        [],
        [
            {"id": "client-1", "public_key": CLIENT_PUBLIC, "tunnel_ipv4": "10.8.0.2"},
            {"id": "client-2", "public_key": CLIENT_PUBLIC, "tunnel_ipv4": "10.8.0.3"},
        ],
        [{"id": "client-1", "public_key": CLIENT_PUBLIC, "tunnel_ipv4": "10.8.0.3"}],
    ],
)
def test_render_rejects_missing_duplicate_or_non_deterministic_peers(peers: list[dict[str, object]]) -> None:
    with pytest.raises(UserDataValidationError):
        render_user_data(
            "aws-lightsail",
            COMMON,
            wireguard_port=51820,
            server_private_key=SERVER_PRIVATE,
            client_peers=peers,
        )


@pytest.mark.parametrize("provider_id", ["aws-lightsail", "digitalocean", "scaleway"])
def test_safe_status_marker_tracks_shared_bootstrap_phases_without_secrets(provider_id: str) -> None:
    bootstrap = bootstrap_from_payload(provider_id, rendered(provider_id))
    assert bootstrap.count("record_bootstrap_status") == 7
    status_function = bootstrap[
        bootstrap.index("record_bootstrap_status() {") : bootstrap.index("trap 'record_bootstrap_failure")
    ]
    assert "build=%s\\nphase=%s\\nupdated_epoch=%s\\n" in status_function
    assert "SERVER_PRIVATE_KEY" not in status_function
    assert "CLIENT_PUBLIC_KEY" not in status_function
    assert "PrivateKey" not in status_function


@pytest.mark.parametrize("provider_id", ["aws-lightsail", "digitalocean", "scaleway"])
def test_bootstrap_preserves_provider_managed_ssh_authorization(provider_id: str) -> None:
    bootstrap = bootstrap_from_payload(provider_id, rendered(provider_id))
    assert "TrustedUserCAKeys" not in bootstrap
    assert "AuthorizedKeysFile" not in bootstrap
    assert "authorized_keys" not in bootstrap
    assert "lightsail_instance_ca" not in bootstrap
    assert "/home/ubuntu/.ssh" not in bootstrap


@pytest.mark.parametrize("provider_id", ["aws-lightsail", "digitalocean", "scaleway"])
def test_needrestart_hook_is_suspended_at_the_shared_package_boundary(provider_id: str) -> None:
    bootstrap = bootstrap_from_payload(provider_id, rendered(provider_id))
    hidden_restart, ssh_entered, marker_created = simulate_prerequisite_phase(
        bootstrap,
        apt_status=0,
        needrestart_present=True,
        needrestart_would_restart_sshd=True,
    )
    assert not hidden_restart
    assert ssh_entered
    assert not marker_created
    assert "ephemeral-vpn bootstrap: package installation completed" in bootstrap
    assert bootstrap.index('phase="prerequisite installation"') < bootstrap.index('phase="SSH hardening/configuration"')


def test_real_package_failure_remains_fatal_before_ssh_and_readiness() -> None:
    bootstrap = bootstrap_from_payload("aws-lightsail", rendered("aws-lightsail"))
    hidden_restart, ssh_entered, marker_created = simulate_prerequisite_phase(
        bootstrap,
        apt_status=100,
        needrestart_present=True,
        needrestart_would_restart_sshd=False,
    )
    assert not hidden_restart
    assert not ssh_entered
    assert not marker_created
    assert "apt-get update failed (exit ${package_status})" in bootstrap
    assert "package installation failed (apt-get exit ${package_status})" in bootstrap
    assert "inspect preceding apt/dpkg output" in bootstrap
    assert 'record_bootstrap_failure "${package_status}" "${LINENO}"' in bootstrap


def test_lightsail_provider_prefix_cannot_force_shared_bootstrap_to_run_under_dash() -> None:
    payload = rendered("aws-lightsail")
    provider_prefix = "#!/bin/sh\nservice sshd restart\necho provider-prefix-complete\n"
    executed = provider_prefix + payload
    assert executed.startswith("#!/bin/sh\n")
    trampoline = executed.index("exec /usr/bin/env bash -s --")
    strict_mode = executed.index("set -Eeuo pipefail")
    assert trampoline < strict_mode
    assert executed.count("exec /usr/bin/env bash -s --") == 1


def test_rendered_phase_boundaries_precede_wireguard_and_readiness() -> None:
    bootstrap = bootstrap_from_payload("aws-lightsail", rendered("aws-lightsail"))
    ordered = (
        'phase="prerequisite installation"',
        "NEEDRESTART_SUSPEND=1 apt-get install -y iptables wireguard",
        'phase="SSH hardening/configuration"',
        'systemctl restart "${ssh_service}"',
        'phase="WireGuard configuration"',
        'touch "${READY_MARKER}"',
    )
    offsets = [bootstrap.index(item) for item in ordered]
    assert offsets == sorted(offsets)


@pytest.mark.parametrize(
    ("load_states", "expected"),
    [
        ({"ssh.service": ["loaded"], "sshd.service": ["not-found"]}, "ssh.service"),
        ({"ssh.service": ["not-found"], "sshd.service": ["loaded"]}, "sshd.service"),
    ],
)
def test_final_rendered_payload_selects_loaded_ssh_unit(load_states: dict[str, list[str]], expected: str) -> None:
    bootstrap = bootstrap_from_payload("aws-lightsail", rendered("aws-lightsail"))
    selected, restarts, _probes, wireguard_entered, marker_created = simulate_ssh_phase(bootstrap, load_states)
    assert selected == expected
    assert restarts == [expected]
    assert wireguard_entered
    assert marker_created


def test_ssh_resolution_fails_before_wireguard_when_no_unit_is_loaded() -> None:
    bootstrap = bootstrap_from_payload("aws-lightsail", rendered("aws-lightsail"))
    selected, restarts, probes, wireguard_entered, marker_created = simulate_ssh_phase(
        bootstrap,
        {"ssh.service": ["not-found"], "sshd.service": ["not-found"]},
    )
    assert selected is None
    assert restarts == []
    assert probes == 10
    assert not wireguard_entered
    assert not marker_created
    assert "ephemeral-vpn bootstrap: no loaded OpenSSH systemd service found" in bootstrap


def test_ssh_resolution_retries_temporary_boot_visibility_without_nonexistent_fallback() -> None:
    bootstrap = bootstrap_from_payload("aws-lightsail", rendered("aws-lightsail"))
    selected, restarts, probes, wireguard_entered, marker_created = simulate_ssh_phase(
        bootstrap,
        {"ssh.service": ["not-found", "loaded"], "sshd.service": ["not-found"]},
    )
    assert selected == "ssh.service"
    assert restarts == ["ssh.service"]
    assert probes == 3
    assert wireguard_entered
    assert marker_created
    assert "for attempt in 1 2 3 4 5; do" in bootstrap


def test_selected_ssh_restart_failure_does_not_fall_back_or_reach_wireguard() -> None:
    bootstrap = bootstrap_from_payload("aws-lightsail", rendered("aws-lightsail"))
    selected, restarts, _probes, wireguard_entered, marker_created = simulate_ssh_phase(
        bootstrap,
        {"ssh.service": ["loaded"], "sshd.service": ["loaded"]},
        restart_succeeds=False,
    )
    assert selected == "ssh.service"
    assert restarts == ["ssh.service"]
    assert not wireguard_entered
    assert not marker_created
    assert bootstrap.count('systemctl restart "${ssh_service}"') == 1


@pytest.mark.parametrize("provider_id", ["aws-lightsail", "digitalocean", "scaleway"])
def test_rendered_payload_has_one_portable_ssh_resolution_path(provider_id: str) -> None:
    payload = rendered(provider_id)
    bootstrap = bootstrap_from_payload(provider_id, payload)
    validate_user_data_payload(provider_id, payload)
    assert "resolve_ssh_service() {" in bootstrap
    assert 'systemctl show --property=LoadState --value "${candidate}"' in bootstrap
    assert bootstrap.count('systemctl restart "${ssh_service}"') == 1
    assert not re.search(r"systemctl\s+restart\s+['\"]?(?:ssh|sshd)\.service", bootstrap)
    assert "systemctl cat" not in bootstrap
    assert "$$" not in bootstrap


@pytest.mark.parametrize(
    "payload",
    [
        "#!/bin/sh\n#cloud-config\npackages: []\nwrite_files: []\nruncmd: []\n",
        "#cloud-config\npackages: [\n",
        "#cloud-config\npackages: []\nwrite_files: []\nruncmd: []\n",
    ],
)
def test_cloud_config_validation_rejects_mixed_malformed_or_incomplete_payload(payload: str) -> None:
    with pytest.raises(UserDataValidationError):
        validate_user_data_payload("digitalocean", payload)


def test_cloud_config_rejects_package_installation_outside_shared_bootstrap() -> None:
    payload = rendered("digitalocean").replace("#cloud-config\n", "#cloud-config\npackages: [wireguard]\n", 1)
    with pytest.raises(UserDataValidationError, match="delegate package installation"):
        validate_user_data_payload("digitalocean", payload)


def test_lightsail_validation_rejects_cloud_config_directives_inside_launch_script() -> None:
    payload = rendered("aws-lightsail") + "package_update: true\n"
    with pytest.raises(UserDataValidationError, match="cloud-config YAML directives"):
        validate_user_data_payload("aws-lightsail", payload)


@pytest.mark.parametrize(
    ("provider_id", "location_id", "expected_prefix"),
    [
        ("aws-lightsail", "us-east-1", "#!/bin/sh\n"),
        ("digitalocean", "nyc3", "#cloud-config\n"),
        ("scaleway", "fr-par-1", "#cloud-config\n"),
    ],
)
def test_orchestrator_passes_the_validated_rendered_payload_to_terraform(
    tmp_path: Path, provider_id: str, location_id: str, expected_prefix: str
) -> None:
    runner = FakeTerraformRunner()
    orchestrator = make_orchestrator(tmp_path, runner)
    result = orchestrator.plan(provider_id, location_id)
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    variables = json.loads(Path(record.runtime_directory, "deployment.auto.tfvars.json").read_text(encoding="utf-8"))
    payload = variables["user_data_payload"]
    assert isinstance(payload, str)
    assert payload.startswith(expected_prefix)
    validate_user_data_payload(provider_id, payload)
    manifest = json.loads(Path(record.runtime_directory, "user-data-manifest.json").read_text(encoding="utf-8"))
    assert manifest["payload_sha256"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert manifest["payload_byte_length"] == len(payload.encode("utf-8"))
    assert manifest["bootstrap_source_sha256"] == bootstrap_source_sha256(
        Path(record.runtime_directory, "terraform-work", "terraform-common")
    )
    assert f"BOOTSTRAP_BUILD={manifest['bootstrap_build']}" in payload
    assert SERVER_PRIVATE not in json.dumps(manifest)
    assert CLIENT_PUBLIC not in json.dumps(manifest)
    assert any(call[0][0] == "plan" for call in runner.calls)
    assert all(call[0][0] != "apply" for call in runner.calls)


def test_invalid_rendered_payload_fails_before_plan_or_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeTerraformRunner()
    orchestrator = make_orchestrator(tmp_path, runner)

    def invalid(*args: object, **kwargs: object) -> str:
        raise UserDataValidationError("Rendered user-data failed structural validation")

    monkeypatch.setattr(orchestrator_module, "render_user_data", invalid)
    result = orchestrator.deploy("aws-lightsail", "us-east-1")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    assert result["status"] == "error"
    assert not record.resources_possible
    assert record.apply_started_at is None
    assert all(call[0][0] not in {"plan", "apply"} for call in runner.calls)


def test_every_terraform_provider_uses_the_backend_validated_payload_directly() -> None:
    for provider in ("vpn-aws-lightsail", "vpn-digitalocean", "vpn-scaleway"):
        main = (ROOT / provider / "main.tf").read_text(encoding="utf-8")
        variables = (ROOT / provider / "variables.tf").read_text(encoding="utf-8")
        assert "var.user_data_payload != null ? var.user_data_payload" in main
        assert 'variable "user_data_payload"' in variables
        assert '"@@BOOTSTRAP_FINGERPRINT@@", substr(sha256(local.bootstrap_source), 0, 12)' in main
    aws_main = (ROOT / "vpn-aws-lightsail" / "main.tf").read_text(encoding="utf-8")
    assert "exec /usr/bin/env bash -s -- <<'EPHEMERAL_VPN_BOOTSTRAP'" in aws_main
    assert "local.lightsail_fallback" in aws_main
    assert "user_data         = local.user_data" in aws_main
    assert "user_data = local.user_data" in (ROOT / "vpn-digitalocean" / "main.tf").read_text(encoding="utf-8")
    assert "user_data  = { cloud-init = local.user_data }" in (ROOT / "vpn-scaleway" / "main.tf").read_text(
        encoding="utf-8"
    )


def test_fresh_deployment_stages_current_bootstrap_and_never_reuses_old_user_data(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path, FakeTerraformRunner())
    first = orchestrator.plan("aws-lightsail", "us-east-1")
    first_record = orchestrator.deployments.get(str(first["deployment_id"]))
    first_manifest = json.loads(
        Path(first_record.runtime_directory, "user-data-manifest.json").read_text(encoding="utf-8")
    )
    first_record.state = orchestrator_module.DeploymentState.DESTROYED
    first_record.resources_possible = False
    first_record.state_present = False
    orchestrator.deployments.save(first_record)

    source = orchestrator.resource_root / "terraform-common" / "bootstrap.sh.tftpl"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    second = orchestrator.plan("aws-lightsail", "us-east-1")
    second_record = orchestrator.deployments.get(str(second["deployment_id"]))
    second_manifest = json.loads(
        Path(second_record.runtime_directory, "user-data-manifest.json").read_text(encoding="utf-8")
    )

    assert first_record.runtime_directory != second_record.runtime_directory
    assert first_manifest["bootstrap_source_sha256"] != second_manifest["bootstrap_source_sha256"]
    assert first_manifest["bootstrap_build"] != second_manifest["bootstrap_build"]
    assert first_manifest["payload_sha256"] != second_manifest["payload_sha256"]
