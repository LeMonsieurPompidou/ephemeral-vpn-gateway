from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DeploymentState(str, Enum):
    IDLE = "idle"
    VALIDATING_CREDENTIALS = "validating_credentials"
    INITIALIZING = "initializing"
    PLANNING = "planning"
    PROVISIONING = "provisioning"
    WAITING_FOR_CLOUD_INIT = "waiting_for_cloud_init"
    CHECKING_WIREGUARD = "checking_wireguard"
    VERIFYING_EGRESS = "verifying_egress"
    READY = "ready"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset(
    state for state in DeploymentState if state not in {DeploymentState.IDLE, DeploymentState.DESTROYED}
)


class StreamingStatus(str, Enum):
    UNVERIFIED = "unverified"
    TESTED = "tested"
    UNAVAILABLE = "unavailable"
    RESIDENTIAL = "residential"


class ServerType(str, Enum):
    CLOUD = "cloud"
    RESIDENTIAL = "residential"


@dataclass(frozen=True)
class Location:
    id: str
    country_code: str
    country_name: str
    city: str
    region: str
    server_type: ServerType
    capabilities: tuple[str, ...]
    streaming_status: StreamingStatus
    description: str | None = None
    estimated_hourly_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["server_type"] = self.server_type.value
        value["streaming_status"] = self.streaming_status.value
        return value


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    display_name: str
    terraform_root: str | None
    locations: tuple[Location, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "terraform_root": self.terraform_root,
            "locations": [location.to_dict() for location in self.locations],
        }


@dataclass(frozen=True)
class DeploymentOptions:
    allowed_ips: tuple[str, ...] = ("0.0.0.0/0",)
    dns_servers: tuple[str, ...] = ("1.1.1.1", "1.0.0.1")
    wireguard_port: int = 51820
    client_mtu: int = 1420
    persistent_keepalive: int = 25
    enable_ipv6: bool = False
    verify_egress: bool = False
    verify_dns: bool = False
    ssh_cidr: str = "127.0.0.1/32"
    expiration_minutes: int | None = None
    automatic_expiration: bool = False
    instance_type: str | None = None


@dataclass
class DeploymentRecord:
    id: str
    provider_id: str
    location_id: str
    terraform_directory: str
    state_path: str
    runtime_directory: str
    created_at: str
    state: DeploymentState = DeploymentState.IDLE
    public_ip: str | None = None
    resource_ids: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None
    expires_at: str | None = None
    auto_expire: bool = False
    updated_at: str = field(default_factory=lambda: now_iso())

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeploymentRecord":
        copy = dict(value)
        copy["state"] = DeploymentState(copy["state"])
        return cls(**copy)


@dataclass(frozen=True)
class StatusEvent:
    deployment_id: str
    state: DeploymentState
    message: str
    timestamp: str = field(default_factory=lambda: now_iso())

    def to_dict(self) -> dict[str, str]:
        return {
            "deployment_id": self.deployment_id,
            "state": self.state.value,
            "message": self.message,
            "timestamp": self.timestamp,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
