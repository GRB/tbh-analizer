"""Cube state, unlocked recipes and material-based hints (synthesis / alchemy).

Formulas are read from catalog columns but their exact consumers were not traced in
the binary: values carry `hypothesis` labels and are intended for comparison only.
"""
import re

from ..catalog.catalog import num
from .items import CONTAINERS

SUB_RECIPE_RANGE = re.compile(r'Lv\.(\d+)\s*~\s*(\d+)')


def _scale(rows, key_col, value, col):
    best = None
    for row in rows:
        k = num(row[key_col])
        if k is not None and value is not None and k <= value and (best is None or k > num(best[key_col])):
            best = row
    return num(best[col], 1000) / 1000 if best else None


def alchemy_estimate(catalog, row):
    """Base alchemy gold / cube XP: grade base x level scale x gear/item type scale (hypothesis)."""
    grade = next((g for g in catalog.table('GradeInfoData') if g['GRADE'] == row['GRADE']), None)
    if not grade:
        return None
    level = num(row['Level'])
    level_gold = _scale(catalog.table('ItemLevelScaleInfoData'), 'Level', level, 'AlchemyGoldScale') or 1
    level_exp = _scale(catalog.table('ItemLevelScaleInfoData'), 'Level', level, 'CubeExpScale') or 1
    type_row = next((g for g in catalog.table('GearTypeScaleInfoData') if g['GearType'] == row['GEARTYPE']), None) \
        or next((g for g in catalog.table('ItemTypeScaleInfoData') if g['ItemType'] == row['ITEMTYPE']), None)
    type_gold = num(type_row['AlchemyGoldScale'], 1000) / 1000 if type_row else 1
    type_exp = num(type_row['CubeExpScale'], 1000) / 1000 if type_row else 1
    return {'gold': num(grade['BaseAlchemyGold'], 0) * level_gold * type_gold,
            'cube_exp': num(grade['BaseCubeExp'], 0) * level_exp * type_exp, 'hypothesis': True}


# Game text: "Immortal grade synthesis is available from Cube Level 10.", "Celestial grade synthesis is
# available from Cube Level 50.", "Cosmic grade cannot be synthesized."
LEVELED_TYPES = ('Gear', 'Accessory')  # synthesis types whose items have a level
SYNTHESIS_MIN_CUBE_LEVEL = {'IMMORTAL': 10, 'CELESTIAL': 50}
NOT_SYNTHESIZABLE = {'COSMIC'}


def synthesis_chances(catalog, grade):
    """Result grade chances of one synthesis, from GradeInfoData weights (same / +1 / +2 grades)."""
    order = [g['GRADE'] for g in catalog.table('GradeInfoData')]
    row = next((g for g in catalog.table('GradeInfoData') if g['GRADE'] == grade), None)
    if not row or grade not in order:
        return []
    i = order.index(grade)
    weights = [(order[i + step], num(row[col], 0)) for step, col in
               ((0, 'SameGradeWeight'), (1, 'Higher1GradeWeight'), (2, 'Higher2GradeWeight')) if i + step < len(order)]
    total = sum(w for _, w in weights)
    return [{'grade': g, 'chance': w / total} for g, w in weights if w] if total else []


def synthesis_gate(grade, cube_level):
    """Why this grade cannot be synthesized now, or None."""
    if grade in NOT_SYNTHESIZABLE:
        return f'{grade.title()} grade cannot be synthesized'
    need = SYNTHESIS_MIN_CUBE_LEVEL.get(grade)
    if need and (cube_level or 0) < need:
        return f'{grade.title()} synthesis needs Cube level {need}'
    return None


def synthesis_selection(snapshot, catalog):
    """The synthesis sub-recipe the Cube shows preselected and the item level range its name shows
    ("Lv.20~40"); only items of one grade and type inside that range can be synthesized there.
    Assumes the preselected sub-recipe is the highest unlocked one (observed in game, 2026-10-02:
    cube level 38, tier 4 "Lv.20~40"). None when the save or the catalog does not give it."""
    recipe = next((r for r in catalog.table('CubeRecipeInfoData') if r['RECIPETYPE'] == 'SYNTHESIS'), None)
    if not recipe:
        return None
    max_key = next((r['MaxUnlockRecipeKey'] for r in snapshot.get('cube_recipes') or []
                    if str(r.get('CubeKey')) == recipe['CubeKey']), None)
    if not max_key:
        return None
    unlocked = [s for s in catalog.table('CubeSubRecipeInfoData')
                if s['RECIPETYPE'] == 'SYNTHESIS' and int(s['CubeSubRecipeKey']) <= int(max_key)]
    if not unlocked:
        return None
    sub = max(unlocked, key=lambda s: num(s['RecipeTier'], 0))
    name = catalog.localize(sub['SubRecipeNameStringKey'], 'en-US')
    match = SUB_RECIPE_RANGE.search(name or '')
    if not match:
        return None
    return {'key': sub['CubeSubRecipeKey'], 'tier': num(sub['RecipeTier']), 'name': name,
            'min_level': int(match.group(1)), 'max_level': int(match.group(2)), 'assumed_preselected': True}


def cube_view(snapshot, catalog, locale='en-US'):
    level = snapshot['cube_level'].get('Level')
    exp = snapshot['cube_level'].get('Exp')
    need = next((num(r['ExpForLevelUp']) for r in catalog.table('CubeLevelInfoData') if num(r['Level']) == level), None)
    unlocked_max = {str(r['CubeKey']): r['MaxUnlockRecipeKey'] for r in snapshot['cube_recipes']}
    recipes = []
    for recipe in catalog.table('CubeRecipeInfoData'):
        max_key = unlocked_max.get(recipe['CubeKey'])
        subs = []
        for sub in catalog.table('CubeSubRecipeInfoData'):
            if sub['RECIPETYPE'] != recipe['RECIPETYPE']:
                continue
            unlocked = bool(max_key) and int(sub['CubeSubRecipeKey']) <= int(max_key)
            subs.append({'key': sub['CubeSubRecipeKey'], 'tier': num(sub['RecipeTier']),
                         'name': catalog.localize(sub['SubRecipeNameStringKey'], locale) or sub['SubRecipeNameStringKey'],
                         'unlocked': unlocked, 'unlock_cube_level': num(sub['UnlockCubeLevel']),
                         'unlock_cost': num(sub['UnlockCost']), 'trigger_gold_cost': num(sub['TriggerGoldCost']),
                         'material': sub['Material'] or None,
                         'unlockable_now': (not unlocked) and level is not None
                         and num(sub['UnlockCubeLevel'], 0) <= level})
        recipes.append({'cube_key': recipe['CubeKey'], 'type': recipe['RECIPETYPE'],
                        'name': catalog.localize(recipe['TooltipStringKey'], locale),
                        'opened': recipe['CubeKey'] in unlocked_max or recipe['IsDefaultUnlocked'] == 'True',
                        'max_unlocked_key': max_key, 'sub_recipes': subs})
    return {'level': level, 'exp': exp, 'exp_for_next': need,
            'progress': exp / need if exp is not None and need else None, 'recipes': recipes,
            'synthesis': synthesis_hints(snapshot, catalog, recipes, locale)}


def _owned_free_items(snapshot, catalog):
    """Items in containers that are not blocked (equipped items are not in containers)."""
    for name in CONTAINERS:
        for slot in snapshot[name]:
            if not slot['unique_id'] or slot['blocked'] or not slot.get('unlocked', True):
                continue
            instance = snapshot['items'].get(slot['unique_id'])
            row = catalog.items.get(str(instance['item_key'])) if instance else None
            if row and not instance.get('blocked'):
                yield name, slot, instance, row


def synthesis_hints(snapshot, catalog, recipes, locale='en-US'):
    synthesis = next((r for r in recipes if r['type'] == 'SYNTHESIS'), None)
    max_tier = max((s['tier'] or 0 for s in synthesis['sub_recipes'] if s['unlocked']), default=0) if synthesis else 0
    ranges = {}
    for sub in (synthesis or {}).get('sub_recipes', []):
        match = SUB_RECIPE_RANGE.search(sub['name'] or '')
        if sub['unlocked'] and match:
            ranges[sub['tier']] = tuple(map(int, match.groups()))
    groups = {}
    material_types = {m['ItemKey']: m['MATERIALTYPE'] for m in catalog.table('MaterialInfoData')}
    cube_level = (snapshot.get('cube_level') or {}).get('Level')
    selected = synthesis_selection(snapshot, catalog)
    for location, slot, instance, row in _owned_free_items(snapshot, catalog):
        stype = row['ItemSynthesisType']
        if not stype:
            continue
        key = (stype, row['GRADE'])
        g = groups.setdefault(key, {'synthesis_type': stype, 'grade': row['GRADE'], 'quantity': 0, 'levels': [],
                                    'alchemy_gold': 0.0, 'cube_exp': 0.0, 'items': [],
                                    'in_selected_range': 0 if selected and stype in LEVELED_TYPES else None})
        qty = slot['quantity'] or 0
        g['quantity'] += qty
        if g['in_selected_range'] is not None and selected['min_level'] <= num(row['Level'], 0) <= selected['max_level']:
            g['in_selected_range'] += qty
        g['levels'].extend([num(row['Level'], 0)] * qty)
        estimate = alchemy_estimate(catalog, row)
        if estimate:
            g['alchemy_gold'] += estimate['gold'] * qty
            g['cube_exp'] += estimate['cube_exp'] * qty
        g['items'].append({'unique_id': slot['unique_id'], 'name': catalog.item_name(row['ItemKey'], locale),
                           'level': num(row['Level']), 'quantity': qty, 'location': location,
                           'material_type': material_types.get(row['ItemKey'])})
    hints = []
    for (stype, grade), g in groups.items():
        levels = sorted(g['levels'])
        average = sum(levels) / len(levels) if levels else 0
        options = []
        for recipe in catalog.table('SynthesisRecipeInfoData'):
            if recipe['ItemSynthesisType'] != stype or recipe['GRADE'] != grade:
                continue
            tier = num(recipe['RecipeTier'], 0)
            amount = num(recipe['MaterialAmount'], 0)
            bounds = ranges.get(tier)
            eligible = [it for it in g['items'] if stype not in LEVELED_TYPES or
                        (bounds and it['level'] is not None and bounds[0] <= it['level'] <= bounds[1])]
            quantity = sum(it['quantity'] for it in eligible)
            eligible_average = sum((it['level'] or 0) * it['quantity'] for it in eligible) / quantity if quantity else 0
            if tier > max_tier or not amount or quantity < amount:
                continue
            if eligible_average < num(recipe['MinMaterialAverageLevel'], 0):
                continue
            weights = [num(recipe[f'LevelWeight{i}'], 0) for i in range(1, 5)]
            total = sum(weights)
            low = num(recipe['MinResultLevel'])
            dist = [{'level': low + i, 'weight': w / total} for i, w in enumerate(weights) if w and low is not None] if total else []
            options.append({'recipe': recipe['SynthesisRecipeKey'], 'tier': tier, 'material_amount': amount,
                            'batches': quantity // amount, 'eligible_quantity': quantity,
                            'eligible_items': eligible, 'input_level_range': bounds,
                            'min_avg_level': num(recipe['MinMaterialAverageLevel']),
                            'result_levels_hypothesis': True,
                            'result_levels': dist})
        best = {}
        for option in options:  # per tier, the most specific recipe the materials satisfy
            current = best.get(option['tier'])
            if current is None or (option['min_avg_level'] or 0) > (current['min_avg_level'] or 0):
                best[option['tier']] = option
        options = list(best.values())
        gate = synthesis_gate(grade, cube_level)
        if gate:
            options = []
        hints.append({'synthesis_type': stype, 'grade': grade, 'quantity': g['quantity'], 'gate': gate,
                      'result_chances': synthesis_chances(catalog, grade),
                      'average_level': round(average, 2), 'alchemy_gold_estimate': round(g['alchemy_gold']),
                      'in_selected_range': g['in_selected_range'],
                      'cube_exp_estimate': round(g['cube_exp']), 'options': sorted(options, key=lambda o: -o['tier'])[:3],
                      'items': g['items']})
    return {'max_unlocked_tier': max_tier, 'selected': selected, 'groups': sorted(hints, key=lambda h: -h['quantity']),
            'note': ('Game rule: 9 items of the same grade and type (any mix) make one item, usually of a higher '
                     'grade; Gear/Accessory must lie in the level range of the selected sub-recipe. Chances come '
                     'from the grade table. Result levels come from the recipe tier; material tier and server rules are not validated. Alchemy values are estimates (hypothetical formula).')}
