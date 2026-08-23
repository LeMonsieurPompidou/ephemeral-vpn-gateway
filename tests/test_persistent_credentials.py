from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

import orchestrator as orchestrator_module
import providers
import pytest
from app_settings import AppSettings, SettingsStore
from credential_preflight import CredentialCheck
from credential_resolver import CredentialResolver
from credential_store import (
    DIGITALOCEAN_TOKEN_TARGET,
    SCALEWAY_ACCESS_KEY_TARGET,
    SCALEWAY_SECRET_KEY_TARGET,
    CredentialStoreError,
    WindowsCredentialStore,
    _CredentialW,
)
from helpers import add_record, make_resource_root


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def save(self, target: str, secret: str) -> None:
        self.values[target] = secret

    def get(self, target: str) -> str | None:
        return self.values.get(target)

    def delete(self, target: str) -> bool:
        return self.values.pop(target, None) is not None


class FakeCredentialApi:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.keepalive: list[object] = []

    def CredWriteW(self, pointer: Any, _flags: int) -> int:
        credential = ctypes.cast(pointer, ctypes.POINTER(_CredentialW)).contents
        self.values[str(credential.TargetName)] = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        return 1

    def CredReadW(self, target: str, _kind: int, _flags: int, output: Any) -> int:
        raw = self.values.get(target)
        if raw is None:
            ctypes.set_last_error(1168)
            return 0
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _CredentialW()
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        pointer = ctypes.pointer(credential)
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(_CredentialW)))[0] = pointer
        self.keepalive.extend((blob, credential, pointer))
        return 1

    def CredDeleteW(self, target: str, _kind: int, _flags: int) -> int:
        if target not in self.values:
            ctypes.set_last_error(1168)
            return 0
        del self.values[target]
        return 1

    def CredFree(self, _pointer: object) -> None:
        pass


def roots(root: Path) -> dict[str, Path]:
    return {
        "digitalocean": root / "vpn-digitalocean",
        "scaleway": root / "vpn-scaleway",
        "aws-lightsail": root / "vpn-aws-lightsail",
    }


def test_windows_credential_store_round_trip_is_namespaced_and_unicode_safe() -> None:
    api = FakeCredentialApi()
    store = WindowsCredentialStore(api)
    value = "unit-test-token-\u00e9"
    store.save(DIGITALOCEAN_TOKEN_TARGET, value)
    assert store.get(DIGITALOCEAN_TOKEN_TARGET) == value
    assert store.get(SCALEWAY_SECRET_KEY_TARGET) is None
    assert store.delete(DIGITALOCEAN_TOKEN_TARGET)
    assert store.get(DIGITALOCEAN_TOKEN_TARGET) is None


def test_windows_credential_store_rejects_invalid_targets_and_never_echoes_secret() -> None:
    store = WindowsCredentialStore(FakeCredentialApi())
    secret = "credential-that-must-not-leak"
    with pytest.raises(CredentialStoreError) as error:
        store.save("OtherApp/token", secret)
    assert secret not in str(error.value)


def test_settings_are_atomic_corruption_safe_and_secret_free(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings(aws_profile="heres-vpn", scaleway_project_id="11111111-1111-1111-1111-111111111111"))
    assert store.load().aws_profile == "heres-vpn"
    text = path.read_text(encoding="utf-8")
    assert "token" not in text and "secret_key" not in text
    assert not list(tmp_path.glob("*.tmp"))
    path.write_text("{not json", encoding="utf-8")
    assert store.load() == AppSettings()


def test_packaged_resolution_precedence_environment_then_credential_manager(tmp_path: Path) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    secrets.save(DIGITALOCEAN_TOKEN_TARGET, "saved-token")
    environment = {"DIGITALOCEAN_TOKEN": "environment-token"}
    resolver = CredentialResolver(
        secrets, SettingsStore(tmp_path / "settings.json"), roots(root), packaged=True, environment=environment
    )
    assert resolver.resolve("digitalocean").values["token"] == "environment-token"
    environment.clear()
    assert resolver.resolve("digitalocean").source == "windows_credential_manager"


def test_source_resolution_retains_tfvars_after_environment_and_store(tmp_path: Path) -> None:
    root = make_resource_root(tmp_path)
    (root / "vpn-digitalocean" / "terraform.tfvars").write_text('do_token = "legacy"\n', encoding="utf-8")
    resolver = CredentialResolver(
        MemoryCredentialStore(), SettingsStore(tmp_path / "settings.json"), roots(root), packaged=False, environment={}
    )
    assert resolver.resolve("digitalocean").source == "terraform_tfvars"
    packaged = CredentialResolver(
        MemoryCredentialStore(), SettingsStore(tmp_path / "settings2.json"), roots(root), packaged=True, environment={}
    )
    assert not packaged.resolve("digitalocean").configured


def test_scaleway_saved_credentials_and_aws_profile_resolve_without_secret_settings(tmp_path: Path) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    secrets.save(SCALEWAY_ACCESS_KEY_TARGET, "access")
    secrets.save(SCALEWAY_SECRET_KEY_TARGET, "secret")
    settings = SettingsStore(tmp_path / "settings.json")
    settings.save(AppSettings(aws_profile="heres-vpn", scaleway_project_id="11111111-1111-1111-1111-111111111111"))
    resolver = CredentialResolver(secrets, settings, roots(root), packaged=True, environment={})
    assert resolver.resolve("scaleway").source == "windows_credential_manager"
    assert resolver.resolve("aws-lightsail").values == {"profile": "heres-vpn"}
    serialized = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "access" not in serialized and "secret" not in serialized


def test_save_and_check_persists_only_valid_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    instance = orchestrator_module.Orchestrator(root, tmp_path / "runtime", credential_store=secrets, packaged=True)
    monkeypatch.setattr(
        orchestrator_module, "digitalocean_check", lambda _token: CredentialCheck(False, "invalid", "Rejected")
    )
    result = instance.save_provider_credentials("digitalocean", {"token": "bad-token"})
    assert not result["saved"] and not secrets.values
    monkeypatch.setattr(
        orchestrator_module, "digitalocean_check", lambda _token: CredentialCheck(True, "valid", "Valid")
    )
    result = instance.save_provider_credentials("digitalocean", {"token": "good-token"})
    assert result["saved"] and secrets.values[DIGITALOCEAN_TOKEN_TARGET] == "good-token"
    assert "good-token" not in str(result)


def test_check_and_terraform_share_the_same_resolved_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    secrets.save(DIGITALOCEAN_TOKEN_TARGET, "saved-token")
    instance = orchestrator_module.Orchestrator(root, tmp_path / "runtime", credential_store=secrets, packaged=True)
    seen: list[str] = []

    def accept(token: str) -> CredentialCheck:
        seen.append(token)
        return CredentialCheck(True, "valid", "Valid")

    monkeypatch.setattr(providers, "digitalocean_check", accept)
    checked = instance.validate_credentials("digitalocean")
    record = add_record(instance, "digitalocean")
    assert checked["valid"] and checked["source"] == "windows_credential_manager"
    assert seen == ["saved-token"]
    assert instance._terraform_env(record)["TF_VAR_do_token"] == "saved-token"


def test_saved_scaleway_credentials_are_injected_only_into_child_environment(tmp_path: Path) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    secrets.save(SCALEWAY_ACCESS_KEY_TARGET, "access-value")
    secrets.save(SCALEWAY_SECRET_KEY_TARGET, "secret-value")
    runtime = tmp_path / "runtime"
    settings = SettingsStore(runtime / "settings.json")
    settings.update(scaleway_project_id="11111111-1111-1111-1111-111111111111")
    instance = orchestrator_module.Orchestrator(root, runtime, credential_store=secrets, packaged=True)
    record = add_record(instance, "scaleway")
    environment = instance._terraform_env(record)
    assert environment["TF_VAR_scaleway_access_key"] == "access-value"
    assert environment["TF_VAR_scaleway_secret_key"] == "secret-value"
    assert environment["TF_VAR_scaleway_project_id"] == "11111111-1111-1111-1111-111111111111"
    serialized = instance.deployments.path.read_text(encoding="utf-8") + settings.path.read_text(encoding="utf-8")
    assert "access-value" not in serialized and "secret-value" not in serialized


def test_active_deployment_requires_confirmation_before_removal(tmp_path: Path) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    secrets.save(DIGITALOCEAN_TOKEN_TARGET, "saved-token")
    instance = orchestrator_module.Orchestrator(root, tmp_path / "runtime", credential_store=secrets, packaged=True)
    record = add_record(instance, "digitalocean")
    record.resources_possible = True
    instance.deployments.save(record)
    result = instance.remove_provider_credentials("digitalocean")
    assert result["requires_confirmation"] and DIGITALOCEAN_TOKEN_TARGET in secrets.values
    result = instance.remove_provider_credentials("digitalocean", confirmed_active=True)
    assert result["removed"] and DIGITALOCEAN_TOKEN_TARGET not in secrets.values


def test_ui_uses_masked_ephemeral_fields_and_no_browser_storage() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (root / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert 'id="manage-credentials"' in html
    assert 'id="do-token" type="password"' in html
    assert 'id="scw-secret-key" type="password"' in html
    assert "clearCredentialInputs" in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "credential_status" in script


def test_registry_and_settings_never_serialize_provider_secrets(tmp_path: Path) -> None:
    root = make_resource_root(tmp_path)
    secrets = MemoryCredentialStore()
    secrets.save(DIGITALOCEAN_TOKEN_TARGET, "registry-secret")
    instance = orchestrator_module.Orchestrator(root, tmp_path / "runtime", credential_store=secrets, packaged=True)
    add_record(instance, "digitalocean")
    combined = instance.deployments.path.read_text(encoding="utf-8")
    if instance.settings.path.exists():
        combined += instance.settings.path.read_text(encoding="utf-8")
    assert "registry-secret" not in combined
