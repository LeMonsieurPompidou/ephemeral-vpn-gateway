from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "terraform-common" / "cloud-init.yaml.tftpl").read_text(encoding="utf-8")


def service_candidates() -> list[str]:
    match = re.search(r"for candidate in ([^;]+); do", TEMPLATE)
    assert match
    return match.group(1).split()


@pytest.mark.parametrize(
    ("available", "expected"),
    [({"ssh.service"}, "ssh.service"), ({"sshd.service"}, "sshd.service")],
)
def test_bootstrap_selects_available_ssh_service(available: set[str], expected: str) -> None:
    selected = next((candidate for candidate in service_candidates() if candidate in available), None)
    assert selected == expected
    assert 'systemctl restart "$$ssh_service"' in TEMPLATE
    assert "systemctl restart sshd.service" not in TEMPLATE


def test_bootstrap_fails_clearly_when_no_ssh_service_exists() -> None:
    selected = next((candidate for candidate in service_candidates() if candidate in set()), None)
    assert selected is None
    assert "neither ssh.service nor sshd.service is available" in TEMPLATE


def test_bootstrap_phases_are_ordered_and_failures_prevent_readiness() -> None:
    phases = [
        'phase="prerequisite/package setup"',
        'phase="SSH hardening/configuration"',
        'phase="WireGuard installation"',
        'phase="WireGuard configuration"',
        'phase="forwarding/NAT/sysctl"',
        'phase="WireGuard service enable/start"',
        'phase="final readiness validation"',
    ]
    offsets = [TEMPLATE.index(phase) for phase in phases]
    assert offsets == sorted(offsets)
    marker = TEMPLATE.index('touch "$$READY_MARKER"')
    assert TEMPLATE.index("systemctl is-enabled --quiet wg-quick@wg0") < marker
    assert TEMPLATE.index("systemctl is-active --quiet wg-quick@wg0") < marker
    assert TEMPLATE.index("wg show wg0 >/dev/null") < marker
    assert "|| true" not in TEMPLATE


def test_shared_bootstrap_is_used_by_every_provider() -> None:
    for provider in ("vpn-aws-lightsail", "vpn-digitalocean", "vpn-scaleway"):
        main = (ROOT / provider / "main.tf").read_text(encoding="utf-8")
        assert "../terraform-common/cloud-init.yaml.tftpl" in main
        assert 'value       = "/var/lib/ephemeral-vpn/ready"' in main
    assert "runcmd:\n  - [ /usr/local/sbin/ephemeral-vpn-bootstrap ]" in TEMPLATE
