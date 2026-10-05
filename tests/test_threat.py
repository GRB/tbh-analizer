"""Stage threat per hero, deaths per stage from the save counter, pet bonuses. Synthetic data only."""
import unittest
from datetime import datetime, timedelta, timezone

from tbh.analysis.combat import RunCombat
from tbh.analysis.threat import enemy_damage, hit, party_threat, stage_threat
from tbh.views.analytics import deaths_by_stage, front_hero, survival_reference
from tbh.views.items import pet_view

K = {'bfmi': 0.4, 'bfmj': 12.0, 'bfmk': 14.0, 'bfml': 0.85, 'bfmm': 0.75, 'bfmq': 0.95, 'bfmr': 3.0, 'bfms': 2.8, 'bfmt': 11.0}
PRIEST = {'hero_key': 401, 'max_hp': 402.0, 'final': {'Armor': 1360.912, 'DamageAbsorption': 8.3},
          'resistances': {'individual': {'Fire': -20, 'Cold': -20, 'Lightning': -20, 'Chaos': -20, 'AllElement': 0},
                          'caps': {'Fire': 75, 'Cold': 75, 'Lightning': 75}}}


class Catalog:
    stages = {'2209': {'StageLevel': '45', 'Monsters': '20111_1000 20091_1000', 'BossMonsterKey': '20111',
                       'BossDamageMultiplier': '3000'},
              '2206': {'StageLevel': '43', 'Monsters': '20031_1000', 'BossMonsterKey': '20081', 'BossDamageMultiplier': '3000'}}
    stage_levels = {'45': {'MonsterAtkDmgMultiplier': '2200'}, '43': {'MonsterAtkDmgMultiplier': '1930'}}
    monsters = {'20111': {'AttackDamage': '17'}, '20091': {'AttackDamage': '11'}, '20031': {'AttackDamage': '18'},
                '20081': {'AttackDamage': '20'}}
    pets = [{'PetKey': '6003', 'NameKey': 'P', 'StatDataKey': '6003'}]
    pet_stats = [{'PetStatKey': '6003', 'STATTYPE': 'IncreaseGoldAmount', 'MODTYPE': 'FLAT', 'Value': '150'},
                 {'PetStatKey': '6003', 'STATTYPE': 'IncreaseExpAmount', 'MODTYPE': 'FLAT', 'Value': '200'}]

    def index(self, name, key):
        return {r[key]: r for r in self.table(name)}

    def table(self, name):
        return {'PetInfoData': self.pets, 'PetStatInfoData': self.pet_stats}.get(name, [])

    def localize(self, key, locale='en-US'):
        return {'P': 'Dragon', 'AccountStat_IncreaseGoldAmount': '{0}% Increased Gold Per Kill',
                'AccountStat_IncreaseExpAmount': '{0}% Increased Exp Gain'}.get(key)


class ThreatTests(unittest.TestCase):
    def test_native_cap_uses_melee_kind_and_unknown_stays_flagged(self):
        hero = {**PRIEST, 'final': {'Armor': 100000, 'DamageAbsorption': 0}}
        melee = hit(100, {**hero, 'is_melee': True}, 45, K)
        ranged = hit(100, {**hero, 'is_melee': False}, 45, K)
        self.assertAlmostEqual(melee['hit'], 15)
        self.assertAlmostEqual(ranged['hit'], 25)
        self.assertFalse(melee['cap_uncertain'])
        self.assertTrue(hit(100, hero, 45, K)['cap_uncertain'])

    def test_hits_match_the_capture(self):
        cat = Catalog()
        self.assertAlmostEqual(hit(enemy_damage(cat, 2209, 20111), PRIEST, 45, K, 20111)['hit'], 3.777, places=2)
        fire = hit(enemy_damage(cat, 2209, 20091), PRIEST, 45, K, 20091)
        self.assertEqual(fire['element'], 'Fire')
        self.assertAlmostEqual(fire['hit'], 20.74, places=2)
        self.assertAlmostEqual(enemy_damage(cat, 2209, 20111, boss=True), 112.2)

    def test_stage_threat_takes_the_worst_normal_and_the_boss(self):
        t = stage_threat(Catalog(), 2209, PRIEST, K)
        self.assertEqual(t['normal']['monster_key'], 20091)          # the fire hit is the biggest here
        self.assertGreater(t['boss']['hit'], t['normal']['hit'])
        self.assertAlmostEqual(t['boss']['hits_to_die'], 402.0 / t['boss']['hit'])
        party = party_threat(Catalog(), 2209, [PRIEST], K)
        self.assertEqual(party['worst_boss_fraction'], t['boss']['fraction'])

    def test_unknown_inputs_give_none(self):
        self.assertIsNone(hit(None, PRIEST, 45, K))
        self.assertIsNone(hit(37.4, {'final': {}}, 45, K))
        self.assertIsNone(stage_threat(Catalog(), 9999, PRIEST, K))


class DeathTests(unittest.TestCase):
    NOW = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)

    def window(self, end_h, stage, deaths, valid=True, fails=None, wall=180):
        end = (self.NOW - timedelta(hours=end_h)).isoformat()
        return {'end_utc': end, 'stage': stage, 'valid': valid, 'wall_s': wall, 'hero_deaths': deaths, 'all_fails': fails or {}}

    def test_failure_windows_count_for_the_failed_stage_and_recent_is_separate(self):
        rows = deaths_by_stage([
            self.window(1, 2206, 3, valid=False, fails={2209: 1}),   # wipe on 2209, game moved to 2206
            self.window(10, 2208, 2),                                # old deaths
            self.window(1, 2206, 1, valid=False),                    # invalid, no failure: unattributable
            self.window(1, 2206, 4, wall=3600)], now=self.NOW)       # too long: possibly offline
        self.assertEqual(rows[2209], {'deaths': 3, 'recent': 3, 'last_utc': (self.NOW - timedelta(hours=1)).isoformat()})
        self.assertEqual((rows[2208]['deaths'], rows[2208]['recent']), (2, 0))
        self.assertNotIn(2206, rows)

    def test_only_recent_deaths_set_the_danger_reference(self):
        ev = [{'label': 'old', 'threat': {'boss_fraction': .2}, 'deaths_total': 8, 'deaths_recent': 0},
              {'label': 'wipe', 'threat': {'boss_fraction': .44}, 'deaths_total': 3, 'deaths_recent': 3},
              {'label': 'safe', 'threat': {'boss_fraction': .48}, 'deaths_total': 0, 'save_minutes': 80}]
        ref = survival_reference(ev)
        self.assertEqual(ref['deaths_from'], (.44, 'wipe'))
        self.assertEqual(ref['no_deaths_up_to'], (.48, 'safe'))


class FrontHeroTests(unittest.TestCase):
    def run_(self, utc, drops):
        return {'ended_utc': utc, 'combat': {'heroes': {str(k): {'hp_drops': v} for k, v in drops.items()}}}

    def test_hero_taking_most_hits_once_enough_are_recorded(self):
        self.assertEqual(front_hero([self.run_('a', {401: 60, 501: 1, 201: 1})]), 401)
        self.assertIsNone(front_hero([self.run_('a', {401: 10, 501: 1})]))             # too few drops yet
        self.assertIsNone(front_hero([self.run_('a', {401: 30, 501: 30})]))            # nobody clearly in front
        self.assertIsNone(front_hero([{'ended_utc': 'a', 'combat': None}]))


class RunHitsTests(unittest.TestCase):
    def test_hp_drops_count_who_gets_hit(self):
        run = RunCombat(('t', 1))
        for hp in (402, 398, 398, 390, 402):
            run.feed({'heroes': [{'hero_key': 401, 'hp': hp, 'max_hp': 402.0, 'state': 'IDLE', 'buffs': []},
                                 {'hero_key': 201, 'hp': 138, 'max_hp': 138.0, 'state': 'IDLE', 'buffs': []}], 'dead': {}})
        heroes = run.summary()['heroes']
        self.assertEqual((heroes[401]['hp_drops'], heroes[201]['hp_drops']), (2, 0))


class PetTests(unittest.TestCase):
    def test_pet_view_and_bonus_factors(self):
        view = pet_view({'pet': 6003, 'pets': [{'pet_key': 6003, 'unlocked': True}]}, Catalog())
        self.assertEqual(view['name'], 'Dragon')
        self.assertEqual([e['text'] for e in view['effects']], ['15% Increased Gold Per Kill', '20% Increased Exp Gain'])
        self.assertIn('multiplies', view['effects'][0]['note'])
        self.assertIsNone(pet_view({'pet': None}, Catalog()))


if __name__ == '__main__':
    unittest.main()
