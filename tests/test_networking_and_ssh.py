from __future__ import annotations

import socket
import threading
from pathlib import Path

import networking
import orchestrator as orchestrator_module
import pytest
from helpers import add_record, make_orchestrator
from models import DeploymentOptions
from networking import PublicIpDetectionError, detect_public_ipv4, normalize_public_ipv4_cidr
from security import PrivateFileSecurityError
from terraform_runner import TerraformError


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AdvancingEvent:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        self.clock.advance(timeout)
        return False


class CloudSequence:
    def __init__(self, statuses: list[str], progress: list[str]) -> None:
        self.statuses = statuses
        self.progress = progress
        self.poll = 0

    def __call__(self, args, cancel, *, timeout):  # type: ignore[no-untyped-def]
        command = args[-1]
        if command == "cloud-init status --long":
            status = self.statuses[min(self.poll, len(self.statuses) - 1)]
            return (2 if status == "error" else 0), f"status: {status}", ""
        if command == orchestrator_module.CLOUD_INIT_PROGRESS_COMMAND:
            value = self.progress[min(self.poll, len(self.progress) - 1)]
            self.poll += 1
            return 0, value, ""
        raise AssertionError(f"Unexpected SSH command: {command}")


def test_public_ipv4_detection_and_normalization(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(networking.urllib.request, "urlopen", lambda request, timeout: Response(b"8.8.8.8\n"))
    assert detect_public_ipv4() == "8.8.8.8/32"
    assert normalize_public_ipv4_cidr("8.8.4.4") == "8.8.4.4/32"


@pytest.mark.parametrize("value", ["127.0.0.1", "10.0.0.1/32", "::1", "8.8.8.0/24", "bad"])
def test_invalid_or_non_public_ssh_override(value: str) -> None:
    with pytest.raises(PublicIpDetectionError):
        normalize_public_ipv4_cidr(value)


def test_public_ipv4_timeout_is_actionable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def timeout(request, timeout):  # type: ignore[no-untyped-def]
        raise TimeoutError

    monkeypatch.setattr(networking.urllib.request, "urlopen", timeout)
    with pytest.raises(PublicIpDetectionError, match="Retry"):
        detect_public_ipv4()


def test_ssh_command_uses_explicit_deployment_identity(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    identity = Path(record.runtime_directory, "ssh.privatekey")
    identity.write_text("private", encoding="utf-8")

    class Connection:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(orchestrator_module, "verify_private_file", lambda path: None)
    captured: list[str] = []

    def run(args, cancel, *, timeout):  # type: ignore[no-untyped-def]
        captured.extend(args)
        if args[-1] == "cloud-init status --long":
            return 0, "status: done", ""
        return 0, "", ""

    monkeypatch.setattr(orchestrator, "_run_cancellable_process", run)
    orchestrator._ssh_health_checks(
        record, DeploymentOptions(ssh_cidr="8.8.8.8/32"), threading.Event(), automatic_ssh_cidr=False
    )
    assert captured[captured.index("-i") + 1] == str(identity)
    assert "IdentitiesOnly=yes" in captured
    assert "PasswordAuthentication=no" in captured
    assert any(value.startswith("UserKnownHostsFile=") for value in captured)


def test_changed_public_ip_refreshes_firewall_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path, ip_detector=lambda: "8.8.4.4/32")
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    Path(record.runtime_directory, "ssh.privatekey").write_text("private", encoding="utf-8")
    attempts = 0

    class Connection:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise OSError("not ready")
        return Connection()

    class NoWaitEvent:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            return False

    refreshed: list[str] = []
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(orchestrator_module, "verify_private_file", lambda path: None)
    monkeypatch.setattr(orchestrator, "_refresh_ssh_firewall", lambda record, cidr, cancel: refreshed.append(cidr))
    monkeypatch.setattr(
        orchestrator,
        "_run_cancellable_process",
        lambda args, *unused, **kwargs: (0, "status: done" if args[-1] == "cloud-init status --long" else "", ""),
    )
    orchestrator._ssh_health_checks(
        record,
        DeploymentOptions(ssh_cidr="8.8.8.8/32"),
        NoWaitEvent(),  # type: ignore[arg-type]
        automatic_ssh_cidr=True,
    )
    assert refreshed == ["8.8.4.4/32"]


def test_ssh_readiness_honors_cancellation(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(Exception, match="cancelled"):
        orchestrator._ssh_health_checks(
            record,
            DeploymentOptions(ssh_cidr="8.8.8.8/32"),
            cancelled,
            automatic_ssh_cidr=False,
        )


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [(0, "status: running", "running"), (0, "status: done", "done"), (2, "status: error", "error")],
)
def test_cloud_init_status_is_classified(returncode: int, output: str, expected: str) -> None:
    assert orchestrator_module.Orchestrator._cloud_init_status(returncode, output) == expected


def test_cloud_init_error_fails_immediately_with_sanitized_phase(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    Path(record.runtime_directory, "ssh.privatekey").write_text("private", encoding="utf-8")

    class Connection:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

    calls = 0

    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        command = args[0][-1]
        if command == "true":
            return 0, "", ""
        if command == "cloud-init status --long":
            return 2, "status: error", ""
        return 0, "ephemeral-vpn bootstrap failed in phase SSH hardening/configuration", ""

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(orchestrator_module, "verify_private_file", lambda path: None)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", run)
    with pytest.raises(TerraformError, match="during SSH hardening/configuration.*must be destroyed"):
        orchestrator._ssh_health_checks(
            record,
            DeploymentOptions(ssh_cidr="8.8.8.8/32"),
            threading.Event(),
            automatic_ssh_cidr=False,
        )
    assert calls == 3


@pytest.mark.parametrize(
    ("phase", "category"),
    [
        ("prerequisite installation", "apt-get-install"),
        ("SSH hardening/configuration", "ssh-service-restart"),
        ("WireGuard configuration", "wireguard-configuration"),
    ],
)
def test_cloud_init_error_uses_persisted_safe_failure_diagnostic(
    tmp_path: Path, monkeypatch, phase: str, category: str
) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    Path(record.runtime_directory, "ssh.privatekey").write_text("private", encoding="utf-8")

    class Connection:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

    outputs = iter(
        [
            (0, "", ""),
            (2, "status: error", ""),
            (
                0,
                "ephemeral-vpn bootstrap failure: "
                f"phase={phase} category={category} exit=1 line=42 build=0123456789ab\n"
                "PrivateKey = must-not-appear",
                "",
            ),
        ]
    )
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(orchestrator_module, "verify_private_file", lambda path: None)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", lambda *args, **kwargs: next(outputs))
    with pytest.raises(TerraformError) as raised:
        orchestrator._ssh_health_checks(
            record,
            DeploymentOptions(ssh_cidr="8.8.8.8/32"),
            threading.Event(),
            automatic_ssh_cidr=False,
        )
    message = str(raised.value)
    assert phase in message
    assert category in message
    assert "exit 1" in message
    assert "build 0123456789ab" in message
    assert "must-not-appear" not in message


def _progress(phase: str, activity: int, *, ready: bool = False) -> str:
    return (
        f"phase={phase}\nbuild=0123456789ab\nactivity_epoch={activity}\nactivity_size={activity}\nready={int(ready)}\n"
    )


def test_cloud_init_can_progress_beyond_old_timeout_and_finish(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    clock = FakeClock()
    event = AdvancingEvent(clock)
    orchestrator.cloud_init_hard_timeout = 900
    orchestrator.cloud_init_inactivity_timeout = 180
    orchestrator.readiness_poll_interval = 60
    sequence = CloudSequence(
        ["running"] * 6 + ["done"],
        [_progress("prerequisite installation", index) for index in range(1, 8)],
    )
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", sequence)

    orchestrator._wait_for_cloud_init(
        record,
        Path("identity"),
        Path("known-hosts"),
        "ubuntu",
        event,  # type: ignore[arg-type]
        overall_started=0,
    )

    assert clock.value == 360
    persisted = orchestrator.deployments.get(record.id)
    assert persisted.bootstrap_phase == "prerequisite installation"
    assert persisted.provisioning_elapsed_seconds == 360
    logs = Path(record.runtime_directory, "deployment.log").read_text(encoding="utf-8")
    assert "Cloud init still active during prerequisite installation" in logs
    assert "elapsed 5m00s" in logs
    assert "Cloud init completed (6m00s)" in logs


def test_cloud_init_running_without_progress_stalls(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    clock = FakeClock()
    event = AdvancingEvent(clock)
    orchestrator.cloud_init_hard_timeout = 60
    orchestrator.cloud_init_inactivity_timeout = 10
    orchestrator.readiness_poll_interval = 5
    sequence = CloudSequence(["running"], [_progress("prerequisite installation", 1)])
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", sequence)

    with pytest.raises(TerraformError, match="stalled during prerequisite installation.*10s"):
        orchestrator._wait_for_cloud_init(
            record,
            Path("identity"),
            Path("known-hosts"),
            "ubuntu",
            event,  # type: ignore[arg-type]
            overall_started=0,
        )


def test_cloud_init_phase_change_resets_inactivity_deadline(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    clock = FakeClock()
    event = AdvancingEvent(clock)
    orchestrator.cloud_init_hard_timeout = 30
    orchestrator.cloud_init_inactivity_timeout = 10
    orchestrator.readiness_poll_interval = 5
    sequence = CloudSequence(
        ["running", "running", "running", "done"],
        [
            _progress("prerequisite installation", 1),
            _progress("prerequisite installation", 1),
            _progress("SSH hardening/configuration", 2),
            _progress("final readiness validation", 3, ready=True),
        ],
    )
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", sequence)

    orchestrator._wait_for_cloud_init(
        record,
        Path("identity"),
        Path("known-hosts"),
        "ubuntu",
        event,  # type: ignore[arg-type]
        overall_started=0,
    )
    assert record.bootstrap_phase == "final readiness validation"


def test_cloud_init_finishing_immediately_before_hard_deadline_succeeds(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    clock = FakeClock()
    event = AdvancingEvent(clock)
    orchestrator.cloud_init_hard_timeout = 10
    orchestrator.cloud_init_inactivity_timeout = 5
    orchestrator.readiness_poll_interval = 1
    sequence = CloudSequence(
        ["running"] * 9 + ["done"],
        [_progress("prerequisite installation", index) for index in range(10)],
    )
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", sequence)

    orchestrator._wait_for_cloud_init(
        record,
        Path("identity"),
        Path("known-hosts"),
        "ubuntu",
        event,  # type: ignore[arg-type]
        overall_started=0,
    )
    assert clock.value == 9


def test_cloud_init_active_progress_hits_hard_maximum(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    clock = FakeClock()
    event = AdvancingEvent(clock)
    orchestrator.cloud_init_hard_timeout = 20
    orchestrator.cloud_init_inactivity_timeout = 10
    orchestrator.readiness_poll_interval = 5
    sequence = CloudSequence(["running"], [_progress("prerequisite installation", index) for index in range(10)])
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock)
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", sequence)

    with pytest.raises(TerraformError, match="progressing.*maximum provisioning time of 20s"):
        orchestrator._wait_for_cloud_init(
            record,
            Path("identity"),
            Path("known-hosts"),
            "ubuntu",
            event,  # type: ignore[arg-type]
            overall_started=0,
        )


def test_ssh_and_wireguard_have_separate_timeouts(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    clock = FakeClock()
    event = AdvancingEvent(clock)
    orchestrator.ssh_readiness_timeout = 10
    orchestrator.wireguard_readiness_timeout = 15
    orchestrator.readiness_poll_interval = 5
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))
    with pytest.raises(TerraformError, match="SSH readiness timed out after 10s"):
        orchestrator._wait_for_ssh(
            record,
            Path("identity"),
            Path("known-hosts"),
            "ubuntu",
            DeploymentOptions(ssh_cidr="8.8.8.8/32"),
            event,  # type: ignore[arg-type]
            overall_started=0,
            automatic_ssh_cidr=False,
        )

    clock.value = 0
    monkeypatch.setattr(orchestrator, "_run_cancellable_process", lambda *args, **kwargs: (1, "", "not ready"))
    with pytest.raises(TerraformError, match="WireGuard readiness timed out after 15s"):
        orchestrator._wait_for_wireguard(
            record,
            Path("identity"),
            Path("known-hosts"),
            "ubuntu",
            DeploymentOptions(ssh_cidr="8.8.8.8/32"),
            event,  # type: ignore[arg-type]
            readiness_marker="/var/lib/ephemeral-vpn/ready",
            overall_started=0,
        )


def test_invalid_private_key_permissions_prevent_any_ssh_attempt(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orchestrator = make_orchestrator(tmp_path)
    record = add_record(orchestrator, "aws-lightsail")
    record.public_ip = "203.0.113.10"
    Path(record.runtime_directory, "ssh.privatekey").write_text("private", encoding="utf-8")
    socket_attempted = False

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal socket_attempted
        socket_attempted = True
        raise AssertionError("SSH networking must not be attempted")

    def invalid(path: Path) -> None:
        raise PrivateFileSecurityError("unrelated principal can read the key")

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(orchestrator_module, "verify_private_file", invalid)
    with pytest.raises(TerraformError, match="Local SSH private-key security check failed"):
        orchestrator._ssh_health_checks(
            record,
            DeploymentOptions(ssh_cidr="8.8.8.8/32"),
            threading.Event(),
            automatic_ssh_cidr=False,
        )
    assert not socket_attempted
