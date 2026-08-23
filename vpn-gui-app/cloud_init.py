from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from client_peers import MAX_CLIENTS, MIN_CLIENTS, client_tunnel_ipv4

CLOUD_CONFIG_PROVIDERS = frozenset({"digitalocean", "scaleway"})
SHELL_SCRIPT_PROVIDERS = frozenset({"aws-lightsail"})
SUPPORTED_PROVIDERS = CLOUD_CONFIG_PROVIDERS | SHELL_SCRIPT_PROVIDERS
BOOTSTRAP_PATH = "/usr/local/sbin/ephemeral-vpn-bootstrap"
READINESS_MARKER = "/var/lib/ephemeral-vpn/ready"
FAILURE_MARKER = "/var/lib/ephemeral-vpn/bootstrap-failure"
LIGHTSAIL_BOOTSTRAP_BOUNDARY = "EPHEMERAL_VPN_BOOTSTRAP"
LIGHTSAIL_BASH_TRAMPOLINE = (
    "#!/bin/sh\n"
    "# Lightsail prepends a POSIX-shell initialization script. Enter Bash explicitly.\n"
    f"exec /usr/bin/env bash -s -- <<'{LIGHTSAIL_BOOTSTRAP_BOUNDARY}'\n"
)


class UserDataValidationError(RuntimeError):
    """Raised when rendered user-data is unsafe or structurally invalid."""


def _read_template(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise UserDataValidationError("User-data template must not contain a UTF-8 byte-order mark")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserDataValidationError("User-data template must be valid UTF-8") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _wireguard_key(value: str, name: str) -> str:
    if "\n" in value or "\r" in value:
        raise UserDataValidationError(f"{name} is malformed")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UserDataValidationError(f"{name} is malformed") from exc
    if len(decoded) != 32:
        raise UserDataValidationError(f"{name} is malformed")
    return value


def bootstrap_source_sha256(common_root: Path) -> str:
    """Fingerprint the normalized, secret-free shared bootstrap source."""
    template = _read_template(common_root / "bootstrap.sh.tftpl")
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def render_user_data(
    provider_id: str,
    common_root: Path,
    *,
    wireguard_port: int,
    server_private_key: str,
    client_peers: Sequence[dict[str, object]],
) -> str:
    """Render and validate the exact user-data string passed to Terraform."""
    if provider_id not in SUPPORTED_PROVIDERS:
        raise UserDataValidationError("Unsupported user-data provider")
    if not 1 <= wireguard_port <= 65535:
        raise UserDataValidationError("WireGuard port is outside the valid range")
    source_sha256 = bootstrap_source_sha256(common_root)
    replacements = {
        "@@WIREGUARD_PORT@@": str(wireguard_port),
        "@@SERVER_PRIVATE_KEY@@": _wireguard_key(server_private_key, "Server WireGuard key"),
        "@@CLIENT_PEERS@@": _render_client_peers(client_peers),
        "@@BOOTSTRAP_FINGERPRINT@@": source_sha256[:12],
    }
    bootstrap = _read_template(common_root / "bootstrap.sh.tftpl")
    for token, value in replacements.items():
        if bootstrap.count(token) == 0:
            raise UserDataValidationError("Bootstrap template is missing a required placeholder")
        bootstrap = bootstrap.replace(token, value)
    bootstrap = bootstrap.rstrip("\n") + "\n"

    if provider_id in SHELL_SCRIPT_PROVIDERS:
        payload = LIGHTSAIL_BASH_TRAMPOLINE + bootstrap + LIGHTSAIL_BOOTSTRAP_BOUNDARY + "\n"
    else:
        cloud_config = _read_template(common_root / "cloud-init.yaml.tftpl")
        if cloud_config.count("@@BOOTSTRAP_SCRIPT@@") != 1:
            raise UserDataValidationError("Cloud-config template must contain one bootstrap placeholder")
        indented = bootstrap.rstrip("\n").replace("\n", "\n      ")
        payload = cloud_config.replace("@@BOOTSTRAP_SCRIPT@@", indented).rstrip("\n") + "\n"

    validate_user_data_payload(provider_id, payload)
    return payload


def _render_client_peers(client_peers: Sequence[dict[str, object]]) -> str:
    if not MIN_CLIENTS <= len(client_peers) <= MAX_CLIENTS:
        raise UserDataValidationError(f"User-data requires between {MIN_CLIENTS} and {MAX_CLIENTS} clients")
    blocks: list[str] = []
    public_keys: set[str] = set()
    addresses: set[str] = set()
    for index, peer in enumerate(client_peers, start=1):
        public_key_raw = peer.get("public_key")
        tunnel_ipv4_raw = peer.get("tunnel_ipv4")
        if not isinstance(public_key_raw, str) or not isinstance(tunnel_ipv4_raw, str):
            raise UserDataValidationError("Client peer metadata is malformed")
        public_key = _wireguard_key(public_key_raw, f"Client {index} WireGuard key")
        if tunnel_ipv4_raw != client_tunnel_ipv4(index):
            raise UserDataValidationError("Client peer tunnel address is outside the deterministic allocation")
        if public_key in public_keys or tunnel_ipv4_raw in addresses:
            raise UserDataValidationError("Client peers must have unique public keys and tunnel addresses")
        public_keys.add(public_key)
        addresses.add(tunnel_ipv4_raw)
        blocks.append(f"[Peer]\nPublicKey = {public_key}\nAllowedIPs = {tunnel_ipv4_raw}/32")
    return "\n\n".join(blocks)


def validate_user_data_payload(provider_id: str, payload: str) -> None:
    """Validate format and bootstrap invariants without exposing payload contents."""
    if provider_id not in SUPPORTED_PROVIDERS:
        raise UserDataValidationError("Unsupported user-data provider")
    if payload.startswith("\ufeff") or "\x00" in payload or "\r" in payload:
        raise UserDataValidationError("Rendered user-data has an unsafe encoding or newline format")
    meaningful = [line for line in payload.splitlines() if line.strip()]
    if not meaningful:
        raise UserDataValidationError("Rendered user-data is empty")

    if provider_id in SHELL_SCRIPT_PROVIDERS:
        if meaningful[0] != "#!/bin/sh" or "#cloud-config" in meaningful:
            raise UserDataValidationError("Lightsail user-data must use the POSIX-to-Bash launch trampoline")
        if re.search(r"(?m)^(?:package_update|packages|write_files|runcmd):", payload):
            raise UserDataValidationError("Lightsail launch script contains cloud-config YAML directives")
        bootstrap = _bootstrap_from_lightsail(payload)
    else:
        if meaningful[0] != "#cloud-config":
            raise UserDataValidationError("Cloud-config header must be the first meaningful line")
        if any(line.startswith("#!") for line in meaningful[: meaningful.index("#cloud-config")]):
            raise UserDataValidationError("A shell shebang must not precede the cloud-config header")
        try:
            document = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            raise UserDataValidationError("Rendered cloud-config is not valid YAML") from exc
        if not isinstance(document, dict):
            raise UserDataValidationError("Rendered cloud-config must be a YAML mapping")
        required = {"write_files", "runcmd"}
        if not required.issubset(document):
            raise UserDataValidationError("Rendered cloud-config is missing required top-level keys")
        if any(key in document for key in ("package_update", "package_upgrade", "packages")):
            raise UserDataValidationError("Rendered cloud-config must delegate package installation to the bootstrap")
        bootstrap = _bootstrap_from_cloud_config(document)

    _validate_bootstrap(bootstrap)


def _bootstrap_from_lightsail(payload: str) -> str:
    suffix = LIGHTSAIL_BOOTSTRAP_BOUNDARY + "\n"
    if (
        not payload.startswith(LIGHTSAIL_BASH_TRAMPOLINE)
        or not payload.endswith(suffix)
        or payload.count(LIGHTSAIL_BOOTSTRAP_BOUNDARY) != 2
    ):
        raise UserDataValidationError("Lightsail user-data has an invalid Bash trampoline")
    bootstrap = payload[len(LIGHTSAIL_BASH_TRAMPOLINE) : -len(suffix)]
    if not bootstrap.endswith("\n"):
        raise UserDataValidationError("Lightsail bootstrap must end with a newline")
    return bootstrap


def _bootstrap_from_cloud_config(document: dict[str, Any]) -> str:
    files = document.get("write_files")
    if not isinstance(files, list):
        raise UserDataValidationError("Rendered cloud-config write_files must be a list")
    for item in files:
        if isinstance(item, dict) and item.get("path") == BOOTSTRAP_PATH and isinstance(item.get("content"), str):
            return str(item["content"])
    raise UserDataValidationError("Rendered cloud-config does not contain the bootstrap script")


def _validate_bootstrap(bootstrap: str) -> None:
    if not bootstrap.startswith("#!/usr/bin/env bash\n"):
        raise UserDataValidationError("Bootstrap script must start with the Bash shebang")
    if "@@" in bootstrap:
        raise UserDataValidationError("Bootstrap script contains an unresolved placeholder")
    if "$$" in bootstrap:
        raise UserDataValidationError("Bootstrap script contains PID-style dollar expansion")
    required = (
        "BOOTSTRAP_BUILD=",
        "STATUS_MARKER=/var/lib/ephemeral-vpn/bootstrap-status",
        "record_bootstrap_status() {",
        "build=%s\\nphase=%s\\nupdated_epoch=%s\\n",
        "ephemeral-vpn bootstrap build: ${BOOTSTRAP_BUILD}",
        "ephemeral-vpn bootstrap failure: phase=${phase} category=${command_category}",
        "NEEDRESTART_SUSPEND=1 apt-get install -y iptables wireguard",
        "ephemeral-vpn bootstrap: automatic needrestart hook suspended for prerequisite installation",
        "ephemeral-vpn bootstrap: package installation completed",
        "ephemeral-vpn bootstrap: package installation failed (apt-get exit ${package_status})",
        "systemctl daemon-reload",
        "resolve_ssh_service() {",
        "for attempt in 1 2 3 4 5; do",
        "for candidate in ssh.service sshd.service; do",
        'systemctl show --property=LoadState --value "${candidate}"',
        "ephemeral-vpn ssh unit: attempt=%s unit=%s LoadState=%s",
        "ephemeral-vpn bootstrap: no loaded OpenSSH systemd service found",
        "ephemeral-vpn ssh unit selected: ${ssh_service}",
        'systemctl restart "${ssh_service}"',
        "[Interface]",
        "ListenPort = ",
        "PrivateKey = ",
        "PostUp = ",
        "[Peer]",
        "PublicKey = ",
        "AllowedIPs = 10.8.0.2/32",
        "sysctl --system",
        "systemctl enable --now wg-quick@wg0",
        "systemctl is-active --quiet wg-quick@wg0",
        'touch "${READY_MARKER}"',
    )
    if not all(value in bootstrap for value in required):
        raise UserDataValidationError("Bootstrap script is missing a required provisioning invariant")
    if not re.search(r"(?m)^BOOTSTRAP_BUILD=[0-9a-f]{12}$", bootstrap):
        raise UserDataValidationError("Bootstrap script has an invalid source fingerprint")
    if "NEEDRESTART_MODE=" in bootstrap:
        raise UserDataValidationError("Bootstrap script must suspend rather than configure the needrestart APT hook")
    if "systemctl cat" in bootstrap:
        raise UserDataValidationError("Bootstrap script uses nondeterministic unit-file probing")
    if re.search(r"systemctl\s+restart\s+['\"]?(?:ssh|sshd)\.service", bootstrap):
        raise UserDataValidationError("Bootstrap script contains a hardcoded OpenSSH service restart")
    if bootstrap.count('systemctl restart "${ssh_service}"') != 1:
        raise UserDataValidationError("Bootstrap script must contain exactly one resolved OpenSSH restart")
    phases = (
        'phase="prerequisite installation"',
        'phase="SSH hardening/configuration"',
        'phase="WireGuard configuration"',
        'phase="forwarding/NAT/sysctl"',
        'phase="WireGuard service enable/start"',
        'phase="final readiness validation"',
    )
    phase_offsets = [bootstrap.index(phase) for phase in phases]
    if phase_offsets != sorted(phase_offsets):
        raise UserDataValidationError("Bootstrap provisioning phases are out of order")
    marker = bootstrap.index('touch "${READY_MARKER}"')
    prerequisites = (
        bootstrap.index("NEEDRESTART_SUSPEND=1 apt-get install -y iptables wireguard"),
        bootstrap.index('systemctl restart "${ssh_service}"'),
        bootstrap.index("wg-quick strip wg0"),
        bootstrap.index("sysctl --system"),
        bootstrap.index("systemctl enable --now wg-quick@wg0"),
        bootstrap.index("systemctl is-enabled --quiet wg-quick@wg0"),
        bootstrap.index("systemctl is-active --quiet wg-quick@wg0"),
        bootstrap.index("iptables -t nat -C POSTROUTING"),
    )
    if any(offset >= marker for offset in prerequisites):
        raise UserDataValidationError("Readiness marker is created before final bootstrap validation")
