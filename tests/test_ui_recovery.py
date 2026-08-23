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


def test_cloud_init_progress_label_uses_safe_persisted_backend_fields() -> None:
    script = (
        "const result={"
        "progress:state.cloudInitStatus({state:'waiting_for_cloud_init',"
        "bootstrap_phase:'prerequisite installation',provisioning_elapsed_seconds:192}),"
        "invalid:state.cloudInitStatus({state:'waiting_for_cloud_init',bootstrap_phase:'<secret>',provisioning_elapsed_seconds:-1}),"
        "other:state.cloudInitStatus({state:'checking_wireguard',"
        "bootstrap_phase:'final readiness validation',provisioning_elapsed_seconds:200})"
        "};process.stdout.write(JSON.stringify(result));"
    )
    assert run_recovery_state(script) == {
        "progress": "Cloud init: prerequisite installation (3m12s)",
        "invalid": "Cloud init: startup (0m00s)",
        "other": None,
    }


def test_legacy_blockers_are_scoped_to_the_selected_provider() -> None:
    script = (
        "const items=["
        "{provider_id:'digitalocean',blocking:true},"
        "{provider_id:'scaleway',blocking:true},"
        "{provider_id:'aws-lightsail',blocking:false}];"
        "const result={"
        "aws:state.blockingLegacyStatesForProvider(items,'aws-lightsail').length,"
        "do:state.blockingLegacyStatesForProvider(items,'digitalocean').length,"
        "scw:state.blockingLegacyStatesForProvider(items,'scaleway').length,"
        "invalid:state.blockingLegacyStatesForProvider(null,'digitalocean').length"
        "};process.stdout.write(JSON.stringify(result));"
    )
    assert run_recovery_state(script) == {"aws": 0, "do": 1, "scw": 1, "invalid": 0}


def test_client_configuration_ui_shows_backend_desktop_path_and_preserves_qr() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert "Mobile" in index and 'id="qrcode"' in index
    assert "Desktop" in index and "Save location" in index
    assert 'id="config-save-path"' in index
    assert 'id="client-count"' in index and 'value="1"' in index
    assert "One client per device." in index
    assert 'id="client-list"' in index
    assert "get_client_configs(targetId)" in script
    assert "get_client_config_export(targetId,clientId)" in script
    assert "proposal.path" in script
    assert "save_client_config(targetId,clientId)" in script
    assert "Configuration saved: ${result.path}" in script
    assert "Save to an absolute path" not in script
    assert "new QRCode" in script


def test_normal_gui_hides_internal_advanced_controls_and_healthy_legacy_states() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert "Advanced settings" not in index
    for internal_id in ("traffic-mode", "allowed-ips", "dns", "port", "ssh-cidr"):
        assert f'id="{internal_id}"' not in index
        assert f"$('{internal_id}')" not in script
    assert "allowed_ips:" not in script
    assert "wireguard_port:" not in script
    assert "selectedLegacyBlockers()" in script
    assert "classification!=='none'" not in script
    assert "Legacy Terraform state requires attention" not in index
    assert 'id="legacy-recovery"' not in index
    assert 'id="legacy-warning"' in index
    assert "Deployment recovery is required before creating a new gateway." in index
    assert 'id="legacy-tools"' in index and "Legacy Terraform recovery" in index
    assert "$('legacy-warning').classList.toggle('hidden',!visible.length)" in script
    assert "providerBlocked()" in script
    assert "Verify cloud state" in script
    assert "Mark stale and reconcile" in script
    assert "verify_legacy_cloud_state(item.provider_id)" in script
    assert "RecoveryState.blockingLegacyStatesForProvider" in script


def test_lifetime_and_client_count_use_top_aligned_equal_height_form_fields() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "vpn-gui-app" / "ui" / "style.css").read_text(encoding="utf-8")
    assert '<span class="field-label">Lifetime</span><select id="expiration" class="form-control">' in index
    assert '<span class="field-label">VPN clients</span><input id="client-count" class="form-control"' in index
    assert '<span class="field-helper">One client per device.</span>' in index
    assert ".form-grid {" in style and "align-items: start" in style
    assert ".form-field {" in style and "align-self: start" in style
    assert ".form-control {" in style and "height: 42px" in style and "min-height: 42px" in style
    assert ".field-helper {" in style


def test_provider_credential_feedback_is_compact_provider_bound_and_secret_safe() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert 'id="credential-feedback"' in index
    assert 'id="credential-help" class="hidden"' in index
    assert "result.provider_id!==expectedProvider" in script
    assert "result.reason==='missing'" in script
    assert "textContent=command" in script
    assert "alert(result.message)" not in script
    assert "DIGITALOCEAN_TOKEN" not in index
    assert "SCW_SECRET_KEY" not in index


def test_packaged_startup_waits_for_the_complete_pywebview_bridge() -> None:
    script = (ROOT / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert "typeof bridge?.list_providers!=='function'" in script
    assert "window.addEventListener('pywebviewready',initialize)" in script
    assert "initializationStarted" in script


def test_destroyed_record_can_show_separate_local_export_cleanup_warning() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert 'id="local-cleanup-warning"' in index
    assert "currentRecord.local_cleanup_warnings" in script
    assert "cleanupWarnings.join('\\n')" in script


def test_running_time_and_estimated_cost_are_present_and_driven_locally() -> None:
    index = (ROOT / "vpn-gui-app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "vpn-gui-app" / "ui" / "script.js").read_text(encoding="utf-8")
    assert 'src="session_cost.js"' in index
    assert 'id="running-time"' in index
    assert 'id="estimated-cost"' in index
    assert "Actual billing may differ" in index
    assert "SessionCost.estimate(currentRecord,Date.now())" in script
    assert "setInterval(updateSessionEstimate,1000)" in script
    assert "setState(await api().get_status(deploymentId));selectedRecoveryId=null" in script
