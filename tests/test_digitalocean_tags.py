from __future__ import annotations

import re
from pathlib import Path

from orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "vpn-digitalocean" / "main.tf").read_text(encoding="utf-8")
VARIABLES = (ROOT / "vpn-digitalocean" / "variables.tf").read_text(encoding="utf-8")
TAG_PATTERN = re.compile(r"^[a-z0-9:_-]+$")


def digitalocean_tags(deployment_id: str, *, expiration: str | None = None, client_count: int = 1) -> list[str]:
    del expiration, client_count  # Local lifetime and peer count are deliberately not provider tags.
    return ["wireguard", "ephemeral-vpn", f"deployment:{deployment_id}"]


def test_digitalocean_static_and_uuid_tags_match_provider_contract() -> None:
    deployment_id = "12345678-abcd-4abc-8abc-1234567890ab"
    tags = digitalocean_tags(deployment_id)
    assert tags == ["wireguard", "ephemeral-vpn", f"deployment:{deployment_id}"]
    assert all(TAG_PATTERN.fullmatch(tag) and len(tag) <= 255 for tag in tags)
    assert 'digitalocean_tags      = ["wireguard", "ephemeral-vpn", "deployment:${var.deployment_id}"]' in MAIN
    assert "tags      = local.digitalocean_tags" in MAIN
    assert 'regex("^[a-z0-9:_-]+$", tag)' in MAIN


def test_lifetime_never_changes_digitalocean_tags() -> None:
    deployment_id = "12345678-abcd-4abc-8abc-1234567890ab"
    without_expiration = digitalocean_tags(deployment_id, expiration=None)
    with_rfc3339_expiration = digitalocean_tags(deployment_id, expiration="2026-08-23T18:10:24.123456+00:00")
    assert without_expiration == with_rfc3339_expiration
    assert "expires-at:" not in MAIN
    assert "var.expires_at" not in MAIN
    assert 'variable "expires_at"' in VARIABLES  # Shared deployment input remains compatible.


def test_no_rfc3339_or_client_count_characters_reach_digitalocean_tags() -> None:
    deployment_id = "12345678-abcd-4abc-8abc-1234567890ab"
    tag_sets = {tuple(digitalocean_tags(deployment_id, client_count=client_count)) for client_count in (1, 2, 10)}
    assert len(tag_sets) == 1
    tags = list(tag_sets.pop())
    assert all(TAG_PATTERN.fullmatch(tag) for tag in tags)
    assert all(character not in "TZ.+" for tag in tags for character in tag)
    assert "client_peers" not in MAIN.split("digitalocean_tags", 1)[1].splitlines()[0]


def test_deployment_id_validation_leaves_aws_and_scaleway_metadata_unchanged() -> None:
    assert 'length(var.deployment_id) <= 244 && can(regex("^[a-z0-9_-]+$", var.deployment_id))' in VARIABLES
    aws = (ROOT / "vpn-aws-lightsail" / "main.tf").read_text(encoding="utf-8")
    scaleway = (ROOT / "vpn-scaleway" / "main.tf").read_text(encoding="utf-8")
    assert 'ExpiresAt = coalesce(var.expires_at, "disabled")' in aws
    assert '"expires-at:${var.expires_at}"' in scaleway


def test_digitalocean_tag_diagnostic_is_sanitized_for_the_gui() -> None:
    raw = RuntimeError(
        "Terraform exited with code 1: tags may contain lowercase letters, numbers, colons, dashes, and underscores"
    )
    message = Orchestrator._deployment_failure_message("digitalocean", raw, True)
    assert message == (
        "DigitalOcean rejected the generated resource metadata. Cloud resources may exist and must be destroyed."
    )
    assert "Terraform exited" not in message


def test_other_provider_errors_are_not_reclassified_as_digitalocean_metadata_errors() -> None:
    raw = RuntimeError("provider-specific failure")
    assert Orchestrator._deployment_failure_message("aws-lightsail", raw, False) == "provider-specific failure"
    assert Orchestrator._deployment_failure_message("scaleway", raw, False) == "provider-specific failure"
