from pathlib import Path

import pytest
from catalog import CatalogError, ProviderCatalog
from providers import ProviderRegistry

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_contains_supported_cloud_providers() -> None:
    catalog = ProviderCatalog(ROOT / "vpn-gui-app" / "provider_catalog.json")
    registry = ProviderRegistry(catalog)
    assert {provider.info.id for provider in registry.list()} >= {"digitalocean", "scaleway", "aws-lightsail"}
    assert catalog.get_location("scaleway", "fr-par-1").region == "fr-par-1"


def test_catalog_rejects_malformed_content(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text('{"version": 1, "providers": []}', encoding="utf-8")
    with pytest.raises(CatalogError, match="at least one"):
        ProviderCatalog(path)


def test_catalog_rejects_malformed_location(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        '{"version":1,"providers":[{"id":"x","display_name":"X","locations":[{"id":"BAD ID"}]}]}', encoding="utf-8"
    )
    with pytest.raises(CatalogError):
        ProviderCatalog(path)
