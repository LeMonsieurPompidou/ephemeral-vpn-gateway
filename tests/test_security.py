from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import security
from security import (
    PrivateFileSecurityError,
    SshIdentityError,
    generate_ssh_keypair,
    generate_wireguard_keypair,
    redact,
    ssh_public_key_fingerprint,
    verify_private_file,
    verify_ssh_keypair,
    write_secret,
)


def test_wireguard_keypair_is_base64_and_distinct() -> None:
    private, public = generate_wireguard_keypair()
    assert len(private) == 44 and len(public) == 44 and private != public


def test_ssh_private_public_files_match_one_fingerprint(tmp_path: Path) -> None:
    private, public = generate_ssh_keypair()
    private_path = tmp_path / "ssh.privatekey"
    public_path = tmp_path / "ssh.publickey"
    private_path.write_text(private, encoding="ascii")
    public_path.write_text(public + " deployment-comment\n", encoding="ascii")
    assert verify_ssh_keypair(private_path, public_path) == ssh_public_key_fingerprint(public)


def test_ssh_keypair_verification_rejects_another_deployment_identity(tmp_path: Path) -> None:
    private, _public = generate_ssh_keypair()
    _other_private, other_public = generate_ssh_keypair()
    private_path = tmp_path / "ssh.privatekey"
    public_path = tmp_path / "ssh.publickey"
    private_path.write_text(private, encoding="ascii")
    public_path.write_text(other_public, encoding="ascii")
    with pytest.raises(SshIdentityError, match="do not match"):
        verify_ssh_keypair(private_path, public_path)


def test_redaction_removes_tokens_and_private_keys() -> None:
    value = redact("token=abc123\nPrivateKey = secret-value\nAWS AKIA1234567890123456")
    assert "abc123" not in value
    assert "secret-value" not in value
    assert "AKIA1234567890123456" not in value


def test_windows_private_file_acl_is_current_user_only_and_handles_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    verification_calls = 0

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal verification_calls
        calls.append((args, kwargs))
        stdout = '"DESKTOP\\current user","S-1-5-21-1-2-3-1001"\n' if args[0] == "whoami.exe" else ""
        if args[0] == "powershell.exe" and "unexpected number of access rules" in args[-1]:
            verification_calls += 1
            if verification_calls == 1:
                return subprocess.CompletedProcess(args, 1, "", "inherited access rule")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(security, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(security.subprocess, "run", run)
    target = tmp_path / "directory with spaces" / "ssh.privatekey"
    write_secret(target, "private")

    assert len(calls) == 4
    assert calls[0][0] == ["whoami.exe", "/user", "/fo", "csv", "/nh"]
    for args, kwargs in calls[1:]:
        assert args[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        assert kwargs["shell"] is False
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["EVG_PRIVATE_FILE_TARGET"] == str(target.resolve())
        assert env["EVG_PRIVATE_FILE_SID"] == "S-1-5-21-1-2-3-1001"
    assert "unexpected number of access rules" in calls[1][0][-1]
    assert "SetAccessRuleProtection($true, $false)" in calls[2][0][-1]
    assert "RemoveAccessRuleSpecific" in calls[2][0][-1]
    assert "unexpected number of access rules" in calls[3][0][-1]

    write_secret(target, "updated private")
    assert len(calls) == 4


@pytest.mark.parametrize("failure_call", ["set", "verify"])
def test_windows_acl_command_or_verification_failure_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: str
) -> None:
    set_calls = 0
    verification_calls = 0

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal set_calls, verification_calls
        if args[0] == "whoami.exe":
            return subprocess.CompletedProcess(args, 0, '"DESKTOP\\user","S-1-5-21-1-2-3-1001"\n', "")
        is_verification = "unexpected number of access rules" in args[-1]
        if is_verification:
            verification_calls += 1
        else:
            set_calls += 1
        failed = verification_calls == 1 if is_verification else failure_call == "set" and set_calls == 1
        if is_verification and verification_calls == 2:
            failed = failure_call == "verify"
        return subprocess.CompletedProcess(args, 1 if failed else 0, "", "access denied" if failed else "")

    monkeypatch.setattr(security, "_platform_is_windows", lambda: True)
    monkeypatch.setattr(security.subprocess, "run", run)
    target = tmp_path / "client.privatekey"
    with pytest.raises(PrivateFileSecurityError, match="ACL"):
        write_secret(target, "private")


def test_posix_private_file_is_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "_platform_is_windows", lambda: False)
    chmod_calls: list[int] = []
    verified: list[Path] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: chmod_calls.append(mode))
    monkeypatch.setattr(security, "verify_private_file", lambda path: verified.append(path))
    target = tmp_path / "client.privatekey"
    write_secret(target, "private")
    assert chmod_calls == [0o600]
    assert verified == [target.resolve()]


def test_posix_private_file_verification_rejects_group_or_other_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security, "_platform_is_windows", lambda: False)
    target = tmp_path / "client.privatekey"
    target.write_text("private", encoding="utf-8")
    monkeypatch.setattr(Path, "stat", lambda self, *args, **kwargs: SimpleNamespace(st_mode=stat.S_IFREG | 0o644))
    with pytest.raises(PrivateFileSecurityError, match="0600"):
        verify_private_file(target)
