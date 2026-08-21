from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import config_export
import pytest
from bridge import BridgeService, SaveDialog, native_save_dialog
from config_export import (
    ConfigExportError,
    default_config_filename,
    normalize_export_destination,
    proposed_export_path,
    resolve_desktop_directory,
)
from helpers import add_record, make_resource_root
from models import DeploymentRecord, DeploymentState


def ready_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    save_dialog: SaveDialog | None = None,
) -> tuple[BridgeService, DeploymentRecord, Path, bytes]:
    desktop = tmp_path / "OneDrive - Example" / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr("bridge.resolve_desktop_directory", lambda: desktop)
    bridge = BridgeService(
        make_resource_root(tmp_path),
        tmp_path / "runtime",
        start_expiration_monitor=False,
        acquire_app_lock=False,
        **({"save_dialog": save_dialog} if save_dialog else {}),
    )
    deployment_id = "a01784ba-a00f-4a53-8f98-557e69bee8f2"
    record = add_record(bridge.orchestrator, "aws-lightsail", deployment_id)
    record.location_id = "us-east-1"
    record.state = DeploymentState.READY
    bridge.orchestrator.deployments.save(record)
    source = Path(record.runtime_directory) / "client.conf"
    payload = b"[Interface]\r\nPrivateKey = unit-test-secret\r\n"
    source.write_bytes(payload)
    return bridge, record, desktop, payload


def test_windows_known_desktop_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    desktop = tmp_path / "Redirected Desktop"
    desktop.mkdir()
    monkeypatch.setattr(config_export, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(config_export, "_windows_known_desktop", lambda: desktop)
    monkeypatch.chdir(tmp_path / "Redirected Desktop")
    assert resolve_desktop_directory() == desktop.resolve()


def test_onedrive_desktop_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    onedrive = tmp_path / "OneDrive - Organization"
    desktop = onedrive / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(config_export, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(config_export, "_windows_known_desktop", lambda: None)
    monkeypatch.setenv("OneDrive", str(onedrive))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert resolve_desktop_directory() == desktop.resolve()


def test_default_filename_and_proposal_are_safe() -> None:
    desktop = Path("C:/Users/example/Desktop")
    filename = default_config_filename("../../US East 1", "a01784ba-a00f-4a53-8f98-557e69bee8f2")
    assert filename == "heres-vpn-us-east-1-a01784ba.conf"
    assert proposed_export_path(desktop, "../../US East 1", "a01784ba-a00f-4a53-8f98-557e69bee8f2") == (
        desktop / filename
    )


def test_export_proposal_uses_backend_desktop_without_touching_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, record, desktop, payload = ready_bridge(tmp_path, monkeypatch)
    source = Path(record.runtime_directory) / "client.conf"
    before = source.read_bytes()
    proposal = bridge.get_client_config_export(record.id)
    assert proposal == {
        "status": "ready",
        "deployment_id": record.id,
        "directory": str(desktop),
        "filename": "heres-vpn-us-east-1-a01784ba.conf",
        "path": str(desktop / "heres-vpn-us-east-1-a01784ba.conf"),
    }
    assert source.read_bytes() == before == payload
    assert not (desktop / proposal["filename"]).exists()


def test_native_save_dialog_receives_directory_filename_and_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Window:
        def create_file_dialog(self, dialog_type, **kwargs):  # type: ignore[no-untyped-def]
            captured["dialog_type"] = dialog_type
            captured.update(kwargs)
            return (str(Path(kwargs["directory"]) / kwargs["save_filename"]),)

    fake_webview = SimpleNamespace(windows=[Window()], FileDialog=SimpleNamespace(SAVE=30))
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    desktop = Path("C:/Users/example/Desktop")
    selected = native_save_dialog(desktop, "heres-vpn-us-east-1-a01784ba.conf")
    assert selected == desktop / "heres-vpn-us-east-1-a01784ba.conf"
    assert captured == {
        "dialog_type": 30,
        "directory": str(desktop),
        "save_filename": "heres-vpn-us-east-1-a01784ba.conf",
        "file_types": ("WireGuard configuration (*.conf)",),
    }


def test_save_exports_identical_bytes_to_selected_path_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = tmp_path / "Folder with spaces" / "my desktop tunnel"
    chosen.parent.mkdir()
    calls: list[tuple[Path, str]] = []

    def dialog(directory: Path, filename: str) -> Path:
        calls.append((directory, filename))
        return chosen

    bridge, record, desktop, payload = ready_bridge(tmp_path, monkeypatch, save_dialog=dialog)
    source = Path(record.runtime_directory) / "client.conf"
    source_before = source.read_bytes()
    result = bridge.save_client_config(record.id)
    destination = chosen.with_suffix(".conf")
    assert calls == [(desktop, "heres-vpn-us-east-1-a01784ba.conf")]
    assert result == {"status": "success", "path": str(destination)}
    assert destination.read_bytes() == payload
    assert source.read_bytes() == source_before


def test_user_cancel_does_not_create_an_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, record, desktop, payload = ready_bridge(tmp_path, monkeypatch, save_dialog=lambda *_args: None)
    result = bridge.save_client_config(record.id)
    assert result["status"] == "cancelled"
    assert result["path"] == str(desktop / "heres-vpn-us-east-1-a01784ba.conf")
    assert list(desktop.iterdir()) == []
    assert (Path(record.runtime_directory) / "client.conf").read_bytes() == payload


def test_existing_destination_is_atomically_replaced_after_dialog_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = tmp_path / "exports" / "existing.conf"
    chosen.parent.mkdir()
    chosen.write_bytes(b"old")
    bridge, record, _desktop, payload = ready_bridge(tmp_path, monkeypatch, save_dialog=lambda *_args: chosen)
    bridge.save_client_config(record.id)
    assert chosen.read_bytes() == payload
    assert not list(chosen.parent.glob(".*.tmp"))


@pytest.mark.parametrize("destination", ["../outside.conf", "relative.conf", "."])
def test_relative_or_traversal_destination_is_rejected(destination: str) -> None:
    with pytest.raises(ConfigExportError, match="absolute|filename"):
        normalize_export_destination(destination)


def test_missing_or_symlinked_runtime_source_fails_without_secret_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, record, _desktop, payload = ready_bridge(tmp_path, monkeypatch)
    source = Path(record.runtime_directory) / "client.conf"
    source.unlink()
    with pytest.raises(RuntimeError) as raised:
        bridge.get_client_config_export(record.id)
    assert payload.decode() not in str(raised.value)
    assert "PrivateKey" not in str(raised.value)


def test_export_default_is_independent_of_source_or_packaged_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, record, desktop, _payload = ready_bridge(tmp_path, monkeypatch)
    for working in (tmp_path / "repository" / "vpn-gui-app", tmp_path / "dist"):
        working.mkdir(parents=True)
        monkeypatch.chdir(working)
        assert bridge.get_client_config_export(record.id)["path"] == str(desktop / "heres-vpn-us-east-1-a01784ba.conf")


def test_symlinked_runtime_source_is_rejected_before_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, record, _desktop, payload = ready_bridge(tmp_path, monkeypatch)
    source = Path(record.runtime_directory) / "client.conf"
    other = tmp_path / "outside.conf"
    other.write_bytes(payload)
    source.unlink()
    try:
        source.symlink_to(other)
    except OSError:
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == source)
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        bridge.get_client_config_export(record.id)


def test_runtime_source_swap_is_rejected_before_payload_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, record, _desktop, _payload = ready_bridge(tmp_path, monkeypatch)
    source = Path(record.runtime_directory) / "client.conf"
    destination = tmp_path / "export.conf"
    real_open = config_export.os.open

    def swapping_open(path: Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == source:
            source.unlink()
            source.write_bytes(b"replacement")
        return real_open(path, flags, mode)

    monkeypatch.setattr(config_export.os, "open", swapping_open)
    with pytest.raises(ConfigExportError, match="changed before"):
        config_export.copy_config_bytes(source, destination)
    assert not destination.exists()


def test_export_cannot_overwrite_the_authoritative_runtime_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, record, _desktop, payload = ready_bridge(tmp_path, monkeypatch)
    source = Path(record.runtime_directory) / "client.conf"
    with pytest.raises(ConfigExportError, match="cannot be overwritten"):
        config_export.copy_config_bytes(source, source)
    assert source.read_bytes() == payload
