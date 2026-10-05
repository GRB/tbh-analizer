"""DB-backed analytics: runs, stage comparison, rates and history."""
import json
import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

from ..analysis.economy import stage_rates, theoretical_stage_table, windows
from ..analysis import actboss, stagemodel
from ..analysis.combat import attack_shares
from ..analysis.impact import power_changes
from ..analysis.progress import xp_gained
from ..analysis.stagestats import best_stage, confidence, describe, party_key, ratio_rate, stage_evidence
from ..analysis.threat import live_heroes, party_threat
from .items import rune_totals
from ..snapshots import snapshots_since

DUPLICATE_SECONDS = 5       # two sessions starting the same stage this close recorded one run twice
EXTRAPOLATION_LIMIT = 2.0   # max monster damage multiplier vs the hardest played stage for an estimate
LEVEL_TOLERANCE = 1         # a run belongs to the current build when every hero was within this many levels of today
STATE_NAMES = {0: 'NONE', 1: 'MONSTERSPAWN', 2: 'BATTLE', 3: 'REORGANIZATION'}
def since_iso(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat() if hours else '0'


def recent_snapshots(store, hours=72):
    return snapshots_since(store, since_iso(hours))


def decorate_run(run, catalog, locale='en-US'):
    run = dict(run)
    run['xp'] = json.loads(run['xp']) if run.get('xp') else {}
    run['anomalies'] = json.loads(run['anomalies']) if run.get('anomalies') else []
    run['combat'] = json.loads(run['combat']) if run.get('combat') else None
    run['stage_label'] = catalog.stage_label(run['stage_key'], locale) if run.get('stage_key') else None
    run['attack_damage'] = [{**r, 'name': catalog.hero_name(r['hero_key'], locale)}
                            for r in attack_shares((run['combat'] or {}).get('heroes'))]
    for key, entry in run['xp'].items():
        entry['name'] = catalog.hero_name(key, locale)
        entry['per_hour'] = entry['gain'] * 3600 / run['duration_s'] if run.get('duration_s') and entry['complete'] else None
    run['gold_per_hour_est'] = (run['gold_gain_est'] * 3600 / run['duration_s']
                                if run.get('gold_gain_est') is not None and run.get('duration_s') else None)
    run.pop('start_mono', None)
    run.pop('end_mono', None)
    return run


def list_runs(store, catalog, stage=None, limit=200, locale='en-US'):
    sql, params = 'SELECT * FROM runs', []
    if stage:
        sql += ' WHERE stage_key = ?'
        params.append(stage)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    rows = sorted(unique_runs(store.query(sql, params)), key=lambda r: -r['id'])
    runs = [decorate_run(r, catalog, locale) for r in rows]
    if runs:
        fights = act_boss_attempts(store, catalog, min(r['started_utc'] for r in runs), locale)
        for run in runs:
            run['act_boss_inside'] = actboss.contaminated(run, fights)
    return runs


def act_boss_attempts(store, catalog, since='0', locale='en-US'):
    """Act boss attempts from save counters (the runtime does not show them; see analysis/actboss.py)."""
    found = actboss.attempts(snapshots_since(store, since), catalog)
    actboss.match_runtime(found, act_boss_runtime_runs(store, catalog, since))
    for attempt in found:
        attempt['stage_label'] = catalog.stage_label(attempt['stage_key'], locale)
    return found


def act_boss_runtime_runs(store, catalog, since='0'):
    stages = sorted(actboss.act_boss_stages(catalog))
    if not stages:
        return []
    marks = ','.join('?' * len(stages))
    return store.query(f'SELECT * FROM runs WHERE stage_key IN ({marks}) AND ended_utc >= ?', (*stages, since))


def act_boss_rows(store, catalog, stage=None, since='0', locale='en-US'):
    """Act boss attempts shaped like run rows for the runs list: time is only known to the save window."""
    rows = []
    for a in act_boss_attempts(store, catalog, since, locale):
        if a['observed_run_id'] or (stage and a['stage_key'] != stage):
            continue
        rows.append({'id': None, 'kind': 'act_boss', 'stage_key': a['stage_key'], 'stage_label': a['stage_label'],
                     'started_utc': a['after_utc'], 'ended_utc': a['by_utc'], 'window_s': a['window_s'],
                     'outcome': 'clear' if a['clears'] and not a['fails'] else 'fail' if a['fails'] and not a['clears'] else 'mixed',
                     'outcome_source': 'save-counters', 'clears': a['clears'], 'fails': a['fails'],
                     'boss_kills': a['boss_kills'], 'window_gold_earned': a['window_gold_earned']})
    return rows


def unique_runs(rows):
    """Drop copies recorded by concurrent collectors: matching starts, or overlapping runs with
    the same observed end boundary (including a collector that joined midway). Keep the lowest id."""
    def same_end(a, b):
        # A second collector can attach halfway through the same run, so starts need not match.
        # Require matching observed end boundaries and real interval overlap, not just nearby times.
        if not a.get('ended_utc') or not b.get('ended_utc'):
            return False
        if a.get('end_reason') != b.get('end_reason') or a.get('end_reason') not in ('wave_reset', 'stage_change'):
            return False
        ae, be = datetime.fromisoformat(a['ended_utc']), datetime.fromisoformat(b['ended_utc'])
        overlap = min(ae, be) - max(datetime.fromisoformat(a['started_utc']), datetime.fromisoformat(b['started_utc']))
        return abs((ae - be).total_seconds()) <= 2 and overlap.total_seconds() > 2

    kept, seen = [], []
    for row in sorted(rows, key=lambda r: r['id']):
        start = datetime.fromisoformat(row['started_utc'])
        if any(k['stage_key'] == row['stage_key'] and k['session_id'] != row['session_id']
               and (abs((start - s).total_seconds()) < DUPLICATE_SECONDS or same_end(k, row)) for k, s in seen):
            continue
        kept.append(row)
        seen.append((row, start))
    return kept


def complete_runs(store, catalog, hours=None):
    """Complete observed runs only: start seen, ended by a wave reset, no act boss fight inside
    (the runtime records the fight as part of the stage run: its time and gold would bias the stage)."""
    since = since_iso(hours)
    rows = unique_runs(store.query("SELECT * FROM runs WHERE partial_start = 0 AND end_reason = 'wave_reset' "
                                   "AND duration_s > 0 AND COALESCE(gaps, 0) = 0 "
                                   "AND (anomalies IS NULL OR anomalies = '[]') "
                                   "AND ended_utc >= ?", (since,)))
    fights = actboss.attempts(snapshots_since(store, since), catalog)
    actboss.match_runtime(fights, act_boss_runtime_runs(store, catalog, since))
    return [r for r in rows if not actboss.contaminated(r, fights)]


def observed_run_stats(store, catalog, hours=None, locale='en-US'):
    rows = complete_runs(store, catalog, hours)
    groups = {}
    for run in rows:
        key = (run['stage_key'], run['party'])
        groups.setdefault(key, []).append(run)
    result = []
    for (stage, party), runs in groups.items():
        durations = [r['duration_s'] for r in runs if r['duration_s']]
        total = sum(durations)
        gold_known = [r for r in runs if r['gold_gain_est'] is not None]
        gold_time = sum(r['duration_s'] for r in gold_known)
        xp_sum, xp_time = {}, {}
        for r in runs:
            for hero, entry in json.loads(r['xp'] or '{}').items():
                if entry['complete']:
                    xp_sum[hero] = xp_sum.get(hero, 0.0) + entry['gain']
                    xp_time[hero] = xp_time.get(hero, 0.0) + r['duration_s']
        gold = ratio_rate((r['gold_gain_est'], r['duration_s']) for r in runs)
        clears = sum(1 for r in runs if r['outcome'] == 'clear')
        fails = sum(1 for r in runs if r['outcome'] == 'fail')
        confirmed = sum(1 for r in runs if (r['outcome_source'] or '').startswith('save'))
        result.append({
            'stage': stage, 'stage_label': catalog.stage_label(stage, locale), 'party': party,
            'party_names': [catalog.hero_name(k, locale) for k in party.split(',')] if party else [],
            'runs': len(runs), 'clears': clears, 'fails': fails, 'outcomes_confirmed': confirmed,
            'fail_rate': fails / (clears + fails) if clears + fails else None,
            'median_duration_s': statistics.median(durations) if durations else None,
            'duration_stdev_s': statistics.stdev(durations) if len(durations) > 1 else None,
            'minutes': round(total / 60, 1),
            'gold_per_h_est': sum(r['gold_gain_est'] for r in gold_known) * 3600 / gold_time if gold_time else None,
            'gold_per_run_est': statistics.mean(r['gold_gain_est'] for r in gold_known) if gold_known else None,
            'xp_per_h': {h: {'name': catalog.hero_name(h, locale), 'value': xp_sum[h] * 3600 / xp_time[h]}
                         for h in xp_sum if xp_time.get(h)},
            'spend_est': sum(r['gold_spend_est'] or 0 for r in runs),
            'source': 'runtime (1 s samples)',
            'gold_rse': gold['rse'] if gold else None,
            'confidence': confidence(gold['runs'] if gold else 0, gold['rse'] if gold else None),
        })
    return sorted(result, key=lambda r: -(r['gold_per_h_est'] or 0))


# Survival needs this much observed play on a stage before "no deaths" means something.
SAFE_MINUTES = 20
# Deaths older than this were with weaker heroes: they do not tell how the party fares today.
RECENT_DEATH_HOURS = 6


def deaths_by_stage(window_list, now=None):
    """Hero deaths per stage from the save counter, including the windows a failure invalidates
    (a wipe makes the game change stage, so those windows never count as stage rates). A window's
    deaths go to the stage that failed in it, else to its stage when it is valid."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for w in window_list:
        if not w.get('hero_deaths') or not w['wall_s'] or w['wall_s'] > 600:
            continue
        failed = list(w.get('all_fails') or {})
        stage = int(failed[0]) if len(failed) == 1 else (w['stage'] if w['valid'] else None)
        if stage is None:
            continue
        row = out.setdefault(stage, {'deaths': 0, 'recent': 0, 'last_utc': None})
        row['deaths'] += w['hero_deaths']
        if (now - datetime.fromisoformat(w['end_utc'])).total_seconds() <= RECENT_DEATH_HOURS * 3600:
            row['recent'] += w['hero_deaths']
        row['last_utc'] = max(row['last_utc'] or w['end_utc'], w['end_utc'])
    return out


def bonus_factors(snapshot, catalog):
    """Player multipliers on monster gold/XP (D009): (1 + rune %) x (1 + pet / 1000), plus the flat
    per-kill and per-boss runes. Gold matched within ~2% per kill; XP also has a stage term we do not know."""
    if not snapshot:
        return None
    runes = rune_totals(snapshot, catalog)
    value = lambda stat: ((runes.get(stat) or {}).get('effect') or {}).get('value') or 0
    pet_key = (snapshot.get('power') or {}).get(('pet',)) if 'power' in snapshot else snapshot.get('pet')
    pet = (catalog.index('PetInfoData', 'PetKey').get(str(pet_key)) or {}).get('StatDataKey')
    pet_value = lambda stat: sum(float(r['Value']) for r in catalog.table('PetStatInfoData')
                                 if r['PetStatKey'] == pet and r['STATTYPE'] == stat)
    return {'gold': (1 + value('IncreaseGoldAmount') / 100) * (1 + pet_value('IncreaseGoldAmount') / 1000),
            'xp': (1 + value('IncreaseExpAmount') / 100) * (1 + pet_value('IncreaseExpAmount') / 1000),
            'gold_per_kill': value('AdditionalGoldNormalMonster'), 'gold_per_boss': value('AdditionalGoldStageBoss'),
            'xp_per_kill': value('AdditionalExpNormalMonster'), 'xp_per_boss': value('AdditionalExpStageBoss')}


FRONT_MIN_DROPS, FRONT_SHARE = 30, 0.6


def front_hero(runs):
    """The hero enemies reach: most HP drops over the latest recorded runs (D009: Priest 1262 vs 9 and 5).
    None until enough drops are recorded or when no hero clearly takes most of them."""
    totals = {}
    for run in sorted(runs, key=lambda r: r['ended_utc'])[-30:]:
        for key, h in ((run.get('combat') or {}).get('heroes') or {}).items():
            totals[int(key)] = totals.get(int(key), 0) + (h.get('hp_drops') or 0)
    total = sum(totals.values())
    if total < FRONT_MIN_DROPS:
        return None
    key, drops = max(totals.items(), key=lambda kv: kv[1])
    return key if drops >= FRONT_SHARE * total else None


def survival_reference(evidence):
    """Worst boss hit (fraction of a hero's HP, current stats) on played stages with and without deaths."""
    died, clean = [], []
    for e in evidence:
        frac = (e.get('threat') or {}).get('boss_fraction')
        if frac is None:
            continue
        # Only recent deaths count against today's stats; old ones were with weaker heroes.
        if e.get('deaths_recent') or e.get('runs_with_deaths'):
            died.append((frac, e['label']))
        elif not e.get('deaths_total') and ((e.get('save_minutes') or 0) >= SAFE_MINUTES or (e.get('combat_runs') or 0) >= 3):
            clean.append((frac, e['label']))
    return {'deaths_from': min(died) if died else None, 'no_deaths_up_to': max(clean) if clean else None}


def party_build(state, party, attribute_hero):
    """The part of a power state that acts on `party` (hero keys as strings): runes and pet, plus the
    gear, skills and attribute points of its heroes. None when the state is unknown."""
    if state is None:
        return None
    hero = lambda k: attribute_hero.get(str(k[1])) if k[0] == 'attribute' else str(k[1])
    return {k: v for k, v in state.items() if k[0] in ('rune', 'pet') or hero(k) in party}


class BuildTimeline:
    """Party builds over the saves, to tell whether a run was played with a given build. A change and its
    reversal (a hero leaving and rejoining, a skill unequipped and equipped again) is no change."""

    def __init__(self, snapshots, catalog):
        self.snapshots = snapshots
        self.times = [s['last_saved_utc'] for s in snapshots]
        self.attribute_hero = {k: r['HeroKey'] for k, r in catalog.index('AttributeInfoData', 'AttributeKey').items()}
        self._builds = {}

    def builds(self, party):
        party = party_key(party)
        if party not in self._builds:
            heroes = set(party.split(',')) - {''}
            self._builds[party] = [party_build(s.get('power'), heroes, self.attribute_hero) for s in self.snapshots]
        return self._builds[party]

    def current(self, party):
        builds = self.builds(party)
        return builds[-1] if builds else None

    def during(self, run, party=None):
        """Builds of the saves around a run: the last one at or before its start up to the first at or after its end."""
        builds = self.builds(party if party is not None else run['party'])
        first = bisect_right(self.times, run['started_utc']) - 1
        last = min(bisect_left(self.times, run['ended_utc']), len(builds) - 1)
        return builds[first:last + 1] if first >= 0 else [None]

    def matches(self, run, build, party=None):
        return build is not None and all(b == build for b in self.during(run, party))


def stage_comparison(store, catalog, hours=72, locale='en-US', reading=None):
    snapshots = recent_snapshots(store, hours)
    window_list = windows(snapshots, catalog.level_thresholds)
    save_based = stage_rates(window_list)
    for row in save_based:
        row['stage_label'] = catalog.stage_label(row['stage'], locale)
        row['party_names'] = [catalog.hero_name(k, locale) for k in row['party']]
        row['xp_per_h'] = {h: {'name': catalog.hero_name(h, locale), 'value': v} for h, v in row['xp_per_h'].items()}
    runtime_based = observed_run_stats(store, catalog, hours, locale)
    runs = [decorate_run(r, catalog, locale) for r in complete_runs(store, catalog, hours)]
    evidence = stage_evidence(runs, save_based, lambda stage: catalog.stage_label(stage, locale))
    by_key = {(e['stage'], e['party']): e for e in evidence}
    for row in save_based:
        # Exact counters, but the confidence of a stage rate comes from its complete runs.
        match = by_key.get((row['stage'], party_key(row['party'])))
        row['confidence'] = match['gold_confidence'] if match else 'insufficient'
        row['runs'] = match['runs'] if match else 0
    changes = power_changes(snapshots)
    timeline = BuildTimeline(snapshots, catalog)
    for e in evidence:
        e['party_names'] = [catalog.hero_name(k, locale) for k in e['party'].split(',')] if e['party'] else []
        matching = [r for r in runs if r['stage_key'] == e['stage'] and party_key(r['party']) == e['party']]
        # A rate measured before later power changes (gear, runes, points) understates the stage today.
        # Only the net difference to the build of its last run counts: changes undone since then are none.
        later = [c for c in changes if e['last_run_utc'] and
                 datetime.fromisoformat(c['to_utc']) > datetime.fromisoformat(e['last_run_utc'])]
        last = max(matching, key=lambda r: r['ended_utc'], default=None)
        now_build, then = timeline.current(e['party']), timeline.during(last, e['party'])[-1] if last else None
        if now_build is not None and then is not None:
            default = lambda k: 0 if k[0] in ('rune', 'attribute') else None
            moved = {k for k in set(now_build) | set(then) if now_build.get(k, default(k)) != then.get(k, default(k))}
            later = [c for c in later if moved & set(c['changes'])]
            e['changes_since_kinds'] = sorted({k[0] for k in moved})
        else:
            e['changes_since_kinds'] = sorted({k[0] for c in later for k in c['changes']})
        e['changes_since'] = len(later) if e['last_run_utc'] else None
        first = min((r['started_utc'] for r in matching), default=None)
        e['changes_during'] = sum(first is not None and c['to_utc'] >= first
                                  and c['from_utc'] <= e['last_run_utc'] for c in changes)
        levels = {tuple(sorted((k, v.get(endpoint)) for k, v in r['xp'].items()))
                  for r in matching for endpoint in ('level_start', 'level_end')}
        e['mixed_levels'] = len(levels) > 1
        e['current_build_comparable'] = bool(matching) and not (
            e['changes_since'] or e['changes_during'] or e['mixed_levels'])
    theory = theoretical_stage_table(catalog)
    max_stage = max((s['max_completed_stage'] or 0 for s in snapshots), default=0)
    bonus = bonus_factors(snapshots[-1] if snapshots else None, catalog)
    heroes, k = live_heroes(reading), (reading or {}).get('armor_constants')
    names = {h['hero_key']: catalog.hero_name(h['hero_key'], locale) for h in heroes}

    current = (reading or {}).get('stage_key')
    front = front_hero(runs)
    now_t = party_threat(catalog, current, heroes, k) if heroes and k and current else None

    def threat(stage):
        t = party_threat(catalog, stage, heroes, k) if heroes and k else None
        if not t:
            return None

        def vs_now(key, v):
            # How much harder this stage's boss hits the same hero than the boss of the stage played now.
            here = ((now_t or {}).get('heroes', {}).get(key) or {}).get('boss') or {}
            there = v.get('boss') or {}
            return there['hit'] / here['hit'] if here.get('hit') and there.get('hit') is not None else None
        rows = [{'hero_key': key, 'name': names[key], 'boss_hit': (v.get('boss') or {}).get('hit'),
                 'boss_fraction': (v.get('boss') or {}).get('fraction'), 'boss_hits_to_die': (v.get('boss') or {}).get('hits_to_die'),
                 'normal_hit': (v.get('normal') or {}).get('hit'), 'normal_fraction': (v.get('normal') or {}).get('fraction'),
                 'cap_uncertain': bool((v.get('boss') or {}).get('cap_uncertain') or (v.get('normal') or {}).get('cap_uncertain')),
                'vs_current': vs_now(key, v)}
                for key, v in t['heroes'].items()]
        ratios = [r['vs_current'] for r in rows if r['vs_current'] is not None]
        lead = next((r for r in rows if r['hero_key'] == front), None)
        # The front hero's boss hit when known (it takes almost every hit), else the worst hero's.
        return {'worst_boss_fraction': t['worst_boss_fraction'], 'heroes': rows, 'front': front if lead else None,
                'boss_fraction': lead['boss_fraction'] if lead else t['worst_boss_fraction'],
                'vs_current': lead['vs_current'] if lead else (max(ratios) if ratios else None)}
    deaths = deaths_by_stage(window_list)
    for e in evidence:
        e['threat'] = threat(e['stage'])
        d = deaths.get(e['stage']) or {}
        e['deaths_total'], e['deaths_recent'], e['last_death_utc'] = d.get('deaths', 0), d.get('recent', 0), d.get('last_utc')
    for t in theory:
        t['label'] = catalog.stage_label(t['stage'], locale)
        if bonus:
            kills = t['waves'] * t['monsters_per_wave']
            # Rewards with the current runes and pet; the boss kill is included in the catalog value.
            t['gold_per_clear_you'] = t['gold_per_clear'] * bonus['gold'] + kills * bonus['gold_per_kill'] + bonus['gold_per_boss']
            t['exp_per_clear_you'] = t['exp_per_clear'] * bonus['xp'] + kills * bonus['xp_per_kill'] + bonus['xp_per_boss']
        t['reachable'] = t['stage'] <= max_stage if max_stage and t['type'] == 'NORMAL' else None
        t['observed'] = any(r['stage'] == t['stage'] for r in save_based + runtime_based)
    # Unplayed regular stages, ranked per hour: the fit uses the party of the most recent complete run.
    recent_party = max(runs, key=lambda r: r['ended_utc'])['party'] if runs else None
    # Party output now: the latest run that recorded it (the model only uses it when it predicts better).
    powered = [r for r in runs if r['party'] == recent_party and (r.get('combat') or {}).get('party_offence')]
    offence = max(powered, key=lambda r: r['ended_utc'])['combat']['party_offence'] if powered else None
    model = stagemodel.fit([e for e in evidence if e['party'] == recent_party], theory, offence)
    candidates = []
    for t in theory:
        if t['type'] != 'NORMAL' or t['observed']:
            continue
        t['estimate'] = stagemodel.estimate(model, t) if model else None
        t['threat'] = threat(t['stage'])
        est = t['estimate']
        # Far outside the observed monster strength the fit is an extrapolation: leave those out.
        if est and est['dmg_vs_observed'] and est['dmg_vs_observed'] > EXTRAPOLATION_LIMIT:
            continue
        if est or t['reachable']:
            candidates.append(t)
    if model:
        candidates.sort(key=lambda t: -((t['estimate'] or {}).get('gold_h') or 0))
    else:
        candidates.sort(key=lambda t: -t['gold_per_clear'])
    current_party = party_key(snapshots[-1]['party']) if snapshots else recent_party
    # Current build: runs played with the loadout of today (whenever that was, so an undone change does
    # not reset it) and every hero within LEVEL_TOLERANCE levels of today.
    current_build = timeline.current(current_party) if current_party else None
    current_levels = {str(h['hero_key']): h['level'] for h in snapshots[-1]['heroes']
                      if h['hero_key'] in snapshots[-1]['party']} if snapshots else {}

    def near_levels(run):
        return set(run['xp']) == set(current_levels) and all(
            e.get(end) is not None and abs(e[end] - current_levels[h]) <= LEVEL_TOLERANCE
            for h, e in run['xp'].items() for end in ('level_start', 'level_end'))
    current_runs = [r for r in runs if r['party'] == current_party and near_levels(r)
                    and timeline.matches(r, current_build, current_party)]
    cutoff = min((r['started_utc'] for r in current_runs), default=None)
    current_evidence = stage_evidence(current_runs, [], lambda stage: catalog.stage_label(stage, locale))
    for e in current_evidence:
        e['party_names'] = [catalog.hero_name(k, locale) for k in e['party'].split(',')]
        e['threat'] = threat(e['stage'])
        e['current_build_comparable'] = True
        e['changes_since'] = 0
        e['changes_during'] = 0
        e['mixed_levels'] = len({tuple(sorted((h, x.get('level_start')) for h, x in r['xp'].items()))
                                 for r in current_runs if r['stage_key'] == e['stage']}) > 1
    best_gold, gold_ties, gold_promising = best_stage(current_evidence, 'gold_h')
    best_xp, xp_ties, xp_promising = best_stage(current_evidence, 'xp_h')
    return {
        'save_rates': save_based, 'runtime_rates': runtime_based, 'theoretical': theory, 'evidence': evidence,
        'current_evidence': current_evidence, 'current_cohort_since': cutoff,
        'max_completed_stage': max_stage,
        'recommendation': {
            'gold': best_gold, 'gold_ties': gold_ties, 'gold_to_confirm': gold_promising[:5],
            'xp': best_xp, 'xp_ties': xp_ties, 'xp_to_confirm': xp_promising[:5],
            'untested_candidates': candidates[:8], 'model': model,
            'bonus': bonus, 'survival_reference': survival_reference(evidence),
            'current_stage': current,
            'threat_note': ('Boss hit: HP one boss attack takes from each hero with the stats read now (game formula '
                            'checked on real hits; block, dodge, healing and several enemies at once not included). '
                            'The front hero takes almost all hits (Priest 1262 vs 9 and 5 in a 20-min capture): it is '
                            'found from the hits recorded in recent runs, and until enough are recorded the worst-off '
                            'hero is shown instead, which overstates the danger. "×N vs now" compares with the boss '
                            f'of the stage played now. Deaths older than {RECENT_DEATH_HOURS} h are shown but not used '
                            'against today\'s stats.') if heroes and k else None,
            'model_note': stagemodel.describe(model) + (
                f' Stages whose monsters hit more than {EXTRAPOLATION_LIMIT:g}× harder than any played stage '
                'are left out.' if model else ''),
            'note': describe() + ' Recommendations use only runs after the latest recorded power change, '
                    'at the latest saved hero levels. Historical tables retain older observations.',
        },
        'windows_total': len(window_list), 'windows_valid': sum(1 for w in window_list if w['valid']),
    }


def economy(store, catalog, hours=24, locale='en-US'):
    snapshots = recent_snapshots(store, hours)
    window_list = windows(snapshots, catalog.level_thresholds)
    totals = {'gold_earned_total': 0, 'gold_monster': 0, 'gold_alchemy': 0, 'gold_offline': 0,
              'implied_spend': 0, 'seconds': 0.0}
    for w in window_list:
        for k in ('gold_earned_total', 'gold_monster', 'gold_alchemy', 'gold_offline', 'implied_spend'):
            if w[k] is None or (k != 'implied_spend' and w[k] < 0):
                totals[k] = None
            elif totals[k] is not None:
                totals[k] += w[k]
        totals['seconds'] += w['wall_s'] or 0
    spends = store.query("SELECT utc, payload, run_id FROM events WHERE kind = 'gold_spend' AND utc >= ? "
                         "ORDER BY id DESC LIMIT 200", (since_iso(hours),))
    for s in spends:
        s['payload'] = json.loads(s['payload'])
    for w in window_list:
        w['stage_label'] = catalog.stage_label(w['stage'], locale) if w['stage'] else None
    return {'windows': window_list[-300:], 'totals': totals, 'spend_events': spends,
            'note': ('Exact totals from game GoldEarn counters (subtotals by source: '
                     'MonsterKill, CubeAlchemy, OfflineReward; the total includes other sources). Implied spending = '
                     'income minus balance change. Live spending events are balance drops between samples.')}


def recent_rates(store, catalog, minutes=10, locale='en-US'):
    """Rolling XP/h per hero and gold income from stored samples (current session only)."""
    session = store.one('SELECT id FROM sessions ORDER BY id DESC LIMIT 1')
    if not session:
        return None
    start = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    rows = store.query('SELECT utc, mono, gold, heroes FROM samples WHERE session_id = ? AND utc >= ? ORDER BY id',
                       (session['id'], start))
    if len(rows) < 2:
        return None
    thresholds = catalog.level_thresholds
    xp, complete = {}, {}
    gold_complete = True
    gold_in = gold_out = 0
    for a, b in zip(rows, rows[1:]):
        if a['gold'] is not None and b['gold'] is not None:
            d = b['gold'] - a['gold']
            gold_in += max(d, 0)
            gold_out += max(-d, 0)
        else:
            gold_complete = False
        ha = {h['hero_key']: h for h in json.loads(a['heroes'] or '[]')}
        hb = {h['hero_key']: h for h in json.loads(b['heroes'] or '[]')}
        for key in set(ha) ^ set(hb):
            complete[str(key)] = False
        for h in hb.values():
            old = ha.get(h['hero_key'])
            if not old:
                continue
            gain, _ = xp_gained(old['level'], old['xp'], h['level'], h['xp'], thresholds)
            key = str(h['hero_key'])
            if gain is None:
                complete[key] = False
            else:
                xp[key] = xp.get(key, 0.0) + gain
                complete.setdefault(key, True)
    seconds = rows[-1]['mono'] - rows[0]['mono']
    if seconds <= 0:
        return None
    return {'minutes': round(seconds / 60, 1), 'gold_income_per_h_est': gold_in * 3600 / seconds if gold_complete else None,
            'gold_spend_window': gold_out if gold_complete else None,
            'xp_per_h': {k: {'name': catalog.hero_name(k, locale), 'value': v * 3600 / seconds if complete.get(k) else None,
                             'complete': complete.get(k, False)} for k, v in xp.items()}}
