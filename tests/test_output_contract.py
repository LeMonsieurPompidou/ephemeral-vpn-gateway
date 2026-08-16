from pathlib import Path

import pytest
from output_contract import REQUIRED_TERRAFORM_OUTPUTS, validate_provider_outputs
from terraform_runner import TerraformError

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("vpn-aws-lightsail", "vpn-digitalocean", "vpn-scaleway")


def valid_outputs() -> dict[str, object]:
    return {
        "vpn_public_ip": {"value": "203.0.113.1"},
        "server_public_key": {"value": "public"},
        "readiness_hint": {"value": "/ready"},
        "resource_ids": {"value": {"server": "one"}},
    }


def test_all_terraform_roots_declare_backend_and_output_contract() -> None:
    for provider in PROVIDERS:
        main = (ROOT / provider / "main.tf").read_text(encoding="utf-8")
        providers = (ROOT / provider / "providers.tf").read_text(encoding="utf-8")
        assert 'backend "local"' in providers
        for output in REQUIRED_TERRAFORM_OUTPUTS:
            assert f'output "{output}"' in main


@pytest.mark.parametrize("missing", REQUIRED_TERRAFORM_OUTPUTS)
def test_contract_reports_every_missing_key(missing: str) -> None:
    outputs = valid_outputs()
    del outputs[missing]
    with pytest.raises(TerraformError) as error:
        validate_provider_outputs(outputs, provider_id="test-provider", deployment_id="test-deployment")
    message = str(error.value)
    assert missing in message and "test-provider" in message and "test-deployment" in message
    assert "Received keys" in message


@pytest.mark.parametrize(
    ("name", "value"),
    [("vpn_public_ip", 1), ("server_public_key", ""), ("readiness_hint", None), ("resource_ids", [])],
)
def test_contract_rejects_wrong_types_and_empty_strings(name: str, value: object) -> None:
    outputs = valid_outputs()
    outputs[name] = {"value": value}
    with pytest.raises(TerraformError, match=name):
        validate_provider_outputs(outputs, provider_id="provider", deployment_id="deployment")
