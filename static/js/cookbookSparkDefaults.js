// ============================================
// COOKBOOK: NVIDIA UNIFIED-MEMORY (DGX Spark / GB10) SERVE DEFAULTS
//
// The one place Spark-specific vLLM launch defaults live, so they can be
// flipped in a single edit. Pure functions, no DOM, so they are unit-testable
// under node (see tests/test_cookbook_spark_defaults_js.py).
//
// Background: a GB10 has no separate VRAM. The GPU shares one LPDDR pool with
// the CPU, and Linux page cache counts against what CUDA reports as free.
// vLLM refuses to start when free < gpu_memory_utilization * total, so a fixed
// 0.90 fails whenever anything else holds memory. We size the fraction from the
// live free memory instead.
// ============================================

const MIB_PER_GIB = 1024;

export const SPARK_DEFAULTS = Object.freeze({
  // Safety headroom kept free for the OS and other processes:
  // max(headroomMinGiB, headroomFraction * total).
  headroomMinGiB: 4,
  headroomFraction: 0.08,
  // Clamp for the computed --gpu-memory-utilization.
  utilMin: 0.10,
  utilMax: 0.95,
  // Quick-run (cookbook-hwfit.js) passes --enforce-eager today. Left as-is
  // until the CUDA-graph measurements land; flip here to change it.
  quickRunEnforceEager: true,
  // Default KV cache dtype for Spark launches. '' = vLLM default (no flag).
  kvCacheDtype: '',
  // Command that returns page cache to the free pool (needs root).
  dropCacheCommand: 'sync; echo 3 | sudo tee /proc/sys/vm/drop_caches',
});

/** True for an NVIDIA GPU that shares system memory (GB10 / DGX Spark). Apple
 *  and AMD unified-memory parts are deliberately excluded. */
export function isNvidiaUnifiedGpu(gpu, system = null) {
  const g = gpu || {};
  const name = String(g.name || system?.gpu_name || '').toLowerCase();
  const backend = String(g.backend || system?.backend || '').toLowerCase();
  if (backend === 'metal' || backend === 'mps' || backend === 'apple' || name.includes('apple')) return false;
  if (backend === 'rocm' || backend === 'vulkan') return false;
  const unified = !!(g.unified_memory ?? system?.unified_memory);
  return name.includes('gb10') || (unified && (backend === 'cuda' || name.includes('nvidia')));
}

/** Headroom in GiB for a pool of `totalGib`. */
export function sparkHeadroomGib(totalGib, defaults = SPARK_DEFAULTS) {
  return Math.max(defaults.headroomMinGiB, defaults.headroomFraction * totalGib);
}

/**
 * Compute --gpu-memory-utilization from live free memory.
 * Guarantees util * total <= free - headroom (util rounded DOWN to 2 decimals).
 * Returns { util, reason, headroomGib, freeGib, totalGib } or, when there is no
 * usable room, { util: null, insufficient: true, reason }.
 */
export function sparkGpuMemoryUtilization(totalMb, freeMb, defaults = SPARK_DEFAULTS) {
  const total = Number(totalMb) / MIB_PER_GIB;
  const free = Number(freeMb) / MIB_PER_GIB;
  if (!(total > 0) || !Number.isFinite(free) || free < 0) return null;
  const headroom = sparkHeadroomGib(total, defaults);
  const usable = free - headroom;
  const fmt = (n) => n.toFixed(1);
  const parts = `${fmt(free)}G free of ${fmt(total)}G, ${fmt(headroom)}G headroom`;
  if (usable <= 0) {
    return {
      util: null, insufficient: true, headroomGib: headroom, freeGib: free, totalGib: total,
      reason: `only ${parts}; free memory first (stop other GPU jobs or drop page cache)`,
    };
  }
  const raw = Math.floor((usable / total) * 100) / 100;
  const util = Math.min(defaults.utilMax, Math.max(defaults.utilMin, raw));
  if (util * total > usable + 1e-9) {
    // utilMin forced the value above what fits.
    return {
      util: null, insufficient: true, headroomGib: headroom, freeGib: free, totalGib: total,
      reason: `only ${parts}; not enough for the minimum ${defaults.utilMin}`,
    };
  }
  const capped = raw > defaults.utilMax ? ` (capped at ${defaults.utilMax})` : '';
  return {
    util, headroomGib: headroom, freeGib: free, totalGib: total,
    reason: `${util.toFixed(2)} fits live memory (${parts})${capped}`,
  };
}
