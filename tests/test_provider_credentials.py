from __future__ import annotations

from pathlib import Path

import credential_preflight
import providers
import pytest
from catalog import ProviderCatalog
from credential_preflight import CredentialCheck, CredentialNetworkError, digitalocean_check, scaleway_check
from helpers import make_orchestrator, make_resource_root
from models import DeploymentOptions
from providers import ProviderRegistry

ROOT = Path(__file__).resolve().parents[1]
SECRET_VALUES = ("dop_v1_unit_test_token", "unit-test-scaleway-secret", "SCWUNITTESTACCESS")


def registry() -> ProviderRegistry:
    result = ProviderRegistry(ProviderCatalog(ROOT / "vpn-gui-app" / "provider_catalog.json"))
    # Unit tests must not depend on a developer's ignored provider tfvars files.
    result.get("digitalocean").credential_file = None  # type: ignore[attr-defined]
    result.get("scaleway").credential_file = None  # type: ignore[attr-defined]
    return result


def registry_for_root(root: Path) -> ProviderRegistry:
    return ProviderRegistry(ProviderCatalog(root / "vpn-gui-app" / "provider_catalog.json"))


def clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DIGITALOCEAN_TOKEN",
        "DIGITALOCEAN_ACCESS_TOKEN",
        "SCW_ACCESS_KEY",
        "SCW_SECRET_KEY",
        "SCW_DEFAULT_PROJECT_ID",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)


def assert_secret_free(result: CredentialCheck | dict[str, object]) -> None:
    rendered = str(result)
    assert all(secret not in rendered for secret in SECRET_VALUES)


def test_digitalocean_prefers_canonical_token_and_accepts_supported_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_provider_environment(monkeypatch)
    seen: list[str] = []

    def accept_digitalocean(token: str) -> CredentialCheck:
        seen.append(token)
        return CredentialCheck(True, "valid", "DigitalOcean credentials are valid.")

    monkeypatch.setattr(
        providers,
        "digitalocean_check",
        accept_digitalocean,
    )
    adapter = registry().get("digitalocean")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", SECRET_VALUES[0])
    monkeypatch.setenv("DIGITALOCEAN_ACCESS_TOKEN", "fallback-token")
    assert adapter.validate_credentials()[0]
    assert seen == [SECRET_VALUES[0]]

    seen.clear()
    monkeypatch.delenv("DIGITALOCEAN_TOKEN")
    assert adapter.validate_credentials()[0]
    assert seen == ["fallback-token"]


def test_digitalocean_missing_credentials_returns_canonical_setup_only(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_environment(monkeypatch)
    result = registry().get("digitalocean").credential_details()
    assert not result.valid and result.reason == "missing"
    assert result.missing_variables == ("DIGITALOCEAN_TOKEN",)
    assert result.setup_commands == ('$env:DIGITALOCEAN_TOKEN = "<your-token>"',)
    assert "same terminal" in " ".join(result.notes)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(200, "valid"), (401, "invalid"), (403, "invalid"), (429, "api"), (500, "api")],
)
def test_digitalocean_api_result_classification(monkeypatch: pytest.MonkeyPatch, status: int, reason: str) -> None:
    monkeypatch.setattr(credential_preflight, "read_only_api_status", lambda *_args, **_kwargs: status)
    result = digitalocean_check(SECRET_VALUES[0])
    assert result.reason == reason
    assert_secret_free(result)


def test_provider_network_failure_is_distinct_and_secret_free(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> int:
        raise CredentialNetworkError("unit test")

    monkeypatch.setattr(credential_preflight, "read_only_api_status", unavailable)
    digitalocean = digitalocean_check(SECRET_VALUES[0])
    scaleway = scaleway_check(SECRET_VALUES[1], "11111111-1111-1111-1111-111111111111")
    assert digitalocean.reason == scaleway.reason == "network"
    assert_secret_free(digitalocean)
    assert_secret_free(scaleway)


@pytest.mark.parametrize("missing_name", ["SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_PROJECT_ID"])
def test_scaleway_reports_each_missing_variable_by_name_only(
    monkeypatch: pytest.MonkeyPatch, missing_name: str
) -> None:
    clear_provider_environment(monkeypatch)
    values = {
        "SCW_ACCESS_KEY": SECRET_VALUES[2],
        "SCW_SECRET_KEY": SECRET_VALUES[1],
        "SCW_DEFAULT_PROJECT_ID": "11111111-1111-1111-1111-111111111111",
    }
    for name, value in values.items():
        if name != missing_name:
            monkeypatch.setenv(name, value)
    result = registry().get("scaleway").credential_details()
    assert not result.valid and result.reason == "missing"
    assert result.missing_variables == (missing_name,)
    assert missing_name in result.setup_commands[0]
    assert_secret_free(result)


def test_scaleway_reports_multiple_missing_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_environment(monkeypatch)
    monkeypatch.setenv("SCW_ACCESS_KEY", SECRET_VALUES[2])
    result = registry().get("scaleway").credential_details()
    assert result.missing_variables == ("SCW_SECRET_KEY", "SCW_DEFAULT_PROJECT_ID")


def test_scaleway_present_credentials_validate_secret_and_project_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_provider_environment(monkeypatch)
    project = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("SCW_ACCESS_KEY", SECRET_VALUES[2])
    monkeypatch.setenv("SCW_SECRET_KEY", SECRET_VALUES[1])
    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", project)
    seen: list[tuple[str, str]] = []

    def accept_scaleway(secret: str, project_id: str) -> CredentialCheck:
        seen.append((secret, project_id))
        return CredentialCheck(True, "valid", "Scaleway credentials and project are valid.")

    monkeypatch.setattr(providers, "scaleway_check", accept_scaleway)
    result = registry().get("scaleway").credential_details()
    assert result.valid and seen == [(SECRET_VALUES[1], project)]
    assert_secret_free(result)


def test_scaleway_rejects_malformed_project_id_before_api_request(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_environment(monkeypatch)
    monkeypatch.setenv("SCW_ACCESS_KEY", SECRET_VALUES[2])
    monkeypatch.setenv("SCW_SECRET_KEY", SECRET_VALUES[1])
    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "../../not-a-project")
    monkeypatch.setattr(
        providers,
        "scaleway_check",
        lambda *_args: pytest.fail("malformed project ID must not reach the provider request"),
    )
    result = registry().get("scaleway").credential_details()
    assert not result.valid and result.reason == "invalid"
    assert "SCW_DEFAULT_PROJECT_ID" in result.message
    assert_secret_free(result)


@pytest.mark.parametrize(
    ("status", "reason", "message"),
    [
        (200, "valid", "valid"),
        (401, "invalid", "rejected"),
        (403, "invalid", "permission"),
        (404, "invalid", "project"),
        (500, "api", "unavailable"),
    ],
)
def test_scaleway_api_and_project_classification(
    monkeypatch: pytest.MonkeyPatch, status: int, reason: str, message: str
) -> None:
    monkeypatch.setattr(credential_preflight, "read_only_api_status", lambda *_args, **_kwargs: status)
    result = scaleway_check(SECRET_VALUES[1], "11111111-1111-1111-1111-111111111111")
    assert result.reason == reason and message in result.message.lower()
    assert_secret_free(result)


def test_provider_selection_checks_only_the_selected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_environment(monkeypatch)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", SECRET_VALUES[0])
    monkeypatch.setattr(
        providers,
        "digitalocean_check",
        lambda _token: CredentialCheck(True, "valid", "DigitalOcean credentials are valid."),
    )
    provider_registry = registry()
    assert provider_registry.get("digitalocean").credential_details().valid
    assert provider_registry.get("scaleway").credential_details().missing_variables == (
        "SCW_ACCESS_KEY",
        "SCW_SECRET_KEY",
        "SCW_DEFAULT_PROJECT_ID",
    )
    assert not provider_registry.get("aws-lightsail").credential_details().valid


def test_provider_environment_secrets_never_enter_terraform_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_environment(monkeypatch)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", SECRET_VALUES[0])
    monkeypatch.setenv("SCW_ACCESS_KEY", SECRET_VALUES[2])
    monkeypatch.setenv("SCW_SECRET_KEY", SECRET_VALUES[1])
    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "11111111-1111-1111-1111-111111111111")
    provider_registry = registry()
    for provider_id in ("digitalocean", "scaleway"):
        adapter = provider_registry.get(provider_id)
        variables = adapter.terraform_variables(adapter.list_locations()[0], DeploymentOptions())
        assert_secret_free(variables)
        assert not {"do_token", "scaleway_access_key", "scaleway_secret_key", "scaleway_project_id"} & variables.keys()


def test_main_compatible_digitalocean_tfvars_is_accepted_and_staged_securely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    orchestrator = make_orchestrator(tmp_path)
    source = orchestrator._provider_directory("digitalocean")
    legacy = source / "terraform.tfvars"
    content = f'do_token = "{SECRET_VALUES[0]}"\nssh_key_name = "existing-main-key"\n'
    legacy.write_text(content, encoding="utf-8")

    check = orchestrator.providers.get("digitalocean").credential_details()
    assert check.valid and check.reason == "configured" and check.source == "terraform_tfvars"

    result = orchestrator.deploy("digitalocean", "nyc3")
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    staged = runtime / "terraform-work" / "vpn-digitalocean" / "terraform.tfvars"

    assert staged.read_bytes() == legacy.read_bytes()
    assert SECRET_VALUES[0] not in (runtime / "deployment.auto.tfvars.json").read_text(encoding="utf-8")
    assert SECRET_VALUES[0] not in orchestrator.deployments.path.read_text(encoding="utf-8")
    assert SECRET_VALUES[0] not in (runtime / "deployment.log").read_text(encoding="utf-8")


def test_main_compatible_scaleway_tfvars_is_accepted_with_project_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    root = make_resource_root(tmp_path)
    tfvars = root / "vpn-scaleway" / "terraform.tfvars"
    tfvars.write_text(
        "\n".join(
            (
                f'scaleway_access_key = "{SECRET_VALUES[2]}"',
                f'scaleway_secret_key = "{SECRET_VALUES[1]}"',
                'scaleway_project_id = "11111111-1111-1111-1111-111111111111"',
                "",
            )
        ),
        encoding="utf-8",
    )

    result = registry_for_root(root).get("scaleway").credential_details()
    assert result.valid and result.reason == "configured" and result.source == "terraform_tfvars"
    assert_secret_free(result)


def test_incomplete_main_tfvars_uses_environment_for_only_missing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_provider_environment(monkeypatch)
    root = make_resource_root(tmp_path)
    (root / "vpn-scaleway" / "terraform.tfvars").write_text(
        'scaleway_project_id = "11111111-1111-1111-1111-111111111111"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SCW_ACCESS_KEY", SECRET_VALUES[2])

    result = registry_for_root(root).get("scaleway").credential_details()
    assert not result.valid
    assert result.missing_variables == ("SCW_SECRET_KEY",)


def test_credential_source_is_independent_of_aws_and_client_count(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_environment(monkeypatch)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", SECRET_VALUES[0])
    monkeypatch.setattr(
        providers,
        "digitalocean_check",
        lambda _token: CredentialCheck(True, "valid", "DigitalOcean credentials are valid.", source="environment"),
    )
    adapter = registry().get("digitalocean")
    assert adapter.credential_details().source == "environment"
    location = adapter.list_locations()[0]
    one = adapter.terraform_variables(location, DeploymentOptions(client_count=1))
    ten = adapter.terraform_variables(location, DeploymentOptions(client_count=10))
    assert one == ten
    assert "do_token" not in one


@pytest.mark.parametrize(
    ("provider_id", "location_id", "environment"),
    [
        ("digitalocean", "nyc3", {"DIGITALOCEAN_TOKEN": SECRET_VALUES[0]}),
        (
            "scaleway",
            "fr-par-1",
            {
                "SCW_ACCESS_KEY": SECRET_VALUES[2],
                "SCW_SECRET_KEY": SECRET_VALUES[1],
                "SCW_DEFAULT_PROJECT_ID": "11111111-1111-1111-1111-111111111111",
            },
        ),
    ],
)
def test_environment_credentials_stay_out_of_generated_runtime_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    location_id: str,
    environment: dict[str, str],
) -> None:
    clear_provider_environment(monkeypatch)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    orchestrator = make_orchestrator(tmp_path)

    result = orchestrator.deploy(provider_id, location_id)
    record = orchestrator.deployments.get(str(result["deployment_id"]))
    runtime = Path(record.runtime_directory)
    rendered = "\n".join(
        (
            (runtime / "deployment.auto.tfvars.json").read_text(encoding="utf-8"),
            orchestrator.deployments.path.read_text(encoding="utf-8"),
            (runtime / "deployment.log").read_text(encoding="utf-8"),
        )
    )
    assert all(secret not in rendered for secret in environment.values())
    assert not (runtime / "terraform-work" / f"vpn-{provider_id}" / "terraform.tfvars").exists()


def test_scaleway_provider_derives_region_and_zone_from_selected_location() -> None:
    provider = (ROOT / "vpn-scaleway" / "providers.tf").read_text(encoding="utf-8")
    assert "zone       = var.region" in provider
    assert 'region     = join("-", slice(split("-", var.region), 0, 2))' in provider
