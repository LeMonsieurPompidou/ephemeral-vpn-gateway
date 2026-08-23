from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_session_cost(script: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    module = ROOT / "vpn-gui-app" / "ui" / "session_cost.js"
    result = subprocess.run(
        [node, "-e", f"const cost=require(process.argv[1]);{script}", str(module)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_estimated_cost_uses_apply_start_and_fake_clock_without_float_artifacts() -> None:
    script = """
const started='2026-08-21T10:00:00Z';
const record={apply_started_at:started,estimated_hourly_cost_usd:0.005,state:'ready'};
const at=(seconds)=>cost.estimate(record,Date.parse(started)+(seconds*1000));
const values=[at(0),at(30),at(462),at(5400)].map((item)=>({
  seconds:item.elapsedSeconds,raw:item.costUsd,display:cost.formatCost(item.costUsd),
  duration:cost.formatDuration(item.elapsedSeconds)
}));
process.stdout.write(JSON.stringify(values));
"""
    values = run_session_cost(script)
    assert values == [
        {"seconds": 0, "raw": 0, "display": "$0.00000", "duration": "00m 00s"},
        {
            "seconds": 30,
            "raw": pytest.approx(0.000041666666666666665),
            "display": "$0.00004",
            "duration": "00m 30s",
        },
        {"seconds": 462, "raw": pytest.approx(0.0006416666666666667), "display": "$0.00064", "duration": "07m 42s"},
        {"seconds": 5400, "raw": 0.0075, "display": "$0.0075", "duration": "1h 30m 00s"},
    ]


def test_missing_start_or_pricing_is_unavailable() -> None:
    script = """
const noStart=cost.estimate({estimated_hourly_cost_usd:0.005},Date.parse('2026-08-21T10:01:00Z'));
const noPrice=cost.estimate(
  {apply_started_at:'2026-08-21T10:00:00Z',estimated_hourly_cost_usd:null},
  Date.parse('2026-08-21T10:01:00Z')
);
process.stdout.write(JSON.stringify({noStart,noPrice,display:cost.formatCost(noPrice.costUsd)}));
"""
    assert run_session_cost(script) == {
        "noStart": {"elapsedSeconds": None, "costUsd": None, "finalized": False},
        "noPrice": {"elapsedSeconds": 60, "costUsd": None, "finalized": False},
        "display": "Unavailable",
    }


def test_destroyed_timestamp_freezes_cost_and_restart_reconstructs_same_value() -> None:
    script = """
const record={
  apply_started_at:'2026-08-21T10:00:00Z',destroyed_at:'2026-08-21T10:47:18Z',
  updated_at:'2026-08-21T10:47:19Z',estimated_hourly_cost_usd:0.005,state:'destroyed'
};
const first=cost.estimate(record,Date.parse('2026-08-21T11:00:00Z'));
const restarted=cost.estimate(JSON.parse(JSON.stringify(record)),Date.parse('2026-08-22T11:00:00Z'));
process.stdout.write(JSON.stringify({first,restarted,display:cost.formatCost(first.costUsd)}));
"""
    value = cast(dict[str, Any], run_session_cost(script))
    assert value["first"] == value["restarted"]
    assert value["first"]["elapsedSeconds"] == 2838
    assert value["first"]["finalized"] is True
    assert value["display"] == "$0.0039"
