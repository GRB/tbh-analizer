"""Stage statistics and suggestion logic on synthetic analyses: no game, no save."""
import unittest

from tbh.analysis.stagestats import best_stage, confidence, ratio_rate, runs_needed, stage_evidence
from tbh.views.analytics import BuildTimeline, party_build
from tbh.views.items import rune_effect
from tbh.views.suggestions import (gear_suggestions, purchase_suggestions, rune_suggestions, stage_suggestions,
                                   synthesis_suggestion)

PARTY = [201, 401, 501]


def run(stage, seconds, gold, xp=1000.0, outcome='clear'):
    return {'stage_key': stage, 'party': '201,401,501', 'duration_s': seconds, 'gold_gain_est': gold,
            'ended_utc': '2026-09-30T22:00:00+00:00', 'outcome': outcome,
            'xp': {str(h): {'gain': xp, 'complete': True} for h in PARTY}}


def steady(stage, n, seconds, gold, jitter=0.02, xp=1000.0):
    """n runs alternating +-jitter around the same gold per run."""
    return [run(stage, seconds, gold * (1 + jitter * (-1) ** i), xp) for i in range(n)]


def save_row(stage, minutes, gold_h, clears=0):
    return {'stage': stage, 'party': PARTY, 'minutes': minutes, 'gold_monster_per_h': gold_h, 'clears': clears,
            'clears_per_h': 0.0, 'kills_per_h': 2000.0, 'xp_incomplete_heroes': [],
            'xp_per_h': {'201': {'name': 'A', 'value': 5.0}}}


def evidence(runs, saves=()):
    return stage_evidence(runs, list(saves), lambda stage: f'S{stage}', PARTY)


class StatisticsTests(unittest.TestCase):
    def test_ratio_rate_weights_runs_by_time(self):
        r = ratio_rate([(100, 60), (300, 120)])
        self.assertAlmostEqual(r['per_h'], 400 / 180 * 3600)
        self.assertIsNotNone(r['se_h'])

    def test_single_run_has_no_spread(self):
        r = ratio_rate([(100, 540)])
        self.assertIsNone(r['rse'])
        self.assertEqual(confidence(1, r['rse']), 'insufficient')

    def test_confidence_needs_runs_and_low_spread(self):
        self.assertEqual(confidence(2, 0.01), 'insufficient')
        self.assertEqual(confidence(4, 0.01), 'low')
        self.assertEqual(confidence(5, 0.12), 'low')
        self.assertEqual(confidence(5, 0.08), 'medium')
        self.assertEqual(confidence(10, 0.04), 'high')

    def test_runs_needed_grows_with_spread(self):
        self.assertEqual(runs_needed(1, None), 4)
        self.assertEqual(runs_needed(5, 0.2), 15)   # SE halves with 4x the runs: 20 runs total
        self.assertEqual(runs_needed(8, 0.05), 0)


class StageTests(unittest.TestCase):
    def test_one_long_run_is_not_confirmed_however_long(self):
        ev = evidence([run(1, 540, 90_000)])
        self.assertEqual(ev[0]['gold_confidence'], 'insufficient')
        best, _, promising = best_stage(ev, 'gold_h')
        self.assertIsNone(best)
        self.assertEqual(promising[0]['stage'], 1)

    def test_many_short_consistent_runs_are_confirmed(self):
        ev = evidence(steady(1, 6, 90, 2500))
        self.assertEqual(ev[0]['gold_confidence'], 'medium')
        self.assertAlmostEqual(ev[0]['gold_h'], 2500 * 40)

    def test_noisy_runs_stay_unconfirmed(self):
        ev = evidence(steady(1, 6, 90, 2500, jitter=0.6))
        self.assertEqual(ev[0]['gold_confidence'], 'low')

    def test_save_counters_alone_are_insufficient(self):
        ev = evidence([], [save_row(2, 60, 999.0)])
        self.assertEqual(ev[0]['gold_h'], 999.0)
        self.assertEqual(ev[0]['gold_confidence'], 'insufficient')
        self.assertIn('save counters', ev[0]['source'])

    def test_faster_stage_wins_on_gold_per_hour_not_per_run(self):
        # 100 gold in 1 min (6000/h) beats 350 gold in 4 min (5250/h).
        ev = evidence(steady(1, 6, 60, 100) + steady(2, 6, 240, 350))
        best, ties, _ = best_stage(ev, 'gold_h')
        self.assertEqual(best['stage'], 1)
        self.assertAlmostEqual(best['gold_h'], 6000)
        self.assertEqual(ties, [])

    def test_parties_stay_apart_without_a_filter(self):
        runs = steady(1, 3, 90, 100, jitter=0) + [dict(r, party='101') for r in steady(1, 3, 90, 900, jitter=0)]
        ev = stage_evidence(runs, [], lambda stage: f'S{stage}')
        self.assertEqual(sorted((e['party'], round(e['gold_h'])) for e in ev),
                         [('101', 36000), ('201,401,501', 4000)])

    def test_other_party_is_ignored(self):
        other = dict(run(1, 90, 100), party='101')
        self.assertEqual(evidence([other]), [])

    def test_small_gap_is_a_tie_not_a_switch(self):
        ev = evidence(steady(1, 6, 90, 2500, jitter=0.08) + steady(2, 6, 90, 2550, jitter=0.08))
        best, ties, _ = best_stage(ev, 'gold_h')
        self.assertEqual(best['stage'], 2)
        self.assertEqual([t['stage'] for t in ties], [1])
        rows = {s['id']: s for s in stage_suggestions(ev, 1, [])}
        self.assertFalse(rows['stage-gold']['metrics']['significant'])
        self.assertEqual(rows['stage-gold']['priority'], 2)

    def test_significant_gain_is_do_first(self):
        ev = evidence(steady(1, 6, 90, 2500, xp=2000.0) + steady(2, 6, 90, 3750, xp=1000.0))
        rows = {s['id']: s for s in stage_suggestions(ev, 1, [])}
        self.assertEqual(rows['stage-gold']['metrics']['stage'], 2)
        self.assertAlmostEqual(rows['stage-gold']['metrics']['gain_pct_vs_current'], 50.0)
        self.assertTrue(rows['stage-gold']['metrics']['significant'])
        self.assertEqual(rows['stage-gold']['priority'], 1)
        self.assertEqual(rows['stage-xp']['metrics']['stage'], 1)

    def test_close_stage_measured_before_power_changes_is_flagged(self):
        ev = evidence(steady(1, 6, 90, 2500) + steady(2, 6, 90, 3000))
        for e in ev:
            e['changes_since'], e['changes_since_kinds'] = (3, ['gear', 'rune']) if e['stage'] == 1 else (0, [])
        rows = {s['id']: s for s in stage_suggestions(ev, 2, [])}
        self.assertIn('last measured before 3 power change(s) (gear, rune)', rows['stage-gold']['detail'])
        self.assertIn('stage-remeasure-1', rows)
        far = evidence(steady(1, 6, 90, 1000, xp=300.0) + steady(2, 6, 90, 3000))
        for e in far:
            e['changes_since'], e['changes_since_kinds'] = (3, ['gear']) if e['stage'] == 1 else (0, [])
        self.assertNotIn('stage-remeasure-1', {s['id'] for s in stage_suggestions(far, 2, [])})

    def test_history_fills_in_until_the_current_build_is_confirmed(self):
        old = evidence(steady(1, 6, 90, 2500) + steady(2, 6, 90, 3000))
        for e in old:
            e.update(current_build_comparable=False, changes_since=2, changes_since_kinds=['rune'])
        now = evidence([run(1, 90, 2600)])
        rows = {s['id']: s for s in stage_suggestions(now, 1, [], history=old)}
        self.assertNotIn('stage-gold-none', rows)
        card = rows['stage-gold-history']
        self.assertEqual(card['metrics']['stage'], 2)
        self.assertTrue(card['metrics']['historical'])
        self.assertEqual(card['confidence'], 'low')
        self.assertIn('before 2 later power change(s) (rune)', card['detail'])
        self.assertIn('the current build has 1 so far, on S1', card['detail'])
        # Without history there is still nothing to show.
        self.assertIn('stage-gold-none', {s['id'] for s in stage_suggestions(now, 1, [], history=[])})

    def test_confirmed_current_build_wins_over_history(self):
        old = evidence(steady(2, 6, 90, 9000))
        for e in old:
            e.update(current_build_comparable=False, changes_since=1, changes_since_kinds=['gear'])
        rows = {s['id']: s for s in stage_suggestions(evidence(steady(1, 6, 90, 2500)), 1, [], history=old)}
        self.assertEqual(rows['stage-gold']['metrics']['stage'], 1)
        self.assertNotIn('stage-gold-history', rows)
        self.assertIn('stage-remeasure-2', rows)

    def test_promising_stage_reports_runs_needed(self):
        ev = evidence(steady(1, 6, 90, 2500) + [run(2, 540, 60_000)])
        rows = {s['id']: s for s in stage_suggestions(ev, 1, [])}
        test = rows['stage-gold-test-2']
        self.assertEqual(test['metrics']['runs_needed'], 4)
        self.assertIn('~36 min', test['detail'])


class FakeCatalog:
    text = {'AccountStat_IncreaseGoldAmount': '{0}% Increased Gold Per Kill',
            'AccountStat_IncreaseExpAmount': '{0}% Increased Exp Gain',
            'AccountStat_AdditionalGoldStageBoss': 'Gold From Stage Boss Kill +{0}',
            'AccountStat_AdditionalExpActBoss': 'Exp From Act Boss Kill +{0}',
            'AccountStat_AdditionalExpNormalMonster': 'Exp From Normal Monster Kill +{0}',
            'AccountStat_AllHeroArmor': 'All Hero Armor +{0}',
            'AccountStat_AllHeroAttackSpeed': '{0}% Increased All Hero Attack Speed',
            'AccountStat_CubeAlchemyGoldPercent': '{0}% Increased Gold From Cube Alchemy',
            'AccountStat_OfflineRewardGoldPercent': 'Offline Reward Gold +{0}%',
            'AccountStat_UnlockAutoOpenNormalChest': 'Unlock Common Chest Auto Open'}

    def localize(self, key, locale='en-US'):
        return self.text.get(key)


def effect(stat, value):
    return rune_effect(FakeCatalog(), stat, value)


def node(key, stat, cost, value, level=0, next_runes=()):
    return {'rune_key': key, 'name': f'R{key}', 'level': level, 'max_level': 5, 'stat': stat,
            'next_cost': cost, 'next_cost_item': '100001', 'next_value': value, 'purchasable_hint': True,
            'next_runes': list(next_runes), 'next_effect': effect(stat, value)}


def owned(stat, value):
    return {stat: {'value': value, 'effect': effect(stat, value)}}


class RuneTests(unittest.TestCase):
    REF = {'gold_h': 100_000.0, 'xp_h': 1_000_000.0, 'clears_per_h': 10.0, 'kills_per_h': 1000.0}

    def test_stored_value_is_shown_with_the_game_scale(self):
        # Checked in game: "% increased" stores ten times the percent, "+N" stores N.
        e = effect('IncreaseGoldAmount', 50)
        self.assertEqual(e['text'], '5% Increased Gold Per Kill')
        self.assertTrue(e['verified'])
        self.assertEqual(effect('IncreaseExpAmount', 100)['text'], '10% Increased Exp Gain')
        self.assertEqual(effect('AdditionalExpActBoss', 100)['value'], 100)
        self.assertTrue(effect('AdditionalExpNormalMonster', 1)['verified'])
        self.assertEqual(effect('AllHeroArmor', 5)['text'], 'All Hero Armor +5')
        self.assertEqual(effect('AllHeroAttackSpeed', 20)['text'], '2% Increased All Hero Attack Speed')
        # Totals as listed by the in-game Stat List panel.
        self.assertEqual(effect('IncreaseGoldAmount', 1100)['text'], '110% Increased Gold Per Kill')
        self.assertEqual(effect('CubeAlchemyGoldPercent', 200)['text'], '20% Increased Gold From Cube Alchemy')
        self.assertEqual(effect('OfflineRewardGoldPercent', 300)['text'], 'Offline Reward Gold +30%')
        self.assertTrue(effect('CubeAlchemyGoldPercent', 200)['verified'])
        self.assertIsNone(effect('UnlockAutoOpenNormalChest', 300))           # unlock text: unknown scale

    def test_percent_gold_payback_uses_game_percent_and_owned_total(self):
        rows = rune_suggestions([node(1, 'IncreaseGoldAmount', 50_000, 50)],
                                owned('IncreaseGoldAmount', 1000), 0, self.REF)
        # +5% on top of 100% owned: 100000 * 5 / 200 = 2500 gold/h -> payback 20 h.
        self.assertAlmostEqual(rows[0]['metrics']['payback_h'], 20.0)
        self.assertEqual(rows[0]['basis'], 'hypothesis')
        self.assertIn('adds 5% Increased Gold Per Kill', rows[0]['detail'])

    def test_unknown_scale_is_not_estimated(self):
        rows = rune_suggestions([node(2, 'UnlockAutoOpenNormalChest', 10_000, 100)], {}, 0, self.REF)
        self.assertTrue(all(r['metrics']['payback_h'] is None for r in rows))
        self.assertTrue(all('raw value' in r['detail'] for r in rows))

    def test_flat_boss_gold_uses_observed_clears(self):
        rows = rune_suggestions([node(8, 'AdditionalGoldStageBoss', 10_000, 100)], {}, 0, self.REF)
        # +100 gold per boss x 10 clears/h = 1000 gold/h -> payback 10 h.
        self.assertAlmostEqual(rows[0]['metrics']['payback_h'], 10.0)
        self.assertEqual(rows[0]['basis'], 'observed')
        self.assertIn('Gold From Stage Boss Kill +100', rows[0]['detail'])

    def test_flat_normal_kill_xp_is_observed(self):
        rows = rune_suggestions([node(9, 'AdditionalExpNormalMonster', 10_000, 1)], {}, 0, self.REF)
        # +1 XP x 1000 kills/h on 1,000,000 XP/h = +0.1%.
        self.assertAlmostEqual(rows[0]['metrics']['xp_pct'], 0.1)
        self.assertEqual(rows[0]['basis'], 'observed')

    def test_speed_rune_gets_a_scenario_not_a_guaranteed_bound(self):
        rows = rune_suggestions([node(10, 'AllHeroAttackSpeed', 10_000, 20)], owned('AllHeroAttackSpeed', 70),
                                0, self.REF)
        # +2% on top of 7%: proportional throughput would give +1869 gold/h, not a bound.
        power = next(r for r in rows if r['title'].startswith('Power'))
        self.assertIn('scenario +1,869 gold/h', power['title'])
        self.assertIn('not a bound', power['detail'])
        self.assertIsNone(power['metrics']['payback_h'])

    def test_non_gold_cost_and_unpurchasable_are_skipped(self):
        locked = dict(node(3, 'AllHeroAttackDamage', 10, 1), purchasable_hint=False)
        other = dict(node(4, 'AllHeroAttackDamage', 10, 1), next_cost_item='999')
        self.assertEqual(rune_suggestions([locked, other], {}, 10**9, self.REF), [])

    def test_cheap_rune_is_listed_once(self):
        rows = rune_suggestions([node(5, 'MaxInventorySlot', 100, 1, next_runes=[6])], {}, 1000, self.REF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['metrics']['opens'], 1)

    def test_title_names_the_node_effect_and_owned_level(self):
        # Many nodes share a name ("Rune of Wealth"): the title must say which one and where it stands.
        rune = dict(node(7, 'IncreaseGoldAmount', 50_000, 50, level=4), stat_name='Gold Per Monster Kill')
        rows = rune_suggestions([rune], owned('IncreaseGoldAmount', 1000), 0, self.REF)
        self.assertIn('R7 · Gold Per Monster Kill 4→5/5', rows[0]['title'])
        self.assertIn('You own level 4 of 5', rows[0]['detail'])


class PurchaseCardTests(unittest.TestCase):
    def row(self, status, **kw):
        return {'status': status, 'changes': [{'label': 'Hunter · Armor: A → B'}], 'stage_label': '3-8',
                'runs_before': 1, 'runs_after': 0, 'runs_needed': 3, 'level_ups': 0, 'metrics': {}, 'expected': {}, **kw}

    def test_latest_change_is_reported_even_when_not_measurable(self):
        # An older measurable change must not be presented as "your last change".
        cards = purchase_suggestions([self.row('too few runs before'), self.row('measured', runs_before=7)])
        self.assertTrue(cards[0]['title'].startswith('Your last change cannot be measured'))


def gear(uid, name, level, stats, part='HELMET', gear_type='HELMET'):
    return {'unique_id': uid, 'name': name, 'level': level, 'grade': 'RARE', 'type': 'GEAR', 'parts': part,
            'slot_part': part, 'gear_type': gear_type,
            'stats': [{'kind': 'base', 'stat': k, 'value': v} for k, v in stats.items()]}


def party(*heroes):
    return [{'hero_key': k, 'name': n, 'in_party': True, 'equipment': worn} for k, n, worn in heroes]


def stash(*items):
    return {'stash': {'label': 'Stash', 'items': list(items)}}


class GearTests(unittest.TestCase):
    THRESHOLDS = {24: 1000.0, 25: 1000.0, 26: 1000.0, 27: 1000.0, 28: 1000.0, 29: 1000.0}

    def test_equippable_item_goes_to_one_hero_and_needs_all_base_stats_not_lower(self):
        equipment = party((1, 'A', [gear('w1', 'Old', 10, {'Armor': 10})]),
                          (2, 'B', [gear('w2', 'Mid', 20, {'Armor': 20})]))
        rows = gear_suggestions(equipment, stash(gear('s1', 'New', 24, {'Armor': 30})),
                                {1: {'level': 24}, 2: {'level': 24}})
        self.assertEqual([r['id'] for r in rows], ['gear-1-HELMET'])   # largest relative gain wins the one item
        self.assertIn('Equippable now', rows[0]['detail'])

    def test_item_above_hero_level_waits_with_the_level_and_eta(self):
        # The game refuses gear above the hero's level ("Level too low to equip.").
        equipment = party((1, 'A', [gear('w1', 'Old', 10, {'Armor': 10})]))
        rows = gear_suggestions(equipment, stash(gear('s1', 'War Helmet', 27, {'Armor': 50})),
                                {1: {'level': 24, 'xp': 500.0}}, self.THRESHOLDS, xp_h=1000.0)
        self.assertEqual([r['id'] for r in rows], ['gear-later-1'])
        later = rows[0]
        self.assertEqual(later['metrics']['unlock_level'], 27)
        self.assertEqual(later['metrics']['xp_needed'], 2500.0)        # 500 left at 24, then 1000 + 1000
        self.assertIn('about 2.5 h of farming', later['detail'])

    def test_lower_level_item_with_better_base_stats_is_suggested(self):
        # Base stats already reflect item level and grade: a lower-level item can still be better.
        equipment = party((1, 'A', [gear('w1', 'Knight Boots', 15, {'Armor': 38}, 'BOOTS', 'BOOTS')]))
        rows = gear_suggestions(equipment, stash(gear('s1', 'Iron Boots', 10, {'Armor': 48}, 'BOOTS', 'BOOTS')),
                                {1: {'level': 24}})
        self.assertEqual(rows[0]['metrics']['unique_id'], 's1')

    def test_empty_slot_takes_a_fitting_weapon_only(self):
        equipment = party((1, 'A', []))
        rows = gear_suggestions(equipment, stash(gear('s1', 'Sword', 10, {'AttackDamage': 5}, 'MAIN_WEAPON', 'SWORD'),
                                                 gear('s2', 'Bow', 12, {'AttackDamage': 9}, 'MAIN_WEAPON', 'BOW')),
                                {1: {'level': 24}}, weapons={1: {'MAIN_WEAPON': 'BOW', 'SUB_WEAPON': 'ARROW'}})
        self.assertEqual([(r['id'], r['metrics']['unique_id']) for r in rows], [('gear-1-MAIN_WEAPON', 's2')])
        self.assertEqual(rows[0]['priority'], 1)

    def test_losing_a_stat_is_a_trade_off_not_an_upgrade(self):
        # The case seen in game: more armor and HP, but the worn gloves also give attack damage.
        worn = gear('w1', 'Iron Gloves', 10, {'Armor': 46}, 'GLOVES', 'GLOVES')
        worn['stats'] += [{'kind': 'inherent', 'stat': 'AttackDamage', 'value': 3}, {'kind': 'inherent', 'stat': 'MaxHp', 'value': 25}]
        new = gear('s1', 'Chain Gloves', 20, {'Armor': 92}, 'GLOVES', 'GLOVES')
        new['stats'] += [{'kind': 'inherent', 'stat': 'Armor', 'value': 36}, {'kind': 'inherent', 'stat': 'MaxHp', 'value': 40}]
        rows = gear_suggestions(party((1, 'A', [worn])), stash(new), {1: {'level': 24}})
        self.assertEqual([r['id'] for r in rows], ['gear-tradeoff-1-GLOVES'])
        self.assertIn('Armor 46→128', rows[0]['detail'])     # base + inherent armor add up
        self.assertIn('loses AttackDamage 3→0', rows[0]['detail'])
        self.assertEqual(rows[0]['priority'], 4)

    def test_worn_enchant_counts_as_a_loss(self):
        worn = gear('w1', 'Old', 10, {'Armor': 10})
        worn['enchants'] = [{'stat': 'CriticalChance', 'mod_type': 'FLAT', 'value': 5}]
        rows = gear_suggestions(party((1, 'A', [worn])), stash(gear('s1', 'New', 10, {'Armor': 20})), {1: {'level': 24}})
        self.assertTrue(all(not r['id'].startswith('gear-1-') for r in rows))

    def test_trade_off_losing_more_than_it_gains_is_hidden(self):
        worn = gear('w1', 'Old', 10, {'Armor': 50})
        worn['stats'].append({'kind': 'inherent', 'stat': 'MaxHp', 'value': 40})
        new = gear('s1', 'New', 10, {'Armor': 10})
        new['stats'].append({'kind': 'inherent', 'stat': 'AttackSpeed', 'value': 75})
        self.assertEqual(gear_suggestions(party((1, 'A', [worn])), stash(new), {1: {'level': 24}}), [])


class SynthesisTests(unittest.TestCase):
    def group(self, quantity, options=True):
        items = [{'name': 'Minor Sapphire', 'quantity': 5, 'material_type': 'DECORATION'},
                 {'name': 'Minor Sapphire', 'quantity': 3, 'material_type': 'DECORATION'},
                 {'name': 'Wood', 'quantity': quantity - 8, 'material_type': 'CRAFTING'}]
        return {'synthesis_type': 'Material', 'grade': 'COMMON', 'quantity': quantity, 'gate': None, 'items': items,
                'result_chances': [{'grade': 'UNCOMMON', 'chance': 100000 / 105000}, {'grade': 'RARE', 'chance': 5000 / 105000}],
                'options': [{'tier': 3, 'material_amount': 9, 'batches': quantity // 9, 'result_levels': []}] if options else []}

    def test_card_only_with_a_full_set_of_nine(self):
        self.assertEqual(synthesis_suggestion(self.group(8, options=False)), [])

    def test_card_explains_mix_chances_leftover_and_pool(self):
        card = synthesis_suggestion(self.group(20))[0]
        self.assertIn('20 eligible Common material items → up to 2 result(s)', card['title'])
        self.assertIn('95.2% Uncommon, 4.8% Rare', card['detail'])
        self.assertIn('keep 2', card['detail'])
        self.assertIn('Minor Sapphire ×8', card['detail'])          # stacks in two slots are added up
        self.assertIn('crafting ingredients: Wood ×12', card['detail'])
        self.assertEqual(card['priority'], 4)


class SynthesisRulesTests(unittest.TestCase):
    def test_grade_gates_from_game_text(self):
        from tbh.views.cube import synthesis_gate
        self.assertIsNone(synthesis_gate('LEGENDARY', 1))
        self.assertIn('Cube level 10', synthesis_gate('IMMORTAL', 9))
        self.assertIsNone(synthesis_gate('IMMORTAL', 25))
        self.assertIn('cannot be synthesized', synthesis_gate('COSMIC', 99))


class AttributeCatalog:
    def index(self, table, key):
        return {'201001': {'HeroKey': '201'}, '601001': {'HeroKey': '601'}}


def save(t, runes=1, skill=50101, attrs=None, party=(201, 401, 501)):
    power = {('rune', 113): runes, ('pet',): 7, ('skill', 501, 1): skill}
    power.update({('attribute', k): v for k, v in (attrs or {}).items()})
    return {'last_saved_utc': f'2026-10-05T09:{t:02d}:00+00:00', 'power': power, 'party': list(party)}


def timed(start, end):
    return {'party': '201,401,501', 'started_utc': f'2026-10-05T09:{start:02d}:10+00:00',
            'ended_utc': f'2026-10-05T09:{end:02d}:10+00:00'}


class BuildTests(unittest.TestCase):
    def test_attribute_points_of_heroes_outside_the_party_are_ignored(self):
        state = {('rune', 1): 2, ('attribute', 201001): 3, ('attribute', 601001): 5, ('skill', 601, 0): 1}
        self.assertEqual(party_build(state, {'201', '401', '501'}, {'201001': '201', '601001': '601'}),
                         {('rune', 1): 2, ('attribute', 201001): 3})
        self.assertIsNone(party_build(None, {'201'}, {}))

    def test_an_undone_change_keeps_earlier_runs_in_the_current_build(self):
        saves = [save(0), save(4), save(5, skill=None), save(10), save(15, attrs={601001: 5}), save(20)]
        timeline = BuildTimeline(saves, AttributeCatalog())
        build = timeline.current('201,401,501')
        self.assertTrue(timeline.matches(timed(1, 3), build))       # before the skill was taken off and put back
        self.assertFalse(timeline.matches(timed(6, 8), build))      # while it was off
        self.assertFalse(timeline.matches(timed(3, 7), build))      # spans the change
        self.assertTrue(timeline.matches(timed(16, 18), build))     # points on a hero outside the party

    def test_a_permanent_change_starts_a_new_build(self):
        timeline = BuildTimeline([save(0), save(5, runes=2)], AttributeCatalog())
        build = timeline.current('201,401,501')
        self.assertFalse(timeline.matches(timed(1, 3), build))      # the change may have happened during it
        self.assertTrue(timeline.matches(timed(6, 8), build))
        self.assertFalse(timeline.matches(timed(1, 3), None))


if __name__ == '__main__':
    unittest.main()
