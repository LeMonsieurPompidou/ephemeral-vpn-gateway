from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from models import Location, ProviderInfo, ServerType, StreamingStatus

_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class CatalogError(ValueError):
    pass


class ProviderCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._providers = self._load(path)

    def list_providers(self) -> tuple[ProviderInfo, ...]:
        return tuple(self._providers.values())

    def get_provider(self, provider_id: str) -> ProviderInfo:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise CatalogError(f"Unknown provider: {provider_id}") from exc

    def get_location(self, provider_id: str, location_id: str) -> Location:
        provider = self.get_provider(provider_id)
        for location in provider.locations:
            if location.id == location_id:
                return location
        raise CatalogError(f"Unknown location {location_id!r} for provider {provider_id!r}")

    @staticmethod
    def _load(path: Path) -> dict[str, ProviderInfo]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Cannot read provider catalog {path}: {exc}") from exc
        if raw.get("version") != 1 or not isinstance(raw.get("providers"), list):
            raise CatalogError("Catalog must have version 1 and a providers array")
        providers: dict[str, ProviderInfo] = {}
        for item in raw["providers"]:
            provider = ProviderCatalog._parse_provider(item)
            if provider.id in providers:
                raise CatalogError(f"Duplicate provider ID: {provider.id}")
            providers[provider.id] = provider
        if not providers:
            raise CatalogError("Catalog must contain at least one provider")
        return providers

    @staticmethod
    def _parse_provider(item: dict[str, Any]) -> ProviderInfo:
        provider_id = ProviderCatalog._required_id(item, "id")
        display_name = ProviderCatalog._required_text(item, "display_name")
        locations_raw = item.get("locations")
        if not isinstance(locations_raw, list) or not locations_raw:
            raise CatalogError(f"Provider {provider_id} must contain locations")
        locations: list[Location] = []
        seen: set[str] = set()
        for raw in locations_raw:
            location_id = ProviderCatalog._required_id(raw, "id")
            if location_id in seen:
                raise CatalogError(f"Duplicate location {provider_id}/{location_id}")
            seen.add(location_id)
            capabilities = raw.get("capabilities")
            if not isinstance(capabilities, list) or not all(
                isinstance(value, str) and value for value in capabilities
            ):
                raise CatalogError(f"{provider_id}/{location_id} has invalid capabilities")
            country_code = ProviderCatalog._required_text(raw, "country_code").upper()
            if not re.fullmatch(r"[A-Z]{2}", country_code):
                raise CatalogError(f"{provider_id}/{location_id} has invalid country code")
            try:
                locations.append(
                    Location(
                        id=location_id,
                        country_code=country_code,
                        country_name=ProviderCatalog._required_text(raw, "country_name"),
                        city=ProviderCatalog._required_text(raw, "city"),
                        region=ProviderCatalog._required_text(raw, "region"),
                        server_type=ServerType(raw["server_type"]),
                        capabilities=tuple(capabilities),
                        streaming_status=StreamingStatus(raw["streaming_status"]),
                        description=raw.get("description"),
                        estimated_hourly_cost_usd=raw.get("estimated_hourly_cost_usd"),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise CatalogError(f"Invalid {provider_id}/{location_id}: {exc}") from exc
        terraform_root = item.get("terraform_root")
        if terraform_root is not None and not isinstance(terraform_root, str):
            raise CatalogError(f"Provider {provider_id} has invalid terraform_root")
        return ProviderInfo(provider_id, display_name, terraform_root, tuple(locations))

    @staticmethod
    def _required_id(item: dict[str, Any], key: str) -> str:
        value = ProviderCatalog._required_text(item, key)
        if not _ID.fullmatch(value):
            raise CatalogError(f"Invalid {key}: {value!r}")
        return value

    @staticmethod
    def _required_text(item: dict[str, Any], key: str) -> str:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CatalogError(f"Missing or invalid {key}")
        return value.strip()
