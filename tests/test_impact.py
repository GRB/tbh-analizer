"""Before/after effect of rune purchases on synthetic saves and runs: no game needed."""
import unittest
from datetime import datetime, timedelta, timezone

from tbh.analysis.impact import power_changes, power_state, purchase_impact

T0 = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
PARTY = '201,401,501'


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def save(minutes, runes, **state):
    snapshot = {'runes': runes, 'party': [201, 501], 'heroes': [], 'items': {}, 'attributes': {}, 'pet': None, **state}
    return {'last_saved_utc': at(minutes), 'runes': runes, 'power': power_state(snapshot)}


def hero(key, equipped, skills=()):
    return {'hero_key': key, 'equipped': list(equipped), 'skills': list(skills)}


def run(start_min, seconds, gold, stage=1309, level=24, level_ups=0, jitter=0.0):
    seconds = seconds * (1 + jitter)
    return {'started_utc': at(start_min), 'ended_utc': at(start_min + seconds / 60), 'stage_key': stage,
            'party': PARTY, 'duration_s': seconds, 'gold_gain_est': gold, 'outcome': 'clear',
            'xp': {'201': {'gain': 1000.0, 'complete': True, 'level_start': level,
                           'level_end': level + level_ups, 'level_ups': level_ups}}}


def series(start_min, n, seconds, gold, **kw):
    """n back-to-back runs with a small alternating spread."""
    out, t = [], start_min
    for i in range(n):
        r = run(t, seconds, gold, jitter=0.02 * (-1) ** i, **kw)
        out.append(r)
        t += r['duration_s'] / 60
    return out


class ImpactTests(unittest.TestCase):
    def test_changes_between_saves_and_unknown_saves_skipped(self):
        saves = [save(0, {1: 1}), save(1, {}), save(2, {1: 2, 5: 1})]
        events = power_changes(saves)
        self.assertEqual(len(events), 1)
        self.assertEqual({k: v for k, v in events[0]['changes'].items() if k[0] == 'rune'},
                         {('rune', 1): (1, 2), ('rune', 5): (0, 1)})

    def test_faster_runs_after_purchase_are_significant(self):
        before = series(0, 6, 100, 1000)
        after = series(12, 6, 90, 1000)
        saves = [save(10, {406: 1}), save(11, {406: 2})]
        row = purchase_impact(saves, before + after)[0]
        self.assertEqual(row['status'], 'measured')
        self.assertEqual((row['runs_before'], row['runs_after']), (6, 6))
        self.assertAlmostEqual(row['metrics']['duration_s']['pct'], -10.0, places=0)
        self.assertTrue(row['metrics']['duration_s']['significant'])
        self.assertTrue(row['metrics']['gold_h']['significant'])
        self.assertFalse(row['metrics']['gold_per_run']['significant'])
        self.assertEqual(row['level_ups'], 0)

    def test_run_spanning_the_purchase_is_left_out(self):
        runs = series(0, 4, 100, 1000) + [run(9, 180, 1000)] + series(13, 4, 100, 1000)
        saves = [save(10, {406: 1}), save(11, {406: 2})]
        row = purchase_impact(saves, runs)[0]
        self.assertEqual((row['runs_before'], row['runs_after']), (4, 4))

    def test_purchases_without_runs_between_are_merged(self):
        saves = [save(10, {406: 1}), save(11, {406: 2}), save(12, {406: 3, 407: 1})]
        rows = purchase_impact(saves, series(0, 5, 100, 1000) + series(13, 5, 100, 1000))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['changes'], {('rune', 406): (1, 3), ('rune', 407): (0, 1)})

    def test_each_side_stops_at_the_neighbouring_purchase(self):
        runs = series(0, 4, 100, 1000) + series(12, 4, 100, 1000) + series(22, 4, 90, 1000)
        saves = [save(10, {1: 1}), save(11, {1: 2}), save(20, {1: 2}), save(21, {1: 3})]
        newest, older = purchase_impact(saves, runs)
        self.assertEqual((older['runs_before'], older['runs_after']), (4, 4))
        self.assertEqual((newest['runs_before'], newest['runs_after']), (4, 4))

    def test_skill_unequipped_and_equipped_again_is_no_change(self):
        # Seen in the save: slot 3 went 20501 -> -1 -> 20501 between two runs; -1 is an empty slot.
        party = lambda skills: [hero(201, [], skills), hero(501, [], [50101, -1])]
        saves = [save(10, {406: 1}, heroes=party([20101, 20501])), save(11, {406: 1}, heroes=party([20101, -1])),
                 save(12, {406: 2}, heroes=party([20101, 20501]))]
        rows = purchase_impact(saves, series(0, 5, 100, 1000) + series(13, 5, 100, 1000))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['changes'], {('rune', 406): (1, 2)})

    def test_gear_swap_upgrade_and_benched_hero(self):
        items = {'a': {'item_key': 1, 'enchants': []}, 'b': {'item_key': 2, 'enchants': []},
                 'c': {'item_key': 3, 'enchants': []}}
        upgraded = dict(items, a={'item_key': 1, 'enchants': [{'StatModKey': 9}]})
        before = save(0, {1: 1}, items=items, heroes=[hero(501, ['a', None]), hero(101, ['c'])])
        swap = save(1, {1: 1}, items=items, heroes=[hero(501, ['a', 'b']), hero(101, [None])])
        enchant = save(2, {1: 1}, items=upgraded, heroes=[hero(501, ['a', 'b']), hero(101, [None])])
        first, second = power_changes([before, swap, enchant])
        # The amulet slot filled; the benched hero (101, not in the party) is ignored.
        self.assertEqual(first['changes'], {('gear', 501, 1): ((None, None), ('b', 2))})
        # Same item, new enchant: an upgrade, not a gear change.
        self.assertEqual(list(second['changes']), [('upgrade', 501, 0)])

    def test_few_runs_after_is_collecting(self):
        saves = [save(10, {1: 1}), save(11, {1: 2})]
        row = purchase_impact(saves, series(0, 5, 100, 1000) + series(12, 1, 100, 1000))[0]
        self.assertEqual(row['status'], 'collecting')
        self.assertEqual(row['runs_needed'], 2)

    def test_too_few_runs_before_can_never_be_measured(self):
        saves = [save(10, {1: 1}), save(11, {1: 2})]
        row = purchase_impact(saves, series(8, 1, 100, 1000) + series(12, 6, 100, 1000))[0]
        self.assertEqual(row['status'], 'too few runs before')

    def test_level_ups_in_the_window_are_counted(self):
        before = series(0, 4, 100, 1000, level=24)
        after = series(12, 4, 90, 1000, level=25)
        after[0]['xp']['201']['level_ups'] = 1
        saves = [save(10, {1: 1}), save(11, {1: 2})]
        row = purchase_impact(saves, before + after)[0]
        self.assertGreaterEqual(row['level_ups'], 2)   # a level-up inside a run + a level change across the purchase
        self.assertEqual(row['validation']['status'], 'confounded_levels')


if __name__ == '__main__':
    unittest.main()
