"""Can the party win a boss fight, and what would change the outcome (estimates on measured parts).

A fight is a race: the party must take the boss's HP before the boss takes every hero's HP.

- Boss hit per hero: the validated hit formula (analysis/threat.py, D009).
- Hits a hero survives: ceil(HP / hit) (Priest 6.25 -> dies on the 7th; 6-7 HP drops observed in the
  two failed 2210 fights). The boss attacks one hero at a time, so the party lasts the sum of them.
- Boss attacks per second: 100 / catalog AttackSpeed. Fitted on the recorded 2210 fights (D011): the Priest
  lost HP in 8 readings over 14 s before dying (~0.6 attacks/s for AttackSpeed 170); AttackSpeed / 100 as
  attacks per second made the party last ~3x too short. Skills add damage (Value / 1000 of a base attack),
  catalog only. A back-line hit ~5 s after contact killed the Ranger in all three fights: not modelled.
- Kill time: boss HP / single-target damage per second measured on stage bosses (runs.combat), scaled
  by the party's base-attack output when stats change (an estimate: skills and elements not in it).
- Healing, block, dodge and boss adds are not modelled; healing makes real survival longer.

`margin` = time the party lasts / time to kill the boss: above 1 the party should win.
"""
import math

from ..catalog.catalog import num
from .combat import offence
from .threat import hit

SAFETY = 1.5      # retry only when the estimate wins by this much: the model is optimistic (adds, untested parts)


def boss_target(catalog, stage_key):
    """The boss of a stage as the model needs it, or None. Act boss stages have no multipliers."""
    stage = catalog.stages.get(str(stage_key)) or {}
    boss_key = stage.get('BossMonsterKey')
    monster = catalog.monsters.get(boss_key or '')
    scale = catalog.stage_levels.get(stage.get('StageLevel'))
    if not monster or not scale:
        return None
    act = stage['STAGETYPE'] != 'NORMAL'
    mult = lambda col: 1.0 if act else num(stage.get(col), 1000) / 1000
    skills = catalog.index('SkillInfoData', 'SkillKey')
    rate = 100 / float(monster['AttackSpeed'])
    # Extra damage per attack from the boss's skills (catalog Value, 1000 = one base attack). Assumed to
    # replace a base attack when used, so only the excess counts; not validated against a recorded fight.
    extra, catalog_skills = 0.0, []
    for key in (monster.get('SkillKey') or '').split():
        row = skills.get(key) or {}
        value, trigger = num(row.get('Value'), 1000) / 1000, row.get('ACTIVATIONTYPE')
        every = num(row.get('ActivationValue'))
        if trigger in ('COOLDOWN', 'BASEATTACK_COUNT') and every:
            catalog_skills.append({'skill_key': num(key), 'value': value, 'trigger': trigger, 'every': every})
        if trigger == 'COOLDOWN' and every:
            extra += (value - 1) / (every * rate)
        elif trigger == 'BASEATTACK_COUNT' and every:
            extra += (value - 1) / every
    return {'stage': num(stage_key), 'act_boss': act, 'monster_key': num(boss_key), 'level': num(stage['StageLevel']),
            'hp': float(monster['MaxLife']) * int(scale['MonsterHpMultiplier']) / 1000 * mult('BossHpMultiplier'),
            'damage': float(monster['AttackDamage']) * int(scale['MonsterAtkDmgMultiplier']) / 1000 * mult('BossDamageMultiplier'),
            'attacks_per_s': rate, 'skill_factor': 1 + extra, 'skills': catalog_skills}


def fight(target, heroes, k, dps, dps_offence):
    """Estimated race for one party state. `heroes`: [{'hero_key', 'final', 'max_hp', 'resistances'}];
    `dps`: measured single-target damage per second at party output `dps_offence`."""
    rows, hits_total, smooth = [], 0, 0.0
    for h in heroes:
        one = hit(target['damage'], h, target['level'], k, target['monster_key'])
        if not one or not one['hit']:
            return None
        exact = (h.get('max_hp') or h['final'].get('MaxHp')) / one['hit']
        hits = math.ceil(exact)
        hits_total += hits
        smooth += exact
        hp = h.get('max_hp') or h['final'].get('MaxHp')
        rows.append({'hero_key': h['hero_key'], 'hit': one['hit'], 'hits': hits, 'hp': hp,
                     # Max HP to add so the hero survives one more boss hit (cheapest single step).
                     'hp_for_next_hit': math.floor(one['hit'] * hits - hp) + 1,
                     'cap_uncertain': one['cap_uncertain']})
    survive_s = hits_total / (target['attacks_per_s'] * target['skill_factor'])
    output = sum(offence(h['final']) or 0 for h in heroes)
    kill_s = None
    if dps and dps_offence and output:
        kill_s = target['hp'] / (dps * output / dps_offence)
    return {'heroes': rows, 'survive_s': survive_s, 'kill_s': kill_s, 'offence': output,
            'margin': survive_s / kill_s if kill_s else None,
            # margin = survive x dps / HP and dps scales with output, so plans compare by survive x output
            # whether or not damage per second has been measured yet. Fractional hits keep small stat
            # gains visible to the optimizer (whole hits make most of them look worthless).
            'race': smooth / (target['attacks_per_s'] * target['skill_factor']) * output}


def score(result):
    """What a plan maximizes: survival time x base-attack output, proportional to the margin."""
    return result['race'] if result else -1.0


def best_points(options, levels, evaluate, extra_points=0, max_moves=200, valid=lambda plan: True):
    """Greedy point plan for one party. `options`: {hero_key: {attribute_key: max_level}} of the passives
    the model can value; `levels`: current {hero_key: {attribute_key: level}}; `evaluate(levels)` -> score.
    Moves one point at a time between a hero's modelled passives (refund is free), then spends
    `extra_points` per hero (future levels) the same way. Points in other attributes are never touched.
    `valid(plan)` rejects plans the game would not allow (attribute groups unlocked by allocated points)."""
    plan = {h: dict(v) for h, v in levels.items()}
    best = evaluate(plan)

    def candidates():
        for hero, opts in options.items():
            for b, cap in opts.items():
                if plan[hero].get(b, 0) >= cap:
                    continue
                for a in opts:
                    if a != b and plan[hero].get(a, 0) > 0:
                        yield hero, a, b
    for _ in range(max_moves):
        found = None
        for hero, a, b in candidates():
            plan[hero][a] -= 1
            plan[hero][b] = plan[hero].get(b, 0) + 1
            value = evaluate(plan) if valid(plan) else -1.0
            plan[hero][b] -= 1
            plan[hero][a] += 1
            if value > best + 1e-9 and (found is None or value > found[0]):
                found = (value, hero, a, b)
        if not found:
            break
        best, hero, a, b = found
        plan[hero][a] -= 1
        plan[hero][b] = plan[hero].get(b, 0) + 1
    for _ in range(extra_points):
        for hero, opts in options.items():
            choice = None
            for b, cap in opts.items():
                if plan[hero].get(b, 0) >= cap:
                    continue
                plan[hero][b] = plan[hero].get(b, 0) + 1
                value = evaluate(plan) if valid(plan) else None
                plan[hero][b] -= 1
                if value is None:
                    continue
                if choice is None or value > choice[0]:
                    choice = (value, b)
            if choice:
                plan[hero][choice[1]] = plan[hero].get(choice[1], 0) + 1
                best = choice[0]
    return plan, best
