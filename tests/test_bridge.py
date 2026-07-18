import sys
from pathlib import Path

from app import resource_path
from bridge import parse_options


def test_options_conversion() -> None:
    options = parse_options({"allowed_ips": "0.0.0.0/0, 10.0.0.0/8", "wireguard_port": 1234})
    assert options.allowed_ips == ("0.0.0.0/0", "10.0.0.0/8")
    assert options.wireguard_port == 1234


def test_pyinstaller_resource_path(monkeypatch) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(Path("C:/bundle")), raising=False)
    assert resource_path("ui/index.html") == Path("C:/bundle/ui/index.html")
