from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from release_bundle import (
    BUILD_ONLY_FILES,
    DATA_FILES,
    LOCAL_PYTHON_FILES,
    ReleaseBundleError,
    audit_paths,
    audit_release_inputs,
    audited_datas,
    release_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def test_release_manifest_is_explicit_complete_and_secret_free() -> None:
    inputs = audit_release_inputs(ROOT)
    relative = {path.relative_to(ROOT).as_posix() for path in inputs}
    expected = set(LOCAL_PYTHON_FILES) | {source for source, _ in DATA_FILES} | set(BUILD_ONLY_FILES)

    assert relative == expected
    assert not any(path.startswith("tests/") for path in relative)
    assert not any("terraform.tfvars" in path for path in relative)
    assert not any("terraform.tfstate" in path for path in relative)
    assert not any("/.terraform/" in f"/{path}/" for path in relative)


def test_pyinstaller_datas_use_only_manifest_destinations() -> None:
    actual = audited_datas(ROOT)
    assert {(Path(source).relative_to(ROOT).as_posix(), destination) for source, destination in actual} == set(
        DATA_FILES
    )


@pytest.mark.parametrize(
    "name",
    [
        "terraform.tfvars",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "saved.tfplan",
        "tfplan",
        "client.conf",
        "HeresVPN1.conf",
        "ssh.privatekey",
        "client.privatekey",
    ],
)
def test_secret_audit_rejects_sensitive_candidate_names(tmp_path: Path, name: str) -> None:
    candidate = tmp_path / name
    candidate.write_text("placeholder", encoding="utf-8")

    with pytest.raises(ReleaseBundleError, match="Forbidden release file"):
        audit_paths(tmp_path, [name])


def test_secret_audit_rejects_terraform_and_tests_directories(tmp_path: Path) -> None:
    for relative in (Path("vpn-provider/.terraform/plugin.bin"), Path("tests/fixture.txt")):
        candidate = tmp_path / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("placeholder", encoding="utf-8")
        with pytest.raises(ReleaseBundleError, match="Forbidden release path"):
            audit_paths(tmp_path, [relative])


def test_manifest_fails_closed_when_local_python_shape_changes(tmp_path: Path) -> None:
    app_root = tmp_path / "vpn-gui-app"
    app_root.mkdir()
    for relative in LOCAL_PYTHON_FILES:
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (app_root / "unreviewed.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ReleaseBundleError, match="manifest is stale"):
        audit_release_inputs(tmp_path)


def test_manifest_has_hashes_and_no_development_modules() -> None:
    manifest = release_manifest(ROOT)
    files = manifest["files"]
    assert isinstance(files, list)
    assert files
    assert all(len(item["sha256"]) == 64 for item in files)
    assert not any(item["path"].startswith("tests/") for item in files)


def test_packaged_ui_uses_the_allowlisted_local_qr_dependency() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    data_paths = {source for source, _destination in DATA_FILES}

    assert 'src="vendor/qrcode.min.js"' in index
    assert "cdnjs.cloudflare.com" not in index
    assert "vpn-gui-app/ui/vendor/qrcode.min.js" in data_paths
    assert "vpn-gui-app/ui/vendor/qrcodejs-LICENSE.txt" in data_paths
