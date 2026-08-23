from __future__ import annotations

import json
from pathlib import Path

import pytest
from legacy_cloud_verification import LegacyCloudVerifier, ReadOnlyResponse


def write_state(path: Path, resources: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"version": 4, "lineage": "legacy", "serial": 7, "outputs": {}, "resources": resources}),
        encoding="utf-8",
    )


def resource(resource_type: str, identifier: str, **attributes: str) -> dict[str, object]:
    return {
        "mode": "managed",
        "type": resource_type,
        "name": "vpn",
        "instances": [{"attributes": {"id": identifier, **attributes}}],
    }


def test_digitalocean_exact_resource_absence_and_local_resources(tmp_path: Path) -> None:
    state = tmp_path / "terraform.tfstate.backup"
    write_state(
        state,
        [resource("digitalocean_droplet", "575015000"), resource("wireguard_asymmetric_key", "public-id")],
    )
    (tmp_path / "terraform.tfvars").write_text('do_token = "unit-test-token"\n', encoding="utf-8")
    urls: list[str] = []

    def absent(url: str, headers: dict[str, str]) -> ReadOnlyResponse:
        urls.append(url)
        assert headers["Authorization"] == "Bearer unit-test-token"
        return ReadOnlyResponse(404, None)

    result = LegacyCloudVerifier(absent).verify("digitalocean", state, tmp_path)
    assert result["status"] == "all_absent"
    assert urls == ["https://api.digitalocean.com/v2/droplets/575015000"]
    evidence = result["resources"]
    assert isinstance(evidence, list)
    assert [item["status"] for item in evidence] == ["absent", "local_only"]
    assert "unit-test-token" not in str(result)


def test_one_existing_resource_prevents_absence_confirmation(tmp_path: Path) -> None:
    state = tmp_path / "terraform.tfstate.backup"
    write_state(state, [resource("digitalocean_droplet", "42")])
    (tmp_path / "terraform.tfvars").write_text('do_token = "unit-test-token"\n', encoding="utf-8")
    result = LegacyCloudVerifier(
        lambda *_args: ReadOnlyResponse(200, {"droplet": {"id": 42, "name": "legacy"}})
    ).verify("digitalocean", state, tmp_path)
    assert result["status"] == "resources_exist"
    assert result["resources"][0]["status"] == "exists"  # type: ignore[index]


def test_api_unavailable_is_inconclusive_and_secret_safe(tmp_path: Path) -> None:
    state = tmp_path / "terraform.tfstate.backup"
    write_state(state, [resource("digitalocean_droplet", "42")])
    (tmp_path / "terraform.tfvars").write_text('do_token = "unit-test-token"\n', encoding="utf-8")

    def unavailable(*_args: object) -> ReadOnlyResponse:
        raise RuntimeError("network unavailable unit-test-token")

    result = LegacyCloudVerifier(unavailable).verify("digitalocean", state, tmp_path)
    assert result["status"] == "api_unavailable"
    assert "unit-test-token" not in str(result)


def test_missing_credentials_never_calls_provider(tmp_path: Path) -> None:
    state = tmp_path / "terraform.tfstate.backup"
    write_state(state, [resource("digitalocean_droplet", "42")])
    verifier = LegacyCloudVerifier(lambda *_args: pytest.fail("provider must not be called"))
    assert verifier.verify("digitalocean", state, tmp_path)["status"] == "credentials_unavailable"


def test_scaleway_project_identity_mismatch_prevents_queries(tmp_path: Path) -> None:
    state = tmp_path / "terraform.tfstate.backup"
    write_state(
        state,
        [resource("scaleway_instance_server", "fr-par-1/server-id", project_id="state-project")],
    )
    (tmp_path / "terraform.tfvars").write_text(
        "\n".join(
            (
                'scaleway_access_key = "access"',
                'scaleway_secret_key = "secret"',
                'scaleway_project_id = "different-project"',
            )
        ),
        encoding="utf-8",
    )
    verifier = LegacyCloudVerifier(lambda *_args: pytest.fail("mismatched project must not be queried"))
    assert verifier.verify("scaleway", state, tmp_path)["status"] == "identity_mismatch"


@pytest.mark.parametrize(
    ("resource_type", "state_id", "expected_suffix"),
    [
        ("scaleway_instance_server", "fr-par-1/server", "/servers/server"),
        ("scaleway_instance_ip", "fr-par-1/ip", "/ips/ip"),
        ("scaleway_instance_security_group", "fr-par-1/group", "/security_groups/group"),
        ("scaleway_account_ssh_key", "key", "/iam/v1alpha1/ssh-keys/key"),
    ],
)
def test_scaleway_uses_exact_resource_endpoints(
    tmp_path: Path, resource_type: str, state_id: str, expected_suffix: str
) -> None:
    state = tmp_path / "terraform.tfstate.backup"
    write_state(state, [resource(resource_type, state_id, project_id="project")])
    (tmp_path / "terraform.tfvars").write_text(
        "\n".join(
            (
                'scaleway_access_key = "access"',
                'scaleway_secret_key = "secret"',
                'scaleway_project_id = "project"',
            )
        ),
        encoding="utf-8",
    )
    urls: list[str] = []

    def absent(url: str, headers: dict[str, str]) -> ReadOnlyResponse:
        urls.append(url)
        assert headers["X-Auth-Token"] == "secret"
        return ReadOnlyResponse(404, None)

    result = LegacyCloudVerifier(absent).verify("scaleway", state, tmp_path)
    assert result["status"] == "all_absent"
    assert urls[0].endswith(expected_suffix)
