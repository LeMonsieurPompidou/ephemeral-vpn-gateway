from pathlib import Path

import pytest
from file_lock import FileLock, FileLockTimeout
from security import append_redacted_log


def test_persisted_log_is_bounded_redacted_and_has_no_ansi(tmp_path: Path) -> None:
    path = tmp_path / "deployment.log"
    append_redacted_log(path, "\x1b[31mPrivateKey = secret-value\x1b[0m", max_bytes=128)
    for index in range(20):
        append_redacted_log(path, f"ordinary line {index}", max_bytes=128)
    content = path.read_text(encoding="utf-8")
    assert "secret-value" not in content
    assert "\x1b" not in content
    assert len(path.read_bytes()) <= 128


def test_file_lock_prevents_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "provider.lock"
    with FileLock(path):
        with pytest.raises(FileLockTimeout):
            with FileLock(path, timeout=0.05):
                pass
