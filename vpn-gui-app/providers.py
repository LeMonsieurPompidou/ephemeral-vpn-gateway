from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
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
            return CredentialCheck(
                False,
                "missing",
                "Provider credentials are not configured.",
                tuple(missing),
            )
        return CredentialCheck(True, "valid", "Credentials found in the environment.")

    def terraform_variables(self, location: Location, options: DeploymentOptions) -> dict[str, object]:
        result: dict[str, object] = {
            "region": location.region,
            "wireguard_port": options.wireguard_port,
        }
        if options.ssh_cidr:
            result["ssh_allowed_cidr"] = options.ssh_cidr
        for variable, environment_name in self.variable_environment_map:
            value = os.getenv(environment_name)
            if value:
                result[variable] = value
        if options.instance_type:
            result["instance_type"] = options.instance_type
        return result


class AwsLightsailAdapter(TerraformProviderAdapter):
    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        valid, message = self.validate_credentials(cancel)
        reason = "valid" if valid else "invalid"
        setup: tuple[str, ...] = ()
        if "AWS_PROFILE is not set" in message:
            reason = "missing"
            setup = ('$env:AWS_PROFILE = "heres-vpn"', "aws sso login --profile heres-vpn")
        return CredentialCheck(valid, reason, message, setup_commands=setup)

    def validate_credentials(self, cancel: threading.Event | None = None) -> tuple[bool, str]:
        profile = os.getenv("AWS_PROFILE", "").strip()
        if not profile:
            return False, "AWS_PROFILE is not set. Set AWS_PROFILE=heres-vpn after configuring AWS IAM Identity Center."
        executable = shutil.which("aws")
        if not executable and sys.platform.startswith("win"):
            normal_path = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")
            if normal_path.is_file():
                executable = str(normal_path)
        if not executable:
            return False, "AWS CLI v2 was not found. Install it, then run: aws sso login --profile " + profile
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
            return False, f"AWS SSO session expired. Run: aws sso login --profile {profile}"
        if "profile" in detail and any(
            marker in detail for marker in ("not be found", "could not be found", "does not exist")
        ):
            return (
                False,
                f"AWS profile {profile} was not found. Configure it with: aws configure sso --profile {profile}",
            )
        if "accessdenied" in detail or "not authorized" in detail:
            return (
                False,
                f"AWS credentials for profile {profile} are not authorized for STS identity validation.",
            )
        return False, f"AWS credentials for profile {profile} are unavailable. Run: aws sso login --profile {profile}"


class DigitalOceanAdapter(TerraformProviderAdapter):
    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        tfvars_variables = configured_tfvars_variables(self.credential_file)
        if "do_token" in tfvars_variables:
            return tfvars_credential_check("DigitalOcean")
        token = os.getenv("DIGITALOCEAN_TOKEN", "").strip()
        fallback = os.getenv("DIGITALOCEAN_ACCESS_TOKEN", "").strip()
        if not token and not fallback:
            return CredentialCheck(
                False,
                "missing",
                "DigitalOcean credentials are not configured.",
                ("DIGITALOCEAN_TOKEN",),
                ('$env:DIGITALOCEAN_TOKEN = "<your-token>"',),
                (
                    "Restart Hérès from the same terminal after setting the variable.",
                    "A packaged app opened elsewhere cannot inherit a terminal-only variable.",
                ),
            )
        result = digitalocean_check(token or fallback)
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        return result


class ScalewayAdapter(TerraformProviderAdapter):
    _required = ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_PROJECT_ID")

    def credential_details(self, cancel: threading.Event | None = None) -> CredentialCheck:
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
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
        if missing:
            commands = tuple(f'$env:{name} = "<{self._placeholder(name)}>"' for name in missing)
            return CredentialCheck(
                False,
                "missing",
                "Scaleway credentials are not configured.",
                missing,
                commands,
                (
                    "Restart Hérès from the same terminal after setting the variables.",
                    "A packaged app opened elsewhere cannot inherit terminal-only variables.",
                ),
            )
        if any(variable in tfvars_variables for variable in tfvar_names.values()):
            return tfvars_credential_check("Scaleway")
        project_id = os.environ["SCW_DEFAULT_PROJECT_ID"].strip()
        try:
            uuid.UUID(project_id)
        except ValueError:
            return CredentialCheck(
                False,
                "invalid",
                "SCW_DEFAULT_PROJECT_ID is not a valid Scaleway project identifier.",
            )
        result = scaleway_check(os.environ["SCW_SECRET_KEY"].strip(), project_id)
        if cancel and cancel.is_set():
            raise TerraformCancelled("Deployment cancelled during credential validation")
        return result

    @staticmethod
    def _placeholder(name: str) -> str:
        return {
            "SCW_ACCESS_KEY": "access-key",
            "SCW_SECRET_KEY": "secret-key",
            "SCW_DEFAULT_PROJECT_ID": "project-id",
        }[name]


class ProviderRegistry:
    def __init__(self, catalog: ProviderCatalog) -> None:
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
            )
        )
        self.register(
            ScalewayAdapter(
                scaleway_info,
                (("SCW_ACCESS_KEY",), ("SCW_SECRET_KEY",), ("SCW_DEFAULT_PROJECT_ID",)),
                credential_file=resource_root / scaleway_info.terraform_root / "terraform.tfvars",
            )
        )
        self.register(
            AwsLightsailAdapter(
                catalog.get_provider("aws-lightsail"),
                (("AWS_PROFILE",),),
                credential_file=Path.home() / ".aws" / "credentials",
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
