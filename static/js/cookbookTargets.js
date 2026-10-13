// Pure helpers that classify the hardware a Cookbook launch targets.
// No DOM; unit-tested under node (tests/test_cookbook_targets_js.py).

/**
 * True only for a real Apple target (macOS / Metal / MPS). `unified_memory` is
 * NOT an Apple signal: NVIDIA GB10 (DGX Spark) and AMD APUs also share system
 * memory, and must not be narrowed to MLX-only models.
 */
export function isAppleTarget({ platform = '', backend = '', gpuName = '' } = {}) {
  const p = String(platform || '').toLowerCase();
  const b = String(backend || '').toLowerCase();
  const g = String(gpuName || '').toLowerCase();
  return p === 'darwin' || p === 'macos' || p.includes('mac')
    || b === 'metal' || b === 'mps' || b === 'apple'
    || g.includes('apple');
}
