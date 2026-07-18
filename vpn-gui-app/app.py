from __future__ import annotations

import sys
from pathlib import Path

try:
    import webview
except ImportError as exc:
    raise SystemExit("pywebview is required. Install vpn-gui-app/requirements.txt") from exc

from bridge import BridgeService


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def resource_path(relative_path: str) -> Path:
    return bundle_root() / relative_path


def main() -> None:
    index_html = resource_path("vpn-gui-app/ui/index.html")
    if not index_html.exists():  # Compatibility with older PyInstaller layouts.
        index_html = resource_path("ui/index.html")
    if not index_html.exists():
        raise FileNotFoundError(f"Missing UI file: {index_html}")
    bridge = BridgeService(bundle_root())
    webview.create_window(
        title="Hérès VPN",
        url=index_html.as_uri(),
        js_api=bridge,
        width=1280,
        height=900,
        min_size=(1024, 720),
        confirm_close=True,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
