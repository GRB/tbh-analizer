"""Stat effects of gear/rune changes, per-run survival, stat snapshots and the power-aware stage model.
Synthetic data only: no game, no save."""
import json
import tempfile
import types
import unittest
from pathlib import Path

from tbh.analysis import power, stagemodel
from tbh.analysis.combat import RunCombat
from tbh.analysis.impact import stat_change, stats_at
from tbh.analysis.stagestats import stage_evidence
from tbh.collector import Collector
from tbh.store import Store
from tbh.analysis.progress import xp_shares
from tbh.views.combat import _incoming, _skills
from tbh.views.power import item_raw
from tbh.views.suggestions import gear_suggestions, rune_estimate, stage_suggestions, survival


def mod(stat, mode, value, source, layer='base'):
    return {'stat': stat, 'mode': mode, 'value': value, 'source': source, 'layer': layer}


# Priest AttackDamage as read live: (1 + 49 + 7) * 1.35 = 76.95, then x1.9 from Blessing Of Might.
PRIEST = [mod('AttackDamage', 'FLAT', 1, 'BASE'), mod('AttackDamage', 'FLAT', 39, 'ITEM'),
          mod('AttackDamage', 'FLAT', 10, 'ITEM'), mod('AttackDamage', 'FLAT', 7, 'AccountStatus'),
          mod('AttackDamage', 'ADDITIVE', .35, 'AccountStatus'), mod('AttackSpeed', 'FLAT', 1.2, 'BASE'),
          mod('AttackSpeed', 'FLAT', .3, 'ITEM'), mod('AttackDamage', 'MULTIPLICATIVE', .9, 'BuffSkill', 'dynamic')]


class ScaleTests(unittest.TestCase):
    def test_learns_one_power_of_ten_per_stat_and_mode(self):
        scales = power.learn_scales([
            ({('AttackDamage', 'FLAT'): 49.0, ('AttackSpeed', 'FLAT'): .3, ('Armor', 'ADDITIVE'): .656},
             {('AttackDamage', 'FLAT'): 49, ('AttackSpeed', 'FLAT'): 30, ('Armor', 'ADDITIVE'): 656}),
            ({('AttackDamage', 'FLAT'): 61.0, ('AttackSpeed', 'MULTIPLICATIVE'): 1.09},
             {('AttackDamage', 'FLAT'): 61, ('AttackSpeed', 'MULTIPLICATIVE'): 90})])
        self.assertEqual(scales, {('AttackDamage', 'FLAT'): 1.0, ('AttackSpeed', 'FLAT'): .01,
                                  ('Armor', 'ADDITIVE'): .001, ('AttackSpeed', 'MULTIPLICATIVE'): .001})

    def test_inconsistent_or_one_sided_keys_are_not_learned(self):
        scales = power.learn_scales([
            ({('MaxHp', 'FLAT'): 207.0, ('Armor', 'FLAT'): 10.0}, {('MaxHp', 'FLAT'): 207, ('Armor', 'FLAT'): 10}),
            ({('MaxHp', 'FLAT'): 20.7, ('Armor', 'FLAT'): 15.0}, {('MaxHp', 'FLAT'): 207}),   # stale save / other source
            ({('CriticalDamage', 'FLAT'): .677}, {('CriticalDamage', 'FLAT'): 600})])        # not a power of ten
        self.assertEqual(scales, {})

    def test_item_raw_maps_enchants_and_refuses_unique_mods(self):
        enums = {'StatType': {26: 'ColdDamagePercent'}, 'MODTYPE': {0: 'FLAT'}}
        item = {'stats': [{'stat': 'AttackDamage', 'mod': 'FLAT', 'value': 5}, {'stat': 'AttackDamage', 'mod': 'FLAT', 'value': 3}],
                'enchants': [{'stat': 26, 'mod_type': 0, 'value': 250}]}
        self.assertEqual(item_raw(item, enums), {('AttackDamage', 'FLAT'): 8, ('ColdDamagePercent', 'FLAT'): 250})
        self.assertIsNone(item_raw({**item, 'unique_mod': 'X'}, enums))
        self.assertIsNone(item_raw({'enchants': [{'stat': 99, 'mod_type': 0, 'value': 1}]}, enums))


class ChangeTests(unittest.TestCase):
    def test_swap_uses_the_game_formula_with_buffs(self):
        effect = power.change(PRIEST, 'ITEM', [mod('AttackDamage', 'FLAT', 10, 'ITEM')], [mod('AttackDamage', 'FLAT', 20, 'ITEM')])
        ad = effect['stats']['AttackDamage']
        self.assertAlmostEqual(ad['before'], 146.205)
        self.assertAlmostEqual(ad['after'], (67 * 1.35) * 1.9)   # +10 flat is worth +25.65 after increases and buff
        self.assertAlmostEqual(effect['offence_pct'], ad['pct'])  # attack speed and crit unchanged

    def test_same_item_out_and_in_changes_nothing(self):
        same = [mod('AttackDamage', 'FLAT', 39, 'ITEM'), mod('AttackSpeed', 'FLAT', .3, 'ITEM')]
        self.assertEqual(power.change(PRIEST, 'ITEM', same, same)['stats'], {})

    def test_increase_stacks_with_existing_increases(self):
        effect = power.change(PRIEST, 'AccountStatus', (), [mod('AttackDamage', 'ADDITIVE', .05, 'AccountStatus')])
        self.assertAlmostEqual(effect['stats']['AttackDamage']['pct'], (1.40 / 1.35 - 1) * 100)

    def test_multiplier_replacement(self):
        mods = [mod('AttackSpeed', 'FLAT', 1, 'BASE'), mod('AttackSpeed', 'MULTIPLICATIVE', .09, 'ITEM'),
                mod('AttackSpeed', 'MULTIPLICATIVE', .2, 'ITEM')]
        effect = power.change(mods, 'ITEM', [mod('AttackSpeed', 'MULTIPLICATIVE', .2, 'ITEM')], [])
        self.assertAlmostEqual(effect['stats']['AttackSpeed']['after'], 1.09)

    def test_summary_lists_shown_stats_by_size(self):
        effect = {'stats': {'Armor': {'pct': -12.3}, 'AttackSpeed': {'pct': 6.6}, 'HpRegenPerSec': {'pct': 50.0}}}
        self.assertEqual(power.summary(effect, str), 'Armor -12.3%, AttackSpeed +6.6%')


class FakeEffects:
    def __init__(self, gear_effect=None, rune_effect=None):
        self.gear_effect, self.rune_effect = gear_effect, rune_effect

    def gear(self, hero_key, current, candidate):
        return self.gear_effect

    def rune(self, stat, raw):
        return self.rune_effect

    def text(self, effect):
        return power.summary(effect, str) + f"; output {effect['offence_pct']:+.1f}%"

    def name(self, stat):
        return stat

    def hero_name(self, key):
        return f'H{key}'


def gear(uid, name, level, stats, part='GLOVES'):
    return {'unique_id': uid, 'name': name, 'level': level, 'grade': 'RARE', 'type': 'GEAR', 'parts': part,
            'slot_part': part, 'gear_type': part, 'stats': [{'kind': 'base', 'stat': k, 'value': v} for k, v in stats.items()]}


class GearEffectTests(unittest.TestCase):
    EQUIP = [{'hero_key': 401, 'name': 'Priest', 'in_party': True,
              'equipment': [gear('w', 'Chain Gloves', 10, {'Armor': 128, 'MaxHp': 40})]}]
    HEROES = {401: {'level': 34, 'xp': 0}}

    def rows(self, candidate, effect):
        return gear_suggestions(self.EQUIP, {'stash': {'label': 'Stash', 'items': [candidate]}}, self.HEROES,
                                effects=FakeEffects(gear_effect=effect))

    def test_tradeoff_losing_more_stats_is_kept_when_it_adds_output(self):
        effect = {'stats': {'AttackSpeed': {'pct': 6.6}, 'Armor': {'pct': -12.3}}, 'offence_pct': 6.6, 'unknown': []}
        rows = self.rows(gear('c', 'Iron Gloves', 10, {'AttackSpeed': 73}), effect)
        self.assertEqual(len(rows), 1)
        self.assertIn('Trade-off', rows[0]['title'])          # it loses armor: never "likely better"
        self.assertIn('output +6.6%', rows[0]['title'])
        self.assertEqual(rows[0]['metrics']['output_pct'], 6.6)

    def test_output_gain_without_hp_or_armor_loss_is_likely_better(self):
        effect = {'stats': {'AttackSpeed': {'pct': 8.0}, 'MovementSpeed': {'pct': -3.0}}, 'offence_pct': 8.0, 'unknown': []}
        rows = self.rows(gear('c', 'Swift Gloves', 10, {'AttackSpeed': 90, 'Armor': 128, 'MaxHp': 40}), effect)
        # Better on every catalog stat here, so it is a plain upgrade carrying the effect text.
        self.assertIn('Effect on Priest now', rows[0]['detail'])
        effect['stats']['Armor'] = {'pct': 0.0}
        rows = self.rows(gear('c', 'Swift Gloves', 10, {'AttackSpeed': 90, 'Armor': 100, 'MaxHp': 40}), effect)
        self.assertTrue(rows[0]['title'].startswith('Likely better'))
        self.assertEqual(rows[0]['priority'], 3)

    def test_no_effects_keeps_the_catalog_comparison(self):
        rows = gear_suggestions(self.EQUIP, {'stash': {'label': 'Stash', 'items': [gear('c', 'Iron Gloves', 10, {'AttackSpeed': 73})]}},
                                self.HEROES)
        self.assertEqual(rows, [])   # loses 2 stats, gains 1, nothing measured: noise


class RuneEffectTests(unittest.TestCase):
    NODE = {'stat': 'AllHeroAttackDamage', 'next_value': 1, 'next_effect': {'value': 1, 'verified': True}}

    def test_hero_stat_rune_uses_live_effect_and_bounds_gold(self):
        live = {401: {'stats': {'AttackDamage': {'pct': 1.8}}}, 501: {'stats': {'AttackDamage': {'pct': 1.3}}}}
        est = rune_estimate(self.NODE, {}, {'gold_h': 1_000_000}, FakeEffects(rune_effect=live))
        self.assertEqual(est['basis'], 'observed')
        self.assertAlmostEqual(est['gold_h_max'], 18_000)
        self.assertIn('H401 +1.8%', est['how'])

    def test_without_live_effect_flat_damage_rune_is_not_estimated(self):
        self.assertIsNone(rune_estimate(self.NODE, {}, {'gold_h': 1_000_000}, FakeEffects()))


class SurvivalTests(unittest.TestCase):
    def reading(self, hp, ad=100.0):
        hero = lambda key: {'hero_key': key, 'hp': hp, 'max_hp': 200.0, 'state': 'IDLE', 'buffs': [],
                            'stats': {'final': {'AttackDamage': ad, 'AttackSpeed': 2.0, 'CriticalChance': .5, 'CriticalDamage': 2.0}}}
        return {'heroes': [hero(401), hero(501)]}

    def test_summary_has_party_output_lowest_hp_and_deaths(self):
        run = RunCombat(('t', 1))
        wipe = {'heroes': [], 'dead': {401: 89.9, 501: 85.0}}
        for r in (self.reading(200), self.reading(50, 110), wipe, self.reading(200, 120)):
            run.feed(r)
        s = run.summary()
        self.assertEqual((s['deaths'], s['min_hp_fraction']), (2, 0))      # one death per hero
        self.assertEqual(s['party_offence'], 2 * 110 * 2 * 1.5)              # median of the 3 full readings
        empty = RunCombat(None).summary()
        self.assertEqual((empty['samples'], empty['party_offence'], empty['deaths']), (0, None, 0))

    def test_stage_evidence_and_verdicts(self):
        def run_(combat):
            return {'stage_key': 1, 'party': '401,501', 'duration_s': 60, 'gold_gain_est': 100, 'ended_utc': 'x',
                    'outcome': 'clear', 'xp': {}, 'combat': combat}
        safe = [run_({'samples': 50, 'min_hp_fraction': .8 + i / 100, 'deaths': 0, 'party_offence': 600}) for i in range(3)]
        e = stage_evidence(safe + [run_(None)], [], str)[0]
        self.assertEqual((e['combat_runs'], e['runs_with_deaths'], e['lowest_hp'], e['median_offence']), (3, 0, .8, 600))
        self.assertEqual(survival(e), 'safe')
        risky = stage_evidence(safe + [run_({'samples': 50, 'min_hp_fraction': 0, 'deaths': 1})], [], str)[0]
        self.assertEqual(survival(risky), 'risky')
        self.assertIsNone(survival({'combat_runs': 0}))

    def test_harder_untested_stage_is_not_likely_better_when_party_struggles(self):
        runs = [{'stage_key': 1, 'party': '401,501', 'duration_s': 60, 'gold_gain_est': 100, 'ended_utc': 'x',
                 'outcome': 'clear', 'xp': {}, 'combat': {'samples': 50, 'min_hp_fraction': hp, 'deaths': d}}
                for hp, d in ((0.0, 1), (0.1, 0), (0.0, 1), (0.3, 0))]
        current = stage_evidence(runs, [], lambda k: f'S{k}')[0]
        cand = {'stage': 2, 'label': 'S2', 'reachable': True,
                'estimate': {'gold_h': 500.0, 'gold_low': 400.0, 'gold_high': 600.0, 'duration_s': 60, 'xp_h': 1.0,
                             'dmg_vs_observed': 1.5}}
        rows = [r for r in stage_suggestions([current], 1, [cand]) if r['id'] == 'stage-untested-2']
        self.assertTrue(rows[0]['title'].startswith('Maybe better'))
        self.assertIn('already struggles on S1', rows[0]['detail'])
        self.assertEqual(rows[0]['priority'], 4)


class StatSnapshotTests(unittest.TestCase):
    EVENTS = [('2026-10-01T10:00:00+00:00', {'401': {'AttackDamage': 57.0, 'Armor': 100.0}}),
              ('2026-10-01T11:00:00+00:00', {'401': {'AttackDamage': 58.0, 'Armor': 100.0}})]

    def test_state_at_a_time_is_the_latest_snapshot(self):
        self.assertIsNone(stats_at(self.EVENTS, '2026-10-01T09:00:00+00:00'))
        self.assertEqual(stats_at(self.EVENTS, '2026-10-01T10:30:00+00:00')['401']['AttackDamage'], 57.0)

    def test_change_between_two_saves(self):
        change = stat_change(self.EVENTS, '2026-10-01T10:30:00+00:00', '2026-10-01T11:05:00+00:00', ('AttackDamage', 'Armor'))
        self.assertAlmostEqual(change['401']['AttackDamage']['pct'], (58 / 57 - 1) * 100)
        self.assertNotIn('Armor', change['401'])
        self.assertIsNone(stat_change(self.EVENTS, '2026-10-01T09:00:00+00:00', '2026-10-01T11:05:00+00:00', ('AttackDamage',)))
        self.assertIsNone(stat_change(self.EVENTS, '2026-10-01T10:10:00+00:00', '2026-10-01T10:20:00+00:00', ('AttackDamage',)))


class CollectorPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'tbh.sqlite3')
        self.collector = Collector.__new__(Collector)
        self.collector.store, self.collector.session_id, self.collector._stats_key = self.store, 1, None

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_stat_snapshot_stored_only_when_base_stats_change(self):
        reading = lambda ad, buffed: {'utc': '2026-10-01T10:00:00+00:00', 'heroes': [
            {'hero_key': 401, 'stats': {'base': {'AttackDamage': ad, 'Armor': 0.0}, 'final': {'AttackDamage': buffed}}}]}
        for r in (reading(57.0, 108.3), reading(57.0, 50.0), reading(58.0, 110.2)):
            self.collector._store_stats(r)
        rows = self.store.query("SELECT payload FROM events WHERE kind = 'hero_stats' ORDER BY id")
        self.assertEqual([json.loads(r['payload']) for r in rows],
                         [{'401': {'AttackDamage': 57.0}}, {'401': {'AttackDamage': 58.0}}])
        self.collector._store_stats({'utc': 'x', 'heroes': [{'hero_key': 401, 'stats': None}]})   # unreadable: skipped
        self.assertEqual(len(self.store.query("SELECT id FROM events WHERE kind = 'hero_stats'")), 2)

    def test_closed_run_gets_the_combat_summary_of_that_run_only(self):
        stored = []
        import tbh.collector as module
        original = module.persist_run
        module.persist_run = lambda store, run: stored.append(run)
        try:
            self.collector.run_combat = RunCombat(('2026-10-01T10:00:00+00:00', 2206))
            self.collector._run_closed({'started_utc': '2026-10-01T10:00:00+00:00', 'stage_key': 2206})
            self.collector._run_closed({'started_utc': '2026-10-01T09:00:00+00:00', 'stage_key': 2206})
        finally:
            module.persist_run = original
        self.assertEqual(stored[0]['combat']['samples'], 0)
        self.assertNotIn('combat', stored[1])


class PowerModelTests(unittest.TestCase):
    A, B = 10.0, 50.0

    def stage(self, key, waves, monsters, hp):
        return {'stage': key, 'waves': waves, 'monsters_per_wave': monsters, 'monster_hp_mult': hp,
                'monster_dmg_mult': 1.0, 'gold_per_clear': 1000.0, 'exp_per_clear': 1000.0}

    def played(self, t, offence):
        seconds = self.A * t['waves'] + self.B * t['waves'] * t['monsters_per_wave'] * t['monster_hp_mult'] / offence
        return {'stage': t['stage'], 'runs': 6, 'xp_runs': 6, 'median_duration_s': seconds, 'minutes': 6 * seconds / 60,
                'gold_h': 200 * 3600 / seconds, 'xp_h': 500 * 3600 / seconds, 'median_offence': offence}

    def test_power_aware_model_wins_when_the_party_grew_between_stages(self):
        theory = [self.stage(1, 10, 5, 10), self.stage(2, 14, 8, 80), self.stage(3, 18, 10, 250), self.stage(4, 16, 9, 150)]
        evidence = [self.played(t, o) for t, o in zip(theory, (100.0, 300.0, 900.0, 500.0))]
        model = stagemodel.fit(evidence, theory, offence=1000.0)
        self.assertIsNotNone(model['power'])
        self.assertAlmostEqual(model['error']['duration_s'], 0, places=6)
        new = self.stage(9, 20, 12, 400)
        expected = self.A * 20 + self.B * 20 * 12 * 400 / 1000.0
        self.assertAlmostEqual(stagemodel.estimate(model, new)['duration_s'], expected, places=4)
        self.assertIn('party output', stagemodel.describe(model))

    def test_plain_model_stays_without_output_or_when_not_better(self):
        theory = [self.stage(1, 10, 5, 10), self.stage(2, 14, 8, 80), self.stage(3, 18, 10, 250)]
        evidence = [self.played(t, 500.0) for t in theory]
        self.assertIsNone(stagemodel.fit(evidence, theory)['power'])            # no current output
        self.assertIsNone(stagemodel.fit(evidence, theory, 500.0)['power'])     # constant output: not better


class AccountSplitTests(unittest.TestCase):
    def test_rune_share_is_relabelled_and_the_rest_stays_account(self):
        mods = power.split_account(PRIEST, {('AttackDamage', 'FLAT'): 5.0, ('AttackDamage', 'ADDITIVE'): .35})
        by = {(m['source'], m['mode']): m['value'] for m in mods if m['stat'] == 'AttackDamage' and m['layer'] == 'base'
              and m['source'] in ('Runes', 'AccountStatus')}
        self.assertEqual(by, {('Runes', 'FLAT'): 5.0, ('AccountStatus', 'FLAT'): 2.0, ('Runes', 'ADDITIVE'): .35})
        self.assertAlmostEqual(power.final_stats(mods)['AttackDamage'], power.final_stats(PRIEST)['AttackDamage'])

    def test_xp_stat_change_is_shown_in_points(self):
        effect = {'stats': {'IncreaseExpAmount': {'before': 1.0, 'after': 1.036, 'pct': 3.6}, 'Armor': {'pct': -1.0}}}
        self.assertEqual(power.summary(effect, str), 'IncreaseExpAmount +0.036, Armor -1.0%')


class XpShareTests(unittest.TestCase):
    def run_(self, utc, gains, party='201,401,501', level_up=False):
        return {'ended_utc': utc, 'party': party,
                'xp': {k: {'gain': g, 'complete': True, 'level_ups': 1 if level_up and k == '401' else 0} for k, g in gains.items()}}

    def test_median_share_of_latest_party_skipping_level_ups(self):
        runs = [self.run_('2026-10-01T10:00', {'401': 1014.0, '501': 1000.0, '201': 1000.0}),
                self.run_('2026-10-01T10:05', {'401': 1014.0, '501': 1000.0, '201': 1000.0}),
                self.run_('2026-10-01T10:10', {'401': 5000.0, '501': 1000.0, '201': 1000.0}, level_up=True),
                self.run_('2026-10-01T09:00', {'101': 10.0, '501': 1.0}, party='101,501')]
        shares = xp_shares(runs)
        self.assertEqual((shares['party'], shares['runs']), ('201,401,501', 2))
        self.assertAlmostEqual(shares['heroes']['401'] / shares['heroes']['501'], 1.014)
        self.assertIsNone(xp_shares([self.run_('x', {'401': 1.0}, level_up=True)]))


class SkillAndSettingTests(unittest.TestCase):
    class Catalog:
        def index(self, name, key):
            return {'50101': {'SkillNameKey': 'S', 'ACTIVATIONTYPE': 'BASEATTACK_COUNT', 'ActivationValue': '5'},
                    '50301': {'SkillNameKey': 'Q', 'ACTIVATIONTYPE': 'COOLDOWN', 'ActivationValue': '14'}}

        def localize(self, key, locale='en-US'):
            return {'S': 'Explosive Bolt', 'Q': 'Quick Loader'}.get(key)

    def test_skills_are_named_with_their_catalog_trigger(self):
        rows = _skills(self.Catalog(), [{'skill_key': 50101, 'class': 'HunterExplosiveBolt', 'cast_distance': 11.0},
                                        {'skill_key': 50301, 'class': 'HunterQuickLoader', 'cast_distance': 10.5}], 'en-US')
        self.assertEqual([(r['name'], r['trigger']) for r in rows],
                         [('Explosive Bolt', 'every 5 attacks (game data)'), ('Quick Loader', 'cooldown 14 s (game data)')])
        self.assertIsNone(_skills(self.Catalog(), None, 'en-US'))


class MeasuredFormulaTests(unittest.TestCase):
    def test_hero_xp_stat_adds_to_the_final_factor(self):
        # Per-kill capture: 2.504 for heroes at 1.0 and 2.539 for the Priest at 1.036 (runes +110%, pet 200).
        hunter, priest = power.xp_factor(110, 200, 1.0), power.xp_factor(110, 200, 1.036)
        self.assertAlmostEqual(hunter, 2.52)
        self.assertAlmostEqual(priest / hunter, 1.0143, places=3)   # measured 1.0140

    class Catalog:
        stages = {'2209': {'BossMonsterKey': '20111', 'BossDamageMultiplier': '3000', 'StageLevel': '45'}}
        stage_levels = {'45': {'MonsterHpMultiplier': '70770', 'MonsterAtkDmgMultiplier': '2200'}}
        monsters = {'20111': {'AttackDamage': '17', 'MaxLife': '260', 'MonsterNameStringKey': 'M'},
                    '20091': {'AttackDamage': '11', 'MaxLife': '75', 'MonsterNameStringKey': 'F'}}

        def localize(self, key, locale='en-US'):
            return {'M': 'Mummy', 'F': 'Fire Elemental'}.get(key)

    K = {'bfmi': 0.4, 'bfmj': 12.0, 'bfmk': 14.0, 'bfml': 0.85, 'bfmm': 0.75, 'bfmq': 0.95, 'bfmr': 3.0, 'bfms': 2.8, 'bfmt': 11.0}

    def test_incoming_hits_physical_elemental_and_boss(self):
        reading = {'armor_constants': self.K, 'stage_level': 45, 'stage_key': 2209, 'enemies': [
            {'monster_key': 20111, 'max_hp': 18400.2}, {'monster_key': 20111, 'max_hp': 110401.2},
            {'monster_key': 20091, 'max_hp': 5307.7}]}
        hero = {'max_hp': 402.0, 'stats': {'final': {'Armor': 1360.912, 'DamageAbsorption': 8.3}},
                'resistances': {'individual': {'Fire': -20, 'Cold': -20, 'Lightning': -20, 'Chaos': -20, 'AllElement': 0},
                                'caps': {'Fire': 75, 'Cold': 75, 'Lightning': 75}}}
        rows = {(r['monster_key'], r['boss']): r for r in _incoming(self.Catalog(), reading, hero, 'en-US')}
        self.assertAlmostEqual(rows[(20111, False)]['hit'], 3.777, places=2)       # observed 3.78
        self.assertAlmostEqual(rows[(20091, False)]['hit'], 20.74, places=2)       # observed 20.74, fire
        self.assertEqual(rows[(20091, False)]['element'], 'Fire')
        self.assertAlmostEqual(rows[(20111, True)]['damage'], 112.2)               # boss x3 damage
        self.assertIsNone(_incoming(self.Catalog(), {**reading, 'armor_constants': None}, hero, 'en-US'))


if __name__ == '__main__':
    unittest.main()
