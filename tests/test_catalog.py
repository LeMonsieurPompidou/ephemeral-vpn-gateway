from pathlib import Path

import pytest
from catalog import CatalogError, ProviderCatalog
from providers import ProviderRegistry

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_contains_supported_cloud_providers() -> None:
    catalog = ProviderCatalog(ROOT / "vpn-gui-app" / "provider_catalog.json")
    registry = ProviderRegistry(catalog)
    assert {provider.info.id for provider in registry.list()} == {"digitalocean", "scaleway", "aws-lightsail"}
    assert "residential" not in {provider.id for provider in catalog.list_providers()}
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


def test_catalog_rejects_non_numeric_or_negative_pricing(tmp_path: Path) -> None:
    template = (
        '{"version":1,"providers":[{"id":"x","display_name":"X","terraform_root":"x",'
        '"locations":[{"id":"one","country_code":"US","country_name":"United States",'
        '"city":"Test","region":"one","server_type":"cloud","capabilities":["wireguard"],'
        '"streaming_status":"unverified","estimated_hourly_cost_usd":PRICE}]}]}'
    )
    for price in ('"cheap"', "-1", "true"):
        path = tmp_path / f"catalog-{price.replace('-', 'negative').replace(chr(34), '')}.json"
        path.write_text(template.replace("PRICE", price), encoding="utf-8")
        with pytest.raises(CatalogError, match="estimated_hourly_cost_usd"):
            ProviderCatalog(path)
