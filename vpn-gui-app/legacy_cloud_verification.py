from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_TFVARS_STRING = re.compile(r'(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?$')
_LOCAL_RESOURCE_PREFIXES = ("local_", "wireguard_", "random_", "external_")


@dataclass(frozen=True)
class ReadOnlyResponse:
    status: int
    payload: dict[str, Any] | None


Transport = Callable[[str, dict[str, str]], ReadOnlyResponse]


def read_only_json(url: str, headers: dict[str, str]) -> ReadOnlyResponse:
    request = urllib.request.Request(
        url,
        headers={**headers, "Accept": "application/json", "User-Agent": "HeresVPN-LegacyAudit/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed HTTPS provider endpoints
            body = response.read(1024 * 1024)
            value = json.loads(body.decode("utf-8")) if body else None
            return ReadOnlyResponse(int(response.status), value if isinstance(value, dict) else None)
    except urllib.error.HTTPError as exc:
        return ReadOnlyResponse(int(exc.code), None)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Provider read-only verification endpoint is unavailable") from exc


class LegacyCloudVerifier:
    """Verify exact legacy resource identities using GET requests only."""

    def __init__(self, transport: Transport = read_only_json) -> None:
        self.transport = transport

    def verify(self, provider_id: str, state_path: Path, credential_directory: Path) -> dict[str, object]:
        state = self._read_state(state_path)
        if provider_id == "digitalocean":
            return self._verify_digitalocean(state, credential_directory)
        if provider_id == "scaleway":
            return self._verify_scaleway(state, credential_directory)
        return {
            "status": "unsupported",
            "message": "Read-only legacy cloud verification is not available for this provider.",
            "resources": [],
        }

    def _verify_digitalocean(self, state: dict[str, Any], directory: Path) -> dict[str, object]:
        token = self._tfvar(directory / "terraform.tfvars", "do_token") or self._environment(
            "DIGITALOCEAN_TOKEN", "DIGITALOCEAN_ACCESS_TOKEN"
        )
        if not token:
            return self._unavailable("credentials_unavailable", "DigitalOcean credentials are not configured.")
        endpoints = {
            "digitalocean_droplet": "droplets",
            "digitalocean_reserved_ip": "reserved_ips",
            "digitalocean_floating_ip": "floating_ips",
            "digitalocean_firewall": "firewalls",
            "digitalocean_ssh_key": "account/keys",
        }
        return self._verify_resources(
            state,
            "digitalocean_",
            endpoints,
            lambda endpoint, identifier, _attributes: (
                f"https://api.digitalocean.com/v2/{endpoint}/{urllib.parse.quote(identifier, safe='')}"
            ),
            {"Authorization": f"Bearer {token}"},
            wrapper_by_type={"digitalocean_droplet": "droplet", "digitalocean_firewall": "firewall"},
        )

    def _verify_scaleway(self, state: dict[str, Any], directory: Path) -> dict[str, object]:
        access_key = self._tfvar(directory / "terraform.tfvars", "scaleway_access_key") or self._environment(
            "SCW_ACCESS_KEY"
        )
        secret_key = self._tfvar(directory / "terraform.tfvars", "scaleway_secret_key") or self._environment(
            "SCW_SECRET_KEY"
        )
        project_id = self._tfvar(directory / "terraform.tfvars", "scaleway_project_id") or self._environment(
            "SCW_DEFAULT_PROJECT_ID"
        )
        if not access_key or not secret_key or not project_id:
            return self._unavailable("credentials_unavailable", "Scaleway credentials are not configured.")
        for resource in self._managed_instances(state):
            attributes = resource[3]
            state_project = attributes.get("project_id")
            if isinstance(state_project, str) and state_project and state_project != project_id:
                return self._unavailable(
                    "identity_mismatch",
                    "Configured Scaleway project does not match the legacy state project.",
                )
        endpoints = {
            "scaleway_instance_server": "servers",
            "scaleway_instance_ip": "ips",
            "scaleway_instance_security_group": "security_groups",
            "scaleway_account_ssh_key": "iam-ssh-key",
            "scaleway_iam_ssh_key": "iam-ssh-key",
        }

        def url(endpoint: str, identifier: str, attributes: dict[str, Any]) -> str:
            if endpoint == "iam-ssh-key":
                return f"https://api.scaleway.com/iam/v1alpha1/ssh-keys/{urllib.parse.quote(identifier, safe='')}"
            parts = identifier.split("/", 1)
            zone = parts[0] if len(parts) == 2 else str(attributes.get("zone", ""))
            resource_id = parts[1] if len(parts) == 2 else identifier
            if not zone:
                raise ValueError("Legacy Scaleway resource has no zone")
            return (
                f"https://api.scaleway.com/instance/v1/zones/{urllib.parse.quote(zone, safe='')}/"
                f"{endpoint}/{urllib.parse.quote(resource_id, safe='')}"
            )

        return self._verify_resources(
            state,
            "scaleway_",
            endpoints,
            url,
            {"X-Auth-Token": secret_key},
            composite_ids=True,
        )

    def _verify_resources(
        self,
        state: dict[str, Any],
        provider_prefix: str,
        endpoints: dict[str, str],
        url_builder: Callable[[str, str, dict[str, Any]], str],
        headers: dict[str, str],
        *,
        wrapper_by_type: dict[str, str] | None = None,
        composite_ids: bool = False,
    ) -> dict[str, object]:
        evidence: list[dict[str, object]] = []
        cloud_count = 0
        overall = "all_absent"
        message = "No legacy cloud resources were found."
        for address, resource_type, identifier, attributes in self._managed_instances(state):
            if resource_type.startswith(_LOCAL_RESOURCE_PREFIXES):
                evidence.append(self._evidence(address, resource_type, identifier, "local_only"))
                continue
            if not resource_type.startswith(provider_prefix) or resource_type not in endpoints or not identifier:
                evidence.append(self._evidence(address, resource_type, identifier, "unsupported"))
                overall = "api_unavailable"
                message = "One or more legacy resource types could not be verified safely."
                continue
            cloud_count += 1
            try:
                response = self.transport(url_builder(endpoints[resource_type], identifier, attributes), headers)
            except (RuntimeError, ValueError):
                evidence.append(self._evidence(address, resource_type, identifier, "api_unavailable"))
                overall = "api_unavailable"
                message = "Cloud verification could not reach every required read-only endpoint."
                continue
            if response.status == 404:
                evidence.append(self._evidence(address, resource_type, identifier, "absent"))
            elif response.status in {401, 403}:
                evidence.append(self._evidence(address, resource_type, identifier, "credentials_unavailable"))
                overall = "credentials_unavailable"
                message = "Provider credentials could not verify the legacy resources."
            elif response.status == 200:
                payload = response.payload or {}
                wrapper = (wrapper_by_type or {}).get(resource_type)
                item = payload.get(wrapper) if wrapper else payload
                expected_id = identifier.split("/", 1)[-1] if composite_ids else identifier
                actual_id = item.get("id") if isinstance(item, dict) else None
                if actual_id is not None and str(actual_id) != expected_id:
                    evidence.append(self._evidence(address, resource_type, identifier, "identity_mismatch"))
                    overall = "identity_mismatch"
                    message = "A provider response did not match the legacy resource identity."
                else:
                    evidence.append(self._evidence(address, resource_type, identifier, "exists"))
                    overall = "resources_exist"
                    message = "Legacy cloud resources still exist."
            else:
                evidence.append(self._evidence(address, resource_type, identifier, "api_unavailable"))
                overall = "api_unavailable"
                message = "Cloud verification returned an inconclusive response."
        if cloud_count == 0:
            overall = "api_unavailable"
            message = "The legacy state contains no supported cloud resource identity to verify."
        return {"status": overall, "message": message, "resources": evidence}

    @staticmethod
    def _read_state(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Legacy state cannot be parsed for cloud verification") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Legacy state cannot be parsed for cloud verification")
        return value

    @staticmethod
    def _managed_instances(state: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
        result: list[tuple[str, str, str, dict[str, Any]]] = []
        resources = state.get("resources", [])
        if not isinstance(resources, list):
            return result
        for resource in resources:
            if not isinstance(resource, dict) or resource.get("mode", "managed") != "managed":
                continue
            resource_type = resource.get("type")
            name = resource.get("name")
            instances = resource.get("instances")
            if not isinstance(resource_type, str) or not isinstance(name, str) or not isinstance(instances, list):
                continue
            for index, instance in enumerate(instances):
                if not isinstance(instance, dict):
                    continue
                attributes = instance.get("attributes")
                if not isinstance(attributes, dict):
                    attributes = {}
                identifier = attributes.get("id")
                safe_identifier = str(identifier)[:256] if isinstance(identifier, (str, int)) else ""
                address = f"{resource_type}.{name}" + (f"[{index}]" if len(instances) > 1 else "")
                result.append((address, resource_type, safe_identifier, attributes))
        return result

    @staticmethod
    def _tfvar(path: Path, name: str) -> str | None:
        try:
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                return None
            assignments = dict(_TFVARS_STRING.findall(path.read_text(encoding="utf-8")))
            encoded = assignments.get(name)
            value = json.loads(encoded) if encoded else None
            return value.strip() if isinstance(value, str) and value.strip() else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _environment(*names: str) -> str | None:
        return next((value for name in names if (value := os.getenv(name, "").strip())), None)

    @staticmethod
    def _evidence(address: str, resource_type: str, identifier: str, status: str) -> dict[str, object]:
        return {"address": address, "type": resource_type, "identifier": identifier, "status": status}

    @staticmethod
    def _unavailable(status: str, message: str) -> dict[str, object]:
        return {"status": status, "message": message, "resources": []}
