"""Player suggestions built from the analyses we already have (read-only).

Every suggestion states its basis so estimates are never shown as facts:
- `observed`: measured by us (save counters or 1 s runtime samples);
- `catalog`: read from the installed game tables/text;
- `hypothesis`: a model on top of observed/catalog data (e.g. rune % stacking additively).
Nothing here acts in the game.
"""
from datetime import datetime, timezone

from ..analysis.stagestats import (CONFIRM_RSE, CONFIRM_RUNS, best_stage, describe, field,
                                   party_key, runs_needed, significant)
from . import analytics, items
from .cube import cube_view
from .impact import purchases
from .analytics import SAFE_MINUTES
from .live import heroes as hero_list, live
from .challenge import RETRY_REACH, act_boss_steps, challenges
from .power import PowerContext
from ..analysis import power
from ..analysis.progress import xp_to_level

CHEAP_INCOME_MIN = 15     # a rune costing <= this many minutes of income is a "just buy it"
STALE_WITHIN = 0.75        # a stage measured before later power changes is worth re-measuring if this close
SWITCH_GAIN_PCT = 10      # a significant switch is a "do first" only when it is also worth >= this much
RISKY_HP, SAFE_HP = 0.35, 0.70   # lowest hero HP in a run: below RISKY is close to a death, above SAFE leaves margin
SAFE_RUNS = 3              # runs with combat readings needed before calling a stage safe
TRADEOFF_OUTPUT_PCT = 3   # gear trade-off: this much more base-attack output, no HP/armor loss -> "likely better"
GOLD_ITEM = '100001'

# Rune stat semantics taken from the game's own AccountStat_<STAT> text.
PERCENT_GOLD = {'IncreaseGoldAmount'}            # "{0}% Increased Gold Per Kill"
PERCENT_XP = {'IncreaseExpAmount'}               # "{0}% Increased Exp Gain"
# Faster heroes: a run cannot get faster than the relative gain in the stat, whatever the model.
SPEED_PERCENT = {'AllHeroAttackDamagePercent', 'AllHeroAttackSpeed', 'AllHeroMoveSpeed'}
FLAT_PER_CLEAR = {'AdditionalGoldStageBoss': 'gold', 'AdditionalExpStageBoss': 'xp'}      # "+{0} per stage boss kill"
FLAT_PER_KILL = {'AdditionalGoldNormalMonster': 'gold', 'AdditionalExpNormalMonster': 'xp'}  # "+{0} per normal kill"
CATEGORY_PREFIXES = [
    ('gold', ('IncreaseGold', 'AdditionalGold', 'CubeAlchemyGold', 'OfflineRewardGold')),
    ('xp', ('IncreaseExp', 'AdditionalExp', 'CubeExp', 'OfflineRewardExp')),
    ('power', ('AllHero',)),
    ('speed', ('WaveCountReduction',)),
    ('loot', ('DropChance', 'MaxAmount', 'ReduceAutoOpen', 'UnlockAutoOpen', 'Open')),
    ('storage', ('MaxInventorySlot', 'UnlockStash', 'UnlockArrange', 'UnlockSkillSlot')),
]


def duration(hours):
    minutes = hours * 60
    return '<1 min' if minutes < 1 else f'{minutes:.0f} min' if minutes < 90 else f'{hours:.1f} h'


def rune_category(stat):
    for category, prefixes in CATEGORY_PREFIXES:
        if stat and stat.startswith(prefixes):
            return category
    return 'other'


# --- stages -----------------------------------------------------------------
def _pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def _evidence_text(e, metric):
    runs = e['xp_runs'] if metric == 'xp_h' else e['runs']
    if not runs:
        return f"{e['source']}, {e['minutes']:.1f} min"
    rse = e[field(metric, 'rse')]
    spread = f', relative standard error {rse * 100:.0f}%' if rse is not None else ', spread unknown'
    median = f", median {e['median_duration_s'] / 60:.1f} min/run" if e.get('median_duration_s') else ''
    return f'{runs} complete run(s){spread}{median}'


def survival(e):
    """'risky' / 'safe' / None (not enough evidence) for one stage. Only recent deaths count: older
    ones were with weaker heroes (RECENT_DEATH_HOURS in views/analytics.py)."""
    if not e:
        return None
    if e.get('runs_with_deaths') or e.get('deaths_recent') or \
            (e.get('median_lowest_hp') is not None and e['median_lowest_hp'] < RISKY_HP):
        return 'risky'
    if e.get('combat_runs', 0) >= SAFE_RUNS and e.get('lowest_hp') is not None and e['lowest_hp'] >= SAFE_HP:
        return 'safe'
    if (e.get('save_minutes') or 0) >= SAFE_MINUTES and not e.get('deaths_recent') and not e.get('runs_with_deaths'):
        return 'safe'
    return None


def _risk_text(e, lead=' Survival:'):
    if not e:
        return ''
    parts = []
    if e.get('deaths_total') is not None or e.get('save_minutes'):
        older = (e.get('deaths_total') or 0) - (e.get('deaths_recent') or 0)
        parts.append(f"{e.get('deaths_recent') or 0} hero death(s) in the last 6 h" + (f" ({older} older)" if older else '')
                     + (f", {e['save_minutes']:.0f} min of saved play" if e.get('save_minutes') else ''))
    if e.get('combat_runs'):
        parts.append(f"{e['runs_with_deaths']} of {e['combat_runs']} recorded runs with a death"
                     + (f", lowest hero HP {e['lowest_hp']:.0%}" if e.get('lowest_hp') is not None else ''))
    return f"{lead} {'; '.join(parts)}." if parts else ''


def _threat_text(threat, reference):
    """' The boss hits the Priest for 62 HP (15% of HP); ...' with what the party survived before."""
    if not threat or threat.get('worst_boss_fraction') is None:
        return ''
    rows = [h for h in threat['heroes'] if h['boss_hits_to_die'] and (not threat.get('front') or h['hero_key'] == threat['front'])]
    if not rows:
        return ''
    text = ' Its boss would hit ' + ', '.join(
        f"{h['name']} for {h['boss_hit']:.0f} HP ({h['boss_hits_to_die']:.1f} hits to die"
        + (f", ×{h['vs_current']:.1f} the current boss" if h['vs_current'] else '') + ')' for h in rows)
    text += (" with today's stats (the front hero, who takes almost all hits)" if threat.get('front') else
             " with today's stats; the front hero takes almost all hits")
    died, clean = (reference or {}).get('deaths_from'), (reference or {}).get('no_deaths_up_to')
    worst = threat['boss_fraction']
    if died and worst >= died[0]:
        text += f"; heroes died recently on {died[1]}, where the boss hits less hard"
    elif clean and worst <= clean[0]:
        text += f"; no deaths on {clean[1]}, where the boss hits as hard or harder"
    return text + (' (above the confirmed armor range)' if any(h['cap_uncertain'] for h in rows) else '') + '.'


def _measured_with(e):
    """Why a historical rate is not a current-build rate."""
    parts = []
    if e.get('changes_since'):
        parts.append(f"before {e['changes_since']} later power change(s) ({', '.join(e.get('changes_since_kinds') or [])})")
    if e.get('changes_during'):
        parts.append('while the loadout changed')
    if e.get('mixed_levels'):
        parts.append('across several hero levels')
    return ', '.join(parts) or 'with earlier hero levels'


def stage_suggestions(evidence, current_stage, candidates, reference=None, history=None):
    """`evidence`: current-build stage rates; `history`: the same party's rates over all its runs
    (defaults to `evidence`), used when the current build has no confirmed stage yet."""
    history = evidence if history is None else history
    out = []
    current = next((e for e in evidence if e['stage'] == current_stage), None)
    for metric, what, unit in (('gold_h', 'gold', 'gold/h'), ('xp_h', 'XP', 'XP/h per hero')):
        best, ties, promising = best_stage(evidence, metric)
        key = 'gold' if metric == 'gold_h' else 'xp'
        if best:
            here = current is not None and current['stage'] == best['stage']
            gain = _pct(best[metric], current[metric]) if current and current.get(metric) else None
            sig = current is not None and not here and significant(best, current, metric)
            detail = f"{best['label']}: {best[metric]:,.0f} {unit} ({_evidence_text(best, metric)})."
            if here:
                detail += ' You are already farming it.'
            elif gain is not None:
                detail += (f" Current stage {current['label']}: {current[metric]:,.0f} {unit} "
                           f"({_evidence_text(current, metric)}); switching is {gain:+.0f}%"
                           + (', a significant difference.' if sig else ', not distinguishable from noise yet.'))
            if ties:
                detail += ' Statistically tied with ' + ', '.join(t['label'] for t in ties) + '.'
            if best['fails']:
                detail += f" {best['fails']} failed run(s) observed."
            detail += _risk_text(best)
            # Stages measured before later power changes (gear, runes, points) look worse than they are today.
            stale = [e for e in history if e['stage'] != best['stage'] and e.get(metric) and e.get('runs')
                     and e.get('changes_since') and e[metric] >= STALE_WITHIN * best[metric]]
            for e in stale:
                detail += (f" {e['label']} ({e[metric]:,.0f}) was last measured before {e['changes_since']} power "
                           f"change(s) ({', '.join(e.get('changes_since_kinds') or [])}), so today it is likely higher.")
                if f"stage-remeasure-{e['stage']}" not in {o['id'] for o in out}:
                    minutes = f" (~{CONFIRM_RUNS * e['median_duration_s'] / 60:.0f} min)" if e.get('median_duration_s') else ''
                    out.append({'id': f"stage-remeasure-{e['stage']}", 'area': 'stages',
                                'title': f"Re-measure {e['label']} for a fair comparison",
                                'detail': (f"Its rate comes from runs before {e['changes_since']} later power change(s) "
                                           f"({', '.join(e.get('changes_since_kinds') or [])}), while {best['label']} was "
                                           f"measured with the current gear. {CONFIRM_RUNS} fresh complete runs{minutes} "
                                           'there would compare both with the same heroes.'),
                                'basis': 'observed', 'confidence': 'low', 'priority': 3,
                                'metrics': {'stage': e['stage'], 'changes_since': e['changes_since']}})
            out.append({'id': f'stage-{key}', 'area': 'stages', 'title': f'Best stage for {what}: {best["label"]}',
                        'detail': detail, 'basis': 'observed', 'confidence': best[field(metric, 'confidence')],
                        'priority': 1 if sig and (gain or 0) >= SWITCH_GAIN_PCT else 2,
                        'metrics': {'stage': best['stage'], metric: best[metric], 'runs': best['runs'],
                                    'rse': best[field(metric, 'rse')], 'gain_pct_vs_current': gain,
                                    'significant': sig}})
        elif (old := best_stage(history, metric, comparable=False)[0]) is not None:
            # Earlier runs with another build: the best known stage, not a confirmed rate for today.
            runs_now = current[('xp_runs' if metric == 'xp_h' else 'runs')] if current else 0
            detail = (f"{old['label']}: {old[metric]:,.0f} {unit} ({_evidence_text(old, metric)}), measured "
                      f"{_measured_with(old)}, so today's rate may differ.")
            if old['stage'] == current_stage:
                detail += ' You are already farming it.'
            detail += (f" Confirming it for the current build needs {CONFIRM_RUNS}+ complete runs with relative "
                       f"standard error ≤{CONFIRM_RSE:.0%} at today's loadout"
                       + (f" (the current build has {runs_now} so far, on {current['label']})." if current else '.'))
            detail += _risk_text(old)
            out.append({'id': f'stage-{key}-history', 'area': 'stages', 'title': f'Best stage for {what} so far: {old["label"]}',
                        'detail': detail, 'basis': 'observed', 'confidence': 'low', 'priority': 2,
                        'metrics': {'stage': old['stage'], metric: old[metric], 'runs': old['runs'],
                                    'rse': old[field(metric, 'rse')], 'historical': True,
                                    'changes_since': old.get('changes_since')}})
        else:
            out.append({'id': f'stage-{key}-none', 'area': 'stages', 'title': f'No confirmed {what} stage yet',
                        'detail': (f'Needs {CONFIRM_RUNS}+ complete runs with relative standard error ≤{CONFIRM_RSE:.0%}, '
                                   'with comparable hero levels and loadout for the current party.'),
                        'basis': 'observed', 'confidence': 'insufficient', 'priority': 3, 'metrics': {}})
        for e in promising[:3]:
            runs = e['xp_runs'] if metric == 'xp_h' else e['runs']
            need = runs_needed(runs, e[field(metric, 'rse')])
            comparable = e.get('current_build_comparable', True)
            if not comparable:
                need = max(need, CONFIRM_RUNS)
            minutes = f" (~{need * e['median_duration_s'] / 60:.0f} min)" if e.get('median_duration_s') else ''
            vs = f" ({_pct(e[metric], best[metric]):+.0f}% vs {best['label']})" if best else ''
            out.append({'id': f'stage-{key}-test-{e["stage"]}', 'area': 'stages',
                        'title': f'Worth confirming for {what}: {e["label"]}',
                        'detail': (f"{e[metric]:,.0f} {unit}{vs} from {_evidence_text(e, metric)}. "
                                   + (f'Collect {need}+ fresh runs at unchanged levels and loadout; the historical cohort is not comparable.'
                                      if not comparable else f'About {need} more complete run(s){minutes} to assess its precision.')),
                        'basis': 'observed', 'confidence': e[field(metric, 'confidence')] if comparable else 'low', 'priority': 2,
                        'metrics': {'stage': e['stage'], metric: e[metric], 'runs': runs, 'runs_needed': need}})
    # Unplayed stages whose estimated gold/h beats the confirmed best (same ranking as the Stages tab).
    best, _, _ = best_stage(evidence, 'gold_h')
    for c in candidates:
        est = c.get('estimate')
        if not est or not est.get('gold_h') or (best and est['gold_h'] <= best['gold_h']):
            continue
        likely = best is not None and est.get('gold_low') is not None and est['gold_low'] > best['gold_h']
        harder = est['dmg_vs_observed'] and est['dmg_vs_observed'] > 1.05
        risk = (f" Monsters hit {est['dmg_vs_observed']:.1f}× harder than any played stage: failure risk unknown."
                if harder else '')
        margin = survival(current)
        threat = c.get('threat') or {}
        died = (reference or {}).get('deaths_from')
        if died and threat.get('boss_fraction') is not None and threat['boss_fraction'] >= died[0]:
            likely = False   # the boss would hit at least as hard as where heroes already died
        risk += _threat_text(threat, reference)
        if harder and margin == 'risky':
            # Already close to deaths where we farm: a harder stage is not a good bet now.
            likely = False
            risk += _risk_text(current, f" Your party already struggles on {current['label']}:")
        elif harder and margin == 'safe':
            risk += _risk_text(current, f" On {current['label']} the party keeps a margin:")
        vs = f" vs {best['gold_h']:,.0f} confirmed on {best['label']}" if best else ''
        range_text = (f"range {est['gold_low']:,.0f}–{est['gold_high']:,.0f}"
                      if est.get('gold_low') is not None and est.get('gold_high') is not None else 'range unknown')
        xp_text = f"{est['xp_h']:,.0f} XP/h per hero" if est.get('xp_h') is not None else 'XP rate unknown'
        out.append({'id': f"stage-untested-{c['stage']}", 'area': 'stages',
                    'title': f"{'Likely better' if likely else 'Maybe better'}, untested: {c['label']}",
                    'detail': (f"Estimated {est['gold_h']:,.0f} gold/h ({range_text}){vs}; "
                               f"about {est['duration_s'] / 60:.1f} min per run, {xp_text}. "
                               f"Estimated from catalog values and our played stages.{risk}"
                               + ('' if c['reachable'] else ' Check that it is unlocked.')),
                    'basis': 'hypothesis', 'confidence': 'low', 'priority': 2 if likely else 4,
                    'metrics': {'stage': c['stage'], 'gold_h_est': est['gold_h'], 'gold_low': est.get('gold_low'),
                                'current_survival': margin, 'boss_fraction': threat.get('boss_fraction'),
                                'gold_high': est.get('gold_high'), 'duration_s_est': est['duration_s']}})
        if sum(1 for o in out if o['id'].startswith('stage-untested-')) >= 3:
            break
    return out


# --- runes ------------------------------------------------------------------
def rune_estimate(node, totals, ref, effects=None):
    """Estimated gain per hour of the next level, or None when we cannot model it.

    Values use the in-game scale (see items.RUNE_DISPLAY_SCALE). Percent stats assume the owned
    percentages add up (multiplier = 1 + total%/100); flat stats multiply by observed clears or kills.
    Stats without a known scale are not estimated: a 10x unit error would make the payback meaningless.

    Hero-stat runes (AllHero*) use `effects` when the game runs: the next level is applied with the
    game's formula to each party hero, which also counts the gear increases it adds up with. A run
    cannot get faster than the largest damage/speed gain, which bounds the gold/h gain."""
    stat, effect = node['stat'], node.get('next_effect')
    if not effect or not effect.get('value'):
        return None
    live = effects.rune(stat, node.get('next_value')) if effects else None
    if live:
        target = power.RUNE_TARGETS[stat][0]
        gains = {k: (e['stats'].get(target) or {}).get('pct') or 0.0 for k, e in live.items()}
        text = ', '.join(f"{effects.hero_name(k)} {v:+.1f}%" for k, v in gains.items())
        guards = [(k, e['defence']) for k, e in live.items() if e.get('defence')]
        guard = ('; stage boss hit ' + ', '.join(f"{effects.hero_name(k)} {g['hit_before']:.0f}→{g['hit_after']:.0f} HP"
                                                 for k, g in guards)) if guards else ''
        out = {'basis': 'observed', 'stat_pct': gains,
               'how': f"{effects.name(target)} now: {text} (game formula, gear increases included){guard}"}
        if target in ('AttackDamage', 'AttackSpeed') and ref.get('gold_h'):
            out['gold_h_max'] = ref['gold_h'] * max(gains.values()) / 100
        return out
    owned = (totals.get(stat) or {}).get('effect') or {}
    step, total = effect['value'], owned.get('value', 0)
    scale = '' if effect['verified'] else ' (in-game scale inferred from similar runes, not verified)'
    basis = 'observed' if effect['verified'] else 'hypothesis'
    if stat in PERCENT_GOLD and ref.get('gold_h'):
        return {'gold_h': ref['gold_h'] * step / (100 + total), 'basis': 'hypothesis',
                'how': f'+{step:g}% on top of {total:g}% already owned, applied to observed monster gold{scale}'}
    if stat in PERCENT_XP and ref.get('xp_h'):
        return {'xp_h': ref['xp_h'] * step / (100 + total), 'basis': 'hypothesis',
                'how': f'+{step:g}% on top of {total:g}% already owned, applied to observed XP{scale}'}
    if stat in FLAT_PER_CLEAR and ref.get('clears_per_h'):
        return {f'{FLAT_PER_CLEAR[stat]}_h': step * ref['clears_per_h'], 'basis': basis,
                'how': f"+{step:g} per stage boss kill × {ref['clears_per_h']:.1f} clears/h{scale}"}
    if stat in SPEED_PERCENT and ref.get('gold_h'):
        # Heroes act (100 + total + step) / (100 + total) times faster at most; even if a whole run were
        # spent on that stat, gold/h could not rise by more than that ratio. Real gains are lower.
        gain = step / (100 + total)
        return {'gold_h_max': ref['gold_h'] * gain, 'basis': 'hypothesis',
                'how': f'+{step:g}% on top of {total:g}% owned: proportional-throughput scenario of {gain * 100:.1f}%'}
    if stat in FLAT_PER_KILL and ref.get('kills_per_h'):
        return {f'{FLAT_PER_KILL[stat]}_h': step * ref['kills_per_h'], 'basis': basis,
                'how': f"+{step:g} per normal monster kill × {ref['kills_per_h']:,.0f} kills/h{scale}"}
    return None


def rune_suggestions(nodes, totals, gold, ref, effects=None):
    gold_h = ref.get('gold_h')
    rows = []
    for node in nodes:
        if not node['purchasable_hint'] or node['next_cost_item'] != GOLD_ITEM or not node['next_cost']:
            continue
        cost = node['next_cost']
        row = {**node, 'category': rune_category(node['stat']), 'affordable': gold >= cost,
               'wait_h': max(0, cost - gold) / gold_h if gold_h else None,
               'cost_minutes_of_income': cost / gold_h * 60 if gold_h else None,
               'estimate': rune_estimate(node, totals, ref, effects)}
        est = row['estimate'] or {}
        row['payback_h'] = cost / est['gold_h'] if est.get('gold_h') else None
        row['xp_pct'] = est['xp_h'] / ref['xp_h'] * 100 if est.get('xp_h') and ref.get('xp_h') else None
        rows.append(row)

    def describe(r):
        effect = r.get('next_effect')
        adds = effect['text'] if effect else f"+{r['next_value']} {r['stat']} (raw value; in-game scale not verified)"
        text = f"You own level {r['level']} of {r['max_level']}; level {r['level'] + 1} adds {adds} for {r['next_cost']:,.0f} gold"
        if not r['affordable'] and r['wait_h'] is not None:
            text += f", affordable after ~{duration(r['wait_h'])} of farming"
        return text + '.'

    def node(r):
        return f"{r['name']} · {r.get('stat_name') or r['stat']} {r['level']}→{r['level'] + 1}/{r['max_level']}"

    out = []
    picked = set()

    def add(r, title, extra, priority, basis=None, conf='medium'):
        picked.add(r['rune_key'])
        est = r['estimate'] or {}
        out.append({'id': f"rune-{r['rune_key']}", 'area': 'runes', 'title': title,
                    'detail': describe(r) + (' ' + extra if extra else ''),
                    'basis': basis or est.get('basis') or 'catalog', 'confidence': conf, 'priority': priority,
                    'metrics': {'rune_key': r['rune_key'], 'cost': r['next_cost'], 'category': r['category'],
                                'payback_h': r['payback_h'], 'xp_pct': r['xp_pct'], 'wait_h': r['wait_h'],
                                'opens': len(r['next_runes']) if r['level'] == 0 else 0}})

    gold_runes = sorted((r for r in rows if r['payback_h']), key=lambda r: r['payback_h'])
    for r in gold_runes[:3]:
        est = r['estimate']
        add(r, f"Gold: {node(r)} (pays back in ~{duration(r['payback_h'])})",
            f"Estimated +{est['gold_h']:,.0f} gold/h ({est['how']}).", 1 if r['payback_h'] <= 24 else 2,
            conf='medium' if est['basis'] == 'observed' else 'low')
    xp_runes = sorted((r for r in rows if r['xp_pct']), key=lambda r: -r['xp_pct'] / r['next_cost'])
    for r in xp_runes[:2]:
        est = r['estimate']
        add(r, f"XP: {node(r)} (+{r['xp_pct']:.1f}% XP/h)", f"Estimated +{est['xp_h']:,.0f} XP/h per hero ({est['how']}).",
            2, conf='medium' if est['basis'] == 'observed' else 'low')
    def best_case(r):
        est = r['estimate'] or {}
        return r['next_cost'] / est['gold_h_max'] if est.get('gold_h_max') else None

    power = sorted((r for r in rows if r['category'] in ('power', 'speed')),
                   key=lambda r: (best_case(r) is None, best_case(r) or r['next_cost']))
    for r in power[:3]:
        est, floor = r['estimate'] or {}, best_case(r)
        if floor:
            add(r, f"Power: {node(r)} (scenario +{est['gold_h_max']:,.0f} gold/h, payback ~{duration(floor)})",
                (f"Proportional-throughput scenario from {est['how']}. This is not a bound: discrete hits, deaths, "
                 'skills and stage unlocks can change the outcome. Check the measured effect of past changes in the Runes tab.'),
                2 if r['affordable'] else 3, basis='hypothesis', conf='low')
        else:
            add(r, f"Power: {node(r)}",
                (f"{est['how']}." if est.get('how') else
                 'Stronger heroes clear faster and unlock harder, richer stages; the gain is not measurable before buying.'),
                2 if r['affordable'] else 3, basis=est.get('basis') or 'catalog')
    cheap = [r for r in rows if r['rune_key'] not in picked and r['cost_minutes_of_income'] is not None
             and r['cost_minutes_of_income'] <= CHEAP_INCOME_MIN]
    for r in sorted(cheap, key=lambda r: r['next_cost'])[:5]:
        opens = f" Opens {len(r['next_runes'])} more node(s)." if r['level'] == 0 and r['next_runes'] else ''
        add(r, f"Cheap: {node(r)}",
            f"Costs about {duration(r['cost_minutes_of_income'] / 60)} of income.{opens}", 2 if r['affordable'] else 3,
            basis='catalog')
    return out


# --- gear, cube, storage, heroes -------------------------------------------
def stat_profile(item):
    """Every stat an item gives, as {(stat, mod): value} plus display names.

    Base and inherent stats come from the catalog in the same units and add up per (stat, mod).
    Enchants and the unique mod belong to the instance and are kept as their own entries: their
    units are not known to match the catalog stats, so they only compare with the same enchant."""
    profile, names = {}, {}
    for st in item.get('stats') or []:
        if st.get('stat') and st['stat'] != 'NONE':
            key = (st['stat'], st.get('mod') or 'FLAT')
            profile[key] = profile.get(key, 0) + (st.get('value') or 0)
            names[key] = st.get('name') or st['stat']
    for e in item.get('enchants') or []:
        key = ('enchant:' + str(e.get('stat')), e.get('mod_type') or '')
        profile[key] = profile.get(key, 0) + (e.get('value') or 0)
        names[key] = f"enchant {e.get('stat')}"
    if item.get('unique_mod'):
        key = ('unique:' + item['unique_mod'], '')
        profile[key], names[key] = 1, f"unique {item['unique_mod']}"
    return profile, names


def compare_items(current, candidate):
    """(gains, losses, names): per stat, (current value, candidate value)."""
    mine, names = stat_profile(current) if current else ({}, {})
    theirs, their_names = stat_profile(candidate)
    names = {**their_names, **names}
    keys = set(mine) | set(theirs)
    gains = {k: (mine.get(k, 0), theirs.get(k, 0)) for k in keys if theirs.get(k, 0) > mine.get(k, 0)}
    losses = {k: (mine.get(k, 0), theirs.get(k, 0)) for k in keys if theirs.get(k, 0) < mine.get(k, 0)}
    return gains, losses, names


def _changes_text(changes, names):
    def label(k):
        mod = '' if k[1] in ('FLAT', '') else f' ({k[1].lower()})'
        return names.get(k, k[0]) + mod
    return ', '.join(f"{label(k)} {a:g}→{b:g}" for k, (a, b) in changes.items())


def gear_suggestions(equipment, containers, heroes, thresholds=None, xp_h=None, weapons=None, grades=(), effects=None):
    """Gear from inventory/stash for party heroes, by slot, comparing every stat the items give.

    - Upgrade: the candidate is at least as good on every stat of the worn item (base, inherent,
      enchants, unique mod) and better on one. Only these are recommended.
    - Trade-off: better on some stats, worse on others (e.g. more armor, less attack damage). How
      they weigh depends on the hero and the stage, so they are listed for the player to decide.
    - The game only lets a hero equip an item whose level is not above the hero's ("Requires Lv.{0}",
      "Level too low to equip."), which matches every equipped item in the save. Upgrades above the
      hero's level are listed with the level they need and an ETA at `xp_h`.
    - With `effects` (views/power.PowerContext, needs the game running) each card also states what
      the swap does to the stats the game uses, and trade-offs are ranked by the change in
      base-attack output (an estimate). A trade-off that raises that output without lowering HP or
      armor is raised to priority 3."""
    weapons, thresholds = weapons or {}, thresholds or {}
    rank = {g: i for i, g in enumerate(grades)}
    pool = [dict(it, location=c['label']) for c in containers.values() for it in c['items']
            if it.get('type') == 'GEAR' and it.get('parts') and it.get('gear_type')]
    upgrades, later, tradeoffs = [], [], []
    for hero in equipment:
        if not hero['in_party'] or hero['hero_key'] not in heroes:
            continue
        level = heroes[hero['hero_key']]['level'] or 0
        worn = {e['slot_part']: e for e in hero['equipment']}
        for part in items.PARTS:
            current = worn.get(part)
            required = weapons.get(hero['hero_key'], {}).get(part)
            for it in pool:
                if it['parts'] != part or (required and it['gear_type'] != required):
                    continue
                gains, losses, names = compare_items(current, it)
                if not gains:
                    continue
                mine = stat_profile(current)[0] if current else {}
                rel = sum((b - a) / a for k, (a, b) in gains.items() if a) / max(1, len(mine))
                eff = effects.gear(hero['hero_key'], current, it) if effects else None
                output = eff['offence_pct'] if eff and eff['offence_pct'] is not None else None
                score = (1 if current is None else 0, output if losses and output is not None else len(gains) - len(losses),
                         rel, it['level'] or 0, rank.get(it['grade'], -1))
                entry = (score, hero, part, current, it, gains, losses, names, eff)
                if losses:
                    # A trade-off that loses more stats than it gains is noise, unless it measurably adds output.
                    if (it['level'] or 0) <= level and (len(gains) >= len(losses) or (output or 0) > 0):
                        tradeoffs.append(entry)
                elif (it['level'] or 0) <= level:
                    upgrades.append(entry)
                else:
                    later.append(entry)
    out, used = [], set()

    def take(entries):
        for entry in sorted(entries, key=lambda e: e[0], reverse=True):
            slot = (entry[1]['hero_key'], entry[2])
            if entry[4]['unique_id'] in used or slot in used:
                continue
            used.update({entry[4]['unique_id'], slot})
            yield entry
    for _, hero, part, current, it, gains, losses, names, eff in take(upgrades):
        out.append(_upgrade_card(hero, part, current, it, gains, names, eff, effects))
    waiting = {}
    for _, hero, part, current, it, gains, losses, names, _eff in take(later):
        waiting.setdefault(hero['hero_key'], (hero, []))[1].append((part, current, it, gains, names))
    for hero_key, (hero, rows) in waiting.items():
        state = heroes[hero_key]
        target = min(it['level'] for _, _, it, _, _ in rows)
        need = xp_to_level(state['level'], state.get('xp'), target, thresholds)
        eta = f", about {duration(need / xp_h)} of farming at the reference XP stage" if need is not None and xp_h else ''
        lines = '; '.join(f"lvl {it['level']} {it['name']} for {part.replace('_', ' ').lower()} ({_changes_text(g, n)})"
                          for part, _, it, g, n in sorted(rows, key=lambda r: r[2]['level']))
        out.append({'id': f'gear-later-{hero_key}', 'area': 'gear',
                    'title': f"{hero['name']} (lvl {state['level']}): better gear unlocks at level {target}",
                    'detail': (f"Better on every stat but too high to equip yet (the game requires the item level): "
                               f"{lines}. Level {target} is {target - state['level']} level(s) away{eta}."),
                    'basis': 'catalog', 'confidence': 'medium', 'priority': 4,
                    'metrics': {'hero_key': hero_key, 'level': state['level'], 'unlock_level': target,
                                'xp_needed': need, 'items': [it['unique_id'] for _, _, it, _, _ in rows]}})
    for _, hero, part, current, it, gains, losses, names, eff in take(tradeoffs):
        output = eff['offence_pct'] if eff else None
        stats = (eff or {}).get('stats', {})
        guard = (eff or {}).get('defence')
        # With the boss hit known, "safe" means the hero survives at least as many boss hits as now.
        safe = (guard['hits_after'] >= guard['hits_before'] - 1e-9 if guard else
                all((stats.get(k) or {}).get('pct') is None or stats[k]['pct'] >= 0 for k in ('MaxHp', 'Armor')))
        likely = output is not None and output >= TRADEOFF_OUTPUT_PCT and safe and not eff['unknown']
        effect = f" Effect on {hero['name']} now: {effects.text(eff)}." if eff else ''
        verdict = ('More estimated base-attack output with no decrease in the checked survival measure; skills are not modelled.'
                   if likely else 'Not a clear upgrade: whether the gain outweighs the loss depends on the hero and the stage.')
        out.append({'id': f"gear-tradeoff-{hero['hero_key']}-{part}", 'area': 'gear',
                    'title': (f"{'Likely better' if likely else 'Trade-off'} for {hero['name']}: {it['name']} instead of "
                              f"{current['name']}" + (f' (output {output:+.1f}%)' if output is not None else '')),
                    'detail': (f"Gains {_changes_text(gains, names)}; loses {_changes_text(losses, names)} "
                               f"({it['location']}, lvl {it['level']} {it['grade']}).{effect} {verdict}"),
                    'basis': 'hypothesis' if eff else 'catalog', 'confidence': 'medium' if likely else 'low',
                    'priority': 3 if likely else 4,
                    'metrics': {'hero_key': hero['hero_key'], 'part': part, 'unique_id': it['unique_id'],
                                'gains': len(gains), 'losses': len(losses), 'output_pct': output}})
    return out


def _upgrade_card(hero, part, current, it, gains, names, eff=None, effects=None):
    slot = part.replace('_', ' ').lower()
    effect = f" Effect on {hero['name']} now: {effects.text(eff)}." if eff else ''
    output = eff['offence_pct'] if eff else None
    if current:
        return {'id': f"gear-{hero['hero_key']}-{part}", 'area': 'gear',
                'title': f"{hero['name']}: {it['name']} over {current['name']}",
                'detail': (f"Better or equal on the compared stats: {_changes_text(gains, names)} "
                           f"({it['location']}, lvl {it['level']} {it['grade']} vs lvl {current['level']} {current['grade']}). "
                           f'Equippable now. Conditional, applied and unique effects need in-game verification.{effect}'),
                'basis': 'catalog', 'confidence': 'medium', 'priority': 2,
                'metrics': {'hero_key': hero['hero_key'], 'part': part, 'unique_id': it['unique_id'], 'output_pct': output}}
    return {'id': f"gear-{hero['hero_key']}-{part}", 'area': 'gear',
            'title': f"{hero['name']}: empty {slot} slot, equip {it['name']}",
            'detail': (f"{it['name']} (lvl {it['level']} {it['grade']}, {it['location']}) adds {_changes_text(gains, names)}. "
                       f'The slot is empty. Equippable now; conditional and applied effects need verification.{effect}'),
            'basis': 'catalog', 'confidence': 'medium', 'priority': 1,
            'metrics': {'hero_key': hero['hero_key'], 'part': part, 'unique_id': it['unique_id'], 'output_pct': output}}


MATERIAL_USE = {'DECORATION': 'decoration (adds stats to gear)', 'ENGRAVING': 'engraving (adds stats to gear)',
                'INSCRIPTION': 'inscription (adds stats to gear)', 'CRAFTING': 'crafting ingredients',
                'OFFERING': 'offering', 'SOULSTONE': 'boss summoning', 'ETC': 'other'}


def synthesis_suggestion(group):
    """What one synthesis group can do: only when it holds at least one full set of 9."""
    option = max(group['options'], key=lambda o: o['tier'], default=None)
    if not option or option['batches'] < 1:
        return []
    amount, batches = option['material_amount'], option['batches']
    quantity = option.get('eligible_quantity', group['quantity'])
    grade, kind = group['grade'].title(), group['synthesis_type'].lower()
    chances = ', '.join(f"{c['chance'] * 100:.1f}% {c['grade'].title()}" for c in group['result_chances'])
    uses = {}
    for it in option.get('eligible_items', group['items']):
        label = MATERIAL_USE.get(it.get('material_type'), 'gear' if kind == 'gear' else 'other')
        names = uses.setdefault(label, {})
        names[it['name']] = names.get(it['name'], 0) + (it['quantity'] or 0)
    pool = '; '.join(f"{label}: {', '.join(f'{name} ×{qty}' for name, qty in names.items())}"
                     for label, names in uses.items())
    levels = (f" Estimated result level {option['result_levels'][0]['level']}–{option['result_levels'][-1]['level']} (tier {option['tier']}; depends on the selected batch)."
              if kind == 'gear' and option['result_levels'] else '')
    warn = (' Crafting ingredients and stat materials are consumed: keep the ones you plan to use in Crafting or '
            'Decoration/Engraving.' if any(k in uses for k in (MATERIAL_USE['CRAFTING'], MATERIAL_USE['DECORATION'],
                                                               MATERIAL_USE['ENGRAVING'], MATERIAL_USE['INSCRIPTION'])) else '')
    return [{'id': f"cube-synthesis-{group['synthesis_type']}-{group['grade']}", 'area': 'cube',
             'title': f"Synthesis: {quantity} eligible {grade} {kind} items → up to {batches} result(s)",
             'detail': (f"Any {amount} {grade} {kind} items make one item (game rule: same grade and type, any mix): "
                        f"Catalog weights: {chances}. With {quantity} eligible items you can do up to {batches} and keep {quantity - batches * amount}."
                        f"{levels} Select tier {option['tier']} in the game; input level range: {option.get('input_level_range') or 'not applicable'}. "
                        f"You pick which {amount} go in each synthesis, from — {pool}.{warn}"),
             'basis': 'catalog', 'confidence': 'medium', 'priority': 4,
             'metrics': {'grade': group['grade'], 'type': group['synthesis_type'], 'quantity': quantity,
                         'batches': batches, 'material_amount': amount}}]


def cube_suggestions(cube, gold):
    out = []
    for recipe in cube['recipes']:
        for sub in recipe['sub_recipes']:
            if sub['unlockable_now'] and not sub['unlocked']:
                name = sub['name'] if sub['name'] and sub['name'] != '-' else recipe['type']
                ok = gold >= (sub['unlock_cost'] or 0)
                out.append({'id': f"cube-unlock-{sub['key']}", 'area': 'cube',
                            'title': f"Unlock {recipe['type'].title()} {name}",
                            'detail': f"Cube level reached; costs {sub['unlock_cost']:,.0f} gold"
                                      + ('.' if ok else ' (not enough gold yet).'),
                            'basis': 'catalog', 'confidence': 'high', 'priority': 2 if ok else 3,
                            'metrics': {'key': sub['key'], 'cost': sub['unlock_cost']}})
    for g in cube['synthesis']['groups']:
        out.extend(synthesis_suggestion(g))
    return out


def storage_suggestions(containers, chests):
    out = []
    inv = containers['inventory']
    if inv['slots_free'] <= 3:
        out.append({'id': 'storage-inventory', 'area': 'storage', 'title': 'Inventory almost full',
                    'detail': f"{inv['slots_free']} free slot(s). Make room before opening chests or receiving items.",
                    'basis': 'observed', 'confidence': 'high', 'priority': 1, 'metrics': {'free': inv['slots_free']}})
    stash = containers['stash']
    if stash['slots_unlocked'] and stash['slots_free'] / stash['slots_unlocked'] < 0.1:
        out.append({'id': 'storage-stash', 'area': 'storage', 'title': 'Stash above 90%',
                    'detail': f"{stash['slots_free']} free of {stash['slots_unlocked']}. Clear it or buy stash runes.",
                    'basis': 'observed', 'confidence': 'high', 'priority': 2, 'metrics': {'free': stash['slots_free']}})
    pending = sum(s['quantity'] for s in chests['stock'])
    if pending:
        out.append({'id': 'storage-chests', 'area': 'storage', 'title': f'{pending} chest(s) waiting to be opened',
                    'detail': f"Inventory has {chests['inventory_free']} free slot(s) for their contents.",
                    'basis': 'observed', 'confidence': 'high', 'priority': 2, 'metrics': {'chests': pending}})
    return out


def hero_suggestions(heroes):
    return [{'id': f"hero-points-{h['hero_key']}", 'area': 'heroes',
             'title': f"{h['name']} has {h['ability_points']} unspent point(s)",
             'detail': 'Allocate them in the hero panel.' + ('' if h['in_party'] else ' (Not in the active party.)'),
             'basis': 'observed', 'confidence': 'high', 'priority': 2 if h['in_party'] else 4,
             'metrics': {'hero_key': h['hero_key'], 'points': h['ability_points']}}
            for h in heroes if h['unlocked'] and h['ability_points']]


def _stats_text(row):
    """' Stats now: Priest Attack Damage +3.1%; ...' from the stat snapshots around the change."""
    heroes = row.get('stat_change') or []
    parts = []
    for h in heroes:
        stats = sorted((s for s in h['stats'] if s['pct'] is not None), key=lambda s: -abs(s['pct']))[:3]
        if stats:
            parts.append(f"{h['name']} " + ', '.join(f"{s['name']} {s['pct']:+.1f}%" for s in stats))
    return f" Stats (read from the game): {'; '.join(parts)}." if parts else ''


def act_boss_suggestions(result):
    """One card for the last failed act boss: 'to beat this boss you need to'."""
    out = []
    for t in (result or {}).get('targets') or []:
        now, best = t['now'], t['both']
        if not now:
            continue
        fights = [f for f in result.get('failed') or [] if f['stage_key'] == t['stage']]
        seen = ''
        if fights:
            hits = fights[0]['hits_taken'] or {}
            one_shot = [n for n, v in hits.items() if v == 0]
            seen = (f"In your last {len(fights)} attempt(s) the party was wiped in about {min(f['duration_s'] for f in fights):.0f}"
                    f"-{max(f['duration_s'] for f in fights):.0f} s"
                    + (f"; {', '.join(one_shot)} died between HP readings (number of hits unknown)" if one_shot else '') + '. ')
        sim = t.get('simulation') or {}
        if sim.get('now'):
            def outcome(x):
                if not x:
                    return '?'
                if not x['won']:
                    return f"lose with the boss at {x['boss_hp_left']:.0%}"
                # Damage per second varied ±25% between recorded fights: a narrow win is a coin flip.
                return (f"win in {x['kill_s']:.0f} s" if x['reach'] >= RETRY_REACH else
                        f"win narrowly ({x['reach']:.2f}× the boss HP; a safe win needs {RETRY_REACH:g}×)")
            m = t.get('mechanics') or {}
            race = (f"Simulated with this boss's attacks learned from {m.get('fights')} recorded fight(s): today you "
                    f"{outcome(sim['now'])}; with the plan you {outcome(sim['plan'])}.")
        else:
            race = f"Boss: {t['boss_hp']:,.0f} HP, {t['boss_damage']:.0f} per hit. Your party lasts ≈{now['survive_s']:.0f} s"
            race += (f" and needs ≈{now['kill_s']:.0f} s to kill it (margin {now['margin']:.2f}; with the plan "
                     f"{best['margin']:.2f}; retry from {result['safety']:g})." if now['kill_s'] else
                     '; its kill time appears after one recorded stage-boss fight.')
        steps = t['steps']
        winnable = bool(t.get('winnable_with_plan'))
        out.append({'id': f"act-boss-{t['stage']}", 'area': 'heroes',
                    'title': f"To beat act boss {t['label']}" + (': winnable with the plan' if winnable else ''),
                    'detail': (seen + race + ' ' + ' '.join(f"{i}) {step}." for i, step in enumerate(steps, 1))
                               + (' Checked on your recorded fights against this boss (see the Act boss tab).' if sim.get('now') else
                                  ' Estimates: boss attack speed and skills come from game data, healing is not counted.')),
                    'steps': steps,
                    'basis': 'hypothesis', 'confidence': 'low', 'priority': 2,
                    'metrics': {'stage': t['stage'], 'margin': now['margin'], 'best_margin': best and best['margin'],
                                'retry_level': (t.get('retry') or {}).get('level')}})
    return out


def purchase_suggestions(rows):
    """The most recent power change: its measured effect, how much is missing, or why it cannot be measured.
    The stat effect is known right away (from the game), before enough runs measure the gold/XP effect."""
    row = rows[0] if rows else None
    if not row:
        return []
    what = '; '.join(c['label'] for c in row['changes'][:3])
    what += f" (+{len(row['changes']) - 3} more)" if len(row['changes']) > 3 else ''
    where = f"{row['stage_label']}: {row['runs_before']} runs before, {row['runs_after']} after"
    if row['status'] in ('no comparable runs', 'too few runs before'):
        return [{'id': 'purchase-latest', 'area': 'runes', 'title': f'Your last change cannot be measured: {what}',
                 'detail': (f"{where}. The previous change or a stage switch left fewer than 3 complete runs on the "
                            'same stage before it, and runs before a change cannot be added later. To measure a change, '
                            f"play 3+ runs on one stage, make the change, then play 3+ more there.{_stats_text(row)}"),
                 'basis': 'observed', 'confidence': 'insufficient', 'priority': 4, 'metrics': {'status': row['status']}}]
    if row['status'] == 'collecting':
        return [{'id': 'purchase-latest', 'area': 'runes', 'title': f'Measuring your last change: {what}',
                 'detail': (f"{where}. About {row['runs_needed']} more complete run(s) on the same stage to measure "
                            f"its gold/XP effect.{_stats_text(row)}"),
                 'basis': 'observed', 'confidence': 'insufficient', 'priority': 3, 'metrics': {'status': row['status']}}]

    def change(label, m):
        if not m:
            return None
        spread = f" ±{m['pct_se']:.1f}" if m.get('pct_se') is not None else ''
        verdict = 'significant' if m['significant'] else 'within noise'
        return f"{label} {m['pct']:+.1f}%{spread} ({verdict})"
    parts = [change('gold/h', row['metrics'].get('gold_h')), change('run time', row['metrics'].get('duration_s')),
             change('XP/h', row['metrics'].get('xp_h'))]
    expected = row['expected'].get('gold_per_run')
    gold_run = row['metrics'].get('gold_per_run')
    check = (f" Gold per run {gold_run['pct']:+.1f}% vs {expected:+.1f}% expected if percent bonuses add up."
             if expected is not None and gold_run else '')
    levels = (f" {row['level_ups']} hero level-up(s) happened in the compared runs, which also speed runs up."
              if row['level_ups'] else '')
    return [{'id': 'purchase-latest', 'area': 'runes', 'title': f'Measured effect of your last change: {what}',
             'detail': f"{where}. " + ', '.join(p for p in parts if p) + '.' + check + levels + _stats_text(row),
             'basis': 'observed', 'confidence': 'low' if row['level_ups'] else 'medium', 'priority': 3,
             'metrics': {'status': row['status'], 'gold_h': row['metrics'].get('gold_h'),
                         'level_ups': row['level_ups']}}]


# --- assembly ---------------------------------------------------------------
def suggestions(app, hours=168, locale='en-US'):
    catalog = app.catalog
    snapshot = app.save()
    reading = app.collector.last_combat if app.collector else None
    stages = analytics.stage_comparison(app.store, catalog, hours, locale, reading=reading)
    party = party_key(snapshot['party'])
    evidence = [e for e in stages['current_evidence'] if e['party'] == party]
    history = [e for e in stages['evidence'] if e['party'] == party]
    now = live(app, locale)
    current_stage = (now['runtime'] or {}).get('stage_key') or snapshot['current_stage']

    def reference_stage(metric):
        """Confirmed best with the current build, else the best measured with earlier builds."""
        best = best_stage(evidence, metric)[0]
        return (best, False) if best else (best_stage(history, metric, comparable=False)[0], True)
    (best_gold, gold_old), (best_xp, xp_old) = reference_stage('gold_h'), reference_stage('xp_h')
    ref = {'gold_h': best_gold['gold_h'] if best_gold else None, 'xp_h': best_xp['xp_h'] if best_xp else None,
           'clears_per_h': best_gold['clears_per_h'] if best_gold else None,
           'kills_per_h': best_gold['kills_per_h'] if best_gold else None,
           'stage_label': best_gold['label'] if best_gold else None,
           'historical': {'gold_h': bool(best_gold) and gold_old, 'xp_h': bool(best_xp) and xp_old}}
    totals = items.rune_totals(snapshot, catalog, locale)
    containers = items.containers(snapshot, catalog, locale)
    hero_rows = hero_list(app, locale)
    effects = PowerContext(app, snapshot, locale)
    effects = effects if effects.available else None
    rows = (stage_suggestions(evidence, current_stage, stages['recommendation']['untested_candidates'],
                              stages['recommendation']['survival_reference'], history)
            + rune_suggestions(items.rune_tree(snapshot, catalog, locale), totals, snapshot['gold'], ref, effects)
            + purchase_suggestions(purchases(app.store, catalog, hours, locale)['purchases'])
            + gear_suggestions(items.equipment(snapshot, catalog, locale), containers,
                               {h['hero_key']: h for h in hero_rows}, catalog.level_thresholds, ref['xp_h'],
                               {int(k): {'MAIN_WEAPON': h['MainWeaponGearType'], 'SUB_WEAPON': h['SubWeaponGearType']}
                                for k, h in catalog.heroes.items()},
                               [g['GRADE'] for g in catalog.table('GradeInfoData')], effects)
            + cube_suggestions(cube_view(snapshot, catalog, locale), snapshot['gold'])
            + storage_suggestions(containers, items.chests(snapshot, catalog, locale))
            + hero_suggestions(hero_rows)
            + act_boss_suggestions(challenges(app, locale)))
    return {
        'generated_utc': datetime.now(timezone.utc).isoformat(), 'hours': hours, 'gold': snapshot['gold'],
        'save_utc': snapshot['last_saved_utc'],
        'reference': ref, 'current_stage': current_stage,
        'current_stage_label': catalog.stage_label(current_stage, locale) if current_stage else None,
        'stages': sorted(evidence, key=lambda e: -(e.get('gold_h') or 0)),
        'suggestions': sorted(rows, key=lambda s: s['priority']),
        'note': ('Stage rates are sum of gold or XP / sum of time over complete runs with the current party, '
                 'loadout and hero levels (within 1 level); a change that was undone does not count. '
                 'Without a confirmed stage for this build, the best stage measured with earlier builds is shown '
                 'with low confidence. ' + describe() + ' '
                 'Rune estimates use the game text for each stat ("% increased", "+N per boss kill") and assume '
                 'percent bonuses stack additively; the reference rates are the confirmed best stage, or the '
                 'best earlier one when none is confirmed yet (see reference.historical).'),
    }
