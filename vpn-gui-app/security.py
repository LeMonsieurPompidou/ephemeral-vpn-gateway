from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

_SECRET_PATTERNS = (
    re.compile(r"(?i)(token|secret|private[_ -]?key|password)(\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?m)^PrivateKey\s*=\s*.+$"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


def redact(value: str) -> str:
    result = value
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


def write_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        path.chmod(0o600)
    except OSError:
        pass
