from __future__ import annotations

import json
from pathlib import Path

import pytest
from client_peers import (
    MAX_CLIENTS,
    SERVER_TUNNEL_IPV4,
    client_tunnel_ipv4,
    generate_client_peers,
    validate_client_count,
)
from helpers import FakeTerraformRunner, add_record, make_orchestrator
from models import DeploymentOptions, DeploymentRecord, DeploymentState
from security import generate_wireguard_keypair


def test_default_client_count_and_boundaries() -> None:
    assert DeploymentOptions().client_count == 1
    validate_client_count(1)
    validate_client_count(MAX_CLIENTS)
    for invalid in (0, 11, -1, True, 1.5):
        with pytest.raises(ValueError, match="between 1 and 10"):
            validate_client_count(invalid)  # type: ignore[arg-type]


def test_deterministic_client_addresses_are_unique_and_avoid_server() -> None:
    addresses = [client_tunnel_ipv4(index) for index in range(1, MAX_CLIENTS + 1)]
    assert addresses == [f"10.8.0.{index}" for index in range(2, 12)]
    assert len(addresses) == len(set(addresses))
    assert SERVER_TUNNEL_IPV4 == "10.8.0.1"
    assert SERVER_TUNNEL_IPV4 not in addresses


def test_duplicate_client_keypair_is_rejected_without_exposing_key() -> None:
    with pytest.raises(RuntimeError, match="duplicate identity") as raised:
        generate_client_peers(2, lambda: ("private-secret", "public-value"))
    assert "private-secret" not in str(raised.value)


@pytest.mark.parametrize("count", [1, 2, 10])
def test_every_client_receives_unique_keypair_and_configuration(count: int, tmp_path: Path) -> None:
    peers = generate_client_peers(count, generate_wireguard_keypair)
    assert len({peer.private_key for peer in peers}) == count
    assert len({peer.public_key for peer in peers}) == count
    assert len({peer.tunnel_ipv4 for peer in peers}) == count
    assert all(peer.private_key not in repr(peer) for peer in peers)
    orchestrator = make_orchestrator(tmp_path)
    configs = [
        orchestrator._render_client_config(
            "203.0.113.10",
            "server-public",
            peer.private_key,
            peer.tunnel_ipv4,
            DeploymentOptions(),
        )
        for peer in peers
    ]
    assert len(set(configs)) == count
    for peer, config in zip(peers, configs, strict=True):
        assert f"PrivateKey = {peer.private_key}" in config
        assert f"Address = {peer.tunnel_ipv4}/32" in config
        assert "AllowedIPs = 0.0.0.0/0" in config


@pytest.mark.parametrize(
    "provider_id,location_id", [("aws-lightsail", "us-east-1"), ("digitalocean", "nyc3"), ("scaleway", "fr-par-1")]
)
def test_public_peer_contract_reaches_terraform_without_client_private_keys(
    tmp_path: Path, provider_id: str, location_id: str
) -> None:
    orchestrator = make_orchestrator(tmp_path, FakeTerraformRunner())
    result = orchestrator.deploy(provider_id, location_id, DeploymentOptions(client_count=2))
    assert result["status"] == "success"
    assert "config" not in result
    assert result["clients"] == [
        {"id": "client-1", "index": 1, "display_name": "Client 1", "tunnel_ipv4": "10.8.0.2"},
        {"id": "client-2", "index": 2, "display_name": "Client 2", "tunnel_ipv4": "10.8.0.3"},
    ]
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    assert record.client_schema_version == 1
    assert [client.id for client in record.clients] == ["client-1", "client-2"]
    assert [client.tunnel_ipv4 for client in record.clients] == ["10.8.0.2", "10.8.0.3"]

    private_values = [
        runtime.joinpath(*client.private_key_relative_path.split("/")).read_text(encoding="ascii").strip()
        for client in record.clients
    ]
    assert len(set(private_values)) == 2
    variables_text = (runtime / "deployment.auto.tfvars.json").read_text(encoding="utf-8")
    variables = json.loads(variables_text)
    assert "client_public_key" not in variables
    assert [peer["tunnel_ipv4"] for peer in variables["client_peers"]] == ["10.8.0.2", "10.8.0.3"]
    assert len({peer["public_key"] for peer in variables["client_peers"]}) == 2
    assert variables["user_data_payload"].count("[Peer]") == 2
    registry_text = orchestrator.deployments.path.read_text(encoding="utf-8")
    log_text = (runtime / "deployment.log").read_text(encoding="utf-8")
    state_text = Path(record.state_path).read_text(encoding="utf-8")
    for private_key in private_values:
        assert private_key not in variables_text
        assert private_key not in registry_text
        assert private_key not in log_text
        assert private_key not in state_text


def test_ready_client_metadata_survives_restart_and_destroy_removes_all_client_secrets(tmp_path: Path) -> None:
    root_orchestrator = make_orchestrator(tmp_path)
    result = root_orchestrator.deploy("aws-lightsail", "us-east-1", DeploymentOptions(client_count=3))
    record = root_orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    assert len(root_orchestrator.list_client_configs(record.id)) == 3

    restarted = type(root_orchestrator)(
        root_orchestrator.resource_root,
        root_orchestrator.runtime_root,
        root_orchestrator.runner,
        ip_detector=lambda: "198.51.100.10/32",
    )
    assert restarted.list_client_configs(record.id) == root_orchestrator.list_client_configs(record.id)
    assert root_orchestrator.destroy(record.id)["status"] == "success"
    assert not (runtime / "clients").exists()
    assert not list(runtime.rglob("*.privatekey"))
    assert not list(runtime.rglob("client.conf"))


def test_historical_single_client_record_uses_legacy_runtime_paths(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail", "historical")
    record.state = DeploymentState.READY
    orchestrator.deployments.save(record)
    runtime = Path(record.runtime_directory)
    (runtime / "client.conf").write_text("historical-config", encoding="utf-8")
    serialized = record.to_dict()
    serialized.pop("clients")
    serialized.pop("client_schema_version")
    historical = DeploymentRecord.from_dict(serialized)
    orchestrator.deployments.save(historical)
    assert orchestrator.list_client_configs(record.id) == [
        {
            "id": "client-1",
            "index": 1,
            "display_name": "Client 1",
            "tunnel_ipv4": "10.8.0.2",
        }
    ]
    assert orchestrator.get_client_config(record.id, "client-1") == "historical-config"


def test_registry_snapshots_numeric_catalog_pricing_for_restart(tmp_path: Path) -> None:
    priced = make_orchestrator(tmp_path / "priced")
    aws = priced.reserve_deployment("aws-lightsail", "us-east-1", DeploymentOptions())
    assert aws.estimated_hourly_cost_usd == 0.005
    assert priced.deployments.get(aws.id).estimated_hourly_cost_usd == 0.005

    unknown = make_orchestrator(tmp_path / "unknown")
    scaleway = unknown.reserve_deployment("scaleway", "fr-par-1", DeploymentOptions())
    assert scaleway.estimated_hourly_cost_usd is None
