from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from terraform_runner import TerraformError

REQUIRED_TERRAFORM_OUTPUTS = {
    "vpn_public_ip": str,
    "server_public_key": str,
    "readiness_hint": str,
    "resource_ids": dict,
}


@dataclass(frozen=True)
class ProviderOutputs:
    vpn_public_ip: str
    server_public_key: str
    readiness_hint: str
    resource_ids: dict[str, Any]


def validate_provider_outputs(outputs: dict[str, object], *, provider_id: str, deployment_id: str) -> ProviderOutputs:
    problems: list[str] = []
    values: dict[str, Any] = {}
    for name, expected_type in REQUIRED_TERRAFORM_OUTPUTS.items():
        item = outputs.get(name)
        if not isinstance(item, dict) or "value" not in item:
            problems.append(f"{name}: missing")
            continue
        value = item["value"]
        if not isinstance(value, expected_type):
            problems.append(f"{name}: expected {expected_type.__name__}, received {type(value).__name__}")
            continue
        if isinstance(value, str) and not value.strip():
            problems.append(f"{name}: must not be empty")
            continue
        values[name] = value
    if problems:
        expected = ", ".join(REQUIRED_TERRAFORM_OUTPUTS)
        received = ", ".join(sorted(outputs)) or "none"
        raise TerraformError(
            f"Terraform output contract failed for provider {provider_id}, deployment {deployment_id}. "
            f"Expected keys: {expected}. Received keys: {received}. Problems: {'; '.join(problems)}"
        )
    return ProviderOutputs(
        vpn_public_ip=values["vpn_public_ip"],
        server_public_key=values["server_public_key"],
        readiness_hint=values["readiness_hint"],
        resource_ids=values["resource_ids"],
    )
