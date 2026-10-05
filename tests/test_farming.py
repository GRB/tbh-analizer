"""Failure-inclusive exposure and independently priced spending evidence."""
import copy
import unittest
from types import SimpleNamespace

from tbh.analysis.farming import farming_report, recovery_cycles
from tbh.analysis.quality import ledger
from tbh.analysis.spending import rune_spending
from tests.test_quality import saves, sample, THRESHOLDS, utc


class SpendingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = SimpleNamespace(runes={'1': {'LevelDataKey': 'x'}}, rune_levels={'x': {
            1: {'CostValue': '100', 'CostItemKey': '100001'},
            2: {'CostValue': '200', 'CostItemKey': '100001'}}})

    def test_multiple_levels_price_each_increment(self):
        row = rune_spending({'runes': {}}, {'runes': {1: 2}}, self.catalog, 350)
        self.assertEqual(row['catalog_gold_cost'], 300)
        self.assertEqual(row['spend_minus_catalog_cost'], 50)
        self.assertEqual(row['status'], 'difference')

    def test_missing_price_does_not_explain_spending(self):
        row = rune_spending({'runes': {}}, {'runes': {1: 3}}, self.catalog, 300)
        self.assertEqual(row['status'], 'incomplete')
        self.assertIsNone(row['spend_minus_catalog_cost'])

    def test_refund_and_other_currency_are_not_gold(self):
        self.catalog.rune_levels['x'][1]['CostItemKey'] = 'other'
        row = rune_spending({'runes': {}}, {'runes': {1: 1}}, self.catalog, 0)
        self.assertEqual(row['catalog_gold_cost'], 0)
        row = rune_spending({'runes': {1: 2}}, {'runes': {1: 1}}, self.catalog, -200)
        self.assertIsNone(row['spend_minus_catalog_cost'])


class FarmingTests(unittest.TestCase):
    def test_recovery_keeps_other_stage_time_and_unfinished_cycles(self):
        episode = {'start_utc': utc(0), 'end_utc': utc(300), 'session_id': 1}
        rows = [{'id': i, 'session_id': 1, 'stage_key': stage, 'outcome': outcome,
                 'started_utc': utc(start), 'ended_utc': utc(end)} for i, stage, outcome, start, end in
                [(1, 1109, 'fail', 0, 30), (2, 1108, 'clear', 30, 90), (3, 1109, 'clear', 90, 150),
                 (4, 1109, 'fail', 150, 180)]]
        complete, censored = recovery_cycles([episode], rows)
        self.assertEqual(complete['failure_and_recovery_s'], 150)
        self.assertEqual(complete['after_failure_s'], 120)
        self.assertEqual(censored['failure_and_recovery_s'], 150)
        self.assertEqual(censored['status'], 'not_observed_before_episode_end')
        self.assertIsNone(censored['gold'])

    def make_report(self, snapshots=None, samples=None):
        snapshots = snapshots or saves()
        for s in snapshots:
            s['power'] = {('rune', 1): 1}
        rows = ledger(snapshots, samples or [sample(0, 100, 0), sample(30, 200, 30), sample(60, 300, 60)], [], THRESHOLDS)
        return farming_report(snapshots, rows['windows'], THRESHOLDS)

    def test_stage_change_failure_time_is_included(self):
        snapshots = saves()
        snapshots[1]['current_stage'] = 1108
        snapshots[1]['aggregates'][1]['value'] = 1
        report = self.make_report(snapshots)
        e = report['episodes'][0]
        self.assertEqual(e['seconds'], 60)
        self.assertEqual(e['stages'], [1108, 1109])
        self.assertEqual(e['gross_gold_h'], 12000)
        self.assertEqual(e['fails'], 1)

    def test_gaps_are_not_turned_into_free_recovery(self):
        report = self.make_report(samples=[sample(0, 100, 0), sample(60, 300, 60)])
        self.assertEqual(report['episodes'], [])
        self.assertIn('runtime continuity unavailable', report['excluded_windows'])

    def test_level_change_is_not_same_cohort(self):
        snapshots = copy.deepcopy(saves())
        snapshots[1]['heroes'][0]['level'] += 1
        report = self.make_report(snapshots)
        self.assertEqual(report['episodes'], [])
        self.assertIn('levels changed or unknown', report['excluded_windows'])
