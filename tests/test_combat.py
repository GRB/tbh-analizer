import struct
import types
import unittest

from tbh.analysis.combat import RunCombat, armor_reduction, attack_shares, layer_value, origins, physical_hit, resistance
from tbh.runtime.combat import CombatReader, CombatUnavailable
from tbh.runtime.layout import enum_values, field_map, subclasses
from tbh.views.combat import _buff, _enemies


def mod(stat, mode, value, source, layer='base'):
    return {'stat': stat, 'mode': mode, 'value': value, 'source': source, 'layer': layer}


class ResistanceTests(unittest.TestCase):
    def test_elements_add_all_element_and_chaos_does_not(self):
        r = resistance({'Fire': -20, 'Cold': -20, 'Lightning': -20, 'Chaos': -20, 'AllElement': 9},
                       {'Fire': 75, 'Cold': 75, 'Lightning': 75, 'Chaos': 75})
        self.assertEqual(r['Fire']['resistance'], -11)
        self.assertAlmostEqual(r['Fire']['damage_factor'], 1.11)
        self.assertEqual(r['Chaos']['resistance'], -20)
        self.assertAlmostEqual(r['Chaos']['damage_factor'], 1.20)

    def test_cap_applies_after_the_sum_and_not_to_chaos(self):
        r = resistance({'Fire': 70, 'Cold': 0, 'Lightning': 0, 'Chaos': 90, 'AllElement': 10},
                       {'Fire': 75, 'Cold': 75, 'Lightning': 75, 'Chaos': 75})
        self.assertEqual(r['Fire']['resistance'], 75)
        self.assertEqual(r['Chaos']['resistance'], 90)
        self.assertAlmostEqual(r['Chaos']['damage_factor'], 0.1)

    def test_missing_inputs_are_unknown_not_zero(self):
        self.assertIsNone(resistance(None, None))
        r = resistance({'Fire': -20, 'Chaos': -20}, {'Fire': 75})
        self.assertIsNone(r['Fire'])          # AllElement unknown
        self.assertIsNone(r['Cold'])
        self.assertEqual(r['Chaos']['resistance'], -20)

    def test_damage_factor_never_negative(self):
        r = resistance({'Fire': 0, 'Cold': 0, 'Lightning': 0, 'Chaos': 150, 'AllElement': 0}, {'Fire': 75, 'Cold': 75, 'Lightning': 75})
        self.assertEqual(r['Chaos']['damage_factor'], 0.0)


K = {'bfmi': 0.4, 'bfmj': 12.0, 'bfmk': 14.0, 'bfml': 0.85, 'bfmm': 0.75, 'bfmq': 0.95, 'bfmr': 3.0,
     'bfms': 2.8, 'bfmt': 11.0}


class HitTests(unittest.TestCase):
    # Observed 2026-10-01, stage level 45: Priest armor 1360.912, Damage Absorption 8.3.
    def test_physical_hits_match_observed_hp_drops(self):
        self.assertAlmostEqual(physical_hit(37.4, 1360.912, 8.3, 45, K), 3.777, places=2)   # seen 127 times as 3.78
        self.assertAlmostEqual(physical_hit(44.0, 1360.912, 8.3, 45, K), 5.927, places=2)   # seen 19 times as 5.93

    def test_absorption_never_heals_and_caps_apply(self):
        self.assertEqual(physical_hit(10.0, 1360.912, 8.3, 45, K), 0.0)
        self.assertAlmostEqual(armor_reduction(100000, 10, 1, K), 0.95, places=2)   # approaches 95%, never above
        self.assertEqual(armor_reduction(100000, 10, 1, K, cap=0.75), 0.75)
        self.assertLess(armor_reduction(1360.912, 37.4, 45, K), 0.75)


class OriginTests(unittest.TestCase):
    # Priest AttackDamage observed live: 76.950 before buffs, 146.205 with Blessing Of Might.
    PRIEST = [mod('AttackDamage', 'FLAT', 1, 'BASE'), mod('AttackDamage', 'FLAT', 49, 'ITEM'),
              mod('AttackDamage', 'FLAT', 7, 'AccountStatus'), mod('AttackDamage', 'ADDITIVE', .35, 'AccountStatus'),
              mod('AttackDamage', 'MULTIPLICATIVE', .9, 'BuffSkill', 'dynamic')]

    def test_layers_reproduce_observed_values(self):
        base = layer_value(0, [m for m in self.PRIEST if m['layer'] == 'base'])
        self.assertAlmostEqual(base, 76.95)
        self.assertAlmostEqual(layer_value(base, [m for m in self.PRIEST if m['layer'] == 'dynamic']), 146.205)

    def test_origins_group_by_source_and_check_against_read_values(self):
        o = origins(self.PRIEST, {'base': {'AttackDamage': 76.95}, 'final': {'AttackDamage': 146.205}})['AttackDamage']
        self.assertTrue(o['matches'])
        self.assertEqual([s['source'] for s in o['sources']], ['BASE', 'ITEM', 'AccountStatus', 'BuffSkill'])
        account = o['sources'][2]
        self.assertEqual((account['FLAT'], account['ADDITIVE'], account['count']), (7, .35, 2))
        self.assertAlmostEqual(o['sources'][3]['MULTIPLICATIVE'], 1.9)

    def test_breakdown_that_does_not_reproduce_is_flagged(self):
        o = origins(self.PRIEST, {'base': {'AttackDamage': 80.0}, 'final': {'AttackDamage': 146.205}})
        self.assertFalse(o['AttackDamage']['matches'])
        self.assertIsNone(origins(None, None))


class RunCombatTests(unittest.TestCase):
    def reading(self, hp, buffs=(), dead=None):
        """A dead hero leaves the party list and appears in the dead-unit list (D009)."""
        if dead:
            return {'heroes': [], 'dead': {401: dead}}
        return {'heroes': [{'hero_key': 401, 'hp': hp, 'max_hp': 200.0, 'state': 'IDLE',
                            'buffs': None if buffs is None else [{'group': g} for g in buffs]}], 'dead': {}}

    def test_deaths_come_from_the_dead_list_and_count_once_each(self):
        run = RunCombat(('t', 2206))
        for r in (self.reading(200, buffs=[40201]), self.reading(50, buffs=[40201, 50301]),
                  self.reading(None, dead=89.9), self.reading(None, dead=89.8), self.reading(200, buffs=None),
                  self.reading(200, buffs=[40201]), self.reading(None, dead=90.0)):
            run.feed(r)
        h = run.summary()['heroes'][401]
        self.assertEqual(h['deaths'], 2)
        self.assertEqual(h['min_hp_fraction'], 0)
        self.assertEqual(h['samples'], 4)                           # readings with the hero in the party
        self.assertAlmostEqual(h['buff_presence'][40201], 3 / 3)   # the unreadable sample is excluded
        self.assertAlmostEqual(h['buff_presence'][50301], 1 / 3)

    def test_zero_hp_alone_is_not_a_death(self):
        run = RunCombat(None)
        run.feed(self.reading(0))
        self.assertEqual(run.summary()['deaths'], 0)

    def test_unreadable_party_is_not_a_sample(self):
        run = RunCombat(None)
        run.feed({'heroes': None})
        run.feed(None)
        s = run.summary()
        self.assertEqual((s['samples'], s['party_offence'], s['min_hp_fraction'], s['deaths'], s['heroes'], s['boss']),
                         (0, None, None, 0, {}, None))


class AttackShareTests(unittest.TestCase):
    @staticmethod
    def reading(counts, ad=None):
        """`counts`: {hero_key: Unit.beib}; `ad`: {hero_key: AttackDamage}, None when stats are unreadable."""
        ad = ad or {}
        return {'heroes': [{'hero_key': k, 'hp': 100.0, 'max_hp': 100.0, 'state': 'IDLE', 'buffs': [], 'attack_count': c,
                            'stats': {'final': {'AttackDamage': ad[k], 'CriticalChance': 0.5, 'CriticalDamage': 3.0}}
                            if k in ad else None} for k, c in counts.items()], 'dead': {}}

    def test_attacks_valued_at_each_reading_and_counter_reset(self):
        run = RunCombat(None)
        ad = {401: 10.0, 501: 30.0}
        # 401: 5 attacks before the first reading are not counted; then +3, a reset to 2 (new run on the
        # same RunCombat), +4. 501: +1 and +1.
        for counts in ({401: 5, 501: 0}, {401: 8, 501: 1}, {401: 2, 501: 2}, {401: 6, 501: 2}):
            run.feed(self.reading(counts, ad))
        heroes = run.summary()['heroes']
        self.assertEqual((heroes[401]['attacks'], heroes[501]['attacks']), (9, 2))
        hit = 1 + 0.5 * (3.0 - 1)   # average crit multiplier
        self.assertAlmostEqual(heroes[401]['attack_damage_est'], 9 * 10 * hit)
        rows = attack_shares(heroes)
        self.assertEqual([r['hero_key'] for r in rows], [401, 501])
        self.assertAlmostEqual(rows[0]['share'], 90 / 150)

    def test_unvalued_attacks_give_no_shares(self):
        run = RunCombat(None)
        run.feed(self.reading({401: 0, 501: 0}, {401: 10.0, 501: 10.0}))
        run.feed(self.reading({401: 3, 501: 3}, {401: 10.0}))      # 501's stats unreadable here
        rows = attack_shares(run.summary()['heroes'])
        self.assertEqual([r['share'] for r in rows], [None, None])
        self.assertEqual(sorted(r['attacks'] for r in rows), [3, 3])

    def test_runs_without_attack_counts_have_no_shares(self):
        self.assertEqual(attack_shares({'401': {'samples': 3}}), [])
        self.assertEqual(attack_shares(None), [])


class FakeMemory:
    def __init__(self):
        self.bytes = {}

    def write(self, address, data):
        for i, b in enumerate(data):
            self.bytes[address + i] = b

    def read(self, address, size):
        out = bytearray()
        for i in range(size):
            if address + i not in self.bytes:
                break
            out.append(self.bytes[address + i])
        return bytes(out)

    def scalar(self, address, fmt='Q'):
        size = struct.calcsize('<' + fmt)
        raw = self.read(address, size)
        return struct.unpack('<' + fmt, raw)[0] if len(raw) == size else None


LAYOUT = {'combat': {'fields': {}, 'enums': {'StatType': {'1': 'AttackDamage'}}, 'buff_classes': ['bam']}}


class CheckedCollectionTests(unittest.TestCase):
    DICT, ENTRIES = 0x1000, 0x2000

    def reader(self, entries, free=0):
        mem = FakeMemory()
        names = {0x10: 'Dictionary`2', 0x20: 'array'}
        runtime = types.SimpleNamespace(mem=mem, layout=LAYOUT, _class_name=names.get)
        mem.write(self.DICT, struct.pack('<Q', 0x10))
        mem.write(self.ENTRIES, struct.pack('<Q', 0x20))
        mem.write(self.DICT + 0x18, struct.pack('<Qiiii', self.ENTRIES, len(entries), -1, free, 7))
        mem.write(self.ENTRIES + 0x18, struct.pack('<Q', 8))
        for i, (hash_code, key, value) in enumerate(entries):
            mem.write(self.ENTRIES + 0x20 + i * 16, struct.pack('<iiif', hash_code, -1, key, value))
        return CombatReader(runtime)

    def test_reads_float_dictionary_and_skips_removed_entries(self):
        reader = self.reader([(1, 1, 76.95), (-1, 2, 9.0), (3, 3, 0.5)], free=1)
        self.assertEqual({k: round(v, 3) for k, v in reader.dictionary(self.DICT).items()}, {1: 76.95, 3: 0.5})

    def test_inconsistent_count_is_unreadable_not_empty(self):
        reader = self.reader([(1, 1, 1.0), (-1, 2, 2.0)], free=0)   # one removed entry not accounted for
        self.assertIsNone(reader.dictionary(self.DICT))
        self.assertIsNone(reader.dictionary(0x9999))                 # not a dictionary at all
        self.assertEqual(self.reader([]).dictionary(self.DICT), {})

    def test_layout_without_combat_section_is_refused(self):
        with self.assertRaises(CombatUnavailable):
            CombatReader(types.SimpleNamespace(mem=None, layout={'fields': {}}))


class LayoutParsingTests(unittest.TestCase):
    DUMP = ('public class wu // TypeDefIndex: 1031\n{\n\t// Fields\n\t[CompilerGenerated]\n'
            '\tprivate StatType <bgvw>k__BackingField; // 0x10\n\tprivate Dictionary<int, float> bhmv; // 0x20\n\n'
            '\t// Properties\n}\n'
            'public enum MODTYPE // TypeDefIndex: 1\n{\n\tpublic int value__; // 0x0\n'
            '\tpublic const MODTYPE FLAT = 0;\n\tpublic const MODTYPE ADDITIVE = 1;\n}\n'
            'public class bam : bah // TypeDefIndex: 2\n{\n}\npublic class bap : bah // TypeDefIndex: 3\n{\n}\n')

    def test_backing_fields_enums_and_buff_classes(self):
        self.assertEqual(field_map(self.DUMP, 'wu'), {'<bgvw>k__BackingField': 0x10, 'bhmv': 0x20})
        self.assertEqual(enum_values(self.DUMP, 'MODTYPE'), {0: 'FLAT', 1: 'ADDITIVE'})
        self.assertEqual(subclasses(self.DUMP, 'bah'), ['bam', 'bap'])


class FakeCatalog:
    stages = {}
    stage_levels = {'43': {'MonsterHpMultiplier': '63470'}}
    monsters = {'20031': {'MonsterNameStringKey': 'MonsterName_20031', 'MONSTERTYPE': 'MONSTER', 'MaxLife': '55'},
                '20081': {'MonsterNameStringKey': 'MonsterName_20081', 'MONSTERTYPE': 'BOSS', 'MaxLife': '900'}}

    def table(self, name):
        return [{'SkillKey': '50301', 'BuffGroupKey': '50301', 'SkillNameKey': 'SkillName_50301'}] if name == 'SkillInfoData' else []

    def localize(self, key, locale='en-US'):
        return {'SkillName_50301': 'Quick Loader', 'MonsterName_20031': 'Cobra', 'StatName_AttackSpeed': 'Attack Speed'}.get(key)

    def hero_name(self, key, locale='en-US'):
        return {501: 'Hunter', 101: 'Knight'}.get(key, f'Hero {key}')


class CombatViewTests(unittest.TestCase):
    def test_attack_count_buff_and_environment_caster_hidden(self):
        hero = {'attack_count': 300}
        quick = _buff(FakeCatalog(), {'group': 50301, 'class': 'bap', 'caster_key': 501,
                                      'modifiers': [mod('AttackSpeed', 'MULTIPLICATIVE', 1.0, 'BuffSkill')],
                                      'expiry': {'kind': 'attack_count', 'threshold': 307}}, hero, 'en-US')
        self.assertEqual((quick['name'], quick['caster'], quick['expiry']['attacks_left']), ('Quick Loader', 'Hunter', 7))
        env = _buff(FakeCatalog(), {'group': 910001, 'class': 'bay', 'caster_key': 101,
                                    'modifiers': [mod('FireResistance', 'FLAT', -20, 'ENVIROUNMENT')],
                                    'expiry': {'kind': 'event'}}, hero, 'en-US')
        self.assertEqual((env['kind'], env['name'], env['caster']), ('environment', 'Stage environment', None))

    def test_enemies_grouped_and_checked_against_catalog_hp(self):
        e = _enemies(FakeCatalog(), [
            {'monster_key': 20031, 'hp': 1000.0, 'max_hp': 3490.85, 'state': 'MOVE'},
            {'monster_key': 20031, 'hp': 3490.85, 'max_hp': 3490.85, 'state': 'IDLE'},
            {'monster_key': 20081, 'hp': None, 'max_hp': None, 'state': None}], 43, 'en-US')
        self.assertEqual((e['count'], e['hp_unknown']), (3, 1))
        cobra = next(g for g in e['groups'] if g['monster_key'] == 20031)
        self.assertEqual((cobra['name'], cobra['count'], cobra['catalog_match']), ('Cobra', 2, True))
        boss = next(g for g in e['groups'] if g['monster_key'] == 20081)
        self.assertIsNone(boss['catalog_match'])                    # bosses are not validated
        self.assertIsNone(_enemies(FakeCatalog(), None, 43, 'en-US'))


if __name__ == '__main__':
    unittest.main()
