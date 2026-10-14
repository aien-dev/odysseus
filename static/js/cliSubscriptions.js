// static/js/cliSubscriptions.js — Settings card for local vendor CLI
// subscription bridges (Claude Code, Codex, Grok, Antigravity).
//
// The backend (routes/cli_subscription_routes.py) is admin only and never
// reads the CLIs' token files: login state comes from each CLI's own status
// command. This module only renders that status and calls connect/disconnect.

const STATUS_URL = '/api/cli-subscription/status';

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** Plain-language state for one vendor row from the /status payload. */
export function cliVendorState(v) {
  if (v.blocked_reason) return { tone: 'blocked', text: "Not available: this program's own tools can't be switched off, so Odysseus won't connect it.", detail: v.blocked_reason, action: v.connected ? 'disconnect' : null };
  if (v.connected) return { tone: 'ok', text: 'Connected' + (v.plan ? ` (${v.plan} plan)` : ''), action: 'disconnect' };
  if (!v.installed) return { tone: 'off', text: 'Program not installed on this computer.', action: null };
  if (!v.logged_in) return { tone: 'warn', text: 'Installed but not signed in. Sign in with the program itself, then press Check again.', action: null };
  return { tone: 'ready', text: 'Installed and signed in' + (v.plan ? ` (${v.plan} plan)` : '') + '.', action: 'connect' };
}

const _TONE_COLOR = {
  ok: 'var(--green)',
  ready: 'var(--fg)',
  warn: 'var(--warn)',
  off: 'var(--border)',
  blocked: 'var(--red)',
};

export function renderCliSubscriptionRows(vendors) {
  if (!vendors || !vendors.length) return '<div class="admin-empty">No subscription programs known.</div>';
  return vendors.map((v) => {
    const st = cliVendorState(v);
    const dot = `<span style="width:8px;height:8px;border-radius:50%;background:${_TONE_COLOR[st.tone]};flex-shrink:0;"></span>`;
    const tag = v.experimental ? ' <span style="opacity:0.55;font-size:10px;">experimental</span>' : '';
    const btn = st.action
      ? `<button class="admin-btn-sm" data-cli-sub-action="${st.action}" data-cli-sub-vendor="${_esc(v.vendor)}" style="font-size:11px;flex-shrink:0;">${st.action === 'connect' ? 'Connect' : 'Disconnect'}</button>`
      : '';
    return `<div class="adm-cli-sub-row" data-cli-sub-row="${_esc(v.vendor)}" style="display:flex;align-items:center;gap:8px;padding:7px 2px;border-bottom:1px solid var(--border);">
      ${dot}
      <div style="flex:1;min-width:0;">
        <div style="font-size:12.5px;font-weight:600;">${_esc(v.label)}${tag}</div>
        <div style="font-size:11px;opacity:0.7;" data-cli-sub-text${st.detail ? ` title="${_esc(st.detail)}"` : ''}>${_esc(st.text)}</div>
      </div>
      ${btn}
    </div>`;
  }).join('');
}

async function _json(res) {
  try { return await res.json(); } catch { return {}; }
}

/**
 * Load status into the card. Hides the card for non-admins (403).
 * `onChange` runs after a successful connect/disconnect so the model lists refresh.
 */
export async function loadCliSubscriptions({ onChange, keepMsg = false } = {}) {
  const card = document.getElementById('adm-cliSubCard');
  const list = document.getElementById('adm-cliSubList');
  const msg = document.getElementById('adm-cliSubMsg');
  if (!card || !list) return;
  list.innerHTML = '<div class="admin-empty">Checking which programs are installed and signed in…</div>';
  if (msg && !keepMsg) msg.textContent = '';
  let data;
  try {
    const res = await fetch(STATUS_URL, { credentials: 'same-origin' });
    if (res.status === 401 || res.status === 403) { card.style.display = 'none'; return; }
    if (!res.ok) throw new Error((await _json(res)).detail || `HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    list.innerHTML = `<div class="admin-empty">Could not check: ${_esc(e.message || e)}</div>`;
    return;
  }
  card.style.display = '';
  list.innerHTML = renderCliSubscriptionRows(data.vendors || []);
  list.querySelectorAll('[data-cli-sub-action]').forEach((btn) => {
    btn.addEventListener('click', () => _act(btn, { onChange }));
  });
}

async function _act(btn, { onChange }) {
  const vendor = btn.dataset.cliSubVendor;
  const action = btn.dataset.cliSubAction;
  const msg = document.getElementById('adm-cliSubMsg');
  btn.disabled = true;
  btn.textContent = action === 'connect' ? 'Connecting…' : 'Disconnecting…';
  try {
    const url = action === 'connect'
      ? `/api/cli-subscription/${encodeURIComponent(vendor)}/connect`
      : `/api/cli-subscription/${encodeURIComponent(vendor)}`;
    const res = await fetch(url, { method: action === 'connect' ? 'POST' : 'DELETE', credentials: 'same-origin' });
    const body = await _json(res);
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    if (msg) msg.textContent = action === 'connect'
      ? `Connected. ${(body.models || []).length} model(s) added to your model list.`
      : 'Disconnected.';
    if (onChange) { try { onChange(); } catch {} }
  } catch (e) {
    if (msg) msg.textContent = (action === 'connect' ? 'Connect failed: ' : 'Disconnect failed: ') + (e.message || e);
  }
  await loadCliSubscriptions({ onChange, keepMsg: true });
}
