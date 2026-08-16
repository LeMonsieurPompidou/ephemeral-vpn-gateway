from __future__ import annotations

import ipaddress

from models import DeploymentOptions


def validate_options(options: DeploymentOptions) -> None:
    if not options.allowed_ips:
        raise ValueError("At least one AllowedIPs CIDR is required")
    for value in options.allowed_ips:
        network = ipaddress.ip_network(value, strict=False)
        if network.version == 6 and not options.enable_ipv6:
            raise ValueError("IPv6 AllowedIPs require IPv6 to be explicitly enabled")
    if not options.dns_servers:
        raise ValueError("At least one DNS server is required")
    for value in options.dns_servers:
        ipaddress.ip_address(value)
    if not 1 <= options.wireguard_port <= 65535:
        raise ValueError("WireGuard port must be between 1 and 65535")
    if not 576 <= options.client_mtu <= 9000:
        raise ValueError("Client MTU must be between 576 and 9000")
    if not 0 <= options.persistent_keepalive <= 65535:
        raise ValueError("Persistent keepalive must be between 0 and 65535")
    if options.ssh_cidr:
        from networking import normalize_public_ipv4_cidr

        normalize_public_ipv4_cidr(options.ssh_cidr)
    if options.expiration_minutes is not None and options.expiration_minutes < 1:
        raise ValueError("Expiration must be at least one minute")
