"""Boss attacks learned from recorded fights, and the fight simulation. Synthetic data shaped like the
three recorded 2210 fights (D011); no game, no save."""
import unittest

from tbh.analysis import bossprofile as bp
from tbh.analysis.combat import RunCombat

K = {'bfmi': 0.4, 'bfmj': 12.0, 'bfmk': 14.0, 'bfml': 0.85, 'bfmm': 0.75, 'bfmq': 0.95, 'bfmr': 3.0, 'bfms': 2.8, 'bfmt': 11.0}
TARGET = {'stage': 2210, 'monster_key': 20902, 'level': 45, 'hp': 138001.5, 'damage': 220.0, 'attacks_per_s': 100 / 170,
          'skill_factor': 1.2, 'skills': [{'skill_key': 209021, 'value': 1.8, 'trigger': 'COOLDOWN', 'every': 8},
                                           {'skill_key': 209031, 'value': 1.35, 'trigger': 'BASEATTACK_COUNT', 'every': 6}]}


def hero(key, hp, armor, ad=100.0):
    stats = {'Armor': armor, 'MaxHp': hp, 'DamageAbsorption': 0.0, 'AttackDamage': ad, 'AttackSpeed': 1.0}
    return {'hero_key': key, 'max_hp': hp, 'final': stats, 'base': stats, 'resistances': None}


def fight(won, died, heroes, dps=7000.0, drops=8, first_hit=4.0, seconds=15.0):
    combat = {'first_hit_s': first_hit, 'party_offence': None,
              'boss': {'dps': dps, 'killed': won, 'hp_left_fraction': 0.0 if won else 0.1, 'seconds': seconds},
              'heroes': {str(k): {'hp_drops': drops if k == 401 else 0, 'died_s': died.get(k), 'deaths': 1 if k in died else 0}
                         for k in (401, 501, 201)}}
    return {'combat': combat, 'heroes': heroes, 'won': won}


PARTY_A = [hero(401, 510, 1403), hero(501, 303, 1031), hero(201, 66, 754)]
PARTY_B = [hero(401, 510, 1564), hero(501, 78, 1290), hero(201, 151, 818)]
FIGHTS = [fight(False, {201: 9.08, 401: 18.16, 501: 21.19}, PARTY_A),
          fight(True, {201: 9.08}, PARTY_A, dps=9000.0),
          fight(False, {201: 9.11, 501: 9.11}, PARTY_B)]


class LearnTests(unittest.TestCase):
    def test_burst_found_from_deaths_at_the_same_moment(self):
        profile = bp.learn(FIGHTS)
        self.assertEqual(len(profile['bursts']), 1)
        burst = profile['bursts'][0]
        self.assertAlmostEqual(burst['at_s'], 9.08, places=1)
        self.assertEqual(burst['killed'], {201: 3, 501: 1})
        self.assertEqual(burst['survived'][501], 2)
        self.assertEqual(profile['contact_s'], [4.0, 4.0, 4.0])
        # Front hero: HP losses until it died (first fight) or until the boss fight ended (the other two).
        self.assertAlmostEqual(profile['attack_interval_s'], sorted([(18.16 - 4.0) / 8, 15 / 8, 15 / 8])[1])

    def test_only_the_skill_that_explains_every_kill_and_survival_is_kept(self):
        profile = bp.fit_bursts(bp.learn(FIGHTS), TARGET, FIGHTS, K, front=401)
        burst = profile['bursts'][0]
        self.assertEqual(burst['candidates'], [209021])     # x1.35 would not kill a 151 HP Ranger
        self.assertEqual(burst['period_s'], 8)
        self.assertFalse(burst['ambiguous'])

    def test_ambiguous_burst_assumes_the_strongest_skill(self):
        two = FIGHTS[:2]                                     # a 66 HP Ranger dies to either skill
        burst = bp.fit_bursts(bp.learn(two), TARGET, two, K, front=401)['bursts'][0]
        self.assertTrue(burst['ambiguous'])
        self.assertEqual(burst['skill']['skill_key'], 209021)


class SimulateTests(unittest.TestCase):
    def profile(self):
        return bp.fit_bursts(bp.learn(FIGHTS), TARGET, FIGHTS, K, front=401)

    def test_back_line_dies_to_the_burst_and_hp_left_is_reported(self):
        sim = bp.simulate(TARGET, self.profile(), PARTY_B, K, front=401)
        self.assertAlmostEqual(sim['deaths'][201], 9.1, places=1)
        self.assertAlmostEqual(sim['deaths'][501], 9.1, places=1)
        self.assertGreater(sim['deaths'][401], 15)
        self.assertEqual(sim['won'], sim['boss_hp_left'] == 0.0)

    def test_more_back_line_hp_survives_the_burst_and_deals_more(self):
        weak = bp.simulate(TARGET, self.profile(), PARTY_B, K, front=401)
        strong = bp.simulate(TARGET, self.profile(), [PARTY_B[0], hero(501, 400, 1290), hero(201, 400, 818)], K, front=401)
        self.assertNotIn(501, {k for k, v in strong['deaths'].items() if v < 10})
        self.assertGreater(strong['reach'], weak['reach'])

    def test_no_measured_damage_no_simulation(self):
        profile = {**self.profile(), 'dps': []}
        self.assertIsNone(bp.simulate(TARGET, profile, PARTY_B, K, front=401))


class TimelineTests(unittest.TestCase):
    def test_act_boss_fights_keep_a_timeline(self):
        run = RunCombat(('t', 2210), {'monster_key': 20902, 'min_max_hp': 0, 'act': True})
        for second, hp in ((0, 400.0), (1, 380.0)):
            run.feed({'utc': f'2026-10-02T10:00:0{second}+00:00', 'dead': {}, 'enemies': [],
                      'heroes': [{'hero_key': 401, 'hp': hp, 'max_hp': 400.0, 'state': 'IDLE', 'buffs': []}]})
        self.assertEqual(run.summary()['timeline'], [[0.0, None, {'401': 400.0}], [1.0, None, {'401': 380.0}]])
        normal = RunCombat(('t', 2206), {'monster_key': 20081, 'min_max_hp': 1, 'act': False})
        self.assertIsNone(normal.summary()['timeline'])


if __name__ == '__main__':
    unittest.main()
