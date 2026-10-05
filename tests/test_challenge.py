"""Act boss plan: boss data, the fight race, point plans, fight recording. Synthetic data only."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tbh.analysis.challenge import best_points, boss_target, fight, score
from tbh.analysis.combat import RunCombat
from tbh.views.challenge import Party, act_boss_steps, _gear_options

K = {'bfmi': 0.4, 'bfmj': 12.0, 'bfmk': 14.0, 'bfml': 0.85, 'bfmm': 0.75, 'bfmq': 0.95, 'bfmr': 3.0, 'bfms': 2.8, 'bfmt': 11.0}


class Catalog:
    stages = {'2210': {'STAGETYPE': 'ACTBOSS', 'StageLevel': '45', 'BossMonsterKey': '20902'}}
    stage_levels = {'45': {'MonsterHpMultiplier': '70770', 'MonsterAtkDmgMultiplier': '2200'}}
    monsters = {'20902': {'MaxLife': '1950', 'AttackDamage': '100', 'AttackSpeed': '170', 'SkillKey': '1 2 3'}}

    def index(self, name, key):
        return {'1': {'ACTIVATIONTYPE': 'BASEATTACK', 'Value': '1000', 'ActivationValue': '0'},
                '2': {'ACTIVATIONTYPE': 'COOLDOWN', 'Value': '1800', 'ActivationValue': '8'},
                '3': {'ACTIVATIONTYPE': 'BASEATTACK_COUNT', 'Value': '1350', 'ActivationValue': '6'}}


def hero(key, hp, armor, absorption=0.0, ad=100.0, aspd=1.0):
    return {'hero_key': key, 'max_hp': hp, 'resistances': None,
            'final': {'Armor': armor, 'DamageAbsorption': absorption, 'MaxHp': hp, 'AttackDamage': ad, 'AttackSpeed': aspd}}


class TargetTests(unittest.TestCase):
    def test_gear_plan_rejects_unmapped_losses_and_locked_candidates(self):
        current = {'stats': [{'stat': 'Armor', 'value': 50}]}
        item = {'type': 'GEAR', 'parts': 'ARMOR', 'level': 1, 'stats': [{'stat': 'MaxHp', 'value': 20}]}
        ctx = SimpleNamespace(enums={}, gear_scales={('MaxHp', 'FLAT'): 1}, worn={401: {'ARMOR': current}})
        party = SimpleNamespace(ctx=ctx, snapshot={}, catalog=SimpleNamespace(heroes={}), live={401: {}}, locale='en-US')
        with patch('tbh.views.challenge.containers', return_value={'stash': {'items': [item]}}):
            self.assertEqual(_gear_options(party, 20), {})
            ctx.gear_scales[('Armor', 'FLAT')] = 1
            self.assertIn((401, 'ARMOR'), _gear_options(party, 20))
            item['blocked'] = True
            self.assertEqual(_gear_options(party, 20), {})

    def test_act_boss_has_no_stage_multipliers_and_skills_add_damage(self):
        t = boss_target(Catalog(), 2210)
        self.assertAlmostEqual(t['hp'], 1950 * 70.77)
        self.assertAlmostEqual(t['damage'], 220.0)
        self.assertAlmostEqual(t['attacks_per_s'], 100 / 170)    # fitted on the recorded 2210 fights (D011)
        rate = 100 / 170
        self.assertAlmostEqual(t['skill_factor'], 1 + 0.8 / (8 * rate) + 0.35 / 6)   # 1.8 every 8 s, 1.35 every 6th
        self.assertIsNone(boss_target(Catalog(), 9999))


class FightTests(unittest.TestCase):
    def test_hits_match_the_lost_fights_and_hp_for_one_more_hit(self):
        t = boss_target(Catalog(), 2210)
        party = [hero(401, 428.0, 1244.0, 9.8), hero(201, 104.0, 652.0)]
        f = fight(t, party, K, dps=6522.0, dps_offence=1507.0)
        priest, ranger = f['heroes']
        self.assertEqual(priest['hits'], 7)            # 6.25 -> dies on the 7th (6-7 HP drops recorded)
        self.assertEqual(ranger['hits'], 1)            # one hit (no HP reading before the death)
        self.assertEqual(ranger['hp_for_next_hit'], int(ranger['hit'] - 104.0) + 1)
        self.assertAlmostEqual(f['survive_s'], 8 / (t['attacks_per_s'] * t['skill_factor']))
        self.assertAlmostEqual(f['kill_s'], t['hp'] / (6522.0 * 200.0 / 1507.0))
        self.assertAlmostEqual(f['margin'], f['survive_s'] / f['kill_s'])

    def test_without_measured_damage_plans_still_compare(self):
        t = boss_target(Catalog(), 2210)
        weak, strong = fight(t, [hero(401, 400.0, 1000.0)], K, None, None), fight(t, [hero(401, 500.0, 1000.0)], K, None, None)
        self.assertIsNone(weak['margin'])
        self.assertGreater(score(strong), score(weak))


class PointPlanTests(unittest.TestCase):
    def test_moves_points_to_the_better_option_within_caps_and_rules(self):
        # Value: 3 per point of 'b', 1 per point of 'a'; 'b' capped at 4; a plan with 'c' is never allowed.
        evaluate = lambda plan: plan[1].get('a', 0) + 3 * plan[1].get('b', 0)
        plan, value = best_points({1: {'a': 10, 'b': 4, 'c': 10}}, {1: {'a': 6}}, evaluate,
                                  valid=lambda p: not p[1].get('c'))
        self.assertEqual((plan[1]['a'], plan[1]['b'], plan[1].get('c', 0)), (2, 4, 0))
        self.assertEqual(value, 14)
        plan, _ = best_points({1: {'a': 10, 'b': 4}}, {1: {'a': 6}}, evaluate, extra_points=3, max_moves=0)
        self.assertEqual((plan[1]['a'], plan[1].get('b', 0)), (6, 3))   # new points go where they are worth most

    def test_group_rule_counts_points_in_earlier_groups(self):
        party = Party.__new__(Party)
        party.group_of = {(1, 'g1'): '10001', (1, 'g2'): '10002'}
        party.thresholds = {'10001': 0, '10002': 10}
        party.all_levels = {1: {'g1': 10, 'g2': 2}}
        self.assertTrue(party.valid({1: {'g1': 10, 'g2': 2}}))
        self.assertFalse(party.valid({1: {'g1': 9, 'g2': 3}}))          # group 2 needs 10 points before it


class FightRecordingTests(unittest.TestCase):
    def reading(self, second, boss_hp, heroes=True, dead=None):
        party = [{'hero_key': 401, 'hp': 400.0 - second, 'max_hp': 428.0, 'state': 'IDLE', 'buffs': []}] if heroes else []
        return {'utc': f'2026-10-01T21:49:{second:02d}+00:00', 'heroes': party, 'dead': dead or {},
                'enemies': [{'id': 'b1', 'monster_key': 20902, 'hp': boss_hp, 'max_hp': 138001.5}] if boss_hp is not None else []}

    def test_boss_damage_and_wipe_time(self):
        run = RunCombat(('t', 2210), {'monster_key': 20902, 'min_max_hp': 0})
        for r in (self.reading(0, 138001.5), self.reading(2, 130000.0), self.reading(6, 100000.0),
                  self.reading(10, 80000.0, heroes=False, dead={401: 90.0})):
            run.feed(r)
        s = run.summary()
        self.assertEqual(s['boss']['killed'], False)
        self.assertAlmostEqual(s['boss']['hp_left_fraction'], 80000.0 / 138001.5)
        self.assertAlmostEqual(s['boss']['dps'], (138001.5 - 80000.0) / 10)    # damage began before the 2 s reading
        self.assertEqual(s['wipe_s'], 10.0)
        self.assertEqual(s['heroes'][401]['died_s'], 10.0)


class StepsTests(unittest.TestCase):
    def test_plan_reads_as_ordered_steps(self):
        t = {'now': {'heroes': [{'name': 'Ranger', 'hits': 1, 'hp_for_next_hit': 42}, {'name': 'Priest', 'hits': 9, 'hp_for_next_hit': 50}]},
             'both': {'margin': 0.7}, 'retry': None, 'missing_factor': 2.1,
             'respec': {'changes': [{'hero': 'Priest', 'stat': 'Max HP', 'from': 0, 'to': 10}]},
             'gear': {'swaps': []}, 'stat_rolls': [{'stat': '+50 Max HP', 'hero': 'Priest', 'margin_pct': 8.0}]}
        steps = act_boss_steps(t, {'safety': 1.5})
        self.assertTrue(steps[0].startswith('Survive more boss hits (cheapest first): Ranger +42 Max HP → 2 hits'))
        self.assertIn('Priest Max HP 0→10', steps[1])
        self.assertIn('×2.1', steps[-1])


if __name__ == '__main__':
    unittest.main()
