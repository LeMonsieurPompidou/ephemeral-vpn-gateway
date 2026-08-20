from __future__ import annotations

import re


class SshProbeError(RuntimeError):
    """A secret-safe readiness-probe failure."""


class SshTransientError(SshProbeError):
    """A bounded, retryable SSH transport or probe failure."""


class SshProbeTimeout(SshTransientError):
    """The SSH process did not finish within its bounded deadline."""


class SshConnectTimeout(SshTransientError):
    """OpenSSH could not establish its transport before ConnectTimeout."""


class SshTransportFailure(SshTransientError):
    """The SSH transport was temporarily unavailable."""


class SshAuthenticationFailure(SshProbeError):
    """The deployment identity was rejected by the server."""


class SshHostKeyFailure(SshProbeError):
    """The server host key could not be verified safely."""


class SshRemoteCommandFailure(SshProbeError):
    """SSH succeeded but the requested readiness command failed."""


_AUTH_FAILURE = re.compile(
    r"permission denied|too many authentication failures|no supported authentication methods",
    flags=re.IGNORECASE,
)
_HOST_KEY_FAILURE = re.compile(
    r"remote host identification has changed|host key verification failed|"
    r"no matching host key type found|offending .* key",
    flags=re.IGNORECASE,
)
_CONNECT_TIMEOUT = re.compile(r"connection timed out|operation timed out", flags=re.IGNORECASE)


def classify_ssh_failure(returncode: int, output: str) -> SshProbeError:
    """Classify an SSH failure without retaining argv, paths, or remote commands."""
    if returncode != 255:
        return SshRemoteCommandFailure(f"Remote SSH readiness command failed (exit {returncode}).")
    if _AUTH_FAILURE.search(output):
        return SshAuthenticationFailure("SSH authentication failed for the deployment identity.")
    if _HOST_KEY_FAILURE.search(output):
        return SshHostKeyFailure("SSH host-key verification failed for the deployment server.")
    if _CONNECT_TIMEOUT.search(output):
        return SshConnectTimeout("SSH connection timed out.")
    return SshTransportFailure("SSH transport is temporarily unavailable.")
