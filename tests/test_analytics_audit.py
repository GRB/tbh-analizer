"""Regression cases found by the October analytics audit; synthetic data only."""
import json
import tempfile
import unittest
from pathlib import Path

from tbh.analysis.economy import save_window, stage_rates
from tbh.analysis.progress import xp_to_level
from tbh.analysis.stagestats import ratio_rate, run_xp, best_stage
from tbh.analysis.stagemodel import _per_run
from tbh.analysis.threat import hit, stage_threat
from tbh.save.es3 import SaveReadError, encrypt, load_save
from tbh.views.analytics import complete_runs
from tbh.views.cube import synthesis_hints
from tbh.views.power import item_raw
from tbh.views.suggestions import compare_items, synthesis_suggestion, stage_suggestions
from tests.test_core import snap, sample, THRESHOLDS, FakeCatalog
from tbh.analysis.runs import RunTracker
from tests.test_suggestions import evidence, run, steady, gear
from tests.test_threat import K, Catalog, PRIEST


class AccountingAuditTests(unittest.TestCase):
    def window(self, **end):
        a = snap('2026-10-01T10:00:00+00:00', 10, gold_earned=100, play=0)
        b = snap('2026-10-01T10:01:00+00:00', 11, gold_earned=200, play=60)
        b.update(end)
        return save_window(a, b, THRESHOLDS)

    def test_missing_play_time_cannot_prove_continuity(self):
        self.assertFalse(self.window(play_time=None)['valid'])

    def test_missing_counter_does_not_become_zero(self):
        row = stage_rates([self.window()])[0]
        self.assertIsNone(row['kills_per_h'])
        self.assertIsNone(row['hero_deaths'])

    def test_partial_xp_is_not_divided_by_full_exposure(self):
        good, bad = self.window(), self.window()
        good['xp']['401']['gain'] = 100
        bad['xp']['401']['gain'] = None
        row = stage_rates([good, bad])[0]
        self.assertNotIn('401', row['xp_per_h'])
        self.assertEqual(row['xp_incomplete_heroes'], ['401'])

    def test_missing_hero_xp_is_incomplete(self):
        row = stage_rates([self.window(heroes=[])])[0]
        self.assertEqual(row['xp_incomplete_heroes'], ['401'])

    def test_other_stage_between_matching_endpoints_invalidates_window(self):
        a = snap('2026-10-01T10:00:00+00:00', 10, play=0)
        b = snap('2026-10-01T10:01:00+00:00', 11, play=60)
        b['aggregates'].append({'type': 13, 'subkey': 1310, 'content': 0, 'value': 1})
        self.assertIn('other stage played inside window', save_window(a, b, THRESHOLDS)['reasons'])

    def test_counter_rollback_is_not_negative_income(self):
        a = snap('2026-10-01T10:00:00+00:00', 10, gold_earned=100, play=0)
        b = snap('2026-10-01T10:01:00+00:00', 9, gold_earned=50, play=60)
        self.assertFalse(save_window(a, b, THRESHOLDS)['valid'])

    def test_xp_eta_requires_current_xp(self):
        self.assertIsNone(xp_to_level(1, None, 2, THRESHOLDS))


class RateAuditTests(unittest.TestCase):
    def test_unplayed_stage_with_no_xp_calibration_stays_unknown(self):
        candidate = {'stage': 2, 'label': 'S2', 'reachable': True,
                     'estimate': {'gold_h': 100, 'xp_h': None, 'duration_s': 60, 'dmg_vs_observed': 1}}
        cards = stage_suggestions([], 1, [candidate])
        card = next(c for c in cards if c['id'] == 'stage-untested-2')
        self.assertIn('XP rate unknown', card['detail'])
        self.assertIn('range unknown', card['detail'])

    def test_new_stage_wave_cannot_clear_previous_stage(self):
        tracker = RunTracker(FakeCatalog())
        tracker.feed(sample(0, 1))
        ended = tracker.feed(sample(1, 14, stage=1110))[0]
        self.assertIsNone(ended['final_utc'])
        self.assertEqual(ended['outcome'], 'unknown')

    def test_clears_do_not_count_failed_attempts(self):
        e = evidence([run(1, 60, 100), run(1, 60, 10, outcome='fail')])[0]
        self.assertEqual(e['clears_per_h'], 30)

    def test_confidence_counts_only_known_gold(self):
        rows = steady(1, 10, 60, 100)
        for r in rows[2:]:
            r['gold_gain_est'] = None
        e = evidence(rows)[0]
        self.assertEqual(e['gold_runs'], 2)
        self.assertEqual(e['gold_confidence'], 'insufficient')

    def test_missing_party_hero_is_not_complete_xp(self):
        r = run(1, 60, 100)
        del r['xp']['501']
        self.assertIsNone(run_xp(r))

    def test_model_uses_only_xp_exposure(self):
        e = {'runs': 10, 'xp_runs': 5, 'minutes': 10, 'xp_h': 6000, 'xp_seconds': 300}
        self.assertEqual(_per_run(e, 'xp_h'), 100)
        del e['xp_seconds']
        self.assertIsNone(_per_run(e, 'xp_h'))

    def test_invalid_rates_excluded(self):
        r = ratio_rate([(1, -1), (float('nan'), 2), (2, 0), (-1, 10), (100, 60)])
        self.assertEqual(r['runs'], 1)
        self.assertEqual(r['per_h'], 6000)

    def test_even_median_uses_both_middle_durations(self):
        self.assertEqual(evidence([run(1, 60, 100), run(1, 120, 100)])[0]['median_duration_s'], 90)

    def test_mixed_build_is_not_a_confirmed_recommendation(self):
        ev = evidence(steady(1, 10, 60, 100))
        ev[0]['current_build_comparable'] = False
        self.assertIsNone(best_stage(ev, 'gold_h')[0])


class ItemAndCombatAuditTests(unittest.TestCase):
    def test_synthesis_excludes_out_of_range_and_locked_slots(self):
        class SynthesisCatalog:
            items = {str(i): {'ItemKey': str(i), 'ItemSynthesisType': 'Gear', 'GRADE': 'COMMON', 'Level': str(lv)}
                     for i, lv in ((1, 10), (2, 25), (3, 30))}

            def item_name(self, key, locale):
                return key

            def table(self, name):
                if name == 'SynthesisRecipeInfoData':
                    return [{'SynthesisRecipeKey': '1', 'ItemSynthesisType': 'Gear', 'GRADE': 'COMMON',
                             'RecipeTier': '1', 'MaterialAmount': '9', 'MinMaterialAverageLevel': '0',
                             'MinResultLevel': '20', **{f'LevelWeight{i}': '1' for i in range(1, 5)}}]
                return []

        slots = [{'unique_id': str(i), 'blocked': False, 'unlocked': i != 3, 'quantity': qty}
                 for i, qty in ((1, 20), (2, 8), (3, 50))]
        snapshot = {'inventory': slots, 'stash': [], 'trading_stash': [], 'cube_level': {'Level': 20},
                    'items': {str(i): {'item_key': i} for i in range(1, 4)}}
        recipes = [{'type': 'SYNTHESIS', 'sub_recipes': [{'tier': 1, 'unlocked': True, 'name': 'Lv.20~40'}]}]
        self.assertEqual(synthesis_hints(snapshot, SynthesisCatalog(), recipes)['groups'][0]['options'], [])
        slots[1]['quantity'] = 9
        option = synthesis_hints(snapshot, SynthesisCatalog(), recipes)['groups'][0]['options'][0]
        self.assertEqual((option['eligible_quantity'], option['batches']), (9, 1))

    def test_new_negative_stat_counts_as_loss(self):
        _, losses, _ = compare_items(gear('a', 'A', 1, {'Armor': 10}),
                                     gear('b', 'B', 1, {'Armor': 20, 'MaxHp': -10}))
        self.assertIn(('MaxHp', 'FLAT'), losses)

    def test_removing_negative_stat_counts_as_gain(self):
        gains, _, _ = compare_items(gear('a', 'A', 1, {'Armor': 10, 'MaxHp': -10}),
                                    gear('b', 'B', 1, {'Armor': 10}))
        self.assertIn(('MaxHp', 'FLAT'), gains)

    def test_zero_armor_is_a_known_value(self):
        self.assertEqual(hit(100, {'final': {'Armor': 0, 'MaxHp': 200}}, 45, K)['hit'], 100)

    def test_boss_without_normal_monsters_still_has_threat(self):
        cat = Catalog()
        cat.stages = {'1': dict(cat.stages['2209'], Monsters='')}
        self.assertIsNotNone(stage_threat(cat, 1, PRIEST, K)['boss'])

    def test_raw_multipliers_are_not_summed(self):
        self.assertIsNone(item_raw({'stats': [{'stat': 'Armor', 'mod': 'MULTIPLICATIVE', 'value': 100}]}, {}))

    def test_synthesis_uses_eligible_quantity_and_does_not_promise_upgrade(self):
        g = {'synthesis_type': 'Gear', 'grade': 'COMMON', 'quantity': 20,
             'items': [], 'result_chances': [{'grade': 'COMMON', 'chance': 1}],
             'options': [{'tier': 1, 'material_amount': 9, 'batches': 1, 'eligible_quantity': 9,
                          'eligible_items': [], 'input_level_range': (10, 20), 'result_levels': []}]}
        c = synthesis_suggestion(g)[0]
        self.assertEqual(c['metrics']['quantity'], 9)
        self.assertNotIn('higher grade', c['title'])

    def test_corrupt_json_with_valid_padding_has_save_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.es3'
            for value in (b'not json', b'[]', b'{"PlayerSaveData":{"value":"[]"}}'):
                path.write_bytes(encrypt(value, 'secret'))
                with self.assertRaises(SaveReadError):
                    load_save(path, 'secret')


if __name__ == '__main__':
    unittest.main()
