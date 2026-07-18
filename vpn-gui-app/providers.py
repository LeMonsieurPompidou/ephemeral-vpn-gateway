from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from catalog import ProviderCatalog
from models import DeploymentOptions, Location, ProviderInfo


class ProviderAdapter(Protocol):
    info: ProviderInfo

    def list_locations(self) -> tuple[Location, ...]: ...
    def validate_credentials(self) -> tuple[bool, str]: ...
    def terraform_variables(self, location: Location, options: DeploymentOptions) -> dict[str, object]: ...


@dataclass
class TerraformProviderAdapter:
    info: ProviderInfo
    required_environment_groups: tuple[tuple[str, ...], ...]
    variable_environment_map: tuple[tuple[str, str], ...] = ()
    credential_file: Path | None = None

    def list_locations(self) -> tuple[Location, ...]:
        return self.info.locations

    def validate_credentials(self) -> tuple[bool, str]:
        missing = [
            " or ".join(group)
            for group in self.required_environment_groups
            if not any(os.getenv(name) for name in group)
        ]
        if missing and not (self.credential_file and self.credential_file.is_file()):
            return False, "Missing credential environment variable(s): " + ", ".join(missing)
        return True, "Credentials found in the environment"

    def terraform_variables(self, location: Location, options: DeploymentOptions) -> dict[str, object]:
        result: dict[str, object] = {
            "region": location.region,
            "wireguard_port": options.wireguard_port,
            "ssh_allowed_cidr": options.ssh_cidr,
        }
        for variable, environment_name in self.variable_environment_map:
            value = os.getenv(environment_name)
            if value:
                result[variable] = value
        if options.instance_type:
            result["instance_type"] = options.instance_type
        return result


class ResidentialProviderAdapter:
    def __init__(self, info: ProviderInfo) -> None:
        self.info = info

    def list_locations(self) -> tuple[Location, ...]:
        return self.info.locations

    def validate_credentials(self) -> tuple[bool, str]:
        return False, "Residential nodes must be imported by the user and are not provisioned by Terraform"

    def terraform_variables(self, location: Location, options: DeploymentOptions) -> dict[str, object]:
        raise NotImplementedError("Residential node import is a future, non-cloud interface")


class ProviderRegistry:
    def __init__(self, catalog: ProviderCatalog) -> None:
        self.catalog = catalog
        self._providers: dict[str, ProviderAdapter] = {}
        self.register(
            TerraformProviderAdapter(
                catalog.get_provider("digitalocean"),
                (("DIGITALOCEAN_TOKEN",),),
                (("ssh_key_name", "DIGITALOCEAN_SSH_KEY_NAME"),),
            )
        )
        self.register(
            TerraformProviderAdapter(
                catalog.get_provider("scaleway"),
                (("SCW_ACCESS_KEY",), ("SCW_SECRET_KEY",), ("SCW_DEFAULT_PROJECT_ID",)),
            )
        )
        self.register(
            TerraformProviderAdapter(
                catalog.get_provider("aws-lightsail"),
                (("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE"),),
                credential_file=Path.home() / ".aws" / "credentials",
            )
        )
        self.register(ResidentialProviderAdapter(catalog.get_provider("residential")))

    def register(self, provider: ProviderAdapter) -> None:
        if provider.info.id in self._providers:
            raise ValueError(f"Provider already registered: {provider.info.id}")
        self._providers[provider.info.id] = provider

    def get(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._providers[provider_id.strip().lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported provider: {provider_id}") from exc

    def list(self) -> tuple[ProviderAdapter, ...]:
        return tuple(self._providers.values())
