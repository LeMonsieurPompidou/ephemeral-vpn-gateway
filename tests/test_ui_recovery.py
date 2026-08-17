from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_recovery_selection_is_cleared_when_record_disappears() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    module = ROOT / "vpn-gui-app" / "ui" / "recovery_state.js"
    script = (
        "const state=require(process.argv[1]);"
        "const result={"
        "kept:state.selectionAfterRefresh('present',[{id:'present'}]),"
        "removed:state.selectionAfterRefresh('removed',[{id:'present'}]),"
        "empty:state.selectionAfterRefresh('removed',[])"
        "};process.stdout.write(JSON.stringify(result));"
    )
    result = subprocess.run(
        [node, "-e", script, str(module)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"kept": "present", "removed": None, "empty": None}
