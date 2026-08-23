from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from app_settings import SettingsStore
from credential_preflight import configured_tfvars_variables
from credential_store import (
    DIGITALOCEAN_TOKEN_TARGET,
    SCALEWAY_ACCESS_KEY_TARGET,
    SCALEWAY_SECRET_KEY_TARGET,
    CredentialStore,
)


@dataclass(frozen=True)
class ResolvedCredentials:
    provider_id: str
    source: str | None
    values: Mapping[str, str] = field(default_factory=dict, repr=False)
    missing: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return self.source is not None and not self.missing

    def terraform_environment(self) -> dict[str, str]:
        if self.provider_id == "digitalocean":
            token = self.values.get("token")
            return {"TF_VAR_do_token": token} if token else {}
        if self.provider_id == "scaleway":
            mapping = {
                "access_key": "TF_VAR_scaleway_access_key",
                "secret_key": "TF_VAR_scaleway_secret_key",
                "project_id": "TF_VAR_scaleway_project_id",
            }
            return {environment: self.values[key] for key, environment in mapping.items() if key in self.values}
        if self.provider_id == "aws-lightsail":
            profile = self.values.get("profile")
            return {"AWS_PROFILE": profile} if profile else {}
        return {}


class CredentialResolver:
    def __init__(
        self,
        credential_store: CredentialStore,
        settings: SettingsStore,
        provider_roots: Mapping[str, Path],
        *,
        packaged: bool | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.settings = settings
        self.provider_roots = {key: value.resolve() for key, value in provider_roots.items()}
        self.packaged = bool(getattr(sys, "frozen", False)) if packaged is None else packaged
        self.environment = environment if environment is not None else os.environ

    def resolve(self, provider_id: str) -> ResolvedCredentials:
        if provider_id == "digitalocean":
            return self._digitalocean()
        if provider_id == "scaleway":
            return self._scaleway()
        if provider_id == "aws-lightsail":
            return self._aws()
        return ResolvedCredentials(provider_id, None, missing=("credentials",))

    def _digitalocean(self) -> ResolvedCredentials:
        token = self._env("DIGITALOCEAN_TOKEN") or self._env("DIGITALOCEAN_ACCESS_TOKEN")
        if token:
            return ResolvedCredentials("digitalocean", "environment", {"token": token})
        token = self.credential_store.get(DIGITALOCEAN_TOKEN_TARGET)
        if token:
            return ResolvedCredentials("digitalocean", "windows_credential_manager", {"token": token})
        if not self.packaged and "do_token" in self._tfvars("digitalocean"):
            return ResolvedCredentials("digitalocean", "terraform_tfvars")
        return ResolvedCredentials("digitalocean", None, missing=("DIGITALOCEAN_TOKEN",))

    def _scaleway(self) -> ResolvedCredentials:
        env = {
            "access_key": self._env("SCW_ACCESS_KEY"),
            "secret_key": self._env("SCW_SECRET_KEY"),
            "project_id": self._env("SCW_DEFAULT_PROJECT_ID"),
        }
        if all(env.values()):
            return ResolvedCredentials("scaleway", "environment", {key: value for key, value in env.items() if value})
        stored = {
            "access_key": self.credential_store.get(SCALEWAY_ACCESS_KEY_TARGET),
            "secret_key": self.credential_store.get(SCALEWAY_SECRET_KEY_TARGET),
            "project_id": self.settings.load().scaleway_project_id,
        }
        if all(stored.values()):
            return ResolvedCredentials(
                "scaleway", "windows_credential_manager", {key: value for key, value in stored.items() if value}
            )
        if not self.packaged:
            expected = {"scaleway_access_key", "scaleway_secret_key", "scaleway_project_id"}
            if expected <= self._tfvars("scaleway"):
                return ResolvedCredentials("scaleway", "terraform_tfvars")
        names = {"access_key": "SCW_ACCESS_KEY", "secret_key": "SCW_SECRET_KEY", "project_id": "SCW_DEFAULT_PROJECT_ID"}
        combined = {key: env[key] or stored[key] for key in names}
        return ResolvedCredentials(
            "scaleway",
            None,
            missing=tuple(name for key, name in names.items() if not combined[key]),
        )

    def _aws(self) -> ResolvedCredentials:
        profile = self._env("AWS_PROFILE")
        if profile:
            return ResolvedCredentials("aws-lightsail", "environment", {"profile": profile})
        profile = self.settings.load().aws_profile
        if profile:
            return ResolvedCredentials("aws-lightsail", "settings", {"profile": profile})
        return ResolvedCredentials("aws-lightsail", None, missing=("AWS_PROFILE",))

    def _env(self, name: str) -> str | None:
        value = self.environment.get(name, "").strip()
        return value or None

    def _tfvars(self, provider_id: str) -> frozenset[str]:
        root = self.provider_roots.get(provider_id)
        return configured_tfvars_variables(root / "terraform.tfvars" if root else None)
