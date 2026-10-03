"""Estimated run time and gold/XP per hour for stages not played yet (hypothesis).

Ranking unplayed stages by catalog gold per clear ignores how long a clear takes, so a slow
stage with more gold per clear would look better than a fast one that earns more per hour.
This model estimates the duration and converts to per-hour rates:

- duration ~ waves * (a + b * monsters_per_wave * monster_hp_mult), a, b >= 0, fitted by least
  squares to the median run time of stages with complete runs (current party). `a` is the fixed
  time per wave (spawn, walking); `b` the extra time from monster HP to chew through.
- gold (XP) per run ~ catalog per-clear value * median(observed per run / catalog per clear),
  which absorbs runes and other multipliers the catalog formula does not include.

Its accuracy is measured on our own data by leave-one-out (predict each observed stage from the
others) and reported as the relative error, so every estimate carries its own range. Party power
changes as heroes level; old observations make the model worse, and the measured error shows it.

Power-aware variant: when played stages also carry the party's base-attack output of their runs
(`median_offence`, from the combat reader), the HP term becomes monster HP / party output, so runs
recorded with a weaker party still describe the stage. It is used only when its leave-one-out
duration error on those same stages is lower than the plain model's; otherwise the plain one stays.
"""
import statistics

from .stagestats import MIN_RUNS

MIN_STAGES = 3   # stages with complete runs needed to fit (and cross-validate) the model


def _work(t):
    """Monster HP to chew through; divided by party output in the power-aware variant."""
    work = t['waves'] * t['monsters_per_wave'] * t['monster_hp_mult']
    return work / t['offence'] if t.get('offence') else work


def _fit_duration(points):
    """points: [(theory_row, median_seconds)] -> (a, b) with a, b >= 0."""
    xs = [(t['waves'], _work(t)) for t, _ in points]
    ys = [s for _, s in points]
    s11 = sum(x[0] * x[0] for x in xs)
    s12 = sum(x[0] * x[1] for x in xs)
    s22 = sum(x[1] * x[1] for x in xs)
    r1 = sum(x[0] * y for x, y in zip(xs, ys))
    r2 = sum(x[1] * y for x, y in zip(xs, ys))
    det = s11 * s22 - s12 * s12
    if det > 0:
        a, b = (r1 * s22 - r2 * s12) / det, (s11 * r2 - s12 * r1) / det
        if a >= 0 and b >= 0:
            return a, b
    # Constrained fallbacks: time per wave only, or HP work only; keep the better fit.
    only_a = (r1 / s11 if s11 else 0.0, 0.0)
    only_b = (0.0, r2 / s22 if s22 else 0.0)

    def sse(p):
        return sum((p[0] * x[0] + p[1] * x[1] - y) ** 2 for x, y in zip(xs, ys))
    return min(only_a, only_b, key=sse)


def _per_run(e, metric):
    """Observed value per complete run from the per-hour rate and total run time."""
    prefix = metric.removesuffix('_h')
    runs = e.get(prefix + '_runs', e['runs'])
    seconds = e.get(prefix + '_seconds')
    if seconds is None:
        if runs != e['runs']:
            return None  # old evidence cannot recover the valid subset's exposure
        seconds = e['minutes'] * 60
    return e[metric] * seconds / 3600 / runs if e.get(metric) is not None and runs else None


def _fit(points):
    a, b = _fit_duration([(t, e['median_duration_s']) for t, e in points])
    ratios = {}
    for metric, per_clear in (('gold_h', 'gold_per_clear'), ('xp_h', 'exp_per_clear')):
        values = [_per_run(e, metric) / t[per_clear] for t, e in points if _per_run(e, metric) and t[per_clear]]
        ratios[metric] = statistics.median(values) if values else None
    return {'a': a, 'b': b, 'gold_ratio': ratios['gold_h'], 'xp_ratio': ratios['xp_h']}


def _predict(model, t):
    if model.get('offence'):
        t = dict(t, offence=model['offence'])
    duration = model['a'] * t['waves'] + model['b'] * _work(t)
    if duration <= 0:
        return None
    return {'duration_s': duration,
            'gold_h': t['gold_per_clear'] * model['gold_ratio'] * 3600 / duration if model['gold_ratio'] else None,
            'xp_h': t['exp_per_clear'] * model['xp_ratio'] * 3600 / duration if model['xp_ratio'] else None}


def _loo(points):
    """Mean relative leave-one-out error per metric."""
    errors = {'duration_s': [], 'gold_h': [], 'xp_h': []}
    for i, (t, e) in enumerate(points):
        guess = _predict(_fit(points[:i] + points[i + 1:]), t)
        if not guess:
            continue
        actual = {'duration_s': e['median_duration_s'], 'gold_h': e['gold_h'], 'xp_h': e['xp_h']}
        for key, errs in errors.items():
            if guess[key] and actual[key]:
                errs.append(abs(guess[key] / actual[key] - 1))
    return {k: statistics.mean(v) if v else None for k, v in errors.items()}


def fit(evidence, theory, offence=None):
    """Fit on stages with >= MIN_RUNS complete runs; None when there are too few to validate.
    `offence` is the party's current base-attack output, needed by the power-aware variant."""
    by_stage = {t['stage']: t for t in theory}
    points = [(by_stage[e['stage']], e) for e in evidence
              if e['runs'] >= MIN_RUNS and e.get('median_duration_s') and e['stage'] in by_stage]
    if len(points) < MIN_STAGES:
        return None
    error, power = _loo(points), None
    powered = [(dict(t, offence=e['median_offence']), e) for t, e in points if e.get('median_offence')]
    if offence and len(powered) >= MIN_STAGES:
        plain_err = _loo([(by_stage[e['stage']], e) for _, e in powered])['duration_s']
        power_err = _loo(powered)
        if power_err['duration_s'] is not None and plain_err is not None and power_err['duration_s'] < plain_err:
            points, error, power = powered, power_err, {'offence': offence, 'plain_duration_error': plain_err}
    model = _fit(points)
    if power:
        model['offence'] = offence
    model.update({
        'stages': [t['stage'] for t, _ in points], 'power': power,
        'error': error,
        'max_dmg_mult': max(t['monster_dmg_mult'] for t, _ in points),
        'max_hp_mult': max(t['monster_hp_mult'] for t, _ in points),
    })
    return model


def estimate(model, t):
    """Estimated duration and rates with a +- range from the measured leave-one-out error."""
    guess = _predict(model, t)
    if not guess:
        return None
    for key in ('gold_h', 'xp_h'):
        err = model['error'].get(key)
        if guess[key] is not None and err is not None:
            guess[key.replace('_h', '_low')] = guess[key] * max(0.0, 1 - err)
            guess[key.replace('_h', '_high')] = guess[key] * (1 + err)
    guess['dmg_vs_observed'] = t['monster_dmg_mult'] / model['max_dmg_mult'] if model['max_dmg_mult'] else None
    guess['hp_vs_observed'] = t['monster_hp_mult'] / model['max_hp_mult'] if model['max_hp_mult'] else None
    return guess


def describe(model):
    if not model:
        return (f'Per-hour estimates for unplayed stages need {MIN_STAGES}+ stages with {MIN_RUNS}+ complete runs '
                'each; until then candidates are listed by catalog gold per clear, which ignores clear time.')
    err = model['error']
    pct = lambda v: f'±{v:.0%}' if v is not None else 'unknown'
    hp = 'HP multiplier / party output' if model.get('power') else 'HP multiplier'
    power = (f" Uses the party's base-attack output of each run (it beat the plain model, whose duration error "
             f"is {pct(model['power']['plain_duration_error'])})." if model.get('power') else '')
    return (f"Unplayed stages are ranked by estimated gold/h: run time ≈ {model['a']:.1f} s × waves + "
            f"{model['b']:.5g} s × waves × monsters × {hp}, fitted on {len(model['stages'])} played stages.{power} "
            f"Leave-one-out error on our own runs: duration {pct(err['duration_s'])}, gold/h {pct(err['gold_h'])}, "
            f"XP/h {pct(err['xp_h'])}. Estimates, not measurements: play a stage to confirm it.")
