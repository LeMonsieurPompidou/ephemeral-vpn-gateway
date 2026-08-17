from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_recovery_state(script: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    module = ROOT / "vpn-gui-app" / "ui" / "recovery_state.js"
    result = subprocess.run(
        [node, "-e", f"const state=require(process.argv[1]);{script}", str(module)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_frontend_recovery_selection_is_cleared_when_record_disappears() -> None:
    script = (
        "const result={"
        "kept:state.selectionAfterRefresh('present',[{id:'present'}]),"
        "removed:state.selectionAfterRefresh('removed',[{id:'present'}]),"
        "empty:state.selectionAfterRefresh('removed',[])"
        "};process.stdout.write(JSON.stringify(result));"
    )
    assert run_recovery_state(script) == {"kept": "present", "removed": None, "empty": None}


def test_backend_record_reconciliation_rejects_stale_or_mismatched_frontend_data() -> None:
    script = (
        "const current={id:'active',state:'planning',updated_at:'2026-08-17T12:00:02Z'};"
        "const result={"
        "newer:state.acceptsBackendRecord(current,{id:'active',state:'provisioning',updated_at:'2026-08-17T12:00:03Z'},'active'),"
        "stale:state.acceptsBackendRecord(current,{id:'active',state:'validating_credentials',updated_at:'2026-08-17T12:00:01Z'},'active'),"
        "mismatched:state.acceptsBackendRecord(current,{id:'recovery',state:'failed',updated_at:'2026-08-17T12:00:04Z'},'active'),"
        "ranked:state.acceptsBackendRecord({id:'active',state:'planning'},{id:'active',state:'provisioning'},'active')"
        "};process.stdout.write(JSON.stringify(result));"
    )
    assert run_recovery_state(script) == {"newer": True, "stale": False, "mismatched": False, "ranked": True}


def test_log_rendering_is_bound_to_active_or_selected_deployment() -> None:
    script = (
        "const result={"
        "active:state.canRenderLogs('active','active','active'),"
        "wrongActive:state.canRenderLogs('recovery','recovery','active'),"
        "selected:state.canRenderLogs('recovery','recovery',null),"
        "staleResponse:state.canRenderLogs('old','new',null)"
        "};process.stdout.write(JSON.stringify(result));"
    )
    assert run_recovery_state(script) == {
        "active": True,
        "wrongActive": False,
        "selected": True,
        "staleResponse": False,
    }
