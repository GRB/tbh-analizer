// Read-only browser acceptance for Data quality. Synthetic API; no connection to the game server.
const { chromium } = require('../build/portal-validation/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '..');
const requests = [], writes = [], errors = [];
const windows = Array.from({ length: 61 }, (_, i) => ({
  save_from: i + 1, save_to: i + 2, start_utc: '2026-10-03T10:00:00Z', end_utc: '2026-10-03T10:01:00Z',
  seconds: 60, stage: 1109, stage_label: 'Stage 1-9', stage_rate_eligible: true, flags: [],
  coverage: { covered_s: 60, sample_count: 3, session_id: 1, start_offset_s: 0, end_offset_s: 0, max_gap_s: 30, continuous: true },
  gold: { status: i === 0 ? 'discrepancy' : i === 1 ? 'boundary_uncertain' : i === 2 ? 'missing' : 'matched',
    residual: i === 0 ? 50 : i === 2 ? null : 0, save_earned: 200, sample_rises: i === 2 ? null : 200,
    sample_falls: 0, save_net: 200, sample_net: 200, implied_spend: 0, start_balance_residual: 0,
    end_balance_residual: 0, sources: { gold_monster: 200, gold_alchemy: null, gold_offline: 0 }, note: 'Sampled rises are not gross income.',
    rune_spending: {catalog_gold_cost: 50, spend_minus_catalog_cost: null, status: 'incomplete', unpriced: [],
      note: 'Catalog costs are not receipts.'} },
  xp: [{ hero_key: 401, name: 'Hero <img src=x onerror=alert(1)>', status: 'matched', residual: 0,
    save_kind: 'same_level', runtime_kinds: { same_level: 2 }, level_before: 20, level_after: 20,
    save_gain: 60, sample_gain: 60, start_xp_residual: 0, end_xp_residual: 0 }],
  runs: { status: 'counts_agree', residual: {}, run_ids: [1], partial_starts: 0, first_clear_adjustments: 0, note: 'Markers only.',
    interval_reconciliation: { status: 'unique_correspondence', reasons: [], evidence: [], assignments: [], note: 'Conditional on recorded evidence.' },
    boundary_markers: { start: [], end: [{ run_id: 99, stage_key: 1109, kind: 'clear_marker',
      before_utc: '2026-10-03T10:00:59Z', observed_utc: '2026-10-03T10:01:01Z', interval_s: 2 }] } },
}));
function report(url) {
  const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 50);
  const status = url.searchParams.get('status') || 'all';
  const filtered = windows.filter(w => status === 'all' || (status === 'attention' ? w.gold.status !== 'matched' : w.gold.status === status));
  return { hours: 24, catalog_build: 'test',
    farming: {summary: {episodes: 1, seconds: 60, with_failures: 1, mixed_stage: 1}, note: 'Failures remain included.',
      excluded_windows: {'loadout unknown': 2}, episodes: [{end_utc: windows[0].end_utc, stages: [1108, 1109],
        seconds: 60, clears: 0, fails: 1, gross_gold_h: 12000, net_gold_h: -3000}]},
    xp_validation: { observed_transitions: 1, statuses: { consistent: 1 }, native_identity_matched_transitions: 1,
      native_rule: {status: 'confirmed_for_catalog_build', build_id: 'test', rule: 'Keep the remainder.',
        source: 'local evidence', game_assembly_sha256: 'test'}, note: 'Not independent reward measurement.', events: [] },
    summary: { windows: 61, seconds: 3660, covered_s: 3660, coverage_fraction: 1,
      gold_statuses: { matched: 58, discrepancy: 1, boundary_uncertain: 1, missing: 1 }, xp_statuses: { matched: 61 },
      run_statuses: { counts_agree: 61 }, gold_comparable_seconds: 3540,
      gold_totals_on_comparable_windows: { save_earned: 11800, sample_rises: 11750, residual: 50, absolute_residual: 50 },
      level_up_windows: 0, gold_differences_with_balance_drops: 0, exclusions: {} },
    scope: { first_save: windows[0].start_utc, last_save: windows[0].end_utc,
      last_save_age_s: 10, last_sample_age_s: 9, duplicates_ignored: 2, incompatible_samples: 0 },
    method: { note: 'Nearest timestamp endpoints; no interpolation.', alignment_seconds: 2, max_stored_gap_seconds: 35 },
    pagination: { offset, limit, filtered_total: filtered.length, has_more: offset + limit < filtered.length },
    windows: filtered.slice(offset, offset + limit) };
}
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (req.method !== 'GET') { writes.push(req.url); res.writeHead(405); return res.end(); }
  let body;
  if (url.pathname === '/api/quality') { requests.push(url.searchParams); body = report(url); }
  else if (url.pathname === '/api/extensions') body = { scripts: [], styles: [] };
  else if (url.pathname === '/api/status') body = { collector: { runtime: 'attached', save: 'ok' }, identity: { build_id: 'test' }, catalog: { rows: 10, tables: 1 }, mode: 'read-only' };
  if (body) { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(body)); }
  const files = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'],
    '/styles.css': ['styles.css', 'text/css'] };
  const file = files[url.pathname];
  if (!file) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'Content-Type': file[1] }); res.end(fs.readFileSync(path.join(root, 'web', file[0])));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/#quality`);
    await page.getByRole('heading', { name: 'Data quality', exact: true }).waitFor();
    assert(await page.getByRole('heading', {name: 'Farming including failures', exact: true}).isVisible());
    await page.getByText('XP level-up evidence: 1 observed transitions', { exact: true }).click();
    assert(await page.getByText('Not independent reward measurement.', { exact: true }).isVisible());
    await page.getByText('1–50 of 61 windows', { exact: true }).waitFor();
    await page.getByText('Inspect window', { exact: true }).first().click();
    assert(await page.getByText(/Catalog costs are not receipts/).first().isVisible());
    assert(await page.getByText('end boundary timing evidence: run 99', { exact: false }).first().isVisible());
    assert(await page.getByText('Interval reconciliation: Unique correspondence.', { exact: false }).first().isVisible());
    await page.getByText('Alchemy', { exact: true }).count();
    assert.equal(await page.locator('#view img').count(), 0, 'Names must be escaped');
    assert(await page.getByText('Hero <img src=x onerror=alert(1)>', { exact: true }).count());
    await page.getByRole('button', { name: 'Next', exact: true }).click();
    await page.getByText('51–61 of 61 windows', { exact: true }).waitFor();
    await page.getByLabel('Evidence', { exact: true }).selectOption('missing');
    await page.getByText('1–1 of 1 windows', { exact: true }).waitFor();
    assert.equal(requests.at(-1).get('offset'), '0');
    await page.getByText('Inspect window', { exact: true }).click();
    await page.getByText('sampled rises —', { exact: false }).waitFor();
    assert(await page.getByText('Summary figures cover all 61 windows', { exact: false }).count());
    await page.getByLabel('History', { exact: true }).selectOption('6');
    await page.waitForFunction(() => document.getElementById('qualityHours')?.value === '6' && document.querySelector('#view h2'));
    assert.equal(requests.at(-1).get('hours'), '6');
    await page.getByLabel('Evidence', { exact: true }).selectOption('discrepancy');
    await page.getByText('1–1 of 1 windows', { exact: true }).waitFor();
    fs.mkdirSync(path.join(root, 'build', 'reports'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'build', 'reports', 'quality-browser.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.getByRole('heading', { name: 'Data quality', exact: true }).isVisible());
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, [], 'The view must not send any write request');
    console.log('Data quality browser acceptance passed: navigation, paging, filters, nulls, escaping, mobile, no writes.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
