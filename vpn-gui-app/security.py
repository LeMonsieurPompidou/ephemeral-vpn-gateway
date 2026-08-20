from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import os
import re
import stat
import subprocess
import threading
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_ssh_private_key,
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(token|secret|private[_ -]?key|password)(\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?m)^PrivateKey\s*=\s*.+$"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"(?s)-----BEGIN OPENSSH PRIVATE KEY-----.*?-----END OPENSSH PRIVATE KEY-----"),
)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PROTECTED_FILES: dict[Path, tuple[int, int]] = {}
_PROTECTED_FILES_LOCK = threading.Lock()


class PrivateFileSecurityError(RuntimeError):
    """Raised when a private file cannot be restricted to the current user."""


class SshIdentityError(RuntimeError):
    """Raised when deployment SSH identity artifacts do not form one keypair."""


_SET_WINDOWS_ACL = r"""
$ErrorActionPreference = 'Stop'
$target = $env:EVG_PRIVATE_FILE_TARGET
$sid = [System.Security.Principal.SecurityIdentifier]::new($env:EVG_PRIVATE_FILE_SID)
$acl = Get-Acl -LiteralPath $target
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.InheritanceFlags]::None,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
[void]$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $target -AclObject $acl
""".strip()

_VERIFY_WINDOWS_ACL = r"""
$ErrorActionPreference = 'Stop'
$target = $env:EVG_PRIVATE_FILE_TARGET
$expectedSid = $env:EVG_PRIVATE_FILE_SID
$acl = Get-Acl -LiteralPath $target
if (-not $acl.AreAccessRulesProtected) { throw 'ACL inheritance is enabled' }
$rules = @($acl.Access)
if ($rules.Count -ne 1) { throw 'ACL contains an unexpected number of access rules' }
$rule = $rules[0]
$actualSid = $rule.IdentityReference.Translate(
    [System.Security.Principal.SecurityIdentifier]
).Value
if ($actualSid -ne $expectedSid) { throw 'ACL grants access to an unrelated principal' }
if ($rule.IsInherited) { throw 'ACL contains an inherited access rule' }
if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
    throw 'ACL does not contain the required allow rule'
}
$required = [System.Security.AccessControl.FileSystemRights]::FullControl
if (($rule.FileSystemRights -band $required) -ne $required) {
    throw 'ACL does not grant the current user full control'
}
""".strip()


def redact(value: str) -> str:
    result = _ANSI.sub("", value)
    for pattern in _SECRET_PATTERNS:
        if "PrivateKey" in pattern.pattern:
            result = pattern.sub("PrivateKey = [REDACTED]", result)
        else:
            result = pattern.sub(
                lambda match: (
                    f"{match.group(1)}{match.group(2)}[REDACTED]"
                    if match.lastindex and match.lastindex >= 3
                    else "[REDACTED]"
                ),
                result,
            )
    return result


def generate_wireguard_keypair() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    private_bytes = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private_bytes).decode(), base64.b64encode(public_bytes).decode()


def generate_ssh_keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_text = private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode("ascii")
    public_text = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    return private_text, public_text


def ssh_public_key_fingerprint(public_text: str) -> str:
    """Return an OpenSSH SHA-256 fingerprint without retaining key comments."""
    parts = public_text.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise SshIdentityError("SSH public key is malformed or is not Ed25519")
    try:
        key_blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SshIdentityError("SSH public key is malformed") from exc
    digest = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def verify_ssh_keypair(private_path: Path, public_path: Path) -> str:
    """Verify deployment private/public files are the same Ed25519 identity."""
    try:
        private_key = load_ssh_private_key(private_path.read_bytes(), password=None)
        public_text = public_path.read_text(encoding="ascii").strip()
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise SshIdentityError("Deployment SSH identity files are missing or malformed") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SshIdentityError("Deployment SSH private key is not Ed25519")
    derived = private_key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    supplied_parts = public_text.split()
    supplied = " ".join(supplied_parts[:2]) if len(supplied_parts) >= 2 else public_text
    if not hmac.compare_digest(derived, supplied):
        raise SshIdentityError("Deployment SSH private and public keys do not match")
    return ssh_public_key_fingerprint(derived)


def _platform_is_windows() -> bool:
    return os.name == "nt"


def _run_security_command(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env.update(env or {})
    creationflags = subprocess.CREATE_NO_WINDOW if _platform_is_windows() else 0
    return subprocess.run(
        args,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged_env,
        creationflags=creationflags,
        check=False,
    )


def _windows_identity_sid() -> str:
    result = _run_security_command(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    if result.returncode:
        raise PrivateFileSecurityError("Cannot determine the current Windows identity")
    try:
        row = next(csv.reader([result.stdout.strip()]))
    except (csv.Error, StopIteration) as exc:
        raise PrivateFileSecurityError("Cannot parse the current Windows identity") from exc
    if len(row) < 2 or not re.fullmatch(r"S-1-(?:\d+-)+\d+", row[1].strip(), flags=re.IGNORECASE):
        raise PrivateFileSecurityError("The current Windows identity did not include a valid SID")
    return row[1].strip()


def _windows_acl_environment(path: Path, sid: str) -> dict[str, str]:
    return {"EVG_PRIVATE_FILE_TARGET": str(path.resolve()), "EVG_PRIVATE_FILE_SID": sid}


def _set_windows_private_acl(path: Path, sid: str) -> None:
    result = _run_security_command(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SET_WINDOWS_ACL],
        env=_windows_acl_environment(path, sid),
    )
    if result.returncode:
        raise PrivateFileSecurityError(f"Cannot secure the Windows ACL: {redact(result.stderr).strip()[-300:]}")


def _verify_windows_private_acl(path: Path, sid: str) -> None:
    result = _run_security_command(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _VERIFY_WINDOWS_ACL],
        env=_windows_acl_environment(path, sid),
    )
    if result.returncode:
        raise PrivateFileSecurityError(f"Windows ACL verification failed: {redact(result.stderr).strip()[-300:]}")


def protect_private_file(path: Path) -> None:
    """Restrict a private file and verify the resulting platform permissions."""
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise PrivateFileSecurityError(f"Private file is missing or is not a regular file: {resolved}")
    if _platform_is_windows():
        sid = _windows_identity_sid()
        try:
            _verify_windows_private_acl(resolved, sid)
            return
        except PrivateFileSecurityError:
            pass
        _set_windows_private_acl(resolved, sid)
        _verify_windows_private_acl(resolved, sid)
        return
    try:
        resolved.chmod(0o600)
    except OSError as exc:
        raise PrivateFileSecurityError(f"Cannot set private file mode on {resolved}") from exc
    verify_private_file(resolved)


def verify_private_file(path: Path) -> None:
    """Fail if a private file would be rejected by the platform SSH client."""
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise PrivateFileSecurityError(f"Private file is missing or is not a regular file: {resolved}")
    if _platform_is_windows():
        _verify_windows_private_acl(resolved, _windows_identity_sid())
        return
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != 0o600:
        raise PrivateFileSecurityError(f"Private file mode must be 0600, not {mode:04o}: {resolved}")


def write_secret(path: Path, content: str) -> None:
    write_secret_bytes(path, content.encode("utf-8"))


def write_secret_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    resolved = path.resolve()
    file_stat = resolved.stat()
    identity = (file_stat.st_dev, file_stat.st_ino)
    with _PROTECTED_FILES_LOCK:
        if _PROTECTED_FILES.get(resolved) == identity:
            return
        protect_private_file(resolved)
        secured_stat = resolved.stat()
        _PROTECTED_FILES[resolved] = (secured_stat.st_dev, secured_stat.st_ino)


def append_redacted_log(path: Path, line: str, max_bytes: int = 512 * 1024) -> None:
    safe = redact(line).replace("\x00", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = b""
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        pass
    payload = existing + (safe.rstrip() + "\n").encode("utf-8", errors="replace")
    if len(payload) > max_bytes:
        payload = payload[-max_bytes:]
        newline = payload.find(b"\n")
        if newline >= 0:
            payload = payload[newline + 1 :]
    write_secret(path, payload.decode("utf-8", errors="replace"))
