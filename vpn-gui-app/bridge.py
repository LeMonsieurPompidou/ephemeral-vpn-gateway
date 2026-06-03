from __future__ import annotations

import json
import logging
import sys
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _repo_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)

    return Path(__file__).resolve().parent.parent


def _provider_directory(provider: str) -> Path:
    normalized_provider = provider.strip().lower()

    provider_map = {
        "scaleway": _repo_root() / "vpn-scaleway",
        "digitalocean": _repo_root() / "vpn-digitalocean",
    }

    try:
        return provider_map[normalized_provider]
    except KeyError as exc:
        raise ValueError(f"Unsupported provider: {provider}") from exc


def _client_config_path(provider: str) -> Path:
    desktop_dir = Path.home() / "Desktop"
    normalized_provider = provider.strip().lower()

    if normalized_provider == "scaleway":
        return desktop_dir / "scaleway-vpn.conf"
    if normalized_provider == "digitalocean":
        return desktop_dir / "digitalocean-vpn.conf"

    raise ValueError(f"Unsupported provider: {provider}")


def _run_terraform(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    logger.info("Running terraform command: terraform %s (cwd=%s)", " ".join(args), cwd)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

    return subprocess.run(
        ["terraform", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=creationflags,
    )


def deploy_infrastructure(provider, region):
    try:
        provider_dir = _provider_directory(provider)
        config_path = _client_config_path(provider)

        _run_terraform(["init"], cwd=provider_dir)
        _run_terraform(["apply", f'-var=region={region}', "-auto-approve"], cwd=provider_dir)

        output_result = _run_terraform(["output", "-json"], cwd=provider_dir)
        outputs = json.loads(output_result.stdout or "{}")

        vpn_public_ip = outputs.get("vpn_public_ip", {}).get("value")
        if not vpn_public_ip:
            raise RuntimeError("terraform output did not include vpn_public_ip")

        if not config_path.exists():
            raise FileNotFoundError(f"Missing client config file: {config_path}")

        client_config_content = config_path.read_text(encoding="utf-8")

        return {
            "status": "success",
            "ip": vpn_public_ip,
            "config": client_config_content,
        }
    except Exception as exc:
        logger.exception("Deployment failed for provider=%s region=%s", provider, region)
        return {"status": "error", "message": str(exc)}


def destroy_infrastructure(provider):
    try:
        provider_dir = _provider_directory(provider)
        _run_terraform(["destroy", "-auto-approve"], cwd=provider_dir)
        return {"status": "success"}
    except Exception as exc:
        logger.exception("Destroy failed for provider=%s", provider)
        return {"status": "error", "message": str(exc)}