"""Exact-counter analysis from consecutive save snapshots.

The game's own cumulative counters (GoldEarn by source, StageClear/Fail, kills, boxes)
make a save-to-save window exact for *totals*, independent of sampling. A window is
used for stage rates only when the game ran continuously (play time advanced like wall
time), with the same stage and party at both ends.
"""
from datetime import datetime

from ..save.model import aggregate, stage_counters
from .progress import xp_gained

MAX_WINDOW_SECONDS = 600
PLAYTIME_TOLERANCE = 20


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _delta(a, b, *key):
    va, vb = aggregate(a, *key), aggregate(b, *key)
    return None if va is None or vb is None else vb - va


def save_window(a, b, thresholds):
    ta, tb = _ts(a['last_saved_utc']), _ts(b['last_saved_utc'])
    wall = tb - ta if ta is not None and tb is not None else None
    play = b['play_time'] - a['play_time'] if None not in (a['play_time'], b['play_time']) else None
    reasons = []
    if wall is None or wall <= 0:
        reasons.append('invalid time')
    elif wall > MAX_WINDOW_SECONDS:
        reasons.append('long window (possibly offline)')
    if play is None:
        reasons.append('play time unknown')
    if wall and play is not None and abs(play - wall) > PLAYTIME_TOLERANCE:
        reasons.append('play time does not track wall time')
    if a['current_stage'] != b['current_stage']:
        reasons.append('stage changed')
    if sorted(a['party']) != sorted(b['party']):
        reasons.append('party changed')
    heroes_a = {h['hero_key']: h for h in a['heroes']}
    xp = {}
    for hero in b['heroes']:
        old = heroes_a.get(hero['hero_key'])
        if not old or hero['hero_key'] not in b['party']:
            continue
        gain, kind = xp_gained(old['level'], old['xp'], hero['level'], hero['xp'], thresholds)
        xp[str(hero['hero_key'])] = {'gain': gain, 'kind': kind, 'level': hero['level']}
    counters_a, counters_b = stage_counters(a), stage_counters(b)
    stage = b['current_stage']
    sa, sb = counters_a.get(stage, {}), counters_b.get(stage, {})
    gold_total = _delta(a, b, 2, 0)
    for key in ((2, 0), (2, 1), (2, 2), (2, 3), (0, 0), (1, 0), (3, 0), (4, 0)):
        delta = _delta(a, b, *key)
        if delta is not None and delta < 0:
            reasons.append('counter decreased')
            break
    for key in set(counters_a) | set(counters_b):
        for field in ('clears', 'fails'):
            delta = counters_b.get(key, {}).get(field, 0) - counters_a.get(key, {}).get(field, 0)
            if delta < 0:
                reasons.append('stage counter decreased')
            elif delta and key != stage:
                reasons.append('other stage played inside window')
    if (_delta(a, b, 2, 3) or 0) > 0:
        reasons.append('offline rewards inside window')
    if a.get('power') != b.get('power'):
        reasons.append('loadout changed')
    balance = (b['gold'] - a['gold']) if a['gold'] is not None and b['gold'] is not None else None
    return {
        'start_utc': a['last_saved_utc'], 'end_utc': b['last_saved_utc'], 'wall_s': wall, 'play_s': play,
        'stage': stage, 'party': sorted(b['party']), 'valid': not reasons, 'reasons': reasons,
        'gold_earned_total': gold_total,
        'gold_monster': _delta(a, b, 2, 1), 'gold_alchemy': _delta(a, b, 2, 2), 'gold_offline': _delta(a, b, 2, 3),
        'balance_delta': balance,
        'implied_spend': gold_total - balance if gold_total is not None and balance is not None else None,
        'kills': _delta(a, b, 0, 0), 'boxes': _delta(a, b, 3, 0), 'items': _delta(a, b, 4, 0),
        'hero_deaths': _delta(a, b, 1, 0),
        'clears': sb.get('clears', 0) - sa.get('clears', 0), 'fails': sb.get('fails', 0) - sa.get('fails', 0),
        'all_clears': {k: v['clears'] - counters_a.get(k, {}).get('clears', 0) for k, v in counters_b.items()
                       if v['clears'] != counters_a.get(k, {}).get('clears', 0)},
        'all_fails': {k: v['fails'] - counters_a.get(k, {}).get('fails', 0) for k, v in counters_b.items()
                      if v['fails'] != counters_a.get(k, {}).get('fails', 0)},
        'xp': xp,
    }


def windows(snapshots, thresholds):
    ordered = sorted(snapshots, key=lambda s: s['last_saved_utc'] or '')
    return [save_window(a, b, thresholds) for a, b in zip(ordered, ordered[1:])]


def stage_rates(window_list):
    """Aggregate valid windows by (stage, party). Rates per hour of observed play."""
    groups = {}
    for w in window_list:
        if not w['valid']:
            continue
        key = (w['stage'], ','.join(map(str, w['party'])))
        g = groups.setdefault(key, {'stage': w['stage'], 'party': w['party'], 'seconds': 0.0, 'windows': 0,
                                    'gold_monster': 0, 'gold_total': 0, 'clears': 0, 'fails': 0, 'kills': 0,
                                    'boxes': 0, 'hero_deaths': 0, 'xp': {}, 'xp_incomplete': set(),
                                    'incomplete': set()})
        g['seconds'] += w['wall_s']
        g['windows'] += 1
        for field, source in (('gold_monster', 'gold_monster'), ('gold_total', 'gold_earned_total'),
                              ('clears', 'clears'), ('fails', 'fails'), ('kills', 'kills'),
                              ('boxes', 'boxes'), ('hero_deaths', 'hero_deaths')):
            if w[source] is None:
                g['incomplete'].add(field)
            else:
                g[field] += w[source]
        g['xp_incomplete'].update(str(h) for h in w['party'] if str(h) not in w['xp'])
        for hero, entry in w['xp'].items():
            if entry['gain'] is None:
                g['xp_incomplete'].add(hero)
            else:
                g['xp'][hero] = g['xp'].get(hero, 0.0) + entry['gain']
    result = []
    for g in groups.values():
        for field in g['incomplete']:
            g[field] = None
        hours = g['seconds'] / 3600
        attempts = g['clears'] + g['fails']
        result.append({
            'stage': g['stage'], 'party': g['party'], 'windows': g['windows'], 'minutes': round(g['seconds'] / 60, 1),
            'gold_monster_per_h': g['gold_monster'] / hours if hours and g['gold_monster'] is not None else None,
            'gold_total_per_h': g['gold_total'] / hours if hours and g['gold_total'] is not None else None,
            'clears': g['clears'], 'fails': g['fails'],
            'fail_rate': g['fails'] / attempts if attempts else None,
            'clears_per_h': g['clears'] / hours if hours else None,
            'kills_per_h': g['kills'] / hours if hours and g['kills'] is not None else None,
            'boxes_per_h': g['boxes'] / hours if hours and g['boxes'] is not None else None,
            'hero_deaths': g['hero_deaths'],
            'xp_per_h': {h: v / hours for h, v in g['xp'].items() if h not in g['xp_incomplete']} if hours else {},
            'xp_incomplete_heroes': sorted(g['xp_incomplete']),
            'source': 'save-counters',
        })
    return sorted(result, key=lambda r: -(r['gold_monster_per_h'] or 0))


def theoretical_stage_table(catalog):
    """Base rewards per clear from catalog columns: normal kills = WaveAmount x WaveMonsterAmount,
    weighted mean of `Monsters` rewards, StageLevel and boss multipliers as per mille. The per-mille
    scale is confirmed live (D001 HP, D009 damage, gold per kill and boss HP/gold); the kill count and
    the monster mix are still assumptions. Player bonuses (runes, pet, ~x2.8 here) are not applied:
    observed rates calibrate them (stagemodel)."""
    table = []
    for key, stage in catalog.stages.items():
        level = catalog.stage_levels.get(stage['StageLevel'])
        if not level or not stage['Monsters']:
            continue
        pairs = [p.split('_') for p in stage['Monsters'].split()]
        weights = [(catalog.monsters.get(m), int(w)) for m, w in pairs]
        weights = [(m, w) for m, w in weights if m]
        total_weight = sum(w for _, w in weights)
        if not total_weight:
            continue
        mean_gold = sum(int(m['RewardGold']) * w for m, w in weights) / total_weight
        mean_exp = sum(int(m['RewardExp']) * w for m, w in weights) / total_weight
        normal = int(stage['WaveAmount'] or 0) * int(stage['WaveMonsterAmount'] or 0)
        boss = catalog.monsters.get(stage['BossMonsterKey'])
        gold_mult = int(level['MonsterGoldMultiplier']) / 1000
        exp_mult = int(level['MonsterExpMultiplier']) / 1000
        boss_gold = int(boss['RewardGold']) * int(stage['BossGoldMultiplier'] or 1000) / 1000 if boss else 0
        boss_exp = int(boss['RewardExp']) * int(stage['BossExpMultiplier'] or 1000) / 1000 if boss else 0
        table.append({
            'stage': int(key), 'act': stage['Act'], 'no': stage['StageNo'], 'difficulty': stage['STAGEDIFFICULITY'],
            'type': stage['STAGETYPE'], 'stage_level': int(stage['StageLevel']),
            'waves': int(stage['WaveAmount'] or 0), 'monsters_per_wave': int(stage['WaveMonsterAmount'] or 0),
            'gold_per_clear': (normal * mean_gold + boss_gold) * gold_mult,
            'exp_per_clear': (normal * mean_exp + boss_exp) * exp_mult,
            'monster_hp_mult': int(level['MonsterHpMultiplier']) / 1000,
            'monster_dmg_mult': int(level['MonsterAtkDmgMultiplier']) / 1000,
            'source': 'catalog (base rewards, before player bonuses)',
        })
    return sorted(table, key=lambda r: r['stage'])
