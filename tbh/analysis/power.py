"""Effect of a gear swap or a rune level on the stats the game actually uses.

The save stores item and rune values as raw integers; the game turns them into stat modifiers
with a fixed scale per (stat, mode) (e.g. AttackDamage FLAT ×1, increases ÷1000, AttackSpeed
FLAT ÷100). Instead of hardcoding those scales we learn them from the live game: the sum of the
raw values of every equipped item (or owned rune) must equal the game's ITEM (AccountStatus)
modifiers for that stat, at one consistent power of ten for every hero. A (stat, mode) whose scale
is not proven that way is reported as unknown, never guessed.

With proven scales a change is recomputed with the game's own formula (analysis/combat.py),
which reproduced every stat read live, so stat deltas are exact for the current loadout.
The offence index on top of them is an estimate (see `offence`).
"""
import math

from .combat import layer_value, offence

# Rune stats that become hero stat modifiers, from the game text ("+{0}" flat, "{0}% Increased" additive).
RUNE_TARGETS = {
    'AllHeroAttackDamage': ('AttackDamage', 'FLAT'), 'AllHeroAttackDamagePercent': ('AttackDamage', 'ADDITIVE'),
    'AllHeroAttackSpeed': ('AttackSpeed', 'ADDITIVE'), 'AllHeroMoveSpeed': ('MovementSpeed', 'ADDITIVE'),
    'AllHeroArmor': ('Armor', 'FLAT'), 'AllHeroArmorPercent': ('Armor', 'ADDITIVE'),
}
SCALES = [10.0 ** e for e in range(-4, 2)]
SHOWN = ('AttackDamage', 'AttackSpeed', 'CriticalChance', 'CriticalDamage', 'MaxHp', 'Armor', 'MovementSpeed',
         'IncreaseExpAmount')
# Shown as a change in points: it adds to the other XP bonuses, so its percent change is not the XP change (D008).
POINTS = {'IncreaseExpAmount'}


def sums(modifiers, source):
    """{(stat, mode): total} of one modifier source (additive totals; multiplicative ones multiply)."""
    out = {}
    for m in modifiers or []:
        if m['source'] != source:
            continue
        key = (m['stat'], m['mode'])
        out[key] = out.get(key, 1.0 if m['mode'] == 'MULTIPLICATIVE' else 0.0)
        out[key] = out[key] * (1 + m['value']) if m['mode'] == 'MULTIPLICATIVE' else out[key] + m['value']
    return out


def learn_scales(observations):
    """observations: [(game_sums, raw_sums)] with {(stat, mode): value} each.

    Returns {(stat, mode): scale} for keys whose game total equals raw × one power of ten in every
    observation where the key appears on either side. Multiplicative raw values are summed: they are
    only proven when an observation holds a single such modifier."""
    candidates, rejected = {}, set()
    for game, raw in observations:
        for key in set(game) | set(raw):
            g, r = game.get(key), raw.get(key)
            if key[1] == 'MULTIPLICATIVE' and g is not None:
                g -= 1.0   # product (1 + v) of a single modifier back to v
            if not r or g is None:
                if r or (g and abs(g) > 1e-9):
                    rejected.add(key)   # one side has it and the other does not
                continue
            fit = [s for s in SCALES if math.isclose(r * s, g, rel_tol=1e-4, abs_tol=1e-6)]
            if len(fit) != 1 or candidates.get(key, fit[0]) != fit[0]:
                rejected.add(key)
            else:
                candidates[key] = fit[0]
    return {k: v for k, v in candidates.items() if k not in rejected}


def to_mods(raw, scales, source):
    """Raw {(stat, mode): value} -> (modifiers, keys whose scale is unknown)."""
    mods, unknown = [], []
    for key, value in raw.items():
        if not value:
            continue
        if key not in scales:
            unknown.append(key)
            continue
        mods.append({'stat': key[0], 'mode': key[1], 'value': value * scales[key], 'source': source, 'layer': 'base'})
    return mods, unknown


def final_stats(modifiers):
    """{stat: final value} recomputed from modifiers: base layer first, then the dynamic one."""
    by_stat = {}
    for m in modifiers:
        by_stat.setdefault(m['stat'], []).append(m)
    out = {}
    for stat, mods in by_stat.items():
        base = layer_value(0.0, [m for m in mods if m['layer'] == 'base'])
        out[stat] = layer_value(base, [m for m in mods if m['layer'] == 'dynamic'])
    return out


def change(modifiers, source, remove=(), add=()):
    """Stat changes when the `remove` modifiers of `source` (base layer) are replaced by `add`.

    The source's modifiers are folded into one total per (stat, mode), which the formula allows:
    flats and increases add up, multipliers multiply."""
    before, after = final_stats(modifiers), final_stats(apply(modifiers, source, remove, add))
    stats = {}
    for stat in set(before) | set(after):
        a, b = before.get(stat, 0.0), after.get(stat, 0.0)
        if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
            stats[stat] = {'before': a, 'after': b, 'pct': (b / a - 1) * 100 if a else None}
    o_before, o_after = offence(before), offence(after)
    return {'stats': stats, 'offence_before': o_before, 'offence_after': o_after,
            'offence_pct': (o_after / o_before - 1) * 100 if o_before and o_after else None}


def apply(modifiers, source, remove=(), add=()):
    """The modifier list with `remove` taken out of and `add` put into `source`'s base layer."""
    totals = sums([m for m in modifiers if m['layer'] == 'base'], source)
    for sign, mods in ((-1, remove), (1, add)):
        for m in mods:
            key = (m['stat'], m['mode'])
            if key[1] == 'MULTIPLICATIVE':
                factor = 1 + m['value']
                totals[key] = totals.get(key, 1.0) * (factor if sign > 0 else 1 / factor)
            else:
                totals[key] = totals.get(key, 0.0) + sign * m['value']
    kept = [m for m in modifiers if not (m['source'] == source and m['layer'] == 'base')]
    return kept + [{'stat': k[0], 'mode': k[1], 'source': source, 'layer': 'base',
                    'value': v - 1 if k[1] == 'MULTIPLICATIVE' else v} for k, v in totals.items()]


def xp_factor(rune_percent, pet_raw, hero_stat):
    """Multiple of the catalog XP a hero gets per kill (D009, per-kill capture 2026-10-01):
    (1 + rune %) x (1 + pet / 1000) + (hero IncreaseExpAmount - 1). Measured 2.504 for heroes with
    stat 1.0 against 2.52 from this formula (-0.6%, unexplained); the hero term matched exactly
    (Priest +0.036 -> 2.539 measured). Same shape for gold, with the pet's gold value."""
    return (1 + rune_percent / 100) * (1 + pet_raw / 1000) + (hero_stat - 1)


def split_account(modifiers, rune_part):
    """Relabel the AccountStatus share that owned runes explain as source 'Runes' (D007); what
    remains (pet, other account bonuses) stays AccountStatus. `rune_part`: {(stat, mode): value}."""
    out, totals = [], {}
    for m in modifiers:
        key = (m['stat'], m['mode'])
        if m['source'] == 'AccountStatus' and m['layer'] == 'base' and key in rune_part:
            totals[key] = totals.get(key, 0.0) + m['value']
        else:
            out.append(m)
    for key, total in totals.items():
        runes = rune_part[key]
        out.append({'stat': key[0], 'mode': key[1], 'value': runes, 'source': 'Runes', 'layer': 'base'})
        if not math.isclose(total, runes, rel_tol=1e-6, abs_tol=1e-9):
            out.append({'stat': key[0], 'mode': key[1], 'value': total - runes, 'source': 'AccountStatus', 'layer': 'base'})
    return out


def summary(effect, names, limit=4):
    """'Attack Damage +9.4%, Armor −3.1%' for the shown stats that change, biggest first."""
    rows = [(stat, v) for stat, v in effect['stats'].items() if stat in SHOWN and v['pct'] is not None]
    rows.sort(key=lambda r: (r[0] not in POINTS, -abs(r[1]['pct'])))
    return ', '.join(f"{names(stat)} {v['after'] - v['before']:+.3f}" if stat in POINTS else f"{names(stat)} {v['pct']:+.1f}%"
                     for stat, v in rows[:limit])
