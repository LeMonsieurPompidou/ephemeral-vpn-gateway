from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from models import ClientMetadata

MIN_CLIENTS = 1
MAX_CLIENTS = 10
CLIENT_SCHEMA_VERSION = 1
VPN_IPV4_NETWORK = ipaddress.ip_network("10.8.0.0/24")
SERVER_TUNNEL_IPV4 = str(VPN_IPV4_NETWORK.network_address + 1)
KeypairFactory = Callable[[], tuple[str, str]]


@dataclass(frozen=True)
class ClientPeer:
    index: int
    id: str
    display_name: str
    tunnel_ipv4: str
    private_key: str = field(repr=False)
    public_key: str
    private_key_relative_path: str
    config_relative_path: str

    def metadata(self) -> ClientMetadata:
        return ClientMetadata(
            id=self.id,
            index=self.index,
            display_name=self.display_name,
            tunnel_ipv4=self.tunnel_ipv4,
            config_relative_path=self.config_relative_path,
            private_key_relative_path=self.private_key_relative_path,
        )

    def terraform_peer(self) -> dict[str, object]:
        return {
            "id": self.id,
            "public_key": self.public_key,
            "tunnel_ipv4": self.tunnel_ipv4,
        }


def validate_client_count(count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or not MIN_CLIENTS <= count <= MAX_CLIENTS:
        raise ValueError(f"VPN clients must be between {MIN_CLIENTS} and {MAX_CLIENTS}")
    if count + 1 >= VPN_IPV4_NETWORK.num_addresses - 1:
        raise ValueError("VPN client count exceeds the configured tunnel subnet capacity")


def client_tunnel_ipv4(index: int) -> str:
    validate_client_count(index)
    address = VPN_IPV4_NETWORK.network_address + index + 1
    if str(address) == SERVER_TUNNEL_IPV4 or address not in VPN_IPV4_NETWORK:
        raise ValueError("VPN client address allocation is invalid")
    return str(address)


def generate_client_peers(count: int, keypair_factory: KeypairFactory) -> list[ClientPeer]:
    validate_client_count(count)
    peers: list[ClientPeer] = []
    private_keys: set[str] = set()
    public_keys: set[str] = set()
    for index in range(1, count + 1):
        private_key, public_key = keypair_factory()
        if private_key in private_keys or public_key in public_keys:
            raise RuntimeError("WireGuard client key generation returned a duplicate identity")
        private_keys.add(private_key)
        public_keys.add(public_key)
        client_id = f"client-{index}"
        root = PurePosixPath("clients") / client_id
        peers.append(
            ClientPeer(
                index=index,
                id=client_id,
                display_name=f"Client {index}",
                tunnel_ipv4=client_tunnel_ipv4(index),
                private_key=private_key,
                public_key=public_key,
                private_key_relative_path=str(root / "client.privatekey"),
                config_relative_path=str(root / "client.conf"),
            )
        )
    return peers


def resolve_client_path(runtime: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise RuntimeError("Client runtime path is unsafe")
    resolved = runtime.joinpath(*pure.parts).resolve()
    try:
        resolved.relative_to(runtime.resolve())
    except ValueError as exc:
        raise RuntimeError("Client runtime path escapes the deployment runtime") from exc
    return resolved
