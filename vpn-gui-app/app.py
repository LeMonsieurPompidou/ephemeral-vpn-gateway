from __future__ import annotations

import sys
from pathlib import Path

try:
    import webview
except ImportError as exc:
    raise SystemExit(
        "pywebview is required to launch this app. Install it with: pip install pywebview"
    ) from exc

from bridge import deploy_infrastructure, destroy_infrastructure



def resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


INDEX_HTML = resource_path("ui/index.html")


class AppBridge:
    def deploy(self, provider: str, region: str) -> None:
        return deploy_infrastructure(provider, region)

    def destroy(self, provider: str) -> None:
        return destroy_infrastructure(provider)


def main() -> None:
    if not INDEX_HTML.exists():
        raise FileNotFoundError(f"Missing UI file: {INDEX_HTML}")

    webview.create_window(
        title="Hérès VPN",
        url=INDEX_HTML.as_uri(),
        js_api=AppBridge(),
        width=1280,
        height=900,
        min_size=(1024, 720),
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()