"""Spark (NVIDIA unified-memory) vLLM launch sizing, driven through node.

vLLM refuses to start when free memory < gpu_memory_utilization * total. The
helper must keep util * total <= free - headroom, where headroom is the larger
of 4 GiB and 8% of total.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from routes.cookbook_helpers import _diagnose_serve_output

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "cookbookSparkDefaults.js"
_HAS_NODE = shutil.which("node") is not None
pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def _run(expr):
    js = (
        "import * as S from '" + _HELPER.as_posix() + "';"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


GIB = 1024


@pytest.mark.parametrize("total_gib,free_gib", [(121, 100), (121, 60), (121, 30), (121, 121), (64, 20), (32, 12)])
def test_util_times_total_fits_free_minus_headroom(total_gib, free_gib):
    r = _run(f"S.sparkGpuMemoryUtilization({total_gib * GIB}, {free_gib * GIB})")
    headroom = max(4.0, 0.08 * total_gib)
    assert r["util"] is not None
    assert r["util"] * total_gib <= free_gib - headroom + 1e-9
    assert r["util"] <= 0.95
    assert "free" in r["reason"] and "headroom" in r["reason"]


def test_headroom_is_larger_of_4gib_and_8_percent():
    assert _run("S.sparkHeadroomGib(32)") == 4.0          # 8% = 2.56 -> 4 wins
    assert _run("S.sparkHeadroomGib(121)") == pytest.approx(9.68)  # 8% wins


def test_no_room_returns_insufficient_not_a_bad_value():
    r = _run(f"S.sparkGpuMemoryUtilization({121 * GIB}, {5 * GIB})")
    assert r["util"] is None and r["insufficient"] is True
    assert "free memory first" in r["reason"]


def test_bad_probe_data_returns_null():
    assert _run("S.sparkGpuMemoryUtilization(0, 0)") is None
    assert _run("S.sparkGpuMemoryUtilization('x', 5)") is None


def test_only_nvidia_unified_devices_get_spark_treatment():
    gb10 = "{name:'NVIDIA GB10', backend:'cuda', unified_memory:true}"
    assert _run(f"S.isNvidiaUnifiedGpu({gb10})") is True
    assert _run("S.isNvidiaUnifiedGpu({name:'NVIDIA GB10'})") is True
    assert _run("S.isNvidiaUnifiedGpu({name:'RTX 4090', backend:'cuda'})") is False
    assert _run("S.isNvidiaUnifiedGpu({name:'Apple M3', backend:'metal', unified_memory:true})") is False
    assert _run("S.isNvidiaUnifiedGpu({name:'AMD Radeon 890M', backend:'rocm', unified_memory:true})") is False


def test_spark_defaults_live_in_one_frozen_place():
    d = _run("S.SPARK_DEFAULTS")
    # Not flipped yet: another worker is measuring these first.
    assert d["quickRunEnforceEager"] is True
    assert d["kvCacheDtype"] == ""
    assert d["headroomMinGiB"] == 4 and d["headroomFraction"] == 0.08
    assert _run("Object.isFrozen(S.SPARK_DEFAULTS)") is True


def test_startup_free_memory_failure_suggests_dropping_page_cache():
    out = (
        "ValueError: Free memory on device cuda:0 (20.1/121.7 GiB) on startup is less "
        "than desired GPU memory utilization (0.9, 109.5 GiB)."
    )
    diag = _diagnose_serve_output(out)
    assert diag is not None
    text = json.dumps(diag)
    assert "drop_caches" in text and "page cache" in text
