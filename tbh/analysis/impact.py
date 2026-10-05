"""Measured effect of power changes on real runs: before vs after, same stage and party.

A power change is anything in the save that can make the party stronger: rune levels, the gear
equipped by party heroes (a different item in a slot, or the same item enchanted/decorated),
attribute points, skills and the pet. It is seen between two consecutive saves (the game saves
about once a minute), so it happened somewhere in (previous save, save]. Runs that finished before
that window are "before"; runs that started after it are "after"; a run that spans it is left out.
Changes with no complete run between them are merged, because nothing can tell them apart.
Each side stops at the neighbouring change, so a comparison never mixes two changes.

Hero level-ups also make runs faster and cannot be excluded without discarding almost every
window (at 3-9 a hero levels up every ~8 runs), so they are counted and reported with the result.
The same statistics as the stage rates apply: rates are sum(value) / sum(time) with a standard
error, and a change counts only when it exceeds Z combined standard errors.
"""
import statistics
from datetime import datetime

from .stagestats import MIN_RUNS, Z, party_key, ratio_rate, run_xp

MAX_RUNS = 20   # runs per side, nearest to the change (older runs drift with hero progress)


def _t(iso):
    return datetime.fromisoformat(iso)


ITEM_FIELDS = ('item_key', 'enchants', 'enchant_count', 'decoration', 'engraving', 'inscription', 'chaotic')


def power_state(snapshot):
    """Everything in a save that can change how strong the party is, as {(kind, ...): value}.

    None when the save has no rune data (unknown, not "every rune at level 0")."""
    if not snapshot.get('runes'):
        return None
    state = {('rune', k): v for k, v in snapshot['runes'].items()}
    items = snapshot.get('items') or {}
    for hero in snapshot.get('heroes') or []:
        if hero['hero_key'] not in (snapshot.get('party') or []):
            continue
        for slot, uid in enumerate(hero.get('equipped') or []):
            instance = items.get(uid) if uid else None
            state[('gear', hero['hero_key'], slot)] = (uid, instance['item_key'] if instance else None)
            if instance:
                state[('upgrade', hero['hero_key'], slot)] = repr([instance.get(f) for f in ITEM_FIELDS])
        for index, skill in enumerate(hero.get('skills') or []):
            state[('skill', hero['hero_key'], index)] = skill if skill and skill > 0 else None   # -1/0: empty slot
    for key, level in (snapshot.get('attributes') or {}).items():
        state[('attribute', key)] = level
    state[('pet',)] = snapshot.get('pet')
    return state


def power_changes(snapshots):
    """Power changes between consecutive saves, oldest first: {key: (old, new)} per event."""
    events, prev = [], None
    for snap in snapshots:
        state = snap.get('power')
        if state is None:
            continue
        if prev is not None:
            old_state = prev[1]
            changes = {}
            for key in set(state) | set(old_state):
                default = 0 if key[0] in ('rune', 'attribute') else None
                old, new = old_state.get(key, default), state.get(key, default)
                if old == new:
                    continue
                if key[0] == 'upgrade' and old_state.get(('gear',) + key[1:]) != state.get(('gear',) + key[1:]):
                    continue   # a different item in the slot is already reported as a gear change
                changes[key] = (old, new)
            if changes:
                events.append({'from_utc': prev[0], 'to_utc': snap['last_saved_utc'], 'changes': changes})
        prev = (snap['last_saved_utc'], state)
    return events


def _group(events, runs):
    """Merge purchases with no complete run finished between them."""
    groups = []
    for e in events:
        if groups:
            last = groups[-1]
            between = any(_t(last['to_utc']) <= _t(r['started_utc']) and _t(r['ended_utc']) <= _t(e['from_utc'])
                          for r in runs)
            if not between:
                last['to_utc'] = e['to_utc']
                for k, (old, new) in e['changes'].items():
                    last['changes'][k] = (last['changes'].get(k, (old, new))[0], new)
                continue
        groups.append({'from_utc': e['from_utc'], 'to_utc': e['to_utc'], 'changes': dict(e['changes'])})
    # A skill unequipped and equipped again, or a hero leaving and rejoining the party, is no net change.
    for g in groups:
        g['changes'] = {k: v for k, v in g['changes'].items() if v[0] != v[1]}
    return [g for g in groups if g['changes']]


def _mean_se(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, None
    se = statistics.stdev(values) / len(values) ** 0.5 if len(values) > 1 else None
    return statistics.mean(values), se


def _delta(a, a_se, b, b_se, n_a, n_b):
    """Relative change from a to b with its standard error and significance."""
    if a is None or b is None or not a:
        return None
    result = {'before': a, 'after': b, 'pct': (b / a - 1) * 100, 'pct_se': None, 'significant': False,
              'enough_runs': n_a >= MIN_RUNS and n_b >= MIN_RUNS}
    if a_se is not None and b_se is not None:
        result['pct_se'] = (b / a) * ((a_se / a) ** 2 + (b_se / b) ** 2) ** 0.5 * 100
        result['significant'] = result['enough_runs'] and abs(b - a) > Z * (a_se ** 2 + b_se ** 2) ** 0.5
    return result


def compare(before, after):
    """Gold/h, XP/h per hero, gold per run and run time, after vs before."""
    out = {}
    for name, pair in (('gold_h', lambda r: (r['gold_gain_est'], r['duration_s'])),
                       ('xp_h', lambda r: (run_xp(r), r['duration_s']))):
        a, b = ratio_rate(map(pair, before)), ratio_rate(map(pair, after))
        out[name] = _delta(a and a['per_h'], a and a['se_h'], b and b['per_h'], b and b['se_h'],
                           a['runs'] if a else 0, b['runs'] if b else 0)
    for name, key in (('gold_per_run', 'gold_gain_est'), ('duration_s', 'duration_s')):
        (a, a_se), (b, b_se) = _mean_se(r[key] for r in before), _mean_se(r[key] for r in after)
        out[name] = _delta(a, a_se, b, b_se, len(before), len(after))
    return out


def stats_at(events, utc):
    """Party stats before buffs at `utc`: the latest `hero_stats` event at or before it, or None.
    `events` are (utc, payload) sorted by time; the collector stores one whenever the stats change."""
    state = None
    for when, payload in events:
        if _t(when) > _t(utc):
            break
        state = payload
    return state


def stat_change(events, from_utc, to_utc, shown):
    """{hero_key: {stat: {'before', 'after', 'pct'}}} between the saves around a change, or None when
    there is no stat snapshot on both sides (before the combat reader, or the game was not read).
    Everything that changed in between is in it, level-ups included."""
    before, after = stats_at(events, from_utc), stats_at(events, to_utc)
    if not before or not after or before is after:
        return None
    out = {}
    for hero, stats in after.items():
        old = before.get(hero)
        if not old:
            continue
        diff = {}
        for stat in shown:
            a, b = old.get(stat), stats.get(stat)
            if a is None or b is None:
                continue
            if a != b:
                diff[stat] = {'before': a, 'after': b, 'pct': (b / a - 1) * 100 if a else None}
        if diff:
            out[hero] = diff
    return out


def _levels(run, end=False):
    return {h: e.get('level_end' if end else 'level_start') for h, e in (run.get('xp') or {}).items()}


def purchase_impact(snapshots, runs, max_runs=MAX_RUNS):
    """One entry per (merged) power change, newest first. `snapshots` carry `power` (see power_state)
    and `runs` are complete runs with `xp` decoded."""
    runs = sorted(runs, key=lambda r: _t(r['started_utc']))
    groups = _group(power_changes(snapshots), runs)
    out = []
    for i, g in enumerate(groups):
        lo = _t(groups[i - 1]['to_utc']) if i else None
        hi = _t(groups[i + 1]['from_utc']) if i + 1 < len(groups) else None
        start, end = _t(g['from_utc']), _t(g['to_utc'])
        before = [r for r in runs if (lo is None or lo <= _t(r['started_utc'])) and _t(r['ended_utc']) <= start]
        after = [r for r in runs if end <= _t(r['started_utc']) and (hi is None or _t(r['ended_utc']) <= hi)]
        keys = {(r['stage_key'], party_key(r['party'])) for r in before + after}

        def side(rows, key):
            return [r for r in rows if (r['stage_key'], party_key(r['party'])) == key]
        # The stage/party with the most runs on its thinner side; ties favour where play continued.
        key = max(keys, key=lambda k: (min(len(side(before, k)), len(side(after, k))), len(side(after, k))),
                  default=None)
        b, a = (side(before, key)[-max_runs:], side(after, key)[:max_runs]) if key else ([], [])
        level_ups = sum(e.get('level_ups') or 0 for r in b + a for e in (r.get('xp') or {}).values())
        if b and a and _levels(b[-1], end=True) != _levels(a[0]):
            level_ups += 1   # a level changed between the last run before and the first run after
        # Runs before cannot be added later: with too few of them the purchase can never be measured.
        status = ('no comparable runs' if not b else
                  'too few runs before' if len(b) < MIN_RUNS else
                  'collecting' if len(a) < MIN_RUNS else 'measured')
        signatures = {tuple(sorted(_levels(r, end=end).items())) for r in b + a for end in (False, True)}
        stable_levels = bool(b and a and len(signatures) == 1 and
                             all(v is not None for _, v in next(iter(signatures))) and
                             all(set(_levels(r)) == set(party_key(r['party']).split(',')) for r in b + a))
        validation = {'stable_levels': stable_levels, 'simultaneous_changes': len(g['changes']),
                      'status': 'insufficient_runs' if status != 'measured' else
                                'confounded_levels' if not stable_levels else
                                'multiple_changes' if len(g['changes']) != 1 else 'observational_comparison',
                      'note': 'Before/after association, not an isolated causal effect or prospective prediction test.'}
        out.append({'from_utc': g['from_utc'], 'to_utc': g['to_utc'], 'changes': g['changes'],
                    'stage': key[0] if key else None, 'party': key[1] if key else None,
                    'runs_before': len(b), 'runs_after': len(a), 'runs_needed': max(0, MIN_RUNS - len(a)),
                    'level_ups': level_ups, 'status': status, 'validation': validation,
                    'metrics': compare(b, a) if b and a else {}})
    return list(reversed(out))
