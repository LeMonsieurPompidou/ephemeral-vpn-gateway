from __future__ import annotations

import ipaddress
import urllib.request

PUBLIC_IPV4_ENDPOINT = "https://checkip.amazonaws.com/"


class PublicIpDetectionError(RuntimeError):
    pass


def normalize_public_ipv4_cidr(value: str) -> str:
    raw = value.strip()
    try:
        if "/" in raw:
            network = ipaddress.ip_network(raw, strict=False)
            if network.version != 4 or network.prefixlen != 32:
                raise ValueError
            address = network.network_address
        else:
            parsed_address = ipaddress.ip_address(raw)
            if not isinstance(parsed_address, ipaddress.IPv4Address):
                raise ValueError
            address = parsed_address
    except ValueError as exc:
        raise PublicIpDetectionError("SSH override must be one public IPv4 address or an IPv4 /32") from exc
    if not address.is_global:
        raise PublicIpDetectionError("SSH source address must be a globally routable IPv4 address")
    return f"{address}/32"


def detect_public_ipv4(timeout: float = 5.0) -> str:
    request = urllib.request.Request(PUBLIC_IPV4_ENDPOINT, headers={"User-Agent": "EphemeralVpnGateway/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
            body = response.read(128).decode("ascii", errors="strict")
    except Exception as exc:
        raise PublicIpDetectionError(
            "Could not detect the current public IPv4 address. Retry or enter a manual /32 in Advanced settings."
        ) from exc
    return normalize_public_ipv4_cidr(body)
