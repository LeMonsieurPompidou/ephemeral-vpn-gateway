import sys
from pathlib import Path

from app import resource_path
from bridge import parse_options


def test_options_conversion() -> None:
    options = parse_options({"allowed_ips": "0.0.0.0/0, 10.0.0.0/8", "wireguard_port": 1234})
    assert options.allowed_ips == ("0.0.0.0/0", "10.0.0.0/8")
    assert options.wireguard_port == 1234


def test_normal_gui_options_retain_validated_backend_defaults() -> None:
    options = parse_options({"client_count": 2, "expiration_minutes": 60, "automatic_expiration": True})
    assert options.client_count == 2
    assert options.allowed_ips == ("0.0.0.0/0",)
    assert options.dns_servers == ("1.1.1.1", "1.0.0.1")
    assert options.wireguard_port == 51820
    assert options.client_mtu == 1420
    assert options.persistent_keepalive == 25
    assert options.ssh_cidr is None


def test_pyinstaller_resource_path(monkeypatch) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(Path("C:/bundle")), raising=False)
    assert resource_path("ui/index.html") == Path("C:/bundle/ui/index.html")
