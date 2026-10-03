"""XP accounting across level-ups.

The carry-over loop is independently confirmed in build 25454993's native code:
subtract LevelInfoData[L].ExpForLevelUp, increment L, repeat while enough XP remains.
"""
import math


def _nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def xp_gained(before_level, before_xp, after_level, after_xp, thresholds):
    """Return (gain, kind). gain is None when the change cannot be explained."""
    if None in (before_level, before_xp, after_level, after_xp):
        return None, 'unknown'
    if (not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in (before_level, after_level))
            or not all(_nonnegative(v) for v in (before_xp, after_xp))):
        return None, 'invalid_progression'
    if after_level == before_level:
        delta = after_xp - before_xp
        return (delta, 'same_level') if delta >= 0 else (None, 'xp_decreased')
    if after_level < before_level:
        return None, 'level_decreased'
    gain = thresholds.get(before_level)
    if gain is None:
        return None, 'missing_threshold'
    if not _nonnegative(gain) or gain == 0:
        return None, 'invalid_threshold'
    gain -= before_xp
    for level in range(before_level + 1, after_level):
        if level not in thresholds:
            return None, 'missing_threshold'
        if not _nonnegative(thresholds[level]) or thresholds[level] == 0:
            return None, 'invalid_threshold'
        gain += thresholds[level]
    gain += after_xp
    return (gain, 'level_up') if gain >= 0 else (None, 'inconsistent_level_up')


def xp_shares(runs, recent=20):
    """XP of each hero relative to the party mean over the latest complete runs of the latest party,
    using only runs where every hero's gain is known and nobody levelled up (the threshold arithmetic
    is the least certain part). Returns {'party', 'runs', 'heroes': {key: median ratio}} or None.

    Measured on 2026-10-01: ratios are stable per loadout but are not the hero's IncreaseExpAmount
    stat (Priest 1.036 gave ×1.014 vs the Hunter), consistent with that stat adding to the other XP
    bonuses rather than multiplying XP (D008)."""
    usable = []
    for run in sorted(runs, key=lambda r: r['ended_utc'], reverse=True):
        xp = run.get('xp') or {}
        party = run.get('party') or []
        expected = set(party.split(',') if isinstance(party, str) else map(str, party))
        if (xp and set(xp) == expected and not run.get('partial_start') and not run.get('gaps')
                and all(e.get('complete') and not e.get('level_ups') and e.get('gain') for e in xp.values())):
            usable.append(run)
    if not usable:
        return None
    party = usable[0]['party']
    rows = [r for r in usable if r['party'] == party][:recent]
    shares = {}
    for run in rows:
        mean = sum(e['gain'] for e in run['xp'].values()) / len(run['xp'])
        for key, e in run['xp'].items():
            shares.setdefault(key, []).append(e['gain'] / mean)
    return {'party': party, 'runs': len(rows),
            'heroes': {k: sorted(v)[len(v) // 2] for k, v in shares.items()}}


def xp_to_level(level, xp, target, thresholds):
    """XP still needed to go from (level, xp) to `target`, or None when a threshold is unknown."""
    if level is None or target is None:
        return None
    if level >= target:
        return 0
    if xp is None:
        return None
    if level not in thresholds:
        return None
    total = thresholds[level] - (xp or 0)
    for lv in range(level + 1, target):
        if lv not in thresholds:
            return None
        total += thresholds[lv]
    return max(0.0, total)


def party_signature(heroes):
    return ','.join(str(k) for k in sorted(h['hero_key'] for h in heroes))
