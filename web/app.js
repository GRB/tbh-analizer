'use strict';
// TBH Analizer UI. Data comes only from the local API; IDs are strings end to end.

const TABS = [
  ['now', 'Now'], ['combat', 'Combat'], ['suggestions', 'Suggestions'], ['runs', 'Runs'], ['stages', 'Stages'], ['actboss', 'Act boss'], ['heroes', 'Heroes'],
  ['inventory', 'Inventory'], ['chests', 'Chests'], ['cube', 'Cube'], ['runes', 'Runes'],
  ['economy', 'Economy'], ['quality', 'Data quality'], ['catalog', 'Catalog'], ['system', 'System'],
];
const state = { tab: 'now', defaultTab: 'now', timer: null, filters: {}, revision: 0 };
// Old tab names in saved links.
const LEGACY_TABS = { agora: 'now', herois: 'heroes', inventario: 'inventory', baus: 'chests', runas: 'runes', economia: 'economy', catalogo: 'catalog', sistema: 'system' };
// Extensions (loaded from /api/extensions before the tabs are built) add tabs and views, and may set
// `afterRender` (called after each render) and `onVisible` (called when the page becomes visible).
const hooks = { afterRender: [], onVisible: [] };
// Tabs whose data changes while the game runs refresh in place (milliseconds).
const LIVE = { now: 1000, combat: 1000, suggestions: 5000 };
const NF = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const NF1 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });

// ---------- helpers -----------------------------------------------------------
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const n = (v, digits = 0) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : (digits ? NF1 : NF).format(v);
const pct = (v) => (v === null || v === undefined) ? '—' : NF1.format(v * 100) + '%';
const dur = (s) => {
  if (s === null || s === undefined) return '—';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
};
const time = (iso) => iso ? new Date(iso).toLocaleString('en-US', { hour12: false }) : '—';
const ago = (sec) => sec === null || sec === undefined ? '—' : sec < 90 ? `${Math.round(sec)} s ago` : `${dur(sec)} ago`;
const pill = (text, kind = '') => `<span class="pill ${kind}">${esc(text)}</span>`;
const kpi = (label, value, sub = '') => `<div class="card kpi"><div class="label">${esc(label)}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
const progress = (p) => `<div class="progress"><span style="width:${Math.max(0, Math.min(1, p || 0)) * 100}%"></span></div>`;
const spread = (rse) => rse === null || rse === undefined ? '' : ` <span class="muted" title="Relative standard error; not a guaranteed accuracy interval">RSE ${NF1.format(rse * 100)}%</span>`;
const confPill = (c) => pill(c || 'insufficient', c === 'high' ? 'good' : c === 'medium' ? '' : 'warn');
const bar = (v, max) => `<span class="bar" style="width:${max ? Math.max(2, 80 * v / max) : 0}px"></span>`;

// Timers render with the time at render and then tick every second in the browser (see tickClocks),
// so durations and ages move smoothly instead of jumping with each data refresh.
const since = (iso) => iso ? `<span data-since="${esc(iso)}">${dur((Date.now() - Date.parse(iso)) / 1000)}</span>` : '—';
const agoAt = (iso) => iso ? `<span data-ago="${esc(iso)}">${ago((Date.now() - Date.parse(iso)) / 1000)}</span>` : '—';
function tickClocks() {
  if (document.hidden) return;
  const now = Date.now();
  const set = (el, t) => { if (el.textContent === t) return; if (el.firstChild) el.firstChild.nodeValue = t; else el.textContent = t; };
  document.querySelectorAll('[data-since]').forEach((el) => set(el, dur((now - Date.parse(el.dataset.since)) / 1000)));
  document.querySelectorAll('[data-ago]').forEach((el) => set(el, ago((now - Date.parse(el.dataset.ago)) / 1000)));
}

// Patch `target` to match `html`, touching only what changed. Elements stay in place, so scroll,
// focus, open <details>, hover and text selection survive a refresh. Children with data-key (or an id)
// are matched by key, so a list that gains an item on top moves nodes instead of rewriting them all.
const keyOf = (n) => n.nodeType === 1 ? (n.dataset.key || n.id || n.dataset.detail || '') : '';
function morph(target, html) {
  const next = document.createElement(target.tagName);
  next.innerHTML = html;
  morphChildren(target, next);
}
function morphChildren(a, b) {
  const keyed = new Map();
  for (const n of a.childNodes) { const k = keyOf(n); if (k) keyed.set(k, n); }
  let i = 0;
  for (const bn of [...b.childNodes]) {
    const k = keyOf(bn);
    let an = a.childNodes[i];
    if (k && keyed.has(k)) {
      const match = keyed.get(k);
      keyed.delete(k);
      if (match !== an) { a.insertBefore(match, an || null); an = match; }
    }
    if (!an) a.appendChild(bn);
    else if (an.nodeType !== bn.nodeType || an.nodeName !== bn.nodeName || keyOf(an) !== k) a.insertBefore(bn, an);
    else patchNode(an, bn);
    i++;
  }
  while (a.childNodes.length > i) a.removeChild(a.lastChild);
}
function patchNode(a, b) {
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  const userOwned = (name) => a.nodeName === 'DETAILS' && name === 'open';   // opened/closed by the user
  for (const { name, value } of [...b.attributes]) if (!userOwned(name) && a.getAttribute(name) !== value) a.setAttribute(name, value);
  for (const { name } of [...a.attributes]) if (!userOwned(name) && !b.hasAttribute(name)) a.removeAttribute(name);
  morphChildren(a, b);
  if (a === document.activeElement) return;   // never overwrite a control the user is using
  if (a.nodeName === 'INPUT' && (a.type === 'checkbox' || a.type === 'radio')) a.checked = b.hasAttribute('checked');
  else if (a.nodeName === 'INPUT' && a.value !== (b.getAttribute('value') ?? '')) a.value = b.getAttribute('value') ?? '';
  else if (a.nodeName === 'SELECT') { const sel = b.querySelector('option[selected]'); a.value = sel ? sel.value : (a.options[0]?.value ?? ''); }
}

function table(columns, rows, empty = 'No data.') {
  if (!rows.length) return `<div class="card empty">${esc(empty)}</div>`;
  const cls = (c) => [c.num ? 'num' : '', c.wrap ? 'wrap' : ''].join(' ').trim();
  const head = columns.map((c) => `<th class="${cls(c)}">${esc(c.label)}</th>`).join('');
  const body = rows.map((r) => '<tr>' + columns.map((c) => `<td class="${cls(c)}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join('') + '</tr>').join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
async function api(path, options) {
  const res = await fetch('/api/' + path, options);
  const body = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

function outcomePill(run) {
  const confirmed = (run.outcome_source || '').startsWith('save');
  const label = { clear: 'clear', fail: 'failure', unknown: 'unknown' }[run.outcome] || run.outcome;
  const kind = run.outcome === 'clear' ? 'good' : run.outcome === 'fail' ? 'bad' : '';
  return pill(label + (confirmed ? ' ✓' : run.outcome_source === 'runtime-heuristic' ? ' ?' : ''), kind);
}
// Estimated base-attack damage per hero (attacks counted by the game x expected hit); skills are not in it.
const attackShares = (rows) => (rows || []).map((h) => `${esc(h.name)}: ${h.share === null ? '<span class="muted">' + n(h.attacks) + ' attacks</span>' : pct(h.share)}`).join('<br>');
const heroXp = (xp) => Object.values(xp || {}).map((h) => `${esc(h.name)}: ${h.complete ? n(h.gain) : '<span class="muted">partial</span>'}${h.level_ups ? ' ⬆' + h.level_ups : ''}`).join('<br>');

// ---------- views ---------------------------------------------------------------
const views = {};

views.now = async () => {
  const d = await api('live');
  const rt = d.runtime, run = d.open_run, save = d.save, rates = d.rates;
  let html = '<h2>Now</h2>';
  if (!rt) html += `<div class="warn-box">No live reading. See the System tab for the reason.</div>`;
  html += '<div class="grid">';
  if (rt) {
    html += kpi('Stage', esc(rt.stage_label || rt.stage_key || '—'), `wave ${n(rt.wave)} / ${n(rt.wave_amount)} · ${esc(rt.state || '—')}`);
    html += kpi('Gold (live)', n(rt.gold), `read ${agoAt(rt.utc)} · ${pill(rt.quality, rt.quality === 'ok' ? 'good' : 'warn')}`);
  }
  if (save) html += kpi('Latest save', n(save.gold) + ' gold', `${time(save.last_saved_utc)} · ${agoAt(save.last_saved_utc)}`);
  const lastBoss = d.act_boss && d.act_boss.last;
  if (save && save.act_boss_stage) html += kpi('Act boss (from save)', esc(save.act_boss_label), `latest save written during the fight · ${agoAt(save.last_saved_utc)}`);
  else if (lastBoss) html += kpi('Last act boss', esc(lastBoss.stage_label), `${outcomePill({ outcome: lastBoss.fails ? 'fail' : 'clear', outcome_source: 'save' })} by ${time(lastBoss.by_utc)} · ${agoAt(lastBoss.by_utc)} · ${n(d.act_boss.count_24h)} in 24 h`);
  if (rates) {
    html += kpi('Gold earned/h (10 min, estimate)', n(rates.gold_income_per_h_est), `window ${n(rates.minutes, 1)} min · spent ${n(rates.gold_spend_window)}`);
  }
  html += '</div>';
  if (run) {
    html += `<h3>Current run ${run.partial_start ? pill('unobserved start', 'warn') : ''}</h3><div class="grid">`;
    html += kpi('Duration', since(run.started_utc), `maximum wave ${n(run.max_wave)} / ${n(run.wave_amount)}`);
    html += kpi('Gold (estimate)', n(run.gold_gain_est), `spent ${n(run.gold_spend_est)}`);
    html += `<div class="card kpi"><div class="label">XP this run</div><div class="sub">${heroXp(run.xp)}</div></div></div>`;
  }
  if (rt && rt.heroes.length) {
    html += '<h3>Active party</h3><div class="grid">';
    for (const h of rt.heroes) {
      const rate = rates && rates.xp_per_h[String(h.hero_key)];
      html += `<div class="card kpi"><div class="label">${esc(h.name)}</div><div class="value">level ${n(h.level)}</div>
        <div class="sub">${n(h.xp)} / ${n(h.xp_for_next)} XP · ${pct(h.progress)}</div>${progress(h.progress)}
        <div class="sub">XP/h (10 min): ${rate ? n(rate.value) + (rate.complete ? '' : ' (partial)') : '—'}
        ${rate && rate.value && h.xp_for_next ? ' · next level in ~' + dur((h.xp_for_next - h.xp) / rate.value * 3600) : ''}</div></div>`;
    }
    html += '</div>';
  }
  if (save && save.pending) {
    const p = save.pending, active = Object.entries(p).filter(([, v]) => v);
    html += `<h3>Pending save operations</h3><div class="note">${active.length ? active.map(([k, v]) => pill(`${k}: ${v}`, 'warn')).join(' ') : 'No pending operations recorded in the latest save.'}</div>`;
  }
  html += `<p class="note">Live gold/h sums balance increases between 1 s samples (estimate). Exact values per window are in Economy/Stages (game counters).</p>`;
  return html;
};

// ---------- combat ---------------------------------------------------------------
// Values are read live from game memory.
const STAT_FMT = { AttackSpeed: 3, CriticalChance: 'pct', CriticalDamage: 'pct', CooldownReduction: 'pct', IncreaseExpAmount: 3, DamageAbsorption: 2, MovementSpeed: 2 };
const statVal = (stat, v) => {
  if (v === null || v === undefined) return '—';
  const f = STAT_FMT[stat];
  return f === 'pct' ? pct(v) : f ? v.toFixed(f) : n(v, 1);
};
const trim = (s) => s.includes('.') ? s.replace(/\.?0+$/, '') : s;
const mult = (v) => '×' + trim(v.toFixed(v >= 10 ? 1 : 3));
const signed = (v, digits = 2) => (v > 0 ? '+' : '') + trim(v.toFixed(digits));
function modText(m) {
  if (m.mode === 'MULTIPLICATIVE') return `${m.name} ${mult(1 + m.value)}`;
  if (m.mode === 'ADDITIVE') return `${m.name} ${signed(m.value * 100, 1)}%`;
  return `${m.name} ${signed(m.value)}`;
}
function buffPill(b) {
  if (b.quality) return pill(`${b.name} (${b.quality})`, 'warn');
  const e = b.expiry || {};
  const ends = e.kind === 'attack_count' && e.attacks_left !== undefined ? ` · ends after ${n(e.attacks_left)} more attacks`
    : e.kind === 'duration' ? ` · lasts ${n(e.configured_s, 1)} s` : '';
  const mods = (b.modifiers || []).map(modText).join(', ');
  const title = `${mods}${b.caster ? ' · cast by ' + b.caster : ''}${ends}`;
  return `<span class="pill ${b.kind === 'environment' ? 'warn' : 'accent'}" title="${esc(title)}">${esc(b.name)}${mods ? ': ' + esc(mods) : ''}${esc(ends)}</span>`;
}
function hpBar(f) {
  const kind = f === null || f === undefined ? '' : f < 0.35 ? 'bad' : f < 0.7 ? 'warn' : 'good';
  return `<div class="progress hp ${kind}"><span style="width:${Math.max(0, Math.min(1, f || 0)) * 100}%"></span></div>`;
}

views.combat = async () => {
  const d = await api('combat');
  let html = '<h2>Combat</h2>';
  if (!d.available) return html + `<div class="warn-box">No combat reading: ${esc(d.reason)}</div>`;
  const heroes = d.heroes || [], en = d.enemies;
  html += '<div class="grid">';
  html += kpi('Stage', esc(d.stage_label || d.stage_key || '—'), `stage level ${n(d.stage_level)}`);
  html += en ? kpi('Enemies on the field', n(en.count), `${n(en.hp)} / ${n(en.max_hp)} HP left${en.hp_unknown ? ` · ${n(en.hp_unknown)} unreadable` : ''}`)
    : kpi('Enemies on the field', '—', 'enemy list unreadable');
  html += kpi('Reading', pill(d.quality, d.quality === 'ok' ? 'good' : 'warn'), `${agoAt(d.utc)} · ${n(d.read_ms, 1)} ms`);
  html += '</div>';
  if (d.problems.length) html += `<div class="warn-box">${d.problems.map(esc).join('<br>')}</div>`;
  if (!heroes.length) html += '<div class="warn-box">Party not readable in this frame.</div>';

  html += '<h3>Party</h3><div class="grid">';
  for (const h of heroes) {
    const r = h.run;
    html += `<div class="card kpi" data-key="hero-${esc(h.hero_key)}"><div class="label">${esc(h.class || '')} ${pill(h.state || '?', h.state === 'DIE' ? 'bad' : '')} ${h.attacking ? pill('attacking', 'accent') : ''}</div>
      <div class="value">${esc(h.name)}</div>
      <div class="sub">HP ${n(h.hp)} / ${n(h.max_hp)} (${pct(h.hp_fraction)})</div>${hpBar(h.hp_fraction)}
      ${h.skills ? `<div class="sub">Skills: ${h.skills.map((k) => `${esc(k.name)} <span class="muted">(${esc(k.trigger)})</span>`).join(', ')}</div>` : ''}
      <div class="tags">${h.buffs === null ? pill('buffs unreadable', 'warn') : h.buffs.length ? h.buffs.map(buffPill).join(' ') : '<span class="muted">no buffs</span>'}</div>
      ${r ? `<div class="sub">This run: lowest HP ${pct(r.min_hp_fraction)} · ${r.deaths ? pill(r.deaths + (r.deaths > 1 ? ' deaths' : ' death'), 'bad') : '0 deaths seen'} · ${n(r.samples)} readings
        ${r.buff_presence.length ? '<br>Buff seen in ' + r.buff_presence.map((b) => `${esc(b.name)} ${pct(b.fraction)}`).join(' · ') + ' of readings' : ''}</div>` : ''}
      </div>`;
  }
  for (const x of d.dead || []) {
    html += `<div class="card kpi" data-key="dead-${esc(x.hero_key)}"><div class="label">${pill('dead', 'bad')}</div><div class="value">${esc(x.name)}</div>
      <div class="sub">Back in ${x.resurrection_s === null ? '—' : n(x.resurrection_s) + ' s'} (the game's resurrection timer)</div></div>`;
  }
  html += '</div>';

  const shares = (d.run || {}).attack_damage || [];
  if (shares.length) {
    html += '<h3>Damage by hero (this run, estimate)</h3>' + table([
      { label: 'Hero', render: (r) => esc(r.name) },
      { label: 'Base attacks', num: true, render: (r) => n(r.attacks) },
      { label: 'Base-attack damage (est.)', num: true, render: (r) => n(r.damage_est) },
      { label: 'Share', render: (r) => r.share === null ? '<span class="muted">stats unreadable in some readings</span>'
        : `<div class="progress"><span style="width:${r.share * 100}%"></span></div> ${pct(r.share)}` },
    ], shares);
    html += `<p class="note">The game does not record damage per hero. This counts the base attacks the game counts for each hero and values each one at
      Attack Damage × average crit (the hero's stats at that moment, buffs included). Skill damage, multi-strike, area hits, enemy armor/resistances and overkill are not in it,
      so a hero who deals most damage with skills is under-ranked.</p>`;
  }

  const statNames = [];
  for (const h of heroes) for (const s of h.key_stats || []) if (!statNames.some((x) => x.stat === s.stat)) statNames.push(s);
  if (statNames.length) {
    html += '<h3>Key stats</h3>' + table([{ label: 'Stat', render: (r) => esc(r.name) },
      ...heroes.map((h) => ({ label: esc(h.name), num: true, render: (r) => {
        const s = (h.key_stats || []).find((x) => x.stat === r.stat);
        if (!s) return '—';
        const changed = s.base !== null && Math.abs(s.final - s.base) > 1e-6 * Math.max(1, Math.abs(s.base));
        return `${statVal(r.stat, s.final)}${changed ? ` <span class="muted">(before buffs ${statVal(r.stat, s.base)})</span>` : ''}`;
      } }))], statNames);
    html += '<p class="note">Values the game uses now, buffs and stage environment included. "Before buffs" is gear, attributes, passives and account bonuses only.</p>';
  }

  if (heroes.some((h) => h.incoming && h.incoming.length)) {
    const enemies = [];
    for (const h of heroes) for (const r of h.incoming || []) if (!enemies.some((e) => e.monster_key === r.monster_key && e.boss === r.boss)) enemies.push(r);
    html += '<h3>Damage taken per hit</h3>' + table([
      { label: 'Enemy', render: (r) => `${esc(r.name)}${r.boss ? ' ' + pill('boss', 'warn') : ''}${r.element ? ' ' + pill(r.element.toLowerCase(), 'accent') : ''} <span class="muted">hits ${n(r.damage, 1)}</span>` },
      ...heroes.map((h) => ({ label: esc(h.name), num: true, render: (r) => {
        const x = (h.incoming || []).find((i) => i.monster_key === r.monster_key && i.boss === r.boss);
        if (!x) return '—';
        const cut = x.element ? (x.armor_reduction < 0 ? `resistance +${NF1.format(-x.armor_reduction * 100)}%` : `resistance −${NF1.format(x.armor_reduction * 100)}%`) : `armor −${NF1.format(x.armor_reduction * 100)}%`;
        return `${n(x.hit, 1)} HP <span class="muted">(${pct(x.hit_fraction)} · ${cut})</span>${x.cap_uncertain ? ' ' + pill('cap?', 'warn') : ''}`;
      } }))], enemies);
    html += `<p class="note">Game formula checked on real hits: enemy damage reduced by armor (depends on the hit and the stage level), then Damage Absorption subtracted as a flat amount.
      Elemental attacks skip armor and use the resistance factor below; the game data does not say which enemies use elements, so only those seen doing it are marked (Fire Elemental). Block and dodge are not applied. "cap?" marks reductions above 75%, where the hero cap (75% or 85%) is not confirmed.</p>`;
  }
  if (heroes.some((h) => h.resistances)) {
    const caps = [...new Set(heroes.flatMap((h) => Object.values(h.resistance_caps || {})))].join('/') || '—';
    html += '<h3>Resistances</h3>' + table([{ label: 'Element', render: (r) => esc(r.e) },
      ...heroes.map((h) => ({ label: esc(h.name), num: true, render: (r) => {
        const v = h.resistances && h.resistances[r.e];
        if (!v) return '—';
        return `${signed(v.resistance, 1)}% <span class="muted">· damage ${mult(v.damage_factor)}</span>`;
      } }))], ['Fire', 'Cold', 'Lightning', 'Chaos'].map((e) => ({ e })));
    html += `<p class="note">Fire, Cold and Lightning: own resistance + All Elemental, capped at ${esc(caps)}. Chaos uses only its own value.
      The damage factor is the resistance step of the game's damage formula, not the whole hit: armor and other reductions also apply.</p>`;
  }

  if (heroes.some((h) => h.origins)) {
    html += '<h3>Where each stat comes from</h3>';
    for (const h of heroes.filter((x) => x.origins)) {
      const rows = [];
      for (const [stat, o] of Object.entries(h.origins).sort((a, b) => a[1].name.localeCompare(b[1].name))) {
        o.sources.forEach((s, i) => rows.push({ stat, o, s, first: i === 0 }));
      }
      html += `<details data-detail="origins-${esc(h.hero_key)}"><summary>${esc(h.name)} · ${n(Object.keys(h.origins).length)} stats · modifiers read ${n(h.modifiers_age_s)} s ago</summary>` + table([
        { label: 'Stat', render: (r) => r.first ? `${esc(r.o.name)} ${r.o.matches ? '' : pill('incomplete', 'warn')}` : '' },
        { label: 'Source', render: (r) => esc(r.s.label) },
        { label: 'Flat', num: true, render: (r) => r.s.FLAT ? signed(r.s.FLAT) : '' },
        { label: 'Increase', num: true, render: (r) => r.s.ADDITIVE ? signed(r.s.ADDITIVE * 100, 1) + '%' : '' },
        { label: 'Multiplier', num: true, render: (r) => Math.abs(r.s.MULTIPLICATIVE - 1) > 1e-9 ? mult(r.s.MULTIPLICATIVE) : '' },
      ], rows) + '</details>';
    }
    html += '<p class="note">Stat = (sum of flat) × (1 + sum of increases) × product of multipliers, first for gear/attributes/account, then for buffs and environment. "incomplete" marks a stat whose breakdown does not reproduce the value the game uses.</p>';
  }

  if (en) {
    html += '<h3>Enemies</h3>' + table([
      { label: 'Monster', render: (r) => `${esc(r.name)}${r.boss ? ' ' + pill('stage boss', 'warn') : ''} <span class="muted">${esc(r.type || '')}</span>` },
      { label: 'Count', num: true, render: (r) => n(r.count) },
      { label: 'HP left', num: true, render: (r) => `${n(r.hp)} / ${n(r.max_hp)}` },
      { label: 'Max HP each', num: true, render: (r) => r.max_hp_each.map((v) => n(v)).join(', ') },
      { label: 'Game data', render: (r) => r.catalog_match === null ? '<span class="muted">not checked</span>' : r.catalog_match ? pill('matches', 'good') : pill(`expected ${n(r.expected_max_hp)}`, 'warn') },
    ], en.groups, 'No enemies on the field.');
  }
  html += `<p class="note">Read-only view of game memory, about once per second. Run figures count readings, so a short HP dip between two readings can be missed; deaths come from the game's dead-unit list, whose 90 s resurrection timer cannot be missed.
    ${heroes.some((h) => (h.buffs || []).some((b) => (b.expiry || {}).kind === 'attack_count')) ? 'Buffs marked "ends after … attacks" end by attack count, not by time; their modifiers here are what the game applies, which can differ from the in-game text.' : ''}</p>`;
  return html;
};

// ---------- act boss -------------------------------------------------------------
// "To beat this boss you need to": only the last act boss fight that was lost.
views.actboss = async () => {
  const d = await api('act-boss');
  let html = '<h2>Act boss</h2>';
  if (!d.available) return html + `<div class="warn-box">${esc(d.reason)}</div>`;
  const t = (d.targets || [])[0];
  if (!t) return html + '<p class="note">No lost act boss fight in the last 7 days (or it was beaten since). Nothing to plan.</p>';
  const secs = (v) => v === null || v === undefined ? '—' : `${NF1.format(v)} s`;
  const marginPill = (m) => m === null || m === undefined ? pill('kill time not measured yet', 'warn')
    : pill('margin ' + NF1.format(m), m >= d.safety ? 'good' : m >= 1 ? 'warn' : 'bad');
  html += `<h3>To beat ${esc(t.boss_name || 'the boss')} — ${esc(t.label)}</h3><div class="grid">`;
  html += kpi('Boss', `${n(t.boss_hp)} HP`, `${n(t.boss_damage)} per hit · ${NF1.format(t.attacks_per_s)} attacks/s · skills ×${NF1.format(t.skill_factor)} (game data)`);
  const sim = t.simulation;
  const outcome = (x) => !x ? '—' : x.won ? pill(`win in ${NF1.format(x.kill_s)} s`, 'good') : pill(`lose · boss at ${pct(x.boss_hp_left)}`, 'bad');
  const deathsText = (x) => x && x.deaths.length ? x.deaths.map((e) => `${esc(e.name)} dies at ${NF1.format(e.at_s)} s`).join(' · ') : 'nobody dies';
  if (sim) {
    html += kpi('Today (simulated)', outcome(sim.now), deathsText(sim.now));
    html += kpi('With the plan below', outcome(sim.plan), deathsText(sim.plan));
  } else {
    html += kpi('Now (estimate)', marginPill(t.now.margin), `party lasts ${secs(t.now.survive_s)} · kill takes ${secs(t.now.kill_s)}`);
    if (t.both) html += kpi('With the plan below', marginPill(t.both.margin), `lasts ${secs(t.both.survive_s)} · kill ${secs(t.both.kill_s)} · retry from ${NF1.format(d.safety)}`);
  }
  html += kpi('Attempts lost', n((d.last_failed || {}).attempts), d.last_failed ? `last ${agoAt(d.last_failed.failed_utc)}` : '');
  html += '</div>';
  html += `<ol class="steps">${(t.steps || []).map((s) => `<li>${esc(s)}.</li>`).join('')}</ol>`;
  const m = t.mechanics;
  if (m) {
    html += `<h3>What this boss does (learned from ${n(m.fights)} recorded fight${m.fights === 1 ? '' : 's'})</h3><ul class="steps">
      <li>First hit on the party at about ${secs(m.contact_s)} into the fight; then a base attack every ${secs(m.attack_interval_s)} on the front hero.</li>
      ${m.bursts.map((b) => `<li>${b.skill ? `×${NF1.format(b.skill.value)} hit` : 'Burst'} on the back line at ${secs(b.at_s)}${b.period_s ? `, every ${secs(b.period_s)}` : ''}
        ${b.ambiguous ? pill('skill not certain: assumed the strongest', 'warn') : ''} — killed ${Object.entries(b.killed).map(([k, v]) => `${esc(k)} ${v}/${b.fights}`).join(', ')};
        ${b.heroes.map((h) => h.survives ? `${esc(h.name || h.hero)} survives it now (${n(h.hit)} of ${n(h.hp)} HP)` : `${esc(h.hero)} needs +${n(h.hp_needed)} HP (takes ${n(h.hit)}, has ${n(h.hp)})`).join('; ')}</li>`).join('')}
      <li>Damage per second on this boss in your fights: ${m.dps.map((v) => n(v)).join(', ') || '—'} (attack-count buffs and crits move it).</li></ul>`;
  }
  if (t.check && t.check.length) {
    html += '<h3>Model check on your fights</h3>' + table([
      { label: 'Fight', render: (r) => agoAt(r.utc) },
      { label: 'Predicted (without this fight)', render: (r) => r.predicted_won ? pill('win', 'good') : pill(`lose · boss at ${pct(r.predicted_left)}`, 'bad') },
      { label: 'What happened', render: (r) => r.won ? pill('won', 'good') : pill(`lost · boss at ${pct(r.left)}`, 'bad') },
    ], t.check) + '<p class="note">Each fight is predicted from the other recorded fights only, with the stats the heroes had then.</p>';
  }

  html += '<h3>Each hero against this boss</h3>' + table([
    { label: 'Hero', render: (r) => esc(r.name) },
    { label: 'Max HP', num: true, render: (r) => n(r.hp) },
    { label: 'Boss hit', num: true, render: (r) => `${n(r.hit)} HP${r.cap_uncertain ? ' ' + pill('cap?', 'warn') : ''}` },
    { label: 'Hits survived', num: true, render: (r) => pill(n(r.hits), r.hits <= 1 ? 'bad' : r.hits <= 3 ? 'warn' : 'good') },
    { label: 'HP for one more hit', num: true, render: (r) => `+${n(r.hp_for_next_hit)}` },
  ], t.now.heroes);

  html += '<h3>What one gear stat is worth here</h3>' + table([
    { label: 'Stat', render: (r) => esc(r.stat) }, { label: 'On', render: (r) => esc(r.hero) },
    { label: 'Survival × damage', num: true, render: (r) => `+${NF1.format(r.margin_pct)}%` },
    { label: 'Hits survived', render: (r) => Object.entries(r.hits).map(([k, v]) => `${esc(k)} ${n(v)}`).join(' · ') },
  ], t.stat_rolls || [], 'Nothing measurable.');

  html += '<h3>Lost fights</h3>' + table([
    { label: 'When', render: (r) => agoAt(r.ended_utc) },
    { label: 'Lasted', num: true, render: (r) => secs(r.duration_s) },
    { label: 'Hits taken (readings with HP lost)', render: (r) => Object.entries(r.hits_taken || {}).map(([k, v]) => `${esc(k)} ${v === null ? '—' : n(v)}`).join(' · ') || '—' },
    { label: 'Deaths', render: (r) => r.deaths.map((x) => esc(x.name) + (x.at_s !== null ? ` at ${NF1.format(x.at_s)} s` : '')).join(', ') || '—' },
    { label: 'Boss HP left', num: true, render: (r) => r.boss ? pct(r.boss.hp_left_fraction) : '<span class="muted">not recorded</span>' },
  ], d.failed, 'No recorded fights.');
  html += `<p class="note">${esc(d.note)} Damage per second ${d.dps ? `measured: ${n(d.dps)} at party output ${n(d.dps_offence)}` : 'not measured yet (one stage-boss fight after the server update)'}.
    ${d.unmodelled_points && d.unmodelled_points.length ? 'Points left where they are (the model cannot price them): ' + esc(d.unmodelled_points.join(', ')) + '.' : ''}</p>`;
  return html;
};

const AREAS = [['stages', 'Stages'], ['runes', 'Runes'], ['gear', 'Gear'], ['cube', 'Cube'], ['storage', 'Storage and chests'], ['heroes', 'Heroes']];
const BASIS = { observed: ['observed', 'good'], catalog: ['game data', 'accent'], hypothesis: ['estimate', 'warn'] };
const suggestionCard = (s) => `<article class="card suggestion p${Math.min(s.priority, 3)}" data-key="s-${esc(s.id)}"><strong>${esc(s.title)}</strong>
  <p>${esc(s.detail)}</p><div class="tags">${pill(...(BASIS[s.basis] || [s.basis]))} ${pill(s.confidence + ' confidence', s.confidence === 'high' ? 'good' : s.confidence === 'medium' ? '' : 'warn')}</div></article>`;

views.suggestions = async () => {
  const hours = Number(state.filters.sugHours || 168);
  const d = await api('suggestions?hours=' + hours);
  const top = d.suggestions.filter((s) => s.priority <= 1);
  const ref = d.reference;
  let html = `<h2>Suggestions</h2><div class="controls"><label>Evidence window <select id="sugHours">${[24, 72, 168, 720].map((h) => `<option value="${h}" ${h === hours ? 'selected' : ''}>${h} h</option>`).join('')}</select></label>
    <span class="muted">Gold ${n(d.gold)} · current stage ${esc(d.current_stage_label || '—')} · updated ${time(d.generated_utc)}</span></div>
    <p class="note">Runes, gold, gear and inventory come from the latest save: ${time(d.save_utc)} (${agoAt(d.save_utc)}). The game saves about once a minute and on each clear, so a purchase shows up within about a minute.</p>
    <p class="note">${esc(d.note)} Suggestions are read-only: nothing is done in the game.</p>`;
  html += '<div class="grid">';
  html += kpi('Reference gold/h', n(ref.gold_h), esc(ref.stage_label || 'no confirmed stage'));
  html += kpi('Reference XP/h per hero', n(ref.xp_h), 'confirmed best XP stage');
  html += kpi('Clears/h at reference', n(ref.clears_per_h, 1), `${n(ref.kills_per_h)} kills/h`);
  html += '</div>';
  if (top.length) html += `<h3>Do first</h3><div class="grid">${top.map(suggestionCard).join('')}</div>`;
  for (const [area, title] of AREAS) {
    const rows = d.suggestions.filter((s) => s.area === area && s.priority > 1);
    if (area === 'stages') {
      html += `<h3>${title}</h3>${rows.length ? `<div class="grid">${rows.map(suggestionCard).join('')}</div>` : ''}`;
      html += `<details class="card" data-detail="suggestion-stages"><summary>Observed stages with the current party (${d.stages.length})</summary>${table([
        { label: 'Stage', render: (r) => esc(r.label) },
        { label: 'Complete runs', num: true, render: (r) => `${n(r.runs)}${r.fails ? ` (${n(r.fails)} failed)` : ''}` },
        { label: 'Median run', num: true, render: (r) => r.median_duration_s ? dur(r.median_duration_s) : '—' },
        { label: 'Gold/h', num: true, render: (r) => n(r.gold_h) + spread(r.gold_rse) },
        { label: 'Gold confidence', render: (r) => confPill(r.gold_confidence) },
        { label: 'XP/h per hero', num: true, render: (r) => n(r.xp_h) + spread(r.xp_rse) },
        { label: 'XP confidence', render: (r) => confPill(r.xp_confidence) },
        { label: 'Power changes since', num: true, render: (r) => r.changes_since === null || r.changes_since === undefined ? '—' : r.changes_since ? pill(`${r.changes_since} (${r.changes_since_kinds.join(', ')})`, 'warn') : '0' },
        { label: 'Save counters (cross-check)', num: true, render: (r) => r.save_gold_h ? `${n(r.save_gold_h)} gold/h · ${n(r.save_minutes, 1)} min` : '—' },
        { label: 'Source', render: (r) => esc(r.source) },
      ], d.stages, 'No stage observed with the current party.')}</details>`;
    } else if (rows.length) {
      html += `<h3>${title}</h3><div class="grid">${rows.map(suggestionCard).join('')}</div>`;
    }
  }
  return html;
};

views.runs = async () => {
  const stage = state.filters.runStage || '';
  const runs = await api('runs?limit=300' + (stage ? '&stage=' + encodeURIComponent(stage) : ''));
  const stages = [...new Set(runs.map((r) => r.stage_key))];
  let html = `<h2>Runs</h2><div class="controls"><label>Stage <input id="runStage" value="${esc(stage)}" placeholder="e.g. 1109" size="8"></label>
    <button class="act" id="runFilter">Filter</button><a class="act" href="/api/export/runs.csv">Export CSV</a>
    <span class="muted">${runs.length} runs · ${stages.length} stages</span></div>`;
  html += `<p class="note">✓ = confirmed by save StageClear/StageFail counters. ? = inferred at runtime (final wave reached). Runs with an unobserved start are excluded from averages.
    Act boss fights read live are runs of their own stage (outcome: boss killed = wave 1). Older fights, from before the reader handled act boss stages,
    come from the save counters and are only placed between two saves; a stage run with such a fight inside is excluded from averages.
    Damage share is each hero's estimated base-attack damage (attacks counted by the game × Attack Damage × average crit); skills are not counted. Runs recorded before this was added show —.</p>`;
  const ab = (r) => r.kind === 'act_boss';
  html += table([
    { label: '#', render: (r) => ab(r) ? pill('act boss', 'accent') : esc(r.id) },
    { label: 'End', render: (r) => ab(r) ? `<span title="between saves at ${esc(time(r.started_utc))} and ${esc(time(r.ended_utc))}">by ${time(r.ended_utc)}</span>` : time(r.ended_utc) },
    { label: 'Stage', render: (r) => esc(r.stage_label) },
    { label: 'Outcome', render: (r) => outcomePill(r) + (ab(r) && r.clears + r.fails > 1 ? ` ×${n(r.clears + r.fails)}` : '') },
    { label: 'Duration', num: true, render: (r) => ab(r) ? `<span class="muted">≤ ${dur(r.window_s)}</span>` : dur(r.duration_s) },
    { label: 'Maximum wave', num: true, render: (r) => ab(r) ? '' : `${n(r.max_wave)}/${n(r.wave_amount)}` },
    { label: 'Gold (est.)', num: true, render: (r) => ab(r) ? `<span class="muted" title="gold earned in the save window, not only the boss">≤ ${n(r.window_gold_earned)}</span>` : n(r.gold_gain_est) },
    { label: 'Gold/h (est.)', num: true, render: (r) => n(r.gold_per_hour_est) },
    { label: 'Spent', num: true, render: (r) => r.gold_spend_est ? n(r.gold_spend_est) : '' },
    { label: 'XP', render: (r) => heroXp(r.xp) },
    { label: 'Damage share (est.)', render: (r) => attackShares(r.attack_damage) || '<span class="muted">—</span>' },
    { label: 'Notes', render: (r) => ab(r) ? '' : [r.partial_start ? pill('partial start', 'warn') : '', r.end_reason !== 'wave_reset' ? pill(r.end_reason) : '', r.gaps ? pill(`${r.gaps} gaps`, 'warn') : '', r.act_boss_inside ? pill('act boss inside', 'warn') : ''].join(' ') },
  ], runs, 'No runs yet. The collector records them while the game runs.');
  return html;
};

views.stages = async () => {
  const d = await api('stages?hours=' + (state.filters.stageHours || 72));
  const rec = d.recommendation;
  let html = `<h2>Stages</h2><div class="controls"><label>Window <select id="stageHours">${[6, 24, 72, 168, 720].map((h) => `<option ${Number(state.filters.stageHours || 72) === h ? 'selected' : ''} value="${h}">${h} h</option>`).join('')}</select></label>
    <span class="muted">${d.windows_valid} of ${d.windows_total} valid save windows</span></div>`;
  html += '<div class="grid">';
  const bestKpi = (title, e, metric, ties, unit) => e
    ? kpi(title, n(e[metric]) + spread(e[metric.replace('_h', '_rse')]), `${esc(e.label)} · ${n(metric === 'xp_h' ? e.xp_runs : e.runs)} complete runs · ${confPill(e[metric.replace('_h', '_confidence')])}${ties.length ? ` · tied with ${esc(ties.map((t) => t.label).join(', '))}` : ''}`)
    : kpi(title, '—', `No stage confirmed yet (${esc(unit)}).`);
  html += bestKpi('Best supported gold/h', rec.gold, 'gold_h', rec.gold_ties, 'needs comparable levels/loadout, 5+ runs and relative standard error ≤10%');
  html += bestKpi('Best supported XP/h per hero', rec.xp, 'xp_h', rec.xp_ties, 'needs comparable levels/loadout, 5+ runs and relative standard error ≤10%');
  const model = rec.model;
  html += kpi('Estimate model (unplayed stages)', model ? `±${NF.format(model.error.gold_h * 100)}% <span class="muted">gold/h error</span>` : '—',
    model ? `observed gold = ×${NF1.format(model.gold_ratio)} catalog · fitted on ${model.stages.length} played stages${model.power ? ' · ' + pill('uses party output', 'accent') : ''}` : 'needs 3+ played stages with 3+ complete runs');
  html += kpi('Highest completed stage', n(d.max_completed_stage), '');
  html += '</div>';
  html += `<p class="note">${esc(rec.note)}</p>`;
  const fought = d.evidence.filter((e) => e.combat_runs || e.save_minutes || e.deaths_total);
  const bossCell = (t) => {
    if (!t || t.boss_fraction === null || t.boss_fraction === undefined) return '—';
    const w = t.front ? t.heroes.find((h) => h.hero_key === t.front)
      : t.heroes.filter((h) => h.boss_fraction !== null).sort((a, b) => b.boss_fraction - a.boss_fraction)[0];
    const kind = t.boss_fraction >= 0.5 ? 'bad' : t.boss_fraction >= 0.25 ? 'warn' : 'good';
    const ratio = t.vs_current ? ` · ${pill('×' + NF1.format(t.vs_current) + ' vs now', t.vs_current > 1.3 ? 'warn' : '')}` : '';
    return `${pill(pct(t.boss_fraction), kind)} <span class="muted">${t.front ? 'front: ' : 'worst: '}${esc(w.name)} · ${n(w.boss_hit)} HP · ${NF1.format(w.boss_hits_to_die)} hits${w.cap_uncertain ? ' · cap?' : ''}</span>${ratio}`;
  };
  const ref = rec.survival_reference || {};
  if (fought.length) {
    html += '<h3>Survival by stage</h3>' + table([
      { label: 'Stage', render: (r) => esc(r.label) },
      { label: 'Party', render: (r) => esc((r.party_names || []).join(', ')) },
      { label: 'Hero deaths (saves)', num: true, render: (r) => (r.deaths_total ? pill(n(r.deaths_total), r.deaths_recent ? 'bad' : 'warn') : '0')
        + (r.deaths_recent ? ` <span class="muted">${n(r.deaths_recent)} recent</span>` : r.last_death_utc ? ` <span class="muted">last ${agoAt(r.last_death_utc)}</span>` : '')
        + (r.save_minutes ? ` <span class="muted">· ${n(r.save_minutes)} min</span>` : '') },
      { label: 'Runs read', num: true, render: (r) => n(r.combat_runs) },
      { label: 'Runs with a death', num: true, render: (r) => r.combat_runs ? (r.runs_with_deaths ? pill(`${n(r.runs_with_deaths)} (${n(r.deaths)} deaths)`, 'bad') : '0') : '—' },
      { label: 'Lowest hero HP', num: true, render: (r) => r.lowest_hp === null || r.lowest_hp === undefined ? '—' : pill(pct(r.lowest_hp), r.lowest_hp < 0.35 ? 'bad' : r.lowest_hp < 0.7 ? 'warn' : 'good') },
      { label: 'Boss hit now', num: true, render: (r) => bossCell(r.threat) },
      { label: 'Party output', num: true, render: (r) => n(r.median_offence) },
    ], fought.sort((a, b) => ((b.threat || {}).boss_fraction ?? -1) - ((a.threat || {}).boss_fraction ?? -1)));
    html += `<p class="note">Hero deaths come from the game's own counter in the saves, including the windows of failed runs (all history; "recent" = last 6 h). Runs read come from ~1 s combat readings (since the combat reader).
      ${rec.threat_note ? esc(rec.threat_note) : 'Boss hits need a live reading of the game.'}
      ${ref.deaths_from ? ` Heroes died where the boss hit ≥ ${pct(ref.deaths_from[0])} (${esc(ref.deaths_from[1])}).` : ''}${ref.no_deaths_up_to ? ` No deaths up to ${pct(ref.no_deaths_up_to[0])} (${esc(ref.no_deaths_up_to[1])}).` : ''}</p>`;
  }
  const toConfirm = [...rec.gold_to_confirm, ...rec.xp_to_confirm.filter((e) => !rec.gold_to_confirm.some((g) => g.stage === e.stage && g.party === e.party))];
  if (toConfirm.length) {
    html += '<h3>Look better but are not confirmed yet</h3>' + table([
      { label: 'Stage', render: (r) => esc(r.label) },
      { label: 'Party', render: (r) => esc(r.party_names.join(', ')) },
      { label: 'Complete runs', num: true, render: (r) => n(r.runs) },
      { label: 'Gold/h', num: true, render: (r) => n(r.gold_h) + spread(r.gold_rse) },
      { label: 'XP/h per hero', num: true, render: (r) => n(r.xp_h) + spread(r.xp_rse) },
      { label: 'Confidence', render: (r) => confPill(r.gold_confidence) },
      { label: 'Power changes since', num: true, render: (r) => r.changes_since ? pill(String(r.changes_since), 'warn') : (r.changes_since === 0 ? '0' : '—') },
      { label: 'Source', render: (r) => esc(r.source) },
    ], toConfirm);
  }
  const maxGold = Math.max(0, ...d.save_rates.map((r) => r.gold_monster_per_h || 0));
  html += '<h3>Exact rates by save window (game counters)</h3>';
  html += table([
    { label: 'Stage', render: (r) => esc(r.stage_label) },
    { label: 'Party', render: (r) => esc(r.party_names.join(', ')) },
    { label: 'Time', num: true, render: (r) => n(r.minutes, 1) + ' min' },
    { label: 'Gold/h (monsters)', num: true, render: (r) => bar(r.gold_monster_per_h, maxGold) + n(r.gold_monster_per_h) },
    { label: 'Gold/h (total)', num: true, render: (r) => n(r.gold_total_per_h) },
    { label: 'Clears/h', num: true, render: (r) => n(r.clears_per_h, 1) },
    { label: 'Failures', num: true, render: (r) => `${n(r.fails)} (${pct(r.fail_rate)})` },
    { label: 'Chests/h', num: true, render: (r) => n(r.boxes_per_h, 1) },
    { label: 'XP/h per hero', render: (r) => Object.values(r.xp_per_h).map((x) => `${esc(x.name)}: ${n(x.value)}`).join('<br>') },
    { label: 'Complete runs', num: true, render: (r) => n(r.runs) },
    { label: 'Confidence (runs)', render: (r) => confPill(r.confidence) },
  ], d.save_rates, 'No valid windows: the game must run on the same stage between saves.');
  html += '<h3>Observed live runs</h3>';
  html += table([
    { label: 'Stage', render: (r) => esc(r.stage_label) },
    { label: 'Party', render: (r) => esc(r.party_names.join(', ')) },
    { label: 'Runs', num: true, render: (r) => `${n(r.runs)} (${n(r.outcomes_confirmed)} ✓)` },
    { label: 'Failures', num: true, render: (r) => `${n(r.fails)} (${pct(r.fail_rate)})` },
    { label: 'Median duration', num: true, render: (r) => dur(r.median_duration_s) + (r.duration_stdev_s ? ` ± ${dur(r.duration_stdev_s)}` : '') },
    { label: 'Gold/run (est.)', num: true, render: (r) => n(r.gold_per_run_est) },
    { label: 'Gold/h (est.)', num: true, render: (r) => n(r.gold_per_h_est) + spread(r.gold_rse) },
    { label: 'XP/h', render: (r) => Object.values(r.xp_per_h).map((x) => `${esc(x.name)}: ${n(x.value)}`).join('<br>') },
    { label: 'Confidence', render: (r) => confPill(r.confidence) },
  ], d.runtime_rates, 'No complete runs observed yet.');
  const est = (r, k) => r.estimate && r.estimate[k] !== undefined && r.estimate[k] !== null ? r.estimate[k] : null;
  html += `<h3>Untested candidates</h3><p class="note">${esc(rec.model_note)}</p>`;
  html += table([
    { label: 'Stage', render: (r) => esc(r.label) + (r.reachable === false ? ' ' + pill('check unlocked', 'warn') : '') },
    { label: 'Waves × monsters', num: true, render: (r) => `${r.waves} × ${r.monsters_per_wave}` },
    { label: 'Monster HP', num: true, render: (r) => '×' + n(r.monster_hp_mult, 1) },
    { label: 'Est. run time', num: true, render: (r) => est(r, 'duration_s') === null ? '—' : dur(est(r, 'duration_s')) },
    { label: 'Est. gold/h', num: true, render: (r) => est(r, 'gold_h') === null ? '—' : `${n(est(r, 'gold_h'))} <span class="muted">(${n(est(r, 'gold_low'))}–${n(est(r, 'gold_high'))})</span>` },
    { label: 'Est. XP/h per hero', num: true, render: (r) => est(r, 'xp_h') === null ? '—' : n(est(r, 'xp_h')) },
    { label: 'Monster damage vs played', num: true, render: (r) => est(r, 'dmg_vs_observed') === null ? '—' : pill('×' + n(est(r, 'dmg_vs_observed'), 1), est(r, 'dmg_vs_observed') > 1.05 ? 'warn' : '') },
    { label: 'Boss hit now', num: true, render: (r) => bossCell(r.threat) },
    { label: 'Gold/clear (you)', num: true, render: (r) => r.gold_per_clear_you === undefined ? n(r.gold_per_clear) : `${n(r.gold_per_clear_you)} <span class="muted">(base ${n(r.gold_per_clear)})</span>` },
  ], rec.untested_candidates, 'No candidates.');
  html += `<details class="card" style="margin-top:12px"><summary>Complete theoretical table (${d.theoretical.length} stages)</summary>${table([
    { label: 'Stage', render: (r) => esc(r.label) }, { label: 'Type', key: 'type' }, { label: 'Level', num: true, key: 'stage_level' },
    { label: 'Gold/clear', num: true, render: (r) => n(r.gold_per_clear) }, { label: 'XP/clear', num: true, render: (r) => n(r.exp_per_clear) },
    { label: 'Gold/clear (you)', num: true, render: (r) => n(r.gold_per_clear_you) }, { label: 'XP/clear (you)', num: true, render: (r) => n(r.exp_per_clear_you) },
    { label: 'Observed', render: (r) => r.observed ? pill('yes', 'good') : '' },
  ], d.theoretical)}<p class="note">Base = catalog rewards × stage multiplier (checked per kill). "You" adds your runes × pet${rec.bonus ? ` (gold ×${NF1.format(rec.bonus.gold)}, XP ×${NF1.format(rec.bonus.xp)})` : ''} and the flat rune bonuses;
    measured gold per kill was ~2% above it and XP also varies by stage, so treat "you" as an estimate. Kills per clear assume waves × monsters per wave.</p></details>`;
  return html;
};

views.heroes = async () => {
  const d = await api('heroes');
  let html = '<h2>Heroes</h2><div class="grid">';
  for (const h of d.heroes) {
    html += `<div class="card"><div class="kpi"><div class="label">${esc(h.class || '')} ${h.in_party ? pill('party', 'accent') : ''} ${h.unlocked ? '' : pill('locked')}</div>
      <div class="value">${esc(h.name)} · ${n(h.level)}</div><div class="sub">${n(h.xp)} / ${n(h.xp_for_next)} XP (${pct(h.progress)}) · source ${esc(h.xp_source)}</div>${progress(h.progress)}</div>
      <div class="note">Skills: ${h.skills.map((s) => esc(s.name || s.key)).join(', ') || '—'}<br>Points: ${n(h.allocated_points)} allocated, ${n(h.ability_points)} free
      ${h.xp_share !== null && h.xp_share !== undefined ? `<br>XP vs party average: ×${h.xp_share.toFixed(3)} <span class="muted">(${n(h.xp_share_runs)} runs)</span>` : ''}
      ${h.xp_stat !== null && h.xp_stat !== undefined ? `<br>XP gain multiplier (game stat): ${h.xp_stat.toFixed(3)}` : ''}</div></div>`;
  }
  html += '</div>';
  if (d.heroes.some((h) => h.xp_share !== null && h.xp_share !== undefined)) html += `<p class="note">"XP vs party average" is measured on the latest complete runs without level-ups.
    It is not the XP gain multiplier: that stat adds to the other XP bonuses (runes…), so +3.6% on a hero gave about +1.4% more XP than the others (hypothesis under test).</p>`;
  html += '<h3>Equipment</h3>';
  for (const hero of d.equipment.filter((e) => e.equipment.length)) {
    html += `<h3 class="muted">${esc(hero.name)} ${hero.in_party ? pill('party', 'accent') : ''}</h3>` + itemTable(hero.equipment, true);
  }
  return html;
};

function statList(item) {
  const stats = (item.stats || []).map((s) => `${esc(s.name)} ${n(s.value, 1)}${s.mod && s.mod !== 'FLAT' ? ' ' + esc(s.mod) : ''}`);
  const enchants = (item.enchants || []).map((e) => `enchant ${esc(e.stat)} T${e.tier} ${n(e.value, 1)}`);
  return [...stats, ...enchants, item.unique_mod ? `unique: ${esc(item.unique_mod)}` : ''].filter(Boolean).join('<br>');
}

function itemTable(items, equipped = false) {
  return table([
    equipped ? { label: 'Slot', render: (r) => esc(r.slot_part) } : { label: 'Location', render: (r) => esc(r.container_label || '') },
    { label: 'Item', render: (r) => `<span class="grade-${esc(r.grade)}">${esc(r.name || r.item_key)}</span>${r.blocked || r.slot_blocked ? ' 🔒' : ''}` },
    { label: 'Type', render: (r) => esc([r.type, r.gear_type || r.synthesis_type].filter(Boolean).join(' · ')) },
    { label: 'Grade', render: (r) => esc(r.grade) },
    { label: 'Level', num: true, render: (r) => n(r.level) },
    ...(equipped ? [] : [{ label: 'Qty', num: true, render: (r) => n(r.quantity) }]),
    { label: 'Stats', render: statList },
    { label: 'ID', render: (r) => `<span class="mono muted">${esc(r.unique_id)}</span>` },
  ], items, 'No items.');
}

views.inventory = async () => {
  const d = await api('inventory');
  const f = state.filters;
  let html = `<h2>Inventory and stash</h2><p class="note">${esc(d.note)} Save: ${time(d.last_saved_utc)}.</p><div class="grid">`;
  const all = [];
  for (const [key, c] of Object.entries(d.containers)) {
    html += kpi(c.label, `${n(c.slots_used)} / ${n(c.slots_unlocked)}`, `used slots · ${n(c.slots_free)} free · ${n(c.quantity_total)} units`);
    for (const it of c.items) all.push({ ...it, container: key, container_label: c.label });
  }
  html += '</div>';
  const types = [...new Set(all.map((i) => i.type).filter(Boolean))];
  html += `<div class="controls"><input id="invText" aria-label="Search by name" placeholder="Search by name" value="${esc(f.invText || '')}">
    <select id="invType" aria-label="Item type"><option value="">All types</option>${types.map((t) => `<option ${f.invType === t ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
    <select id="invWhere" aria-label="Location"><option value="">All locations</option>${Object.entries(d.containers).map(([k, c]) => `<option value="${k}" ${f.invWhere === k ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}</select></div>`;
  const text = (f.invText || '').toLowerCase();
  const rows = all.filter((i) => (!f.invType || i.type === f.invType) && (!f.invWhere || i.container === f.invWhere)
    && (!text || (i.name || '').toLowerCase().includes(text)));
  return html + itemTable(rows);
};

views.chests = async () => {
  const d = await api('chests');
  let html = '<h2>Chests</h2><div class="grid">';
  html += kpi('Chests in containers', n(d.stock.reduce((a, b) => a + b.quantity, 0)), `${d.stock.length} types`);
  html += kpi('Inventory free', n(d.inventory_free), `of ${n(d.inventory_unlocked)} unlocked slots`);
  html += '</div>';
  html += table([
    { label: 'Chest', render: (r) => `<span class="grade-${esc(r.grade)}">${esc(r.name)}</span>` },
    { label: 'Contents', render: (r) => esc(r.content || 'normal') },
    { label: 'Qty', num: true, render: (r) => n(r.quantity) },
    { label: 'Locations', render: (r) => Object.entries(r.locations).map(([k, v]) => `${esc(k)}: ${n(v)}`).join(', ') },
    { label: 'Cooldown (catalog)', num: true, render: (r) => n(r.drop_cooldown) },
  ], d.stock, 'No chests stored in inventory/stash in the latest save.');
  html += `<h3>BoxBucket lists</h3><p class="note">${esc(d.bucket_note)}<br>use: <span class="mono">${esc(d.bucket_use.join(', ') || '—')}</span><br>received: <span class="mono">${esc(d.bucket_get.join(', ') || '—')}</span></p>`;
  return html;
};

views.cube = async () => {
  const d = await api('cube');
  let html = '<h2>Cube</h2><div class="grid">';
  html += `<div class="card kpi"><div class="label">Cube level</div><div class="value">${n(d.level)}</div><div class="sub">${n(d.exp)} / ${n(d.exp_for_next)} XP</div>${progress(d.progress)}</div>`;
  html += kpi('Unlocked synthesis tier', n(d.synthesis.max_unlocked_tier), '');
  const sel = d.synthesis.selected;
  html += kpi('Preselected sub-recipe', sel ? esc(sel.name) : '—', sel ? `tier ${n(sel.tier)} · assumed: highest unlocked` : 'unknown');
  html += '</div><h3>Recipes</h3>';
  html += table([
    { label: 'Recipe', render: (r) => esc(r.type) + (r.opened ? '' : ' ' + pill('locked')) },
    { label: 'Unlocked sub-recipes', render: (r) => `${r.sub_recipes.filter((s) => s.unlocked).length} / ${r.sub_recipes.length}` },
    { label: 'Next unlock', render: (r) => { const nx = r.sub_recipes.find((s) => !s.unlocked); return nx ? [nx.name && nx.name !== '-' ? esc(nx.name) : '', `cube ${n(nx.unlock_cube_level)}`, `cost ${n(nx.unlock_cost)}`].filter(Boolean).join(' · ') + (nx.unlockable_now ? ' ' + pill('level reached', 'good') : '') : '—'; } },
  ], d.recipes);
  html += `<h3>Materials and synthesis</h3><p class="note">${esc(d.synthesis.note)}</p>`;
  html += table([
    { label: 'Type', render: (r) => esc(r.synthesis_type) }, { label: 'Grade', render: (r) => `<span class="grade-${esc(r.grade)}">${esc(r.grade)}</span>` },
    { label: 'Free quantity', num: true, render: (r) => n(r.quantity) },
    { label: sel ? `In ${esc(sel.name)}` : 'In selected range', num: true, render: (r) => r.in_selected_range == null ? '—' : n(r.in_selected_range) },
    { label: 'Average level', num: true, render: (r) => n(r.average_level, 1) },
    { label: 'Synthesis (9 of the same grade and type → 1)', wrap: true, render: (r) => {
      if (r.gate) return `<span class="muted">${esc(r.gate)}</span>`;
      if (r.in_selected_range != null && r.in_selected_range < 9) return `<span class="muted">${9 - r.in_selected_range} more needed in ${esc(sel.name)}</span>`;
      const best = r.options[0];
      if (!best) return `<span class="muted">${r.quantity < 9 ? `${9 - r.quantity} more needed for one synthesis` : 'no unlocked recipe for these items'}</span>`;
      const chances = r.result_chances.map((c) => `${pct(c.chance)} ${esc(c.grade.toLowerCase())}`).join(', ');
      const levels = r.synthesis_type === 'Gear' && best.result_levels.length ? ` · lvl ${best.result_levels[0].level}–${best.result_levels.at(-1).level} (T${best.tier})` : '';
      return `${best.batches}× · keeps ${r.quantity - best.batches * best.material_amount} · ${chances}${levels}`;
    } },
    { label: 'Alchemy (est.)', num: true, render: (r) => n(r.alchemy_gold_estimate) + ' gold' },
    { label: 'XP cube (est.)', num: true, render: (r) => n(r.cube_exp_estimate) },
  ], d.synthesis.groups, 'No available materials in inventory/stash.');
  return html;
};

const deltaCell = (m) => m ? `${m.pct >= 0 ? '+' : ''}${NF1.format(m.pct)}%${m.pct_se !== null && m.pct_se !== undefined ? ` <span class="muted">±${NF1.format(m.pct_se)}</span>` : ''} ${m.significant ? pill('significant', 'good') : m.enough_runs ? pill('noise') : ''}` : '—';

views.runes = async () => {
  const [d, impact] = await Promise.all([api('runes'), api('runes/impact?hours=' + (state.filters.impactHours || 72))]);
  const onlyBuy = state.filters.runeBuy;
  let html = `<h2>Runes</h2><p class="note">${esc(d.note)} Gold in the latest save: ${n(d.gold)}.</p>
    <p class="note">"In game" converts the stored value with the game's display scale where it is known (checked against the in-game Stat List: percent texts store ten times the shown percent; "+N" texts store N). "scale inferred" marks stats not listed there; unlock-type stats show the raw stored value.</p>`;
  if (d.pet) html += `<h3>Pet: ${esc(d.pet.name)}</h3>` + table([
      { label: 'Bonus', render: (r) => esc(r.text) }, { label: 'How it combines', render: (r) => r.note ? esc(r.note) : '<span class="muted">not measured</span>' },
      { label: 'Stat', render: (r) => `<span class="mono muted">${esc(r.stat)}</span>` }], d.pet.effects)
    + `<p class="note">Unlocked pets: ${d.pet.unlocked.map((p) => esc(p.name)).join(', ') || '—'}. The pet adds no hero stats; its gold and XP bonuses multiply the rune bonuses.</p>`;
  const statusKind = { measured: 'good', collecting: 'warn', 'too few runs before': 'warn' };
  html += `<h3>Measured effect of changes (runes, gear, attributes, skills)</h3><div class="controls"><label>Window <select id="impactHours">${[24, 72, 168, 720].map((h) => `<option value="${h}" ${Number(state.filters.impactHours || 72) === h ? 'selected' : ''}>${h} h</option>`).join('')}</select></label></div>
    <p class="note">${esc(impact.note)}</p>` + table([
    { label: 'Saved', render: (r) => new Date(r.to_utc).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) },
    { label: 'Change', wrap: true, render: (r) => r.changes.slice(0, 4).map((c) => `${pill(c.kind)} ${esc(c.label)}${c.effect ? ` <span class="muted">(${esc(c.effect.text)})</span>` : ''}`).join('<br>') + (r.changes.length > 4 ? `<br><span class="muted">+${r.changes.length - 4} more</span>` : '') },
    { label: 'Stage', wrap: true, render: (r) => esc(r.stage_label || '—') },
    { label: 'Runs before / after', num: true, render: (r) => `${n(r.runs_before)} / ${n(r.runs_after)}` },
    { label: 'Gold/h', render: (r) => deltaCell(r.metrics.gold_h) },
    { label: 'Run time', render: (r) => deltaCell(r.metrics.duration_s) },
    { label: 'XP/h per hero', render: (r) => deltaCell(r.metrics.xp_h) },
    { label: 'Gold/run (expected)', render: (r) => deltaCell(r.metrics.gold_per_run) + (r.expected.gold_per_run !== undefined ? ` <span class="muted">exp. +${NF1.format(r.expected.gold_per_run)}%</span>` : '') },
    { label: 'Stats (from game)', wrap: true, render: (r) => r.stat_change === null || r.stat_change === undefined ? '<span class="muted">not recorded</span>'
      : r.stat_change.length ? r.stat_change.map((h) => `${esc(h.name)}: ${h.stats.filter((x) => x.pct !== null).sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct)).slice(0, 3).map((x) => `${esc(x.name)} ${x.pct >= 0 ? '+' : ''}${NF1.format(x.pct)}%`).join(', ')}`).join('<br>') : 'no stat change' },
    { label: 'Level-ups', num: true, render: (r) => r.level_ups ? pill(n(r.level_ups), 'warn') : '0' },
    { label: 'Status', wrap: true, render: (r) => pill(({ 'too few runs before': 'few runs before', 'no comparable runs': 'no runs before' }[r.status] || r.status) + (r.status === 'collecting' ? ` (${r.runs_needed} more)` : ''), statusKind[r.status] || '') },
  ], impact.purchases, 'No power changes in this window.');
  html += '<h3>Accumulated effects</h3>' + table([
    { label: 'Effect', render: (r) => esc(r.name) }, { label: 'Stat', render: (r) => `<span class="mono">${esc(r.stat)}</span>` },
    { label: 'In game', render: (r) => r.effect ? esc(r.effect.text) + (r.effect.verified ? '' : ' ' + pill('scale inferred', 'warn')) : '<span class="muted">scale not verified</span>' },
    { label: 'Raw value', num: true, render: (r) => n(r.value, 1) },
  ], d.totals.sort((a, b) => a.stat.localeCompare(b.stat)));
  html += `<h3>Tree</h3><div class="controls"><label><input type="checkbox" id="runeBuy" ${onlyBuy ? 'checked' : ''}> only available next levels</label></div>`;
  const nodes = d.nodes.filter((x) => !onlyBuy || x.purchasable_hint).sort((a, b) => (a.next_cost ?? 1e18) - (b.next_cost ?? 1e18));
  html += table([
    { label: 'Rune', render: (r) => esc(r.name) }, { label: 'Level', num: true, render: (r) => `${n(r.level)} / ${n(r.max_level)}` },
    { label: 'Stat', render: (r) => `<span class="mono">${esc(r.stat)}</span>` },
    { label: 'Next', num: true, render: (r) => r.next_cost !== null ? `${n(r.next_cost)} ${r.next_cost_item === '100001' ? 'gold' : esc(r.next_cost_item)} → ${r.next_effect ? esc(r.next_effect.text) : `+${n(r.next_value, 1)} <span class="muted">(raw)</span>`}` : '—' },
    { label: 'Available', render: (r) => r.purchasable_hint ? pill(d.gold >= r.next_cost ? 'yes' : 'insufficient gold', d.gold >= r.next_cost ? 'good' : 'warn') : '' },
  ], nodes);
  return html;
};

views.economy = async () => {
  const d = await api('economy?hours=' + (state.filters.ecoHours || 24));
  const t = d.totals;
  let html = `<h2>Economy</h2><div class="controls"><label>Window <select id="ecoHours">${[1, 6, 24, 72, 168].map((h) => `<option ${Number(state.filters.ecoHours || 24) === h ? 'selected' : ''} value="${h}">${h} h</option>`).join('')}</select></label></div>`;
  html += `<p class="note">${esc(d.note)}</p><div class="grid">`;
  html += kpi('Gold earned (total)', n(t.gold_earned_total), `over ${dur(t.seconds)} between saves`);
  html += kpi('Monsters', n(t.gold_monster), ''); html += kpi('Alchemy', n(t.gold_alchemy), ''); html += kpi('Offline', n(t.gold_offline), '');
  html += kpi('Implied spending', n(t.implied_spend), 'income minus balance change');
  html += '</div><h3>Save windows</h3>';
  html += table([
    { label: 'End', render: (r) => time(r.end_utc) }, { label: 'Duration', num: true, render: (r) => dur(r.wall_s) },
    { label: 'Stage', render: (r) => esc(r.stage_label) }, { label: 'Earned', num: true, render: (r) => n(r.gold_earned_total) },
    { label: 'Balance Δ', num: true, render: (r) => n(r.balance_delta) }, { label: 'Spent', num: true, render: (r) => r.implied_spend ? n(r.implied_spend) : '' },
    { label: 'Clears/Failures', num: true, render: (r) => `${n(r.clears)} / ${n(r.fails)}` },
    { label: 'Valid', render: (r) => r.valid ? pill('yes', 'good') : pill(r.reasons.join('; '), 'warn') },
  ], d.windows.slice().reverse());
  html += '<h3>Observed live spending</h3>' + table([
    { label: 'When', render: (r) => time(r.utc) }, { label: 'Value', num: true, render: (r) => n(r.payload.amount) },
    { label: 'Stage / wave', render: (r) => `${esc(r.payload.stage_key)} / ${esc(r.payload.wave)}` }, { label: 'Balance after', num: true, render: (r) => n(r.payload.balance_after) },
  ], d.spend_events, 'No balance drops recorded.');
  return html;
};

const qualityLabels = { matched: 'Agrees', discrepancy: 'Unexplained difference', boundary_uncertain: 'Boundary uncertain',
  incomplete: 'Incomplete coverage', missing: 'Missing evidence', counter_reset: 'Counter decreased',
  counts_agree: 'Counts agree', counts_differ: 'Counts differ', unique_correspondence: 'Unique correspondence',
  ambiguous: 'Ambiguous', insufficient_evidence: 'Insufficient evidence', no_events: 'No events to assign' };
const qualityPill = (status) => pill(qualityLabels[status] || status,
  ['matched', 'counts_agree'].includes(status) ? 'good' : ['discrepancy', 'counter_reset'].includes(status) ? 'bad' : 'warn');
const qualityStatuses = (counts) => Object.entries(counts).map(([s, count]) => `${qualityPill(s)} ${n(count)}`).join(' · ') || 'No comparisons';

views.quality = async () => {
  const hours = state.filters.qualityHours || 24, status = state.filters.qualityStatus || 'all';
  const offset = Number(state.filters.qualityOffset || 0);
  let d;
  try {
    d = await api(`quality?hours=${hours}&status=${encodeURIComponent(status)}&offset=${offset}&limit=50`);
  } catch (error) {
    if (error.message !== 'unknown route') throw error;
    return '<h2>Data quality</h2><p>The running server has not loaded this view yet. Restart the TBH server, then refresh.</p>'
      + '<button class="act" id="qualityRefresh">Refresh</button>';
  }
  const s = d.summary, g = s.gold_totals_on_comparable_windows, p = d.pagination;
  let html = `<h2>Data quality</h2><p class="note">Compare saves, runtime samples and run markers over the same save windows.
    Agreement supports an analysis; it is not a guarantee that every game formula is correct.</p>
    <div class="controls"><label>History <select id="qualityHours" aria-label="History">${[1, 6, 24, 72, 168].map(h =>
      `<option value="${h}" ${Number(hours) === h ? 'selected' : ''}>${h} h</option>`).join('')}</select></label>
    <label>Evidence <select id="qualityStatus" aria-label="Evidence">${[['all', 'All windows'], ['attention', 'Needs attention'],
      ['discrepancy', 'Differences'], ['boundary_uncertain', 'Boundary uncertain'], ['incomplete', 'Incomplete coverage'],
      ['missing', 'Missing evidence'], ['counter_reset', 'Counter decreased'], ['matched', 'All metrics agree']].map(([v, label]) =>
      `<option value="${v}" ${status === v ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    <button class="act" id="qualityRefresh">Refresh</button></div>`;
  html += `<p>Save interval: ${time(d.scope.first_save)} → ${time(d.scope.last_save)}.
    Latest included save: ${ago(d.scope.last_save_age_s)}; sample: ${ago(d.scope.last_sample_age_s)}.
    Build ${esc(d.catalog_build)}. ${n(d.scope.duplicates_ignored)} duplicate run(s) ignored.
    ${n(d.scope.incompatible_samples)} sample(s) with unknown or incompatible build.</p><div class="grid">`;
  html += kpi('Stored-reading coverage', pct(s.coverage_fraction), `${dur(s.covered_s)} of ${dur(s.seconds)} between saves; not event completeness`);
  html += kpi('Gold comparisons', n(s.windows), qualityStatuses(s.gold_statuses));
  html += kpi('XP comparisons (per hero)', n(Object.values(s.xp_statuses).reduce((a, b) => a + b, 0)), qualityStatuses(s.xp_statuses));
  html += kpi('Run-count comparisons', n(s.windows), qualityStatuses(s.run_statuses));
  if (s.run_interval_statuses) html += kpi('Interval reconciliation', n(s.windows), qualityStatuses(s.run_interval_statuses));
  if (d.xp_validation) {
    const x = d.xp_validation;
    html += `<details class="card"><summary>XP level-up evidence: ${n(x.observed_transitions)} observed transitions</summary>
      <p>Native rule: ${esc(x.native_rule.status)} (build ${esc(x.native_rule.build_id)}). ${esc(x.native_rule.rule)}</p>
      <p>${esc(x.note)}</p><p>Observation checks: ${esc(JSON.stringify(x.statuses))}.
      Matching recorded binary identity: ${n(x.native_identity_matched_transitions)}.</p>
      <p>Evidence source: ${esc(x.native_rule.source)}. Binary SHA-256: <span class="mono">${esc(x.native_rule.game_assembly_sha256)}</span>.</p>
      ${table([{label: 'Hero', key: 'hero_key'}, {label: 'Levels', render: e => `${n(e.level_before)} → ${n(e.level_after)}`},
        {label: 'Samples', render: e => `${n(e.before_sample_id)} / ${n(e.after_sample_id)}`},
        {label: 'XP before / after', render: e => `${n(e.xp_before, 2)} / ${n(e.xp_after, 2)}`},
        {label: 'Accounted XP', render: e => n(e.accounted_gain, 2)},
        {label: 'Check', render: e => esc(e.status)}], x.events)}</details>`;
  }
  html += '</div><h3>Gold ledger — aligned, continuous windows only</h3><div class="grid">';
  html += kpi('Save gross income', n(g.save_earned), `${dur(s.gold_comparable_seconds)} of comparable windows`);
  html += kpi('Sampled balance rises', n(g.sample_rises), 'Not exact gross income; simultaneous spending can hide gains');
  html += kpi('Signed residual', n(g.residual), 'Save income − sampled rises; opposite errors can cancel');
  html += kpi('Absolute residual', n(g.absolute_residual), 'Sum of absolute differences; cancellation cannot hide a mismatch');
  html += `</div><p class="note">Summary figures cover all ${n(s.windows)} windows in the selected history, regardless of the table filter.
    ${n(s.gold_differences_with_balance_drops)} gold difference(s) coincide with observed balance drops: earning and spending between readings is a possible explanation, not a confirmed cause.
    ${n(s.level_up_windows)} window(s) cross a level-up: both sources use the same XP threshold model, so agreement alone does not validate that formula.</p>`;
  html += '<details><summary>Method and exclusions</summary><p>' + esc(d.method.note) + '</p>';
  html += `<p>Nearest reading within ${n(d.method.alignment_seconds, 1)} s at each endpoint.
    Maximum accepted stored-reading gap: ${n(d.method.max_stored_gap_seconds)} s (unchanged states have 30 s heartbeats).
    No reward is distributed across missing runs. Raw history is preserved.
    Nonpositive save intervals skipped: ${n(s.nonpositive_intervals)}.</p>`;
  html += table([{ label: 'Condition', key: 'reason' }, { label: 'Windows (overlap possible)', num: true, key: 'count' }],
    Object.entries(s.exclusions).map(([reason, count]) => ({ reason, count })), 'No exclusions in these windows.');
  html += '</details><h3>Window evidence</h3>';
  html += table([
    { label: 'Save window', render: w => `${time(w.end_utc)}<br><span class="muted">${dur(w.seconds)} · ${esc(w.stage_label)}</span>` },
    { label: 'Coverage', render: w => `${pct(w.coverage.covered_s / w.seconds)}<br>${n(w.coverage.sample_count)} stored readings` },
    { label: 'Gold', render: w => `${qualityPill(w.gold.status)}<br>residual ${n(w.gold.residual)}` },
    { label: 'XP', render: w => w.xp.map(h => `${esc(h.name)}: ${qualityPill(h.status)}<br>residual ${n(h.residual, 1)}`).join('<br>') || '—' },
    { label: 'Runs', render: w => qualityPill(w.runs.status) },
    { label: 'Evidence', wrap: true, render: w => `<details data-key="quality-${w.save_from}-${w.save_to}"><summary>Inspect window</summary>
      <p>${time(w.start_utc)} → ${time(w.end_utc)} · saves ${n(w.save_from)} / ${n(w.save_to)} · session ${n(w.coverage.session_id)}.</p>
      <p>Sample offsets (sample − save): start ${n(w.coverage.start_offset_s, 1)} s, end ${n(w.coverage.end_offset_s, 1)} s.
      Largest gap ${n(w.coverage.max_gap_s, 1)} s. ${w.coverage.continuous ? 'Accepted stored-reading continuity.' : 'Continuity not established.'}</p>
      <p>Gold: save gross ${n(w.gold.save_earned)}, sampled rises ${n(w.gold.sample_rises)}, sampled falls ${n(w.gold.sample_falls)}.
      Save net ${n(w.gold.save_net)}, sampled net ${n(w.gold.sample_net)}, implied spend ${n(w.gold.implied_spend)}.</p>
      <p>Balance differences at endpoints (save − sample): ${n(w.gold.start_balance_residual)} / ${n(w.gold.end_balance_residual)}.
      ${esc(w.gold.note)}</p>
      ${w.gold.spending_overlap_possible ? '<p>Observed balance drops in this window make overlapping spending/income possible. The exact event amounts remain unknown.</p>' : ''}
      <p>Save income sources: monsters ${n(w.gold.sources.gold_monster)}, alchemy ${n(w.gold.sources.gold_alchemy)}, offline ${n(w.gold.sources.gold_offline)}.</p>
      ${table([{ label: 'Hero', key: 'name' }, { label: 'Levels', render: h => `${n(h.level_before)} → ${n(h.level_after)}` },
        { label: 'Save XP', num: true, render: h => n(h.save_gain, 1) }, { label: 'Sample XP', num: true, render: h => n(h.sample_gain, 1) },
        { label: 'Endpoint XP Δ', render: h => `${n(h.start_xp_residual, 1)} / ${n(h.end_xp_residual, 1)}` },
        { label: 'Model / observed steps', render: h => esc(h.save_kind + '; ' + JSON.stringify(h.runtime_kinds)) }], w.xp)}
      <p>Run differences (save − runtime, by stage): ${esc(JSON.stringify(w.runs.residual))}.
      Runtime IDs: ${esc(w.runs.run_ids.join(', ') || 'none')}. Partial starts: ${n(w.runs.partial_starts)};
      first-clear timing adjustments: ${n(w.runs.first_clear_adjustments)}. ${esc(w.runs.note)}</p>
      ${w.runs.interval_reconciliation ? `<p>Interval reconciliation: <strong>${esc(qualityLabels[w.runs.interval_reconciliation.status] || w.runs.interval_reconciliation.status)}</strong>.
      ${esc(w.runs.interval_reconciliation.reasons.join('; '))} ${esc(w.runs.interval_reconciliation.note)}</p>
      <p>Assignments: ${esc(JSON.stringify(w.runs.interval_reconciliation.assignments))}.</p>
      <p>Candidate intervals: ${esc(JSON.stringify(w.runs.interval_reconciliation.evidence))}.</p>` : ''}
      ${Object.entries(w.runs.boundary_markers || {}).map(([edge, markers]) => markers.length ? `<p>${esc(edge)} boundary timing evidence: ${markers.map(m =>
        `run ${n(m.run_id)}, stage ${n(m.stage_key)}, ${esc(m.kind)} between ${time(m.before_utc)} and ${time(m.observed_utc)} (${n(m.interval_s, 2)} s)`
        ).join('; ')}. Possible overlap only; the event time and outcome are not established by this bracket.</p>` : '').join('')}
      <p>Stage-rate eligibility: ${w.stage_rate_eligible ? 'passes save-window checks' : 'excluded'}.
      ${esc(w.flags.join('; ') || 'No additional flags.')}</p></details>` },
  ], d.windows, 'No windows match. At least two saved snapshots within the selected history are needed.');
  html += `<div class="controls"><button class="act" id="qualityPrev" ${offset ? '' : 'disabled'}>Previous</button>
    <span>${n(Math.min(offset + 1, p.filtered_total))}–${n(Math.min(offset + d.windows.length, p.filtered_total))} of ${n(p.filtered_total)} windows</span>
    <button class="act" id="qualityNext" ${p.has_more ? '' : 'disabled'}>Next</button></div>`;
  return html;
};

views.catalog = async () => {
  const text = state.filters.catText || '';
  const type = state.filters.catType || '';
  const items = await api(`catalog/items?limit=150&q=${encodeURIComponent(text)}&type=${encodeURIComponent(type)}`);
  let html = `<h2>Catalog (extracted from installation)</h2><div class="controls"><input id="catText" aria-label="Name or key" placeholder="Name or key" value="${esc(text)}">
    <select id="catType" aria-label="Item type"><option value="">All</option>${['GEAR', 'MATERIAL', 'STAGEBOX'].map((t) => `<option ${type === t ? 'selected' : ''}>${t}</option>`).join('')}</select>
    <button class="act" id="catGo">Search</button></div>`;
  html += table([
    { label: 'Key', render: (r) => `<span class="mono">${esc(r.item_key)}</span>` },
    { label: 'Item', render: (r) => `<span class="grade-${esc(r.grade)}">${esc(r.name)}</span>` },
    { label: 'Type', render: (r) => esc([r.type, r.parts, r.gear_type].filter(Boolean).join(' · ')) },
    { label: 'Grade', render: (r) => esc(r.grade) }, { label: 'Level', num: true, render: (r) => n(r.level) },
    { label: 'Base stats', render: statList }, { label: 'Description', render: (r) => `<span class="muted">${esc(r.description || '')}</span>` },
  ], items);
  return html;
};

views.system = async () => {
  const s = await api('status');
  const c = s.collector;
  let html = '<h2>System</h2><div class="grid">';
  html += kpi('Game build', esc(s.identity.build_id), `catalog ${esc(s.catalog.build_id)} ${s.catalog_matches_build ? pill('matches', 'good') : pill('different', 'bad')}`);
  html += kpi('Memory layout', s.layout.available ? 'available' : 'missing', s.layout.available ? 'generated ' + time(s.layout.generated_utc) : 'live reading disabled');
  html += kpi('Runtime', esc(c.runtime), esc(c.runtime_reason || (s.session ? `PID ${s.session.pid}` : '')));
  html += kpi('Save', esc(c.save), esc(c.save_reason || time(s.save_meta.read_utc)));
  html += kpi('Records', n(s.counts.samples) + ' samples', `${n(s.counts.saves)} saves · ${n(s.counts.runs)} runs · ${n(s.counts.sessions)} sessions`);
  html += '</div>';
  if (c.last_error) html += `<div class="warn-box">Latest error: ${esc(c.last_error)}</div>`;
  html += '<h3>Identity</h3>' + table([{ label: 'File', key: 'k' }, { label: 'SHA-256', render: (r) => `<span class="mono">${esc(r.v)}</span>` }],
    [...Object.entries(s.identity.files), ...Object.entries(s.catalog.sources)].map(([k, v]) => ({ k, v })));
  if (s.layout.semantics) html += '<h3>Semantics of observed fields</h3>' + table([{ label: 'Field', render: (r) => `<span class="mono">${esc(r.k)}</span>` }, { label: 'Meaning', key: 'v' }], Object.entries(s.layout.semantics).map(([k, v]) => ({ k, v })));
  return html;
};

// ---------- shell ----------------------------------------------------------------
async function render() {
  const view = document.getElementById('view');
  clearTimeout(state.timer);
  const tab = state.tab, revision = ++state.revision;
  try {
    const html = await views[tab]();
    if (revision !== state.revision || tab !== state.tab) return;
    if (view.dataset.tab === tab) morph(view, html);
    else view.innerHTML = html;
    view.dataset.tab = tab;
    bind();
    for (const fn of hooks.afterRender) fn();
  } catch (err) {
    if (revision !== state.revision) return;
    view.innerHTML = `<h2>${esc(TABS.find((t) => t[0] === state.tab)[1])}</h2><div class="warn-box">${esc(err.message)}</div>`;
    view.dataset.tab = '';
  }
  if (revision === state.revision) scheduleLive();
}

function scheduleLive() {
  clearTimeout(state.timer);
  const ms = LIVE[state.tab];
  if (!ms || document.hidden) return;
  state.timer = setTimeout(() => {
    const active = document.activeElement, view = document.getElementById('view');
    // Re-rendering would close an open dropdown or drop typed text: wait for the next tick.
    if (active && view.contains(active) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(active.tagName)) scheduleLive();
    else render();
  }, ms);
}

function bind() {
  const on = (id, ev, fn) => { const el = document.getElementById(id); if (el) el['on' + ev] = fn; };
  on('runFilter', 'click', () => { state.filters.runStage = document.getElementById('runStage').value.trim(); render(); });
  on('stageHours', 'change', (e) => { state.filters.stageHours = e.target.value; render(); });
  on('sugHours', 'change', (e) => { state.filters.sugHours = e.target.value; render(); });
  on('ecoHours', 'change', (e) => { state.filters.ecoHours = e.target.value; render(); });
  on('qualityHours', 'change', (e) => { state.filters.qualityHours = e.target.value; state.filters.qualityOffset = 0; render(); });
  on('qualityStatus', 'change', (e) => { state.filters.qualityStatus = e.target.value; state.filters.qualityOffset = 0; render(); });
  on('qualityRefresh', 'click', () => render());
  on('qualityPrev', 'click', () => { state.filters.qualityOffset = Math.max(0, Number(state.filters.qualityOffset || 0) - 50); render(); });
  on('qualityNext', 'click', () => { state.filters.qualityOffset = Number(state.filters.qualityOffset || 0) + 50; render(); });
  on('invText', 'change', (e) => { state.filters.invText = e.target.value; render(); });
  on('invType', 'change', (e) => { state.filters.invType = e.target.value; render(); });
  on('invWhere', 'change', (e) => { state.filters.invWhere = e.target.value; render(); });
  on('runeBuy', 'change', (e) => { state.filters.runeBuy = e.target.checked; render(); });
  on('impactHours', 'change', (e) => { state.filters.impactHours = e.target.value; render(); });
  on('catGo', 'click', () => { state.filters.catText = document.getElementById('catText').value; state.filters.catType = document.getElementById('catType').value; render(); });
}

async function health() {
  try {
    const s = await api('status');
    const c = s.collector;
    const rtKind = c.runtime === 'attached' ? 'ok' : c.runtime === 'unavailable' ? 'warn' : '';
    const svKind = c.save === 'ok' ? 'ok' : 'bad';
    morph(document.getElementById('health'), `<span><span class="dot ${rtKind}"></span>game: ${esc(c.runtime)}</span>
      <span><span class="dot ${svKind}"></span>save: ${esc(c.save)}</span><span>build ${esc(s.identity.build_id)}</span>`);
    document.getElementById('mode').textContent = s.mode;
    document.getElementById('foot').textContent = `Catalog ${s.catalog.rows.toLocaleString('en-US')} rows from ${s.catalog.tables} tables extracted from installation · ${s.mode}`;
  } catch (err) {
    morph(document.getElementById('health'), `<span><span class="dot bad"></span>server unavailable</span>`);
  }
}

function setTab(tab) {
  tab = LEGACY_TABS[tab] || tab;
  if (!TABS.some(([key]) => key === tab)) tab = state.defaultTab;
  state.tab = tab;
  history.replaceState(null, '', '#' + tab);
  document.querySelectorAll('nav button').forEach((b) => b.setAttribute('aria-current', b.dataset.tab === tab ? 'page' : 'false'));
  render();
}

// ---------- boot -----------------------------------------------------------------
function loadScript(src) {
  return new Promise((resolve) => {
    const el = document.createElement('script');
    el.src = src; el.onload = resolve;
    el.onerror = () => { console.warn('extension script not loaded:', src); resolve(); };
    document.body.appendChild(el);
  });
}
async function loadExtensions() {
  let ext = { scripts: [], styles: [] };
  try { ext = await api('extensions'); } catch (_) { /* no extensions */ }
  for (const href of ext.styles) {
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = href; document.head.appendChild(link);
  }
  for (const src of ext.scripts) await loadScript(src);
}
const theme = document.getElementById('theme');
try { theme.value = localStorage.getItem('tbh-theme') || 'auto'; } catch (_) { /* optional storage */ }
function applyTheme() { document.documentElement.dataset.theme = theme.value; }
applyTheme(); theme.onchange = () => { applyTheme(); try { localStorage.setItem('tbh-theme', theme.value); } catch (_) {} };
loadExtensions().then(() => {
  document.getElementById('tabs').innerHTML = TABS.map(([k, title]) => `<button data-tab="${k}">${title}</button>`).join('');
  document.querySelectorAll('nav button').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
  setTab(location.hash.slice(1) || state.defaultTab);
});
health();
setInterval(tickClocks, 250);   // checks often so a displayed second is never skipped
setInterval(() => { if (!document.hidden) health(); }, 5000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(state.timer); return; }
  health(); for (const fn of hooks.onVisible) fn();
  if (LIVE[state.tab]) render();
});
