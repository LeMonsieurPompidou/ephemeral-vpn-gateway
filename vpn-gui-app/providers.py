from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from catalog import ProviderCatalog
from credential_preflight import (
    CredentialCheck,
    configured_tfvars_variables,
    digitalocean_check,
    scaleway_check,
    tfvars_credential_check,
)
from credential_resolver import CredentialResolver, ResolvedCredentials
from models import DeploymentOptions, Location, ProviderInfo
from terraform_runner import TerraformCancelled


class ProviderAdapter(Protocol):
    info: ProviderInfo

    def list_locations(self) -> tuple[Location, ...]: ...
    def validate_credentials(self, cancel: threading.Event | None = None) -> tuple[bool, str]: ...
    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck: ...
    def terraform_variables(self, location: Location, options: DeploymentOptions) -> dict[str, object]: ...


@dataclass
class TerraformProviderAdapter:
    info: ProviderInfo
    required_environment_groups: tuple[tuple[str, ...], ...]
    variable_environment_map: tuple[tuple[str, str], ...] = ()
    credential_file: Path | None = None
    credential_resolver: CredentialResolver | None = None

    def list_locations(self) -> tuple[Location, ...]:
        return self.info.locations

    def validate_credentials(self, cancel: threading.Event | None = None) -> tuple[bool, str]:
        result = self.credential_details(cancel)
        return result.valid, result.message

    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        missing = [
            " or ".join(group)
            for group in self.required_environment_groups
            if not any(os.getenv(name) for name in group)
        ]
        if missing and not (self.credential_file and self.credential_file.is_file()):
            return CredentialCheck(False, "missing", "Provider credentials are not configured.", tuple(missing))
        return CredentialCheck(True, "valid", "Credentials found in the environment.")

    def terraform_variables(self, location: Location, options: DeploymentOptions) -> dict[str, object]:
        result: dict[str, object] = {"region": location.region, "wireguard_port": options.wireguard_port}
        if options.ssh_cidr:
            result["ssh_allowed_cidr"] = options.ssh_cidr
        for variable, environment_name in self.variable_environment_map:
            value = os.getenv(environment_name)
            if value:
                result[variable] = value
        if options.instance_type:
            result["instance_type"] = options.instance_type
        return result

    def resolved_credentials(self) -> ResolvedCredentials | None:
        return self.credential_resolver.resolve(self.info.id) if self.credential_resolver else None


class AwsLightsailAdapter(TerraformProviderAdapter):
    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        valid, message = self.validate_credentials(cancel)
        reason = "valid" if valid else "invalid"
        setup: tuple[str, ...] = ()
        if "profile is not configured" in message:
            reason = "missing"
            setup = ("Configure an AWS profile in the app.",)
        resolved = self.resolved_credentials()
        return CredentialCheck(
            valid, reason, message, setup_commands=setup, source=resolved.source if resolved else None
        )

    def validate_credentials(self, cancel: threading.Event | None = None) -> tuple[bool, str]:
        resolved = self.resolved_credentials()
        profile = resolved.values.get("profile", "") if resolved else os.getenv("AWS_PROFILE", "").strip()
        if not profile:
            return False, "AWS profile is not configured. Configure an AWS CLI/IAM Identity Center profile."
        executable = _aws_executable()
        if not executable:
            return False, "AWS CLI v2 was not found. Install it, then sign in with the configured profile."
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        try:
            process = subprocess.Popen(
                [executable, "sts", "get-caller-identity", "--profile", profile, "--no-cli-pager"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
            )
        except OSError:
            return False, "AWS CLI could not be executed. Verify the AWS CLI v2 installation."
        deadline = time.monotonic() + 20
        while process.poll() is None:
            if cancel and cancel.wait(0.1):
                process.terminate()
                raise TerraformCancelled("Deployment cancelled during AWS credential validation")
            if time.monotonic() >= deadline:
                process.kill()
                return False, f"AWS credential check timed out for profile {profile}. Retry credential validation."
            time.sleep(0.05)
        stdout, stderr = process.communicate()
        if process.returncode == 0:
            return True, f"AWS credentials are valid for profile {profile}"
        detail = (stderr or stdout).lower()
        if any(marker in detail for marker in ("sso", "expired", "token has expired", "invalid_grant")):
            return False, f"AWS session expired. Sign in again with: aws sso login --profile {profile}"
        if "profile" in detail and any(
            marker in detail for marker in ("not be found", "could not be found", "does not exist")
        ):
            return False, f"AWS profile {profile} was not found. Run: aws configure sso --profile {profile}"
        if "accessdenied" in detail or "not authorized" in detail:
            return False, f"AWS credentials for profile {profile} are not authorized for STS identity validation."
        return False, "AWS credentials are unavailable. Sign in again."


class DigitalOceanAdapter(TerraformProviderAdapter):
    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        resolved = self.resolved_credentials()
        if resolved:
            if resolved.source == "terraform_tfvars":
                return tfvars_credential_check("DigitalOcean")
            token, missing = resolved.values.get("token", ""), resolved.missing
        else:
            if "do_token" in configured_tfvars_variables(self.credential_file):
                return tfvars_credential_check("DigitalOcean")
            token = os.getenv("DIGITALOCEAN_TOKEN", "").strip() or os.getenv("DIGITALOCEAN_ACCESS_TOKEN", "").strip()
            missing = ("DIGITALOCEAN_TOKEN",)
        if not token:
            return CredentialCheck(
                False,
                "missing",
                "DigitalOcean credentials are not configured.",
                missing,
                ('$env:DIGITALOCEAN_TOKEN = "<your-token>"',),
                (
                    "Configure credentials in the app, or use an environment variable for this process.",
                    "Session variables apply only when the app starts from the same terminal.",
                ),
            )
        result = digitalocean_check(token)
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        return replace(result, source=resolved.source if resolved else "environment")


class ScalewayAdapter(TerraformProviderAdapter):
    _required = ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_PROJECT_ID")

    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        resolved = self.resolved_credentials()
        if resolved:
            if resolved.source == "terraform_tfvars":
                return tfvars_credential_check("Scaleway")
            missing, values = resolved.missing, resolved.values
        else:
            tfvars_variables = configured_tfvars_variables(self.credential_file)
            tfvar_names = {
                "SCW_ACCESS_KEY": "scaleway_access_key",
                "SCW_SECRET_KEY": "scaleway_secret_key",
                "SCW_DEFAULT_PROJECT_ID": "scaleway_project_id",
            }
            missing = tuple(
                name
                for name in self._required
                if tfvar_names[name] not in tfvars_variables and not os.getenv(name, "").strip()
            )
            if any(variable in tfvars_variables for variable in tfvar_names.values()) and not missing:
                return tfvars_credential_check("Scaleway")
            values = {
                "access_key": os.getenv("SCW_ACCESS_KEY", "").strip(),
                "secret_key": os.getenv("SCW_SECRET_KEY", "").strip(),
                "project_id": os.getenv("SCW_DEFAULT_PROJECT_ID", "").strip(),
            }
        if missing:
            commands = tuple(f'$env:{name} = "<{self._placeholder(name)}>"' for name in missing)
            return CredentialCheck(
                False,
                "missing",
                "Scaleway credentials are not configured.",
                missing,
                commands,
                ("Configure credentials in the app, or use environment variables for this process.",),
            )
        project_id = values["project_id"]
        try:
            uuid.UUID(project_id)
        except ValueError:
            return CredentialCheck(
                False, "invalid", "SCW_DEFAULT_PROJECT_ID is not a valid Scaleway project identifier."
            )
        result = scaleway_check(values["secret_key"], project_id)
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        return replace(result, source=resolved.source if resolved else "environment")

    @staticmethod
    def _placeholder(name: str) -> str:
        return {"SCW_ACCESS_KEY": "access-key", "SCW_SECRET_KEY": "secret-key", "SCW_DEFAULT_PROJECT_ID": "project-id"}[
            name
        ]


def _aws_executable() -> str | None:
    executable = shutil.which("aws")
    if not executable and sys.platform.startswith("win"):
        normal_path = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")
        if normal_path.is_file():
            executable = str(normal_path)
    return executable


class ProviderRegistry:
    def __init__(self, catalog: ProviderCatalog, credential_resolver: CredentialResolver | None = None) -> None:
        self.catalog = catalog
        self._providers: dict[str, ProviderAdapter] = {}
        resource_root = catalog.path.resolve().parent.parent
        digitalocean_info = catalog.get_provider("digitalocean")
        scaleway_info = catalog.get_provider("scaleway")
        if not digitalocean_info.terraform_root or not scaleway_info.terraform_root:
            raise ValueError("Cloud providers must define Terraform roots")
        self.register(
            DigitalOceanAdapter(
                digitalocean_info,
                (("DIGITALOCEAN_TOKEN", "DIGITALOCEAN_ACCESS_TOKEN"),),
                credential_file=resource_root / digitalocean_info.terraform_root / "terraform.tfvars",
                credential_resolver=credential_resolver,
            )
        )
        self.register(
            ScalewayAdapter(
                scaleway_info,
                (("SCW_ACCESS_KEY",), ("SCW_SECRET_KEY",), ("SCW_DEFAULT_PROJECT_ID",)),
                credential_file=resource_root / scaleway_info.terraform_root / "terraform.tfvars",
                credential_resolver=credential_resolver,
            )
        )
        self.register(
            AwsLightsailAdapter(
                catalog.get_provider("aws-lightsail"),
                (("AWS_PROFILE",),),
                credential_file=Path.home() / ".aws" / "credentials",
                credential_resolver=credential_resolver,
            )
        )

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
