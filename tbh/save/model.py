"""Normalize PlayerSaveData (schema of game 1.2.8) into a stable snapshot shape.

Instance IDs become strings here so no later layer can round them.
Unknown keys are kept in `unmapped_keys` to surface schema changes instead of hiding them.
"""
import json
from datetime import datetime, timedelta, timezone

KNOWN_KEYS = {
    'commonSaveData', 'settingSaveData', 'BoxBucketUseBoxList', 'BoxBucketGetBoxList',
    'AlchemyPendingIdList', 'AlchemyReceiptList', 'pendingEnchantList', 'pendingExchange',
    'backendPostList', 'pendingItemRestorationList', 'currenySaveDatas', 'heroSaveDatas',
    'mailSaveDatas', 'attributeSaveDatas', 'PetSaveData', 'RuneSaveData', 'inventorySaveDatas',
    'stashSaveDatas', 'remakeTradingStashSaveDatas', 'cubeRecipeSaveDatas', 'cubeSaveLevelData',
    'itemSaveDatas', 'aggregateSaveDatas',
}

AGGREGATE_TYPES = {
    0: 'MonsterKill', 1: 'HeroDeath', 2: 'GoldEarn', 3: 'BoxObtain', 4: 'ItemObtain',
    5: 'Synthesis', 6: 'Alchemy', 7: 'Crafting', 8: 'Offering', 9: 'Extraction',
    10: 'Decoration', 11: 'Engraving', 12: 'Inscription', 13: 'StageClear', 14: 'StageFail',
    15: 'PlayTime', 16: 'BoxOpen', 17: 'ActBossKill', 18: 'TradingStashLoad', 19: 'SteamOriginItemBuy',
}
GOLD_SOURCES = {0: 'Total', 1: 'MonsterKill', 2: 'CubeAlchemy', 3: 'OfflineReward'}
GOLD_CURRENCY_KEY = 100001
DOTNET_EPOCH = datetime(1, 1, 1)


def ticks_to_local_iso(ticks):
    """`lastSavedTime` is .NET ticks of a local wall-clock time (validated against file mtime)."""
    if not ticks:
        return None
    return (DOTNET_EPOCH + timedelta(microseconds=int(ticks) // 10)).isoformat()


def ticks_to_utc(ticks):
    if not ticks:
        return None
    local = DOTNET_EPOCH + timedelta(microseconds=int(ticks) // 10)
    return local.astimezone(timezone.utc).isoformat()  # naive -> interpreted as machine local time


def _sid(value):
    return str(value) if value not in (None, 0, '0') else None


def _slots(rows, unlock_key):
    slots = []
    for row in rows:
        slots.append({
            'index': row.get('Index'),
            'unique_id': _sid(row.get('ItemUniqueId')),
            'unlocked': bool(row.get(unlock_key, row.get('IsUnlock', row.get('IsUnLock')))),
            'blocked': bool(row.get('IsBlocked', False)),
            'quantity': row.get('Quantity', 0),
            'unlocked_by_rune': row.get('IsUnlockedByRune'),
        })
    return slots


def normalize(player_json):
    data = json.loads(player_json)
    common = data.get('commonSaveData') or {}
    heroes = []
    for hero in data.get('heroSaveDatas') or []:
        heroes.append({
            'hero_key': hero.get('heroKey'), 'level': hero.get('HeroLevel'), 'unlocked': hero.get('IsUnLock'),
            'xp': hero.get('HeroExp'), 'ability_points': hero.get('AbilityPoint'),
            'allocated_points': hero.get('AllocatedHeroAbilityPoint'),
            'equipped': [_sid(x) for x in hero.get('equippedItemIds') or []],
            'equipped_blocked': hero.get('equippedItemBlocked') or [],
            'skills': hero.get('equippedSKillKey') or [],
            'attribute_groups': hero.get('unlockedAttributeGroupKeys') or [],
            'skin': hero.get('EquippedSkinKey'),
        })
    items = {}
    for item in data.get('itemSaveDatas') or []:
        uid = _sid(item.get('UniqueId'))
        if uid:
            items[uid] = {
                'unique_id': uid, 'item_key': item.get('ItemKey'), 'prev_unique_id': _sid(item.get('PrevUniqueId')),
                'chaotic': item.get('IsChaotic'), 'blocked': item.get('IsBlocked'),
                'enchant_count': item.get('EnchantCount'), 'enchants': item.get('EnchantData') or [],
                'source_type': item.get('ItemGetSourceType'),
                'decoration': item.get('DecorationAppliedTotalCount'),
                'engraving': item.get('EngravingAppliedTotalCount'),
                'inscription': item.get('InscriptionAppliedTotalCount'),
            }
    aggregates = [{'type': a.get('Type'), 'type_name': AGGREGATE_TYPES.get(a.get('Type'), str(a.get('Type'))),
                   'subkey': a.get('SubKey'), 'content': a.get('Content'), 'value': a.get('Value')}
                  for a in data.get('aggregateSaveDatas') or []]
    currency = {c.get('Key'): c.get('Quantity') for c in data.get('currenySaveDatas') or []}
    return {
        'version': common.get('version'),
        'last_saved_ticks': str(common.get('lastSavedTime')) if common.get('lastSavedTime') else None,
        'last_saved_local': ticks_to_local_iso(common.get('lastSavedTime')),
        'last_saved_utc': ticks_to_utc(common.get('lastSavedTime')),
        'play_time': common.get('playTime'),
        'current_stage': common.get('currentStageKey'),
        'current_wave': common.get('currentStageWave'),
        'max_completed_stage': common.get('maxCompletedStage'),
        'last_cleared_stage': common.get('lastClearedStageKey'),
        'prev_normal_stage': common.get('prevNormalStageKey'),
        'party': [k for k in common.get('arrangedHeroKey') or [] if k and k > 0],  # -1 = empty slot
        'pet': common.get('ArrangedPetKey'),
        'plague_intensity': common.get('lastPlayedPlagueIntensity'),
        'gold': currency.get(GOLD_CURRENCY_KEY),
        'currency': {str(k): v for k, v in currency.items()},
        'heroes': heroes,
        'items': items,
        'inventory': _slots(data.get('inventorySaveDatas') or [], 'IsUnlock'),
        'stash': _slots(data.get('stashSaveDatas') or [], 'IsUnLock'),
        'trading_stash': _slots(data.get('remakeTradingStashSaveDatas') or [], 'IsUnLock'),
        'aggregates': aggregates,
        'runes': {r.get('RuneKey'): r.get('Level') for r in data.get('RuneSaveData') or []},
        'attributes': {a.get('Key'): a.get('Level') for a in data.get('attributeSaveDatas') or []},
        'pets': [{'pet_key': p.get('PetKey'), 'unlocked': p.get('IsUnlock')} for p in data.get('PetSaveData') or []],
        'cube_level': data.get('cubeSaveLevelData') or {},
        'cube_recipes': data.get('cubeRecipeSaveDatas') or [],
        'settings': data.get('settingSaveData') or {},
        'box_bucket_use': [str(x) for x in data.get('BoxBucketUseBoxList') or []],
        'box_bucket_get': [str(x) for x in data.get('BoxBucketGetBoxList') or []],
        'pending': {
            'alchemy_pending': len(data.get('AlchemyPendingIdList') or []),
            'alchemy_receipts': len(data.get('AlchemyReceiptList') or []),
            'enchant': len(data.get('pendingEnchantList') or []),
            'exchange': data.get('pendingExchange') is not None,
            'backend_posts': len(data.get('backendPostList') or []),
            'item_restoration': len(data.get('pendingItemRestorationList') or []),
            'mail': len((data.get('mailSaveDatas') or {}).get('Keys') or []),
        },
        'unmapped_keys': sorted(set(data) - KNOWN_KEYS),
    }


def aggregate(snapshot, type_, subkey=0, content=0):
    """Counters use Content=0. Content=1 (PLAGUE) duplicates were observed; never sum both."""
    for row in snapshot['aggregates']:
        if row['type'] == type_ and row['subkey'] == subkey and row['content'] == content:
            return row['value']
    return None


def stage_counters(snapshot, content=0):
    result = {}
    for row in snapshot['aggregates']:
        if row['content'] != content or row['subkey'] == 0:
            continue
        if row['type'] in (13, 14):
            entry = result.setdefault(row['subkey'], {'clears': 0, 'fails': 0})
            entry['clears' if row['type'] == 13 else 'fails'] = row['value']
    return result
