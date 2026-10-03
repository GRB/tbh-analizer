"""Power changes (runes, gear, attributes, skills, pet) and their measured effect on runs, for the API."""
import json

from ..analysis.impact import purchase_impact, stat_change
from ..analysis.power import SHOWN
from ..snapshots import snapshots_since
from .analytics import complete_runs, decorate_run, since_iso
from .items import PARTS, account_stat_name, rune_effect, rune_totals, stat_name


def _effect_pct(totals, stat):
    return ((totals.get(stat) or {}).get('effect') or {}).get('value', 0)


def expected_per_run(before_runes, after_runes, catalog, locale='en-US'):
    """Expected change of gold and XP per run from percent runes, if they stack additively (hypothesis)."""
    old = rune_totals({'runes': before_runes}, catalog, locale)
    new = rune_totals({'runes': after_runes}, catalog, locale)
    out = {}
    for metric, stat in (('gold_per_run', 'IncreaseGoldAmount'), ('xp_per_run', 'IncreaseExpAmount')):
        a, b = _effect_pct(old, stat), _effect_pct(new, stat)
        if a != b:
            out[metric] = ((100 + b) / (100 + a) - 1) * 100
    return out


def _item(catalog, value, locale):
    uid, item_key = value if value else (None, None)
    return catalog.item_name(item_key, locale) or f'item {item_key}' if item_key else 'empty'


def _slot(slot):
    return PARTS[slot].replace('_', ' ').title() if slot < len(PARTS) else f'slot {slot}'


def attribute_name(catalog, key, locale='en-US'):
    row = catalog.index('AttributeInfoData', 'AttributeKey').get(str(key)) or {}
    if row.get('ATTRIBUTETYPE') == 'PASSIVESKILL':
        passive = catalog.index('PassiveSkillInfoData', 'PassiveSkillKey').get(row['Value']) or {}
        name = catalog.localize(passive.get('SkillNameKey'), locale)
    elif row.get('ATTRIBUTETYPE') == 'ACTIVESKILL':
        skill = catalog.index('SkillInfoData', 'SkillKey').get(row['Value']) or {}
        name = catalog.localize(skill.get('SkillNameKey'), locale)
    else:
        name = catalog.localize(f"AttributeName_{row.get('ATTRIBUTETYPE')}", locale)
    hero = catalog.hero_name(row['HeroKey'], locale) if row.get('HeroKey') else None
    return f"{hero} · {name or f'attribute {key}'}" if hero else name or f'attribute {key}'


def describe_change(catalog, key, old, new, gear_after, locale='en-US'):
    """{'kind', 'label', 'effect'?} for one entry of a power change."""
    kind = key[0]
    if kind == 'rune':
        rune = catalog.runes.get(str(key[1])) or {}
        levels = catalog.rune_levels.get(rune.get('LevelDataKey'), {})
        stat = (levels.get(1) or {}).get('STATTYPE')
        gained = sum(float(levels[lv]['Value'] or 0) for lv in range(old + 1, new + 1) if lv in levels)
        name = catalog.localize(rune.get('NameKey'), locale) or str(key[1])
        return {'kind': kind, 'label': f"{name} · {account_stat_name(catalog, stat, locale) or stat} {old}→{new}",
                'rune_key': key[1], 'stat': stat, 'from': old, 'to': new,
                'effect': rune_effect(catalog, stat, gained, locale)}
    if kind == 'gear':
        copy = ' (another copy: different rolls)' if old and new and old[1] == new[1] else ''
        return {'kind': kind, 'label': (f"{catalog.hero_name(key[1], locale)} · {_slot(key[2])}: "
                                        f"{_item(catalog, old, locale)} → {_item(catalog, new, locale)}{copy}")}
    if kind == 'upgrade':
        item = _item(catalog, gear_after.get(('gear',) + key[1:]), locale)
        return {'kind': kind, 'label': f"{catalog.hero_name(key[1], locale)} · {_slot(key[2])}: {item} enchanted or decorated"}
    if kind == 'attribute':
        return {'kind': kind, 'label': f"{attribute_name(catalog, key[1], locale)} {old}→{new}"}
    if kind == 'skill':
        skills = catalog.index('SkillInfoData', 'SkillKey')
        name = lambda k: catalog.localize((skills.get(str(k)) or {}).get('SkillNameKey'), locale) or (str(k) if k else 'empty')
        return {'kind': kind, 'label': f"{catalog.hero_name(key[1], locale)} · skill slot {key[2] + 1}: {name(old)} → {name(new)}"}
    return {'kind': kind, 'label': f"Pet {old or 'none'} → {new or 'none'}"}


def purchases(store, catalog, hours=None, locale='en-US'):
    snapshots = snapshots_since(store, since_iso(hours))
    runes_at = {s['last_saved_utc']: s['runes'] for s in snapshots}
    power_at = {s['last_saved_utc']: s['power'] or {} for s in snapshots}
    runs = [decorate_run(r, catalog, locale) for r in complete_runs(store, catalog, hours)]
    rows = purchase_impact(snapshots, runs)
    events = [(e['utc'], json.loads(e['payload'])) for e in
              store.query("SELECT utc, payload FROM events WHERE kind = 'hero_stats' ORDER BY utc")]
    for row in rows:
        change = stat_change(events, row['from_utc'], row['to_utc'], SHOWN)
        row['stat_change'] = None if change is None else [
            {'hero_key': int(hero), 'name': catalog.hero_name(hero, locale),
             'stats': [{'stat': k, 'name': stat_name(catalog, k, locale), **v} for k, v in stats.items()]}
            for hero, stats in change.items()]
        order = {'gear': 0, 'upgrade': 1, 'rune': 2, 'attribute': 3, 'skill': 4, 'pet': 5}
        changes = [describe_change(catalog, key, old, new, power_at.get(row['to_utc'], {}), locale)
                   for key, (old, new) in sorted(row['changes'].items(), key=lambda kv: (order[kv[0][0]], str(kv[0])))]
        row['changes'] = changes
        row['stage_label'] = catalog.stage_label(row['stage'], locale) if row['stage'] else None
        row['expected'] = expected_per_run(runes_at.get(row['from_utc'], {}), runes_at.get(row['to_utc'], {}),
                                           catalog, locale)
    return {'purchases': rows,
            'note': ('A change is anything that can make the party stronger: runes, gear of party heroes (swap or '
                     'enchant), attribute points, skills, pet. Runs on the same stage and party before and after each '
                     'change; each side stops at the neighbouring change. Rates use sum(value) / sum(time) with a standard error; a change counts '
                     'only when it exceeds 2 combined standard errors. Hero level-ups inside the comparison also '
                     'speed runs up and are counted. "Expected" applies the rune percent to gold or XP per run, '
                     'assuming percent bonuses add up (hypothesis being tested). "Stats" is read from the game '
                     'right away: stats before buffs at the save before and after the change (level-ups in between '
                     'included); recorded since the combat reader.')}
