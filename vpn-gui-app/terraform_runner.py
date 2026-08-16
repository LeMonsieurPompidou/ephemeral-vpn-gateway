from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from security import redact


class TerraformError(RuntimeError):
    pass


class TerraformCancelled(TerraformError):
    pass


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class TerraformRunner:
    _directory_locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, executable: str = "terraform", timeout: float = 900) -> None:
        self.executable = executable
        self.timeout = timeout

    @classmethod
    def lock_for(cls, directory: Path) -> threading.Lock:
        resolved = directory.resolve()
        with cls._locks_guard:
            return cls._directory_locks.setdefault(resolved, threading.Lock())

    def run(
        self,
        args: list[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> CommandResult:
        if not cwd.is_dir():
            raise TerraformError(f"Terraform working directory does not exist: {cwd}")
        command = [self.executable, *args]
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            creationflags=creationflags,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def consume(stream: object, target: list[str]) -> None:
            assert hasattr(stream, "readline")
            for line in iter(stream.readline, ""):  # type: ignore[attr-defined]
                safe = redact(line.rstrip())
                target.append(safe + "\n")
                if progress and safe:
                    progress(safe)

        threads = [
            threading.Thread(target=consume, args=(process.stdout, stdout_lines), daemon=True),
            threading.Thread(target=consume, args=(process.stderr, stderr_lines), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while process.poll() is None:
            if cancel and cancel.is_set():
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise TerraformCancelled("Terraform operation cancelled")
            if time.monotonic() >= deadline:
                process.kill()
                raise TerraformError(f"Terraform command timed out after {timeout or self.timeout:g} seconds")
            time.sleep(0.1)
        for thread in threads:
            thread.join(timeout=2)
        result = CommandResult(tuple(command), process.returncode, "".join(stdout_lines), "".join(stderr_lines))
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise TerraformError(f"Terraform exited with code {result.returncode}: {detail}")
        return result

    def output_json(self, cwd: Path, **kwargs: object) -> dict[str, object]:
        result = self.run(["output", "-json"], cwd, **kwargs)  # type: ignore[arg-type]
        try:
            value = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TerraformError("Terraform returned malformed JSON output") from exc
        if not isinstance(value, dict):
            raise TerraformError("Terraform JSON output must be an object")
        return value


def output_value(outputs: dict[str, object], name: str) -> object:
    item = outputs.get(name)
    if not isinstance(item, dict) or "value" not in item:
        raise TerraformError(f"Terraform output did not include {name}")
    return item["value"]
