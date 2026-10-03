"""Inventory, stash, equipment and chest views built from a save snapshot + catalog.

Quantities are always taken from slots. The item catalog of the save (`itemSaveDatas`)
is a list of instances, not a count of owned items.
"""
import re

from ..catalog.catalog import num

PARTS = ['MAIN_WEAPON', 'SUB_WEAPON', 'HELMET', 'ARMOR', 'GLOVES', 'BOOTS', 'AMULET', 'EARING', 'RING', 'BRACER']
CONTAINERS = {'inventory': 'Inventory', 'stash': 'Stash', 'trading_stash': 'Trading stash'}


# Stats the game has no localized name for.
STAT_LABELS = {'IncreaseExpAmount': 'XP gain multiplier', 'AdditionalExp': 'Additional XP'}


def stat_name(catalog, stat_type, locale='en-US'):
    name = catalog.localize(f'StatName_{stat_type}', locale)
    if name:
        return name
    return STAT_LABELS.get(stat_type) or re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', str(stat_type))


def describe_item(catalog, instance, locale='en-US'):
    key = str(instance['item_key']) if instance else None
    row = catalog.items.get(key) if key else None
    result = {'unique_id': instance['unique_id'] if instance else None, 'item_key': key,
              'known': row is not None}
    if not row:
        return result
    result.update({
        'name': catalog.item_name(key, locale), 'type': row['ITEMTYPE'], 'grade': row['GRADE'],
        'content': row['CONTENTTYPE'] or None, 'parts': row['PARTS'] or None, 'gear_type': row['GEARTYPE'] or None,
        'gear_group': row['GearGroup'] or None, 'level': num(row['Level']), 'max_stack': num(row['MaxStack']),
        'synthesis_type': row['ItemSynthesisType'] or None, 'steam_item': row['IsSteamItem'] == 'True',
        'marketable': row['IsCanExchangeMarketable'] == 'True', 'icon': row['IconPath'],
        'description': catalog.localize(row['DescriptionKey'], locale),
        'blocked': bool(instance.get('blocked')), 'chaotic': bool(instance.get('chaotic')),
        'source_type': instance.get('source_type'),
    })
    gear = catalog.gears.get(row['GearKey']) if row['GearKey'] else None
    if gear:
        gear_type = next((g for g in catalog.table('GearTypeInfoData') if g['GearType'] == row['GEARTYPE']), None)
        stats = []
        if gear_type:
            for i in (1, 2):
                stat = gear_type[f'BaseStat{i}_STATTYPE']
                if stat and stat != 'NONE':
                    stats.append({'kind': 'base', 'stat': stat, 'name': stat_name(catalog, stat, locale),
                                  'mod': gear_type[f'BaseStat{i}_MODTYPE'], 'value': num(gear[f'BaseStat{i}_Value'])})
        for i in (1, 2, 3):
            stat = gear[f'InherentStat{i}_STATTYPE']
            if stat and stat != 'NONE':
                stats.append({'kind': 'inherent', 'stat': stat, 'name': stat_name(catalog, stat, locale),
                              'mod': gear[f'InherentStat{i}_MODTYPE'], 'value': num(gear[f'InherentStat{i}_Value'])})
        unique = catalog.index('UniqueModInfoData', 'UniqueModKey').get(gear['UniqueModKey']) if gear['UniqueModKey'] else None
        result['stats'] = stats
        result['unique_mod'] = unique['UniqueMod'] if unique else None
    enchants = [e for e in instance.get('enchants') or [] if e.get('StatModKey')]
    result['enchants'] = [{'stat_mod': e['StatModKey'], 'tier': e.get('Tier'), 'value': e.get('Value'),
                           'stat': e.get('StatType'), 'mod_type': e.get('ModType'), 'recipe_type': e.get('RecipeType')}
                          for e in enchants]
    result['enchant_count'] = instance.get('enchant_count')
    result['applied'] = {'decoration': instance.get('decoration'), 'engraving': instance.get('engraving'),
                         'inscription': instance.get('inscription')}
    return result


def containers(snapshot, catalog, locale='en-US'):
    result = {}
    for name, label in CONTAINERS.items():
        slots = snapshot[name]
        entries = []
        for slot in slots:
            if not slot['unique_id']:
                continue
            item = describe_item(catalog, snapshot['items'].get(slot['unique_id']), locale)
            if not item['unique_id']:
                item['unique_id'] = slot['unique_id']
                item['missing_instance'] = True
            entries.append({**item, 'slot': slot['index'], 'quantity': slot['quantity'],
                            'slot_blocked': slot['blocked']})
        unlocked = sum(1 for s in slots if s['unlocked'])
        used = sum(1 for s in slots if s['unlocked'] and s['unique_id'])
        result[name] = {'label': label, 'slots_total': len(slots), 'slots_unlocked': unlocked,
                        'slots_used': used, 'slots_free': unlocked - used,
                        'quantity_total': sum(e['quantity'] or 0 for e in entries), 'items': entries}
    return result


def equipment(snapshot, catalog, locale='en-US'):
    heroes = []
    for hero in snapshot['heroes']:
        gear = []
        for index, uid in enumerate(hero['equipped']):
            if not uid:
                continue
            item = describe_item(catalog, snapshot['items'].get(uid), locale)
            item['unique_id'] = uid
            item['slot_part'] = PARTS[index] if index < len(PARTS) else str(index)
            item['slot_blocked'] = bool(hero['equipped_blocked'][index]) if index < len(hero['equipped_blocked']) else None
            gear.append(item)
        heroes.append({'hero_key': hero['hero_key'], 'name': catalog.hero_name(hero['hero_key'], locale),
                       'in_party': hero['hero_key'] in snapshot['party'], 'equipment': gear})
    return heroes


def chests(snapshot, catalog, locale='en-US'):
    """Chest stock in containers and free inventory space."""
    stock = {}
    for name in CONTAINERS:
        for slot in snapshot[name]:
            instance = snapshot['items'].get(slot['unique_id']) if slot['unique_id'] else None
            row = catalog.items.get(str(instance['item_key'])) if instance else None
            if row and row['ITEMTYPE'] == 'STAGEBOX':
                entry = stock.setdefault(row['ItemKey'], {
                    'item_key': row['ItemKey'], 'name': catalog.item_name(row['ItemKey'], locale), 'grade': row['GRADE'],
                    'content': row['CONTENTTYPE'] or None, 'drop_cooldown': num(row['DropCooldown']),
                    'bucket_box': row['IsBucketBox'] == 'True', 'quantity': 0, 'locations': {}})
                entry['quantity'] += slot['quantity'] or 0
                entry['locations'][name] = entry['locations'].get(name, 0) + (slot['quantity'] or 0)
    inventory = containers(snapshot, catalog, locale)['inventory']
    return {
        'stock': sorted(stock.values(), key=lambda e: -e['quantity']),
        'bucket_use': snapshot['box_bucket_use'], 'bucket_get': snapshot['box_bucket_get'],
        'bucket_note': 'Save BoxBucket lists: queue/pending semantics not yet validated; these are not the total stock.',
        'inventory_free': inventory['slots_free'], 'inventory_unlocked': inventory['slots_unlocked'],
    }


# In-game display = stored rune Value x scale. The catalog has no scale column; it lives in game code.
# Verified on 2026-09-30 against rune tooltips and the in-game "Stat List" panel, which showed every
# total below exactly as computed here (sum of the Value of each owned level, then scaled):
#   "{0}% Increased ..." and "... +{0}%" texts store ten times the shown percent
#     (stored 1100 -> "110% Increased Gold Per Kill", stored 300 -> "Offline Reward Gold +30%");
#   "... +{0}" / "... -{0}" texts store the shown value (stored 780 -> "Gold From Stage Boss Kill +780").
# Unlock-type stats (auto open, open all at once, offline unlock) are not listed there and stay unknown.
RUNE_STATS_SEEN_IN_GAME = {
    # Exploration
    'IncreaseExpAmount', 'AdditionalExp', 'AdditionalExpStageBoss', 'AdditionalExpActBoss',
    'AdditionalExpNormalMonster', 'UnlockArrangeSlotCount',
    # Combat
    'AllHeroMoveSpeed', 'AllHeroAttackSpeed', 'AllHeroAttackDamage', 'AllHeroAttackDamagePercent',
    'AllHeroArmor', 'AllHeroArmorPercent', 'UnlockSkillSlotCount',
    # Reward
    'IncreaseGoldAmount', 'AdditionalGold', 'CubeExpPercent', 'CubeAlchemyGoldPercent', 'MaxAmountNormalChest',
    'AdditionalGoldStageBoss', 'AdditionalGoldActBoss', 'AdditionalGoldNormalMonster', 'MaxInventorySlot',
    'DropChanceNormalChestPercent', 'DropChanceStageBossChestPercent', 'OfflineRewardGoldPercent',
    'OfflineRewardExpPercent',
}


def rune_scale(template):
    if not template:
        return None
    if template.startswith('{0}% Increased') or template.endswith('+{0}%'):
        return 0.1
    if template.endswith(('+{0}', '-{0}')):
        return 1
    return None


def rune_effect(catalog, stat, value, locale='en-US'):
    """{'value': shown value, 'text': the game's own wording, 'verified': seen in game} or None if unknown."""
    template = catalog.localize(f'AccountStat_{stat}', locale) if stat else None
    scale = rune_scale(template)
    if scale is None or value is None:
        return None
    shown = value * scale
    return {'value': shown, 'text': template.replace('{0}', f'{shown:g}'), 'verified': stat in RUNE_STATS_SEEN_IN_GAME}


# How the pet's bonuses combine, where measured (D009: per-kill capture + 824 save windows).
PET_NOTES = {'IncreaseGoldAmount': 'multiplies the rune gold bonus (measured)',
             'IncreaseExpAmount': 'multiplies the rune XP bonus (measured within 1%)'}


def pet_view(snapshot, catalog, locale='en-US'):
    """The active pet and its account bonuses (it adds no hero stats: D008)."""
    key = snapshot.get('pet')
    row = catalog.index('PetInfoData', 'PetKey').get(str(key)) if key else None
    if not row:
        return None
    effects = []
    for r in catalog.table('PetStatInfoData'):
        if r['PetStatKey'] == row['StatDataKey']:
            effect = rune_effect(catalog, r['STATTYPE'], float(r['Value']), locale)
            effects.append({'stat': r['STATTYPE'], 'raw': float(r['Value']),
                            'text': effect['text'] if effect else f"{account_stat_name(catalog, r['STATTYPE'], locale)} {r['Value']} (raw)",
                            'note': PET_NOTES.get(r['STATTYPE'])})
    owned = [p['pet_key'] for p in snapshot.get('pets') or [] if p.get('unlocked')]
    return {'pet_key': key, 'name': catalog.localize(row.get('NameKey'), locale) or f'Pet {key}', 'effects': effects,
            'unlocked': [{'pet_key': k, 'name': catalog.localize((catalog.index('PetInfoData', 'PetKey').get(str(k)) or {}).get('NameKey'), locale) or f'Pet {k}'}
                         for k in owned]}


def account_stat_name(catalog, stat, locale='en-US'):
    """Name the game gives an account (rune) stat, e.g. 'Gold Per Monster Kill'."""
    if not stat:
        return None
    return catalog.localize(f'AccountStatName_{stat}', locale) or stat_name(catalog, stat, locale)


def rune_totals(snapshot, catalog, locale='en-US'):
    """Sum of per-level rune values up to the owned level. Additivity per level is a
    hypothesis consistent with the table shape (one Value per level row)."""
    totals = {}
    for rune_key, level in snapshot['runes'].items():
        rune = catalog.runes.get(str(rune_key))
        if not rune or not level:
            continue
        levels = catalog.rune_levels.get(rune['LevelDataKey'], {})
        for lv in range(1, level + 1):
            row = levels.get(lv)
            if not row or not row['STATTYPE']:
                continue
            entry = totals.setdefault(row['STATTYPE'], {'stat': row['STATTYPE'], 'value': 0, 'runes': set(),
                                                        'name': catalog.localize(f"RuneName_{row['STATTYPE']}", locale)
                                                        or stat_name(catalog, row['STATTYPE'], locale)})
            entry['value'] += num(row['Value'], 0)
            entry['runes'].add(int(rune_key))
    for entry in totals.values():
        entry['runes'] = sorted(entry['runes'])
        entry['effect'] = rune_effect(catalog, entry['stat'], entry['value'], locale)
    return totals


def rune_tree(snapshot, catalog, locale='en-US'):
    nodes = []
    for key, rune in catalog.runes.items():
        level = snapshot['runes'].get(int(key), 0)
        levels = catalog.rune_levels.get(rune['LevelDataKey'], {})
        nxt = levels.get(level + 1)
        nodes.append({'rune_key': int(key), 'name': catalog.localize(rune['NameKey'], locale) or rune['NameKey'],
                      'level': level, 'max_level': num(rune['MaxLevel']),
                      'stat': (levels.get(1) or {}).get('STATTYPE'),
                      'stat_name': account_stat_name(catalog, (levels.get(1) or {}).get('STATTYPE'), locale),
                      'next_effect': rune_effect(catalog, (levels.get(1) or {}).get('STATTYPE'),
                                                 num(nxt['Value']) if nxt else None, locale),
                      'next_cost': num(nxt['CostValue']) if nxt else None,
                      'next_cost_item': nxt['CostItemKey'] if nxt else None,
                      'next_value': num(nxt['Value']) if nxt else None,
                      'requires_prev_level': num(rune['PrevNodeRequiredLevel']),
                      'next_runes': [int(x) for x in rune['NextRuneKey'].split()] if rune['NextRuneKey'] else []})
    parents = {}
    for node in nodes:
        for child in node['next_runes']:
            parents.setdefault(child, []).append(node)
    for node in nodes:
        needed = node['requires_prev_level'] or 1
        ups = parents.get(node['rune_key'], [])
        node['parent_ok'] = not ups or any(p['level'] >= needed for p in ups)
        node['purchasable_hint'] = bool(node['parent_ok'] and node['next_cost'] is not None
                                        and (node['max_level'] is None or node['level'] < node['max_level']))
    return nodes
