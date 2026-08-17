from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from helpers import FakeTerraformRunner, add_record, legacy_state, make_orchestrator
from orchestrator import Orchestrator
from terraform_runner import TerraformError


def active_legacy(orchestrator: Orchestrator, *, backup: bool = False) -> Path:
    source = orchestrator._provider_directory("aws-lightsail") / "terraform.tfstate"
    state = legacy_state("aws-lightsail")
    resources = state["resources"]
    assert isinstance(resources, list)
    instance = resources[0]["instances"][0]  # type: ignore[index]
    instance["attributes"] = {"id": "vpn-historical", "private_key": "must-not-be-summarized"}
    state["outputs"] = {
        **state["outputs"],  # type: ignore[dict-item]
        "server_private_key": {"sensitive": True, "value": "must-not-enter-receipt"},
    }
    source.write_text(json.dumps(state), encoding="utf-8")
    if backup:
        source.with_name("terraform.tfstate.backup").write_text(
            json.dumps({"version": 4, "lineage": "older", "serial": 4, "resources": []}),
            encoding="utf-8",
        )
    return source


def report_for(orchestrator: Orchestrator, provider_id: str = "aws-lightsail") -> dict[str, object]:
    return next(item for item in orchestrator.list_legacy_states() if item["provider_id"] == provider_id)


def receipt_path(orchestrator: Orchestrator) -> Path:
    return orchestrator.runtime_root / "legacy-reconciliations" / "aws-lightsail.json"


def reconcile(orchestrator: Orchestrator) -> dict[str, object]:
    report = report_for(orchestrator)
    assert report["stale_reconciliation_available"]
    return orchestrator.reconcile_stale_legacy_state("aws-lightsail", True)


def test_stale_reconciliation_creates_verified_receipt_and_quarantine(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator, backup=True)
    original = source.read_bytes()

    result = reconcile(orchestrator)

    assert result["status"] == "success"
    assert result["source_preserved"]
    assert source.read_bytes() == original
    receipt = json.loads(receipt_path(orchestrator).read_text(encoding="utf-8"))
    assert receipt["provider_id"] == "aws-lightsail"
    assert receipt["reason"] == "cloud_absence_confirmed"
    assert receipt["status"] == "reconciled-stale"
    assert receipt["resource_count"] == 1
    assert receipt["resources"][0]["identifiers"] == ["vpn-historical"]
    assert all("value" not in output for output in receipt["outputs"])
    serialized = json.dumps(receipt)
    assert "must-not-enter-receipt" not in serialized
    assert "must-not-be-summarized" not in serialized
    assert {item["source_name"] for item in receipt["quarantine_files"]} == {
        "terraform.tfstate",
        "terraform.tfstate.backup",
    }
    for item in receipt["quarantine_files"]:
        copy = Path(item["path"])
        assert copy.is_file()
        assert orchestrator._sha256(copy) == item["sha256"]

    verified = report_for(orchestrator)
    assert verified["classification"] == "reconciled-stale"
    assert not verified["blocking"]
    assert verified["reconciliation"]
    orchestrator._assert_provider_ready_for_new_deployment("aws-lightsail")

    restarted = Orchestrator(
        orchestrator.resource_root,
        orchestrator.runtime_root,
        FakeTerraformRunner(),
        ip_detector=lambda: "198.51.100.10/32",
    )
    assert restarted._inspect_legacy_provider("aws-lightsail")["classification"] == "reconciled-stale"


def test_stale_reconciliation_requires_explicit_confirmation(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator)
    report_for(orchestrator)
    with pytest.raises(TerraformError, match="Explicit confirmation"):
        orchestrator.reconcile_stale_legacy_state("aws-lightsail", False)
    assert source.is_file()
    assert not receipt_path(orchestrator).exists()


def test_state_change_after_display_aborts_reconciliation(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator)
    report_for(orchestrator)
    state = json.loads(source.read_text(encoding="utf-8"))
    state["serial"] = 6
    source.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(TerraformError, match="changed after it was displayed"):
        orchestrator.reconcile_stale_legacy_state("aws-lightsail", True)
    assert not receipt_path(orchestrator).exists()
    assert orchestrator._inspect_legacy_provider("aws-lightsail")["blocking"]


@pytest.mark.parametrize("content", ["not-json", "[]"])
def test_malformed_state_cannot_be_reconciled(tmp_path: Path, content: str) -> None:
    orchestrator = make_orchestrator(tmp_path)
    source = orchestrator._provider_directory("aws-lightsail") / "terraform.tfstate"
    source.write_text(content, encoding="utf-8")
    assert report_for(orchestrator)["blocking"]
    with pytest.raises(TerraformError, match="refreshed and reviewed"):
        orchestrator.reconcile_stale_legacy_state("aws-lightsail", True)
    assert not receipt_path(orchestrator).exists()


def test_missing_state_cannot_be_reconciled(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    assert report_for(orchestrator)["classification"] == "none"
    with pytest.raises(TerraformError, match="refreshed and reviewed"):
        orchestrator.reconcile_stale_legacy_state("aws-lightsail", True)


def test_quarantine_write_failure_aborts_without_receipt(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator)
    report_for(orchestrator)

    def fail_copy(source_path: Path, destination: Path) -> None:
        raise OSError("simulated quarantine failure")

    monkeypatch.setattr(orchestrator, "_copy_quarantine_file", fail_copy)
    with pytest.raises(OSError, match="simulated quarantine failure"):
        orchestrator.reconcile_stale_legacy_state("aws-lightsail", True)
    assert source.is_file()
    assert not receipt_path(orchestrator).exists()


def test_quarantine_hash_mismatch_aborts_without_receipt(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator)
    report_for(orchestrator)

    def corrupt_copy(source_path: Path, destination: Path) -> None:
        shutil.copy2(source_path, destination)
        destination.write_bytes(destination.read_bytes() + b"changed")

    monkeypatch.setattr(orchestrator, "_copy_quarantine_file", corrupt_copy)
    with pytest.raises(TerraformError, match="Quarantine verification failed"):
        orchestrator.reconcile_stale_legacy_state("aws-lightsail", True)
    assert source.is_file()
    assert not receipt_path(orchestrator).exists()


def test_corrupt_receipt_reblocks_provider(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    active_legacy(orchestrator)
    reconcile(orchestrator)
    receipt_path(orchestrator).write_text("not-json", encoding="utf-8")
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["classification"] == "active"
    assert report["blocking"]
    assert "receipt cannot be read" in str(report["reason"])


def test_changed_source_fingerprint_reblocks_provider(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator)
    reconcile(orchestrator)
    source.write_bytes(source.read_bytes() + b"\n")
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["classification"] == "active"
    assert report["blocking"]
    assert "fingerprint differs" in str(report["reason"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("terraform_lineage", "other", "lineage differs"), ("terraform_serial", 999, "serial differs")],
)
def test_changed_receipt_metadata_reblocks_provider(tmp_path: Path, field: str, value: object, message: str) -> None:
    orchestrator = make_orchestrator(tmp_path)
    active_legacy(orchestrator)
    reconcile(orchestrator)
    receipt = json.loads(receipt_path(orchestrator).read_text(encoding="utf-8"))
    receipt[field] = value
    orchestrator.legacy_reconciliations.write("aws-lightsail", receipt)
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["blocking"]
    assert message in str(report["reason"])


def test_deleted_quarantine_copy_reblocks_provider(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    active_legacy(orchestrator)
    reconcile(orchestrator)
    receipt = json.loads(receipt_path(orchestrator).read_text(encoding="utf-8"))
    Path(receipt["quarantine_files"][0]["path"]).unlink()
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["blocking"]
    assert "missing or unsafe" in str(report["reason"])


def test_receipt_for_another_provider_reblocks_provider(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    active_legacy(orchestrator)
    reconcile(orchestrator)
    receipt = json.loads(receipt_path(orchestrator).read_text(encoding="utf-8"))
    receipt["provider_id"] = "digitalocean"
    receipt_path(orchestrator).write_text(json.dumps(receipt), encoding="utf-8")
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["blocking"]
    assert "another provider" in str(report["reason"])


def test_missing_source_after_reconciliation_is_unverified_and_blocking(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    source = active_legacy(orchestrator)
    reconcile(orchestrator)
    source.unlink()
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["classification"] == "unverified-stale"
    assert report["blocking"]


def migrated_setup(tmp_path: Path) -> tuple[Orchestrator, Path, Path]:
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    source = active_legacy(orchestrator)
    runtime_state = Path(record.state_path)
    shutil.copy2(source, runtime_state)
    record.legacy_source_path = str(source)
    record.legacy_source_sha256 = orchestrator._sha256(source)
    orchestrator.deployments.save(record)
    return orchestrator, source, runtime_state


def test_migrated_state_requires_verified_identical_runtime_state(tmp_path: Path) -> None:
    orchestrator, _, _ = migrated_setup(tmp_path)
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["classification"] == "migrated"
    assert not report["blocking"]


def test_migrated_state_with_missing_runtime_state_is_blocking(tmp_path: Path) -> None:
    orchestrator, _, runtime_state = migrated_setup(tmp_path)
    runtime_state.unlink()
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["classification"] == "active"
    assert report["blocking"]
    assert "runtime state is missing" in str(report["reason"])


def test_migrated_state_with_different_runtime_hash_is_blocking(tmp_path: Path) -> None:
    orchestrator, _, runtime_state = migrated_setup(tmp_path)
    runtime_state.write_text(json.dumps(legacy_state("aws-lightsail", "different")), encoding="utf-8")
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["blocking"]
    assert "fingerprint differs" in str(report["reason"])


def test_migrated_state_with_malformed_runtime_state_is_blocking(tmp_path: Path) -> None:
    orchestrator, _, runtime_state = migrated_setup(tmp_path)
    runtime_state.write_text("not-json", encoding="utf-8")
    report = orchestrator._inspect_legacy_provider("aws-lightsail")
    assert report["blocking"]
    assert "runtime state is malformed" in str(report["reason"])
