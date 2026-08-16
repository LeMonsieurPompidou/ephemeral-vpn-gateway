from pathlib import Path

import providers
import pytest
from catalog import ProviderCatalog
from providers import ProviderRegistry

ROOT = Path(__file__).resolve().parents[1]


class Process:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def poll(self) -> int:
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        return self.stdout, self.stderr

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def adapter():  # type: ignore[no-untyped-def]
    catalog = ProviderCatalog(ROOT / "vpn-gui-app" / "provider_catalog.json")
    return ProviderRegistry(catalog).get("aws-lightsail")


def test_aws_valid_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AWS_PROFILE", "heres-vpn")
    monkeypatch.setattr(providers.shutil, "which", lambda name: "aws")
    monkeypatch.setattr(providers.subprocess, "Popen", lambda *args, **kwargs: Process(0, "{}"))
    assert adapter().validate_credentials()[0]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("The SSO session associated with this profile has expired", "aws sso login --profile heres-vpn"),
        ("The config profile could not be found", "aws configure sso"),
        ("AccessDenied: not authorized", "not authorized"),
    ],
)
def test_aws_preflight_errors(monkeypatch, stderr: str, expected: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AWS_PROFILE", "heres-vpn")
    monkeypatch.setattr(providers.shutil, "which", lambda name: "aws")
    monkeypatch.setattr(providers.subprocess, "Popen", lambda *args, **kwargs: Process(1, stderr=stderr))
    valid, message = adapter().validate_credentials()
    assert not valid and expected in message


def test_aws_missing_cli_or_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert not adapter().validate_credentials()[0]
    monkeypatch.setenv("AWS_PROFILE", "heres-vpn")
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    valid, message = adapter().validate_credentials()
    assert not valid and "AWS CLI" in message
