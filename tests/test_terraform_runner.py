import json
import sys
import threading
from pathlib import Path

import pytest
from terraform_runner import TerraformCancelled, TerraformError, TerraformRunner, output_value


def test_command_construction_and_json_parsing(tmp_path: Path) -> None:
    runner = TerraformRunner(executable=sys.executable)
    result = runner.run(["-c", "import json; print(json.dumps({'vpn_public_ip': {'value': '1.2.3.4'}}))"], tmp_path)
    assert output_value(json.loads(result.stdout), "vpn_public_ip") == "1.2.3.4"


def test_subprocess_output_is_decoded_as_utf8(tmp_path: Path) -> None:
    runner = TerraformRunner(executable=sys.executable)
    result = runner.run(
        ["-c", "import sys; sys.stdout.buffer.write('│ ╵'.encode('utf-8'))"],
        tmp_path,
    )
    assert result.stdout == "│ ╵\n"


def test_provider_environment_is_inherited_without_being_added_to_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "unit-test-token-never-log")
    runner = TerraformRunner(executable=sys.executable)
    result = runner.run(
        [
            "-c",
            "import os; print('inherited' if os.getenv('DIGITALOCEAN_TOKEN') else 'missing')",
        ],
        tmp_path,
    )
    assert result.stdout == "inherited\n"
    assert "unit-test-token-never-log" not in result.stdout


def test_timeout(tmp_path: Path) -> None:
    runner = TerraformRunner(executable=sys.executable, timeout=0.1)
    with pytest.raises(TerraformError, match="timed out"):
        runner.run(["-c", "import time; time.sleep(2)"], tmp_path)


def test_cancellation(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    runner = TerraformRunner(executable=sys.executable)
    with pytest.raises(TerraformCancelled):
        runner.run(["-c", "import time; time.sleep(2)"], tmp_path, cancel=cancel)


def test_malformed_output_is_actionable() -> None:
    with pytest.raises(TerraformError, match="did not include"):
        output_value({}, "vpn_public_ip")
