"""/api/cookbook/gpus on an NVIDIA unified-memory device (GB10 / DGX Spark).

nvidia-smi reports memory.free/total/used as [N/A] there, which used to drop the
only GPU row, so the Cookbook had no live free-memory figure to size vLLM's
--gpu-memory-utilization from. The probe now falls back to /proc/meminfo.
"""
import asyncio

import pytest

from routes import cookbook_routes

SMI_GPU_ROW = "0, NVIDIA GB10, [N/A], [N/A], [N/A], 0, GPU-abc\n"
MEMINFO = (
    "MemTotal:       124000000 kB\n"
    "MemFree:         30000000 kB\n"
    "Buffers:           100000 kB\n"
    "Cached:          40000000 kB\n"
)


class _FakeProc:
    def __init__(self, out: str, rc: int = 0):
        self._out = out.encode()
        self.returncode = rc

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


def _fake_exec_factory(gpu_row):
    async def fake_exec(*args, **kwargs):
        joined = " ".join(args)
        if "--query-gpu" in joined:
            return _FakeProc(gpu_row)
        if "--query-compute-apps" in joined:
            return _FakeProc("1234, GPU-abc, python, [N/A]\n")
        return _FakeProc("", 1)

    async def fake_shell(cmd, *args, **kwargs):
        return _FakeProc(MEMINFO if "/proc/meminfo" in cmd else "")

    return fake_exec, fake_shell


def _endpoint():
    router = cookbook_routes.setup_cookbook_routes()
    return next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/cookbook/gpus")


def test_gb10_probe_reports_live_free_memory_from_meminfo(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    fake_exec, fake_shell = _fake_exec_factory(SMI_GPU_ROW)
    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", fake_shell)

    data = asyncio.run(_endpoint()(object(), host=None, ssh_port=None))

    assert data["ok"] is True and data["backend"] == "cuda"
    assert len(data["gpus"]) == 1
    gpu = data["gpus"][0]
    assert gpu["name"] == "NVIDIA GB10"
    assert gpu["unified_memory"] is True
    assert gpu["total_mb"] == 124000000 // 1024
    # MemFree, NOT MemAvailable: page cache is not free to CUDA on GB10.
    assert gpu["free_mb"] == 30000000 // 1024
    assert gpu["reclaimable_cache_mb"] == (40000000 + 100000) // 1024
    assert gpu["used_mb"] == gpu["total_mb"] - gpu["free_mb"]
    # [N/A] per-process memory must not drop the process rows.
    assert gpu["processes"] and gpu["processes"][0]["used_mb"] == 0


def test_discrete_gpu_probe_is_unchanged(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    fake_exec, fake_shell = _fake_exec_factory("0, RTX 4090, 20000, 24000, 4000, 5, GPU-xyz\n")
    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", fake_shell)

    data = asyncio.run(_endpoint()(object(), host=None, ssh_port=None))
    gpu = data["gpus"][0]
    assert (gpu["free_mb"], gpu["total_mb"], gpu["used_mb"]) == (20000, 24000, 4000)
    assert "unified_memory" not in gpu
