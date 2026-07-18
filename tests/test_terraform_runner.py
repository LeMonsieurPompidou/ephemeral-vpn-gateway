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
