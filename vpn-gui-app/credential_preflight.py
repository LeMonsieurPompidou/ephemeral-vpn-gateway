from __future__ import annotations

import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

_TFVARS_ASSIGNMENT = re.compile(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_MAX_CREDENTIAL_TFVARS_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CredentialCheck:
    valid: bool
    reason: str
    message: str
    missing_variables: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "message": self.message,
            "missing_variables": list(self.missing_variables),
            "setup_commands": list(self.setup_commands),
            "notes": list(self.notes),
            "source": self.source,
        }


class CredentialNetworkError(RuntimeError):
    """The provider credential endpoint could not be reached safely."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def configured_tfvars_variables(path: Path | None) -> frozenset[str]:
    """Return only assigned variable names from a bounded legacy tfvars file.

    Values are deliberately neither parsed nor returned: this compatibility probe
    must never move provider secrets into the registry, logs, or bridge response.
    Terraform remains responsible for parsing and validating the actual file.
    """
    if path is None or not path.is_file():
        return frozenset()
    try:
        if path.stat().st_size > _MAX_CREDENTIAL_TFVARS_BYTES:
            return frozenset()
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return frozenset()
    return frozenset(_TFVARS_ASSIGNMENT.findall(content))


def tfvars_credential_check(provider_name: str) -> CredentialCheck:
    return CredentialCheck(
        True,
        "configured",
        f"{provider_name} credentials are configured in provider terraform.tfvars. "
        "Terraform will validate them during deployment.",
        notes=("Using the existing git-ignored provider configuration.",),
        source="terraform_tfvars",
    )


def read_only_api_status(url: str, headers: Mapping[str, str], *, timeout: float = 10.0) -> int:
    """Perform a bounded GET and return only its status, never its response body."""
    request = urllib.request.Request(
        url,
        headers={**headers, "Accept": "application/json", "User-Agent": "HeresVPN-CredentialCheck/1"},
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise CredentialNetworkError("Provider credential endpoint is unavailable") from exc


def digitalocean_check(token: str) -> CredentialCheck:
    try:
        status = read_only_api_status(
            "https://api.digitalocean.com/v2/account",
            {"Authorization": f"Bearer {token}"},
        )
    except CredentialNetworkError:
        return CredentialCheck(
            False,
            "network",
            "DigitalOcean credential validation could not reach the provider API. "
            "Check network connectivity and retry.",
        )
    if status == 200:
        return CredentialCheck(True, "valid", "DigitalOcean credentials are valid.", source="environment")
    if status in {401, 403}:
        return CredentialCheck(
            False,
            "invalid",
            "DigitalOcean credentials were rejected or lack the required account permission.",
        )
    return CredentialCheck(
        False,
        "api",
        "DigitalOcean credential validation was unavailable. Retry later.",
    )


def scaleway_check(secret_key: str, project_id: str) -> CredentialCheck:
    try:
        status = read_only_api_status(
            f"https://api.scaleway.com/account/v3/projects/{project_id}",
            {"X-Auth-Token": secret_key},
        )
    except CredentialNetworkError:
        return CredentialCheck(
            False,
            "network",
            "Scaleway credential validation could not reach the provider API. Check network connectivity and retry.",
        )
    if status == 200:
        return CredentialCheck(
            True,
            "valid",
            "Scaleway credentials and project are valid.",
            source="environment",
        )
    if status in {401, 403}:
        return CredentialCheck(
            False,
            "invalid",
            "Scaleway credentials were rejected or lack permission to access the configured project.",
        )
    if status == 404:
        return CredentialCheck(False, "invalid", "The configured Scaleway project could not be found or accessed.")
    return CredentialCheck(False, "api", "Scaleway credential validation was unavailable. Retry later.")
