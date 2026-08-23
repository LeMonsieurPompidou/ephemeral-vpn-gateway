from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Iterable

LOCAL_PYTHON_FILES = (
    "vpn-gui-app/app.py",
    "vpn-gui-app/app_settings.py",
    "vpn-gui-app/bridge.py",
    "vpn-gui-app/catalog.py",
    "vpn-gui-app/client_peers.py",
    "vpn-gui-app/cloud_init.py",
    "vpn-gui-app/config_export.py",
    "vpn-gui-app/credential_preflight.py",
    "vpn-gui-app/credential_resolver.py",
    "vpn-gui-app/credential_store.py",
    "vpn-gui-app/file_lock.py",
    "vpn-gui-app/legacy_cloud_verification.py",
    "vpn-gui-app/legacy_reconciliation.py",
    "vpn-gui-app/models.py",
    "vpn-gui-app/networking.py",
    "vpn-gui-app/orchestrator.py",
    "vpn-gui-app/output_contract.py",
    "vpn-gui-app/providers.py",
    "vpn-gui-app/runtime_registry.py",
    "vpn-gui-app/security.py",
    "vpn-gui-app/ssh_probe.py",
    "vpn-gui-app/terraform_runner.py",
    "vpn-gui-app/validation.py",
)

DATA_FILES = (
    ("vpn-gui-app/provider_catalog.json", "vpn-gui-app"),
    ("vpn-gui-app/ui/index.html", "vpn-gui-app/ui"),
    ("vpn-gui-app/ui/style.css", "vpn-gui-app/ui"),
    ("vpn-gui-app/ui/script.js", "vpn-gui-app/ui"),
    ("vpn-gui-app/ui/recovery_state.js", "vpn-gui-app/ui"),
    ("vpn-gui-app/ui/session_cost.js", "vpn-gui-app/ui"),
    ("vpn-gui-app/ui/assets/Hérès_VPN_logo.png", "vpn-gui-app/ui/assets"),
    ("vpn-gui-app/ui/vendor/qrcode.min.js", "vpn-gui-app/ui/vendor"),
    ("vpn-gui-app/ui/vendor/qrcodejs-LICENSE.txt", "vpn-gui-app/ui/vendor"),
    ("vpn-aws-lightsail/main.tf", "vpn-aws-lightsail"),
    ("vpn-aws-lightsail/providers.tf", "vpn-aws-lightsail"),
    ("vpn-aws-lightsail/variables.tf", "vpn-aws-lightsail"),
    ("vpn-aws-lightsail/.terraform.lock.hcl", "vpn-aws-lightsail"),
    ("vpn-digitalocean/main.tf", "vpn-digitalocean"),
    ("vpn-digitalocean/providers.tf", "vpn-digitalocean"),
    ("vpn-digitalocean/variables.tf", "vpn-digitalocean"),
    ("vpn-digitalocean/.terraform.lock.hcl", "vpn-digitalocean"),
    ("vpn-scaleway/main.tf", "vpn-scaleway"),
    ("vpn-scaleway/providers.tf", "vpn-scaleway"),
    ("vpn-scaleway/variables.tf", "vpn-scaleway"),
    ("vpn-scaleway/.terraform.lock.hcl", "vpn-scaleway"),
    ("terraform-common/bootstrap.sh.tftpl", "terraform-common"),
    ("terraform-common/cloud-init.yaml.tftpl", "terraform-common"),
)

BUILD_ONLY_FILES = ("vpn-gui-app/ui/assets/Hérès_VPN_logo.ico",)

_FORBIDDEN_COMPONENTS = {
    ".terraform",
    "__pycache__",
    "tests",
}
_FORBIDDEN_NAMES = {
    "terraform.tfvars",
    "terraform.tfvars.json",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "tfplan",
    "client.conf",
    "ssh.privatekey",
    "client.privatekey",
    "deployments.json",
    "deployment.log",
}
_FORBIDDEN_PATTERNS = (
    "*.tfplan",
    "*.tfstate",
    "*.tfstate.*",
    "*.privatekey",
    "heresvpn*.conf",
)


class ReleaseBundleError(RuntimeError):
    pass


def _relative_file(root: Path, value: str | Path) -> tuple[Path, Path]:
    source = root / value
    if source.is_symlink():
        raise ReleaseBundleError(f"Release input may not be a symlink: {value}")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBundleError(f"Release input is missing or unreadable: {value}") from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseBundleError(f"Release input is outside the repository: {value}") from exc
    if not resolved.is_file():
        raise ReleaseBundleError(f"Release input is not a regular file: {value}")
    return resolved, relative


def _reject_sensitive(relative: Path) -> None:
    components = {part.casefold() for part in relative.parts}
    if components & _FORBIDDEN_COMPONENTS:
        raise ReleaseBundleError(f"Forbidden release path: {relative.as_posix()}")
    name = relative.name.casefold()
    if name in _FORBIDDEN_NAMES or ".tfvars" in name:
        raise ReleaseBundleError(f"Forbidden release file: {relative.as_posix()}")
    if any(fnmatch.fnmatch(name, pattern) for pattern in _FORBIDDEN_PATTERNS):
        raise ReleaseBundleError(f"Forbidden release file: {relative.as_posix()}")


def audit_paths(root: Path, paths: Iterable[str | Path]) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    audited: list[Path] = []
    for value in paths:
        source, relative = _relative_file(root, value)
        _reject_sensitive(relative)
        audited.append(source)
    return tuple(audited)


def _audit_python_shape(root: Path) -> None:
    expected = {Path(value).as_posix() for value in LOCAL_PYTHON_FILES}
    actual = {path.relative_to(root).as_posix() for path in (root / "vpn-gui-app").glob("*.py") if path.is_file()}
    if actual != expected:
        added = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ReleaseBundleError(f"Local Python release manifest is stale; unexpected={added}, missing={missing}")


def audited_datas(root: Path) -> list[tuple[str, str]]:
    root = root.resolve(strict=True)
    audit_release_inputs(root)
    return [(str((root / source).resolve()), destination) for source, destination in DATA_FILES]


def audit_release_inputs(root: Path) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    _audit_python_shape(root)
    paths = (
        *LOCAL_PYTHON_FILES,
        *(source for source, _destination in DATA_FILES),
        *BUILD_ONLY_FILES,
    )
    return audit_paths(root, paths)


def release_manifest(root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    audited = audit_release_inputs(root)
    destinations = {source: destination for source, destination in DATA_FILES}
    entries = []
    for path in audited:
        relative = path.relative_to(root).as_posix()
        role = "python" if relative in LOCAL_PYTHON_FILES else "build" if relative in BUILD_ONLY_FILES else "data"
        entries.append(
            {
                "path": relative,
                "role": role,
                "destination": destinations.get(relative),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {"version": 1, "files": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and list Hérès PyInstaller-owned release inputs")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    manifest = release_manifest(args.root)
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        files = manifest["files"]
        if not isinstance(files, list):
            raise ReleaseBundleError("Release manifest files must be a list")
        for item in files:
            if not isinstance(item, dict):
                raise ReleaseBundleError("Release manifest entry must be an object")
            print(f"{item['role']:>6}  {item['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
