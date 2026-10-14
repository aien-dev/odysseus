import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { cliVendorState, renderCliSubscriptionRows } from '../static/js/cliSubscriptions.js';

const base = { vendor: 'claude-code', label: 'Claude Code', installed: true, logged_in: true, connected: false, blocked_reason: '' };

test('rows offer Connect only when installed and signed in', () => {
  assert.equal(cliVendorState(base).action, 'connect');
  assert.equal(cliVendorState({ ...base, installed: false, logged_in: false }).action, null);
  assert.match(cliVendorState({ ...base, logged_in: false }).text, /not signed in/);
  assert.equal(cliVendorState({ ...base, connected: true }).action, 'disconnect');
});

test('blocked vendors never offer Connect but can still be removed', () => {
  const blocked = { ...base, vendor: 'antigravity', blocked_reason: 'no tool-off switch' };
  assert.equal(cliVendorState(blocked).action, null);
  assert.match(cliVendorState(blocked).text, /^Not available/);
  assert.equal(cliVendorState(blocked).detail, 'no tool-off switch');
  assert.match(renderCliSubscriptionRows([blocked]), /title="no tool-off switch"/);
  assert.equal(cliVendorState({ ...blocked, connected: true }).action, 'disconnect');
});

test('rendered rows escape vendor text', () => {
  const html = renderCliSubscriptionRows([{ ...base, label: '<b>x</b>', plan: 'max' }]);
  assert.ok(!html.includes('<b>x</b>'));
  assert.match(html, /data-cli-sub-action="connect"/);
  assert.match(html, /max plan/);
});

test('page treats CLI bridge endpoints as subscription (no per-token cost shown)', async () => {
  // chatRenderer.js needs a DOM to load, so evaluate just this function from its source.
  const src = await readFile(new URL('../static/js/chatRenderer.js', import.meta.url), 'utf8');
  const body = src.slice(src.indexOf('export function isSubscriptionEndpoint('), src.indexOf('\nfunction _currentEndpointUrl')).replace('export ', '');
  const isSubscriptionEndpoint = new Function(body + '\nreturn isSubscriptionEndpoint;')();
  assert.equal(isSubscriptionEndpoint('http://cli-bridge.invalid/grok-cli/chat/completions'), true);
  assert.equal(isSubscriptionEndpoint('https://chatgpt.com/backend-api/codex'), true);
  assert.equal(isSubscriptionEndpoint('https://api.x.ai/v1'), false);
});
