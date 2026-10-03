"""How hard a stage hits each hero, from the catalog and the hero's current stats (D009).

HP lost per hit = enemy damage x (1 - armor reduction) - Damage Absorption, enemy damage =
AttackDamage x MonsterAtkDmgMultiplier / 1000 (x BossDamageMultiplier / 1000 for the stage boss).
Elemental attacks skip armor and use the resistance factor instead. Validated on hits at stage
level 45 with reductions of 0.47-0.68; above the hero cap (0.75 or 0.85, unknown which) and for
enemies whose element is unknown, results are flagged rather than trusted.

Not modelled: block, dodge, skills of enemies, how many enemies hit at once, healing.
"""
from ..catalog.catalog import num
from .combat import armor_reduction, resistance

# Enemies whose base attack was observed hitting with an element (no armor, resistance applies):
# Fire Elemental 24.2 damage took 20.74 HP = 24.2 x 1.20 - 8.3 absorption, 136 times (D009).
# The catalog does not record attack elements: other enemies count as physical until observed.
OBSERVED_ELEMENTAL = {20091: 'Fire'}


def enemy_damage(catalog, stage_key, monster_key, boss=False):
    stage = catalog.stages.get(str(stage_key)) or {}
    scale = catalog.stage_levels.get(stage.get('StageLevel'))
    row = catalog.monsters.get(str(monster_key))
    if not scale or not row or not row.get('AttackDamage'):
        return None
    damage = float(row['AttackDamage']) * int(scale['MonsterAtkDmgMultiplier']) / 1000
    if boss:
        damage *= num(stage.get('BossDamageMultiplier'), 1000) / 1000
    return damage


def hit(damage, hero, level, k, monster_key=None):
    """{'hit', 'reduction', 'element', 'cap_uncertain'} for one hit on `hero`
    ({'final': stats, 'resistances': {...}, 'max_hp'}), or None when an input is unknown."""
    final = hero.get('final') or {}
    if damage is None or not k or level is None or final.get('Armor') is None:
        return None
    element = OBSERVED_ELEMENTAL.get(monster_key)
    absorption = final.get('DamageAbsorption') or 0.0
    if element:
        res = resistance((hero.get('resistances') or {}).get('individual'), (hero.get('resistances') or {}).get('caps'))
        factor = ((res or {}).get(element) or {}).get('damage_factor')
        if factor is None:
            return None
        reduction = 1 - factor
    else:
        reduction = armor_reduction(final['Armor'], damage, level, k)
    taken = max(0.0, damage * (1 - reduction) - absorption)
    max_hp = hero.get('max_hp') or final.get('MaxHp')
    return {'hit': taken, 'reduction': reduction, 'element': element,
            'fraction': taken / max_hp if max_hp else None,
            'hits_to_die': max_hp / taken if max_hp and taken > 0 else None,
            'cap_uncertain': not element and reduction > min(k['bfml'], k['bfmm'])}


def stage_threat(catalog, stage_key, hero, k):
    """Worst normal-monster hit and the boss hit of a stage on one hero, or None."""
    stage = catalog.stages.get(str(stage_key)) or {}
    level = num(stage.get('StageLevel'))
    if level is None:
        return None
    worst = None
    for pair in (stage.get('Monsters') or '').split():
        key = num(pair.split('_')[0])
        h = hit(enemy_damage(catalog, stage_key, key), hero, level, k, key)
        if h and (worst is None or h['hit'] > worst['hit']):
            worst = {**h, 'monster_key': key}
    boss_key = num(stage.get('BossMonsterKey'))
    boss = hit(enemy_damage(catalog, stage_key, boss_key, boss=True), hero, level, k, boss_key) if boss_key else None
    if worst is None and boss is None:
        return None
    return {'stage': num(stage_key), 'level': level, 'normal': worst,
            'boss': {**boss, 'monster_key': boss_key} if boss else None}


def party_threat(catalog, stage_key, heroes, k):
    """Per hero threat plus the worst boss hit fraction across the party (the weakest link)."""
    rows = {}
    for h in heroes:
        t = stage_threat(catalog, stage_key, h, k)
        if t:
            rows[h['hero_key']] = t
    fractions = [t['boss']['fraction'] for t in rows.values() if t.get('boss') and t['boss'].get('fraction') is not None]
    return {'heroes': rows, 'worst_boss_fraction': max(fractions) if fractions else None} if rows else None


def live_heroes(reading):
    """Heroes of a combat reading in the shape `hit` expects."""
    return [{'hero_key': h['hero_key'], 'final': (h.get('stats') or {}).get('final') or {},
             'resistances': h.get('resistances'), 'max_hp': h.get('max_hp')}
            for h in (reading or {}).get('heroes') or [] if (h.get('stats') or {}).get('final')]
