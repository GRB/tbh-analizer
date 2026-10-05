"""How much a stage rate can be trusted, from complete runs.

A rate is judged by complete runs, not by minutes: each run is one full cycle (boss included),
and the spread between runs is what tells us how precise the rate is. One 9-minute run and
ten 90-second runs cover similar time but carry very different evidence.

Save counters are exact totals, but a window has no per-run spread, so on their own they are
never better than "insufficient"; they stay as a cross-check next to the run-based rate.
"""
import math
import statistics

MIN_RUNS = 3              # below this there is no usable spread: confidence "insufficient"
CONFIRM_RUNS, CONFIRM_RSE = 5, 0.10   # "medium" (confirmed): >= 5 runs, standard error <= 10% of the rate
HIGH_RUNS, HIGH_RSE = 10, 0.05        # "high": >= 10 runs, standard error <= 5%
Z = 2.0                   # gaps smaller than Z combined standard errors are a tie (~95%)
LEVELS = ('insufficient', 'low', 'medium', 'high')


def ratio_rate(pairs):
    """Rate per hour from complete runs as (value, seconds) pairs, with its standard error.

    Ratio estimator R = sum(value) / sum(seconds), so long and short runs weigh by their time;
    SE(R) ~ sqrt(sum((v - R*t)^2) / (n - 1) / n) / mean(t)."""
    pairs = [(v, t) for v, t in pairs if v is not None and t is not None
             and math.isfinite(v) and math.isfinite(t) and v >= 0 and t > 0]
    n, total = len(pairs), sum(t for _, t in pairs)
    if not n or total <= 0:
        return None
    rate = sum(v for v, _ in pairs) / total
    se = None
    if n >= 2:
        residual = sum((v - rate * t) ** 2 for v, t in pairs) / (n - 1)
        se = (residual / n) ** 0.5 / (total / n)
    return {'per_h': rate * 3600, 'se_h': se * 3600 if se is not None else None,
            'rse': se / rate if se is not None and rate else None, 'runs': n, 'seconds': total}


def confidence(runs, rse):
    if runs < MIN_RUNS or rse is None:
        return 'insufficient'
    if runs >= HIGH_RUNS and rse <= HIGH_RSE:
        return 'high'
    if runs >= CONFIRM_RUNS and rse <= CONFIRM_RSE:
        return 'medium'
    return 'low'


def runs_needed(runs, rse):
    """More complete runs expected to reach the "medium" bar, from the spread seen so far
    (the standard error shrinks with the square root of the number of runs)."""
    if rse is None or runs < MIN_RUNS:
        return CONFIRM_RUNS - runs
    return max(0, CONFIRM_RUNS - runs, math.ceil(runs * (rse / CONFIRM_RSE) ** 2) - runs)


def run_xp(run):
    """Mean XP per hero in one run, or None unless every hero's gain is known."""
    xp = run.get('xp') or {}
    expected = set(party_key(run.get('party')).split(',')) - {''}
    entries = xp.values()
    if (not entries or (expected and set(xp) != expected)
            or not all(e.get('complete') and e.get('gain') is not None for e in entries)):
        return None
    return sum(e['gain'] for e in entries) / len(entries)


def party_key(party):
    return party if isinstance(party, str) else ','.join(str(k) for k in sorted(party or []))


def stage_evidence(runs, save_rates, label, party=None):
    """Per (stage, party): rates from complete runs, save counters as a cross-check.

    `runs` are complete observed runs (start seen, ended by a wave reset) with `xp` decoded;
    `save_rates` are rows from `economy.stage_rates`. With `party`, only that party is kept."""
    grouped, saves = {}, {}
    for run in runs:
        grouped.setdefault((run['stage_key'], party_key(run['party'])), []).append(run)
    for row in save_rates:
        saves[(row['stage'], party_key(row['party']))] = row
    wanted = party_key(party) if party is not None else None
    out = []
    for key in set(grouped) | set(saves):
        if wanted is not None and key[1] != wanted:
            continue
        rows, save = grouped.get(key, []), saves.get(key)
        gold = ratio_rate((r['gold_gain_est'], r['duration_s']) for r in rows)
        xp = ratio_rate((run_xp(r), r['duration_s']) for r in rows)
        seconds = sum(r['duration_s'] or 0 for r in rows)
        durations = sorted(r['duration_s'] for r in rows if r['duration_s'])
        fought = [r['combat'] for r in rows if r.get('combat') and r['combat'].get('samples')]
        lows = sorted(c['min_hp_fraction'] for c in fought if c.get('min_hp_fraction') is not None)
        outputs = sorted(c['party_offence'] for c in fought if c.get('party_offence'))
        e = {'stage': key[0], 'party': key[1], 'label': label(key[0]), 'runs': len(rows),
             'xp_runs': xp['runs'] if xp else 0,
             'gold_runs': gold['runs'] if gold else 0,
             'gold_seconds': gold['seconds'] if gold else 0,
             'xp_seconds': xp['seconds'] if xp else 0,
             'clears': sum(1 for r in rows if r['outcome'] == 'clear'),
             'fails': sum(1 for r in rows if r['outcome'] == 'fail'),
             'minutes': seconds / 60, 'median_duration_s': statistics.median(durations) if durations else None,
             'last_run_utc': max((r['ended_utc'] for r in rows), default=None),
             'clears_per_h': sum(r['outcome'] == 'clear' for r in rows) * 3600 / seconds if seconds else None,
             'gold_h': gold['per_h'] if gold else None, 'gold_se': gold['se_h'] if gold else None,
             'gold_rse': gold['rse'] if gold else None,
             'xp_h': xp['per_h'] if xp else None, 'xp_se': xp['se_h'] if xp else None,
             'xp_rse': xp['rse'] if xp else None, 'source': 'complete runs (1 s samples)',
             'save_gold_h': save['gold_monster_per_h'] if save else None,
             'save_minutes': save['minutes'] if save else None, 'save_clears': save['clears'] if save else None,
             'kills_per_h': save['kills_per_h'] if save else None,
             # Survival from ~1 s combat readings (runs recorded since the combat reader): a short dip
             # or a death between two readings can be missed, so these understate the risk.
             'combat_runs': len(fought), 'deaths': sum(c.get('deaths') or 0 for c in fought),
             'runs_with_deaths': sum(1 for c in fought if c.get('deaths')),
             'lowest_hp': lows[0] if lows else None, 'median_lowest_hp': lows[len(lows) // 2] if lows else None,
             'median_offence': outputs[len(outputs) // 2] if outputs else None,
             # Hero deaths from the game's own counter over valid save windows (all history, exact).
             'save_deaths': (save or {}).get('hero_deaths'),
             'save_deaths_per_h': save['hero_deaths'] * 60 / save['minutes']
             if save and save.get('hero_deaths') is not None and save['minutes'] else None}
        if not rows and save:
            xs = list(save['xp_per_h'].values())
            xs = [x['value'] if isinstance(x, dict) else x for x in xs]
            e.update({'gold_h': save['gold_monster_per_h'], 'clears_per_h': save['clears_per_h'] or None,
                      'xp_h': sum(xs) / len(xs) if xs and not save['xp_incomplete_heroes'] else None,
                      'minutes': save['minutes'], 'source': 'save counters only (no complete run)'})
        e['gold_confidence'] = confidence(e['gold_runs'], e['gold_rse'])
        e['xp_confidence'] = confidence(e['xp_runs'], e['xp_rse'])
        out.append(e)
    return out


def field(metric, name):
    """'gold_h' -> 'gold_<name>'."""
    return metric.replace('_h', '_' + name)


def confirmed(e, metric, comparable=True):
    """Enough runs and precision; with `comparable`, also measured with the current build."""
    return (e[field(metric, 'confidence')] in ('medium', 'high')
            and (not comparable or e.get('current_build_comparable', True)))


def significant(a, b, metric):
    """True when a's rate is above b's by more than Z combined standard errors."""
    se = field(metric, 'se')
    if a.get(se) is None or b.get(se) is None:
        return False
    return a[metric] - b[metric] > Z * (a[se] ** 2 + b[se] ** 2) ** 0.5


def best_stage(evidence, metric, comparable=True):
    """(best confirmed, confirmed stages statistically tied with it, unconfirmed stages that look better).
    `comparable=False` ranks historical evidence measured with other builds."""
    rated = [e for e in evidence if e.get(metric)]
    good = [e for e in rated if confirmed(e, metric, comparable)]
    best = max(good, key=lambda e: e[metric]) if good else None
    ties = [e for e in good if best and e is not best and not significant(best, e, metric)]
    promising = sorted((e for e in rated if not confirmed(e, metric, comparable) and (not best or e[metric] > best[metric])),
                       key=lambda e: -e[metric])
    return best, ties, promising


def describe():
    return (f'Confidence comes from complete runs, not time: under {MIN_RUNS} runs is insufficient; '
            f'medium needs {CONFIRM_RUNS}+ runs with relative standard error ≤{CONFIRM_RSE:.0%}; '
            f'high needs {HIGH_RUNS}+ runs and relative standard error ≤{HIGH_RSE:.0%}. These measure sampling '
            'precision, not formula accuracy or a guaranteed confidence interval. Two stages differ when the gap exceeds '
            f'{Z:g} combined standard errors (approximate); save totals do not prove stage attribution. '
            'Historical rates can mix hero levels and loadouts; they are not a controlled comparison of current builds.')
