from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import orchestrator as orchestrator_module
import pytest
import yaml
from cloud_init import UserDataValidationError, render_user_data, validate_user_data_payload
from helpers import FakeTerraformRunner, make_orchestrator

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "terraform-common"
SERVER_PRIVATE = base64.b64encode(bytes(range(32))).decode("ascii")
CLIENT_PUBLIC = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")


def rendered(provider_id: str) -> str:
    return render_user_data(
        provider_id,
        COMMON,
        wireguard_port=51820,
        server_private_key=SERVER_PRIVATE,
        client_public_key=CLIENT_PUBLIC,
    )


def bootstrap_from_payload(provider_id: str, payload: str) -> str:
    if provider_id == "aws-lightsail":
        return payload
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


def test_aws_lightsail_receives_an_explicit_shell_launch_script() -> None:
    payload = rendered("aws-lightsail")
    assert payload.encode("utf-8").startswith(b"#!/usr/bin/env bash\nset -Eeuo pipefail\n")
    assert "#cloud-config" not in payload
    assert "\npackage_update:" not in payload
    assert "\nwrite_files:" not in payload
    assert "\nruncmd:" not in payload


@pytest.mark.parametrize("provider_id", ["digitalocean", "scaleway"])
def test_cloud_config_providers_receive_valid_yaml_with_bootstrap(provider_id: str) -> None:
    payload = rendered(provider_id)
    assert payload.encode("utf-8").startswith(b"#cloud-config\npackage_update: true\n")
    document = yaml.safe_load(payload)
    assert set(("packages", "write_files", "runcmd")).issubset(document)
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
    assert "NEEDRESTART_MODE=l apt-get install -y iptables wireguard" in payload
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
    ):
        assert required in payload
    assert payload.index("systemctl is-active --quiet wg-quick@wg0") < payload.index('touch "${READY_MARKER}"')


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


def test_lightsail_validation_rejects_cloud_config_directives_inside_launch_script() -> None:
    payload = rendered("aws-lightsail") + "package_update: true\n"
    with pytest.raises(UserDataValidationError, match="cloud-config YAML directives"):
        validate_user_data_payload("aws-lightsail", payload)


@pytest.mark.parametrize(
    ("provider_id", "location_id", "expected_prefix"),
    [
        ("aws-lightsail", "us-east-1", "#!/usr/bin/env bash\n"),
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
    assert "user_data         = local.user_data" in (ROOT / "vpn-aws-lightsail" / "main.tf").read_text(encoding="utf-8")
    assert "user_data = local.user_data" in (ROOT / "vpn-digitalocean" / "main.tf").read_text(encoding="utf-8")
    assert "user_data  = { cloud-init = local.user_data }" in (ROOT / "vpn-scaleway" / "main.tf").read_text(
        encoding="utf-8"
    )
