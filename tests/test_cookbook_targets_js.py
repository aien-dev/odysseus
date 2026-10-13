"""Apple-target detection for the Cookbook image-model filter.

Regression: cookbook-hwfit.js treated `system.unified_memory` as an Apple
signal, so an NVIDIA GB10 / DGX Spark (which the hardware probe flags
unified_memory=True) had its image-model list cut down to MLX-only models.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "cookbookTargets.js"
_HWFIT = _REPO / "static" / "js" / "cookbook-hwfit.js"
_HAS_NODE = shutil.which("node") is not None


def _run(expr):
    js = (
        f"import {{ isAppleTarget }} from '{_HELPER.as_posix()}';"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_dgx_spark_is_not_an_apple_target():
    assert _run("isAppleTarget({platform:'linux', backend:'cuda', gpuName:'NVIDIA GB10'})") is False


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_real_apple_targets_still_match():
    assert _run("isAppleTarget({platform:'darwin'})") is True
    assert _run("isAppleTarget({backend:'metal'})") is True
    assert _run("isAppleTarget({gpuName:'Apple M3 Max'})") is True


def test_hwfit_filter_does_not_key_on_unified_memory_flag():
    src = _HWFIT.read_text(encoding="utf-8")
    start = src.index("function _applyEngineFilter")
    body = src[start:start + 2500]
    assert "unified_memory" not in body
