"""Combat view for the API: party health, stats and their origins, buffs, resistances, enemies."""
from datetime import datetime, timezone

from ..analysis.combat import attack_shares, origins, resistance
from ..analysis.threat import enemy_damage, hit
from ..analysis.power import split_account
from ..catalog.catalog import num
from .items import stat_name
from .power import PowerContext

# Shown on each hero card; the full list stays available in the details.
KEY_STATS = ('AttackDamage', 'AttackSpeed', 'CriticalChance', 'CriticalDamage', 'MaxHp', 'Armor',
             'CooldownReduction', 'IncreaseExpAmount', 'MovementSpeed', 'DamageAbsorption')
SOURCE_LABELS = {'BASE': 'Hero base', 'ITEM': 'Equipment', 'ATTRIBUTE': 'Attribute points', 'PASSIVE': 'Passive skills',
                 'Runes': 'Runes', 'AccountStatus': 'Account, not runes (pet…)', 'StatusEffect': 'Status effect',
                 'BuffSkill': 'Buff skill', 'ENVIROUNMENT': 'Stage environment'}
# Without proven rune scales the account share cannot be split.
UNSPLIT_ACCOUNT = 'Account (runes, pet…)'


def _age(iso):
    return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds(), 1) if iso else None


def buff_info(catalog, group, locale='en-US'):
    """Name and kind of a buff group: skill buffs are named after their skill."""
    skill = next((r for r in catalog.table('SkillInfoData') if r.get('BuffGroupKey') == str(group)), None)
    if skill:
        return {'name': catalog.localize(skill['SkillNameKey'], locale) or f'Skill {skill["SkillKey"]}',
                'kind': 'skill', 'skill_key': num(skill['SkillKey'])}
    return {'name': None, 'kind': 'other', 'skill_key': None}


def _buff(catalog, buff, hero, locale):
    if 'quality' in buff:
        return {'group': buff['group'], 'name': f"Buff {buff['group']}", 'quality': buff['quality']}
    info = buff_info(catalog, buff['group'], locale)
    mods = buff['modifiers']
    if info['kind'] == 'other' and mods and all(m['source'] == 'ENVIROUNMENT' for m in mods):
        info['kind'], info['name'] = 'environment', 'Stage environment'
    expiry = dict(buff['expiry'])
    if expiry['kind'] == 'attack_count' and expiry.get('threshold') is not None and hero['attack_count'] is not None:
        # Removed when the attack counter becomes strictly greater than the threshold (D003).
        expiry['attacks_left'] = max(0, expiry['threshold'] - hero['attack_count'])
    return {'group': buff['group'], 'name': info['name'] or f"Buff {buff['group']}", 'kind': info['kind'],
            # The caster link is reused by environment buffs (D002): only skill buffs show it.
            'caster': catalog.hero_name(buff['caster_key'], locale) if info['kind'] == 'skill' and buff['caster_key'] else None,
            'modifiers': None if mods is None else [{**m, 'name': stat_name(catalog, m['stat'], locale)} for m in mods],
            'expiry': expiry}


def _skills(catalog, skills, locale):
    if skills is None:
        return None
    rows = catalog.index('SkillInfoData', 'SkillKey')
    out = []
    for s in skills:
        row = rows.get(str(s['skill_key'])) or {}
        kind, value = row.get('ACTIVATIONTYPE'), num(row.get('ActivationValue'))
        trigger = {'BASEATTACK': 'base attack', 'COOLDOWN': f'cooldown {value:g} s (game data)' if value else 'cooldown',
                   'BASEATTACK_COUNT': f'every {value:g} attacks (game data)' if value else 'after attacks',
                   'CONTINUOUS': 'always on'}.get(kind, (kind or '').lower())
        out.append({'skill_key': s['skill_key'], 'name': catalog.localize(row.get('SkillNameKey'), locale) or s['class'],
                    'trigger': trigger, 'range': s['cast_distance']})
    return out


def _incoming(catalog, reading, hero, locale):
    """HP one hit of each enemy type on the field takes from this hero (analysis/threat.py), largest first."""
    k, level, stage_key = reading.get('armor_constants'), reading.get('stage_level'), reading.get('stage_key')
    stage = catalog.stages.get(str(stage_key)) or {}
    scale = catalog.stage_levels.get(str(level)) if level is not None else None
    target = {'final': (hero.get('stats') or {}).get('final') or {}, 'resistances': hero.get('resistances'),
              'max_hp': hero.get('max_hp')}
    if not k or not scale or not reading.get('enemies'):
        return None
    rows = {}
    for e in reading['enemies']:
        row = catalog.monsters.get(str(e['monster_key']))
        if not row or not row.get('MaxLife'):
            continue
        normal_hp = float(row['MaxLife']) * int(scale['MonsterHpMultiplier']) / 1000
        boss = bool(str(e['monster_key']) == stage.get('BossMonsterKey') and e['max_hp'] and e['max_hp'] > 1.5 * normal_hp)
        if (e['monster_key'], boss) in rows:
            continue
        damage = enemy_damage(catalog, stage_key, e['monster_key'], boss)
        h = hit(damage, target, level, k, e['monster_key'])
        if h:
            rows[(e['monster_key'], boss)] = {
                'monster_key': e['monster_key'], 'boss': boss, 'damage': damage, 'hit': h['hit'], 'element': h['element'],
                'name': catalog.localize(row.get('MonsterNameStringKey'), locale) or f"Monster {e['monster_key']}",
                'armor_reduction': h['reduction'], 'hit_fraction': h['fraction'], 'cap_uncertain': h['cap_uncertain']}
    return sorted(rows.values(), key=lambda r: -r['hit'])


def _hero(catalog, hero, run, locale, rune_part=None, reading=None):
    info = catalog.heroes.get(str(hero['hero_key'])) or {}
    stats = hero['stats']
    result = {
        'hero_key': hero['hero_key'], 'name': catalog.hero_name(hero['hero_key'], locale), 'class': info.get('ClassType'),
        'hp': hero['hp'], 'max_hp': hero['max_hp'],
        'hp_fraction': hero['hp'] / hero['max_hp'] if hero['hp'] is not None and hero['max_hp'] else None,
        'state': hero['state'], 'attacking': hero['attacking'], 'attack_count': hero['attack_count'],
        'stats': None, 'key_stats': None, 'origins': None, 'modifiers_age_s': hero.get('modifiers_age_s'),
        'resistances': resistance((hero['resistances'] or {}).get('individual'), (hero['resistances'] or {}).get('caps')),
        'resistance_caps': (hero['resistances'] or {}).get('caps'),
        'buffs': None if hero['buffs'] is None else [_buff(catalog, b, hero, locale) for b in hero['buffs']],
        'run': None,
    }
    if stats:
        rows = [{'stat': k, 'name': stat_name(catalog, k, locale), 'base': stats['base'].get(k), 'final': v}
                for k, v in stats['final'].items() if v or stats['base'].get(k)]
        result['stats'] = sorted(rows, key=lambda r: r['name'])
        result['key_stats'] = [{'stat': k, 'name': stat_name(catalog, k, locale), 'base': stats['base'].get(k),
                                'final': stats['final'].get(k)} for k in KEY_STATS if k in stats['final']]
    mods = hero.get('modifiers')
    found = origins(split_account(mods, rune_part) if mods and rune_part else mods, stats)
    if found is not None:
        labels = SOURCE_LABELS if rune_part else {**SOURCE_LABELS, 'AccountStatus': UNSPLIT_ACCOUNT}
        result['origins'] = {stat: {**o, 'name': stat_name(catalog, stat, locale),
                                    'sources': [{**s, 'label': labels.get(s['source'], s['source'])} for s in o['sources']]}
                             for stat, o in found.items()}
    result['skills'] = _skills(catalog, hero.get('skills'), locale)
    result['incoming'] = _incoming(catalog, reading, hero, locale) if reading else None
    if run and hero['hero_key'] in run['heroes']:
        r = run['heroes'][hero['hero_key']]
        names = {b['group']: b['name'] for b in result['buffs'] or []}
        presence = [{'group': g, 'name': names.get(g) or buff_info(catalog, g, locale)['name'] or f'Buff {g}', 'fraction': f}
                    for g, f in r['buff_presence'].items()]
        result['run'] = {**{k: r[k] for k in ('samples', 'min_hp_fraction', 'deaths')}, 'buff_presence': presence}
    return result


def _enemies(catalog, enemies, stage_level, locale, stage_key=None):
    if enemies is None:
        return None
    level = catalog.stage_levels.get(str(stage_level)) if stage_level is not None else None
    hp_mult = num(level['MonsterHpMultiplier']) if level else None
    stage = catalog.stages.get(str(stage_key)) or {}
    groups = {}
    for e in enemies:
        row = catalog.monsters.get(str(e['monster_key'])) or {}
        normal = num(row.get('MaxLife')) * hp_mult / 1000 if hp_mult and row.get('MaxLife') else None
        # The stage boss can be the same monster as a normal one: tell them apart by HP.
        boss = str(e['monster_key']) == stage.get('BossMonsterKey') and normal and e['max_hp'] and e['max_hp'] > 1.5 * normal
        g = groups.setdefault((e['monster_key'], bool(boss)), {'monster_key': e['monster_key'], 'boss': bool(boss), 'count': 0, 'hp': 0.0,
                                                 'max_hp': 0.0, 'hp_unknown': 0, 'max_hp_each': set()})
        g['count'] += 1
        if e['hp'] is None:
            g['hp_unknown'] += 1
        else:
            g['hp'] += e['hp']
            g['max_hp'] += e['max_hp']
            g['max_hp_each'].add(round(e['max_hp'], 2))
    rows = []
    for (key, boss), g in groups.items():
        row = catalog.monsters.get(str(key)) or {}
        expected = num(row.get('MaxLife')) * hp_mult / 1000 if row.get('MONSTERTYPE') == 'MONSTER' and hp_mult and row.get('MaxLife') else None
        if boss and expected:
            # x BossHpMultiplier / 1000: matched both stage bosses seen (D009).
            expected *= num(stage.get('BossHpMultiplier'), 1000) / 1000
        each = sorted(g.pop('max_hp_each'))
        rows.append({**g, 'name': catalog.localize(row.get('MonsterNameStringKey'), locale) or f'Monster {key}',
                     'type': row.get('MONSTERTYPE'), 'max_hp_each': each,
                     # MaxLife × MonsterHpMultiplier / 1000 matched every ordinary enemy observed (D001).
                     'expected_max_hp': expected,
                     'catalog_match': None if expected is None or not each else all(abs(v - expected) <= 1e-4 * expected + .01 for v in each)})
    rows.sort(key=lambda r: -r['hp'])
    known = [e for e in enemies if e['hp'] is not None]
    return {'count': len(enemies), 'hp': sum(e['hp'] for e in known), 'max_hp': sum(e['max_hp'] for e in known),
            'hp_unknown': len(enemies) - len(known), 'groups': rows}


def combat(app, locale='en-US'):
    collector, catalog = app.collector, app.catalog
    reading = collector.last_combat if collector else None
    if not reading:
        reason = (collector.status.get('combat_reason') or collector.status.get('runtime_reason')
                  or 'no combat reading yet') if collector else 'collector not running on this server'
        return {'available': False, 'reason': reason}
    run = collector.run_combat.summary() if collector.run_combat else None
    stage = reading.get('stage_key')
    snapshot = app.save() if hasattr(app, 'save') else None
    rune_part = PowerContext(app, snapshot, locale).rune_part() if snapshot else None
    return {
        'available': True, 'utc': reading['utc'], 'age_s': _age(reading['utc']), 'quality': reading['quality'],
        'problems': reading['problems'], 'read_ms': reading['read_ms'], 'stage_key': stage,
        'stage_label': catalog.stage_label(stage, locale) if stage else None, 'stage_level': reading.get('stage_level'),
        'heroes': None if reading['heroes'] is None else [_hero(catalog, h, run, locale, rune_part, reading) for h in reading['heroes']],
        'enemies': _enemies(catalog, reading['enemies'], reading.get('stage_level'), locale, stage),
        'dead': [{'hero_key': k, 'name': catalog.hero_name(k, locale), 'resurrection_s': v}
                 for k, v in (reading.get('dead') or {}).items()],
        'run': {'samples': run['samples'], 'started_utc': collector.run_combat.run_key[0],
                'attack_damage': [{**r, 'name': catalog.hero_name(r['hero_key'], locale)}
                                  for r in attack_shares(run['heroes'])]} if run else None,
    }
