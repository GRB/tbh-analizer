"""Cross-source checks with known truths, ambiguous endpoints and missing observations."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tbh.analysis.quality import ledger
from tbh.server import api_quality
from tbh.store import Store
from tbh.views.quality import quality_report
from tbh.views.analytics import unique_runs
from tests.test_core import snap

BASE = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
THRESHOLDS = {1: 100, 2: 200, 20: 1000}


def utc(t):
    return (BASE + timedelta(seconds=t)).isoformat()


def sample(t, gold, xp, session=1, level=20, quality='ok'):
    return {'id': int(t * 10 + 100), 'session_id': session, 'utc': utc(t), 'mono': t, 'stage_key': 1109,
            'gold': gold, 'heroes': [{'hero_key': 401, 'level': level, 'xp': xp}], 'quality': quality}


def saves():
    return [dict(snap(utc(0), 10, gold_earned=1000, gold=100, xp=0, play=0), save_id=1),
            dict(snap(utc(60), 11, gold_earned=1200, gold=300, xp=60, play=60), save_id=2)]


def runtime_run(**kw):
    return {'id': 1, 'session_id': 1, 'stage_key': 1109, 'started_utc': utc(0), 'ended_utc': utc(60),
            'final_utc': utc(59), 'end_reason': 'wave_reset', 'partial_start': False, **kw}


class LedgerTests(unittest.TestCase):
    def test_first_clear_marker_is_not_shifted_to_force_window_assignment(self):
        rows = [sample(0, 100, 0), sample(30, 200, 30), sample(59, 300, 60),
                dict(sample(60.4, 300, 60), stage_key=1110)]
        run = runtime_run(final_utc=utc(60.4), ended_utc=utc(60.4), end_reason='stage_change')
        w = self.report(samples=rows, runs=[run])['windows'][0]
        self.assertEqual(w['runs']['status'], 'counts_differ')
        self.assertEqual(w['runs']['run_ids'], [])
        self.assertEqual(w['runs']['first_clear_adjustments'], 0)
        marker = w['runs']['boundary_markers']['end'][0]
        self.assertEqual(marker['kind'], 'clear_marker')
        self.assertEqual(marker['before_utc'], utc(59))
        self.assertEqual(marker['observed_utc'], utc(60.4))

    def test_unmarked_departure_is_evidence_not_a_clear(self):
        rows = [sample(0, 100, 0), sample(30, 200, 30), sample(59, 300, 60),
                dict(sample(60.4, 300, 60), stage_key=1110)]
        run = runtime_run(final_utc=None, ended_utc=utc(60.4), end_reason='stage_change')
        w = self.report(samples=rows, runs=[run])['windows'][0]
        self.assertEqual(w['runs']['boundary_markers']['end'][0]['kind'], 'unmarked_stage_departure')
        self.assertEqual(w['runs']['runtime'], {})

    def test_boundary_brackets_reject_bad_quality_gaps_and_other_sessions(self):
        run = runtime_run(final_utc=utc(60.4), ended_utc=utc(60.4), end_reason='stage_change')
        for before in [sample(59, 300, 60, quality='bad'), sample(24, 300, 60),
                       sample(59, 300, 60, session=2)]:
            with self.subTest(before=before):
                w = self.report(samples=[sample(0, 100, 0), before,
                                         dict(sample(60.4, 300, 60), stage_key=1110)], runs=[run])['windows'][0]
                self.assertEqual(w['runs']['boundary_markers']['end'], [])

    def test_wall_clock_jump_is_not_continuous_observation(self):
        rows = [sample(0, 100, 0), dict(sample(30, 200, 30), mono=5), sample(60, 300, 60)]
        r = self.report(samples=rows)
        self.assertEqual(r['windows'][0]['gold']['status'], 'incomplete')
        self.assertEqual(r['summary']['coverage_fraction'], 0)

    def test_second_collector_joining_mid_run_is_not_a_second_clear(self):
        full = runtime_run()
        partial = runtime_run(id=2, session_id=2, started_utc=utc(25), ended_utc=utc(60.5), partial_start=True)
        later = runtime_run(id=3, session_id=2, started_utc=utc(61), ended_utc=utc(120), final_utc=utc(119))
        self.assertEqual([r['id'] for r in unique_runs([full, partial, later])], [1, 3])

    def report(self, snapshots=None, samples=None, runs=None):
        return ledger(snapshots if snapshots is not None else saves(), samples if samples is not None else
                      [sample(0, 100, 0), sample(30, 200, 30), sample(60, 300, 60)],
                      runs if runs is not None else [runtime_run()], THRESHOLDS)

    def test_independent_sources_agree_with_known_income_xp_and_clear(self):
        report = self.report()
        w = report['windows'][0]
        self.assertEqual(w['gold']['status'], 'matched')
        self.assertEqual(w['gold']['sample_rises'], 200)
        self.assertEqual(w['xp'][0]['status'], 'matched')
        self.assertEqual(w['xp'][0]['sample_gain'], 60)
        self.assertEqual(w['runs']['status'], 'counts_agree')
        self.assertEqual(report['summary']['coverage_fraction'], 1)

    def test_hidden_spending_is_a_residual_not_silently_reconciled(self):
        snapshots = saves()
        snapshots[-1]['gold'] = 250  # 200 earned, 50 spent between readings; rises show only 150
        w = self.report(snapshots, [sample(0, 100, 0), sample(30, 200, 30), sample(60, 250, 60)])['windows'][0]
        self.assertEqual(w['gold']['status'], 'discrepancy')
        self.assertEqual(w['gold']['residual'], 50)
        self.assertEqual(w['gold']['implied_spend'], 50)
        self.assertEqual(w['gold']['sample_falls'], 0)

    def test_nearest_time_endpoint_is_not_selected_for_matching_values(self):
        rows = [sample(0, 100, 0), sample(30, 200, 30), sample(59, 300, 60), sample(60.1, 310, 61)]
        w = self.report(samples=rows)['windows'][0]
        self.assertEqual(w['gold']['status'], 'boundary_uncertain')
        self.assertEqual(w['gold']['residual'], -10)
        self.assertAlmostEqual(w['coverage']['end_offset_s'], .1, places=5)
        self.assertEqual(w['xp'][0]['status'], 'boundary_uncertain')

    def test_opposite_boundary_errors_cannot_hide_behind_zero_delta(self):
        w = self.report(samples=[sample(0, 110, 5), sample(30, 210, 35), sample(60, 310, 65)])['windows'][0]
        self.assertEqual(w['gold']['residual'], 0)
        self.assertEqual(w['gold']['status'], 'boundary_uncertain')
        self.assertEqual(w['xp'][0]['status'], 'boundary_uncertain')

    def test_large_gap_stays_incomplete_even_with_identical_endpoints(self):
        r = self.report(samples=[sample(0, 100, 0), sample(60, 300, 60)])
        self.assertEqual(r['windows'][0]['gold']['status'], 'incomplete')
        self.assertEqual(r['summary']['coverage_fraction'], 0)
        self.assertIsNone(r['summary']['gold_totals_on_comparable_windows']['residual'])

    def test_partial_reading_does_not_establish_continuity(self):
        rows = [sample(0, 100, 0), sample(30, 200, 30, quality='partial'), sample(60, 300, 60)]
        self.assertEqual(self.report(samples=rows)['windows'][0]['gold']['status'], 'incomplete')

    def test_sessions_cannot_be_stitched_together(self):
        rows = [sample(0, 100, 0, session=1), sample(30, 200, 30, session=1), sample(60, 300, 60, session=2)]
        r = self.report(samples=rows)
        self.assertEqual(r['windows'][0]['gold']['status'], 'missing')
        self.assertEqual(r['summary']['coverage_fraction'], .5)

    def test_duplicate_collectors_are_not_double_counted(self):
        rows = [sample(t, gold, xp, session=session) for session in (1, 2)
                for t, gold, xp in ((0, 100, 0), (30, 200, 30), (60, 300, 60))]
        r = self.report(samples=rows)
        self.assertEqual(r['summary']['coverage_fraction'], 1)
        self.assertEqual(r['windows'][0]['gold']['sample_rises'], 200)

    def test_no_runtime_does_not_mean_zero_earned(self):
        w = self.report(samples=[])['windows'][0]
        self.assertEqual(w['gold']['save_earned'], 200)
        self.assertIsNone(w['gold']['sample_rises'])
        self.assertIsNone(w['gold']['residual'])
        self.assertEqual(w['gold']['status'], 'missing')

    def test_unknown_interior_gold_does_not_become_zero(self):
        w = self.report(samples=[sample(0, 100, 0), sample(30, None, 30), sample(60, 300, 60)])['windows'][0]
        self.assertEqual(w['gold']['status'], 'missing')
        self.assertEqual(w['xp'][0]['status'], 'matched')

    def test_hero_disappears_and_returns_xp_is_incomplete(self):
        rows = [sample(0, 100, 0), dict(sample(30, 200, 30), heroes=[]), sample(60, 300, 60)]
        w = self.report(samples=rows)['windows'][0]
        self.assertIsNone(w['xp'][0]['sample_gain'])
        self.assertEqual(w['xp'][0]['status'], 'missing')

    def test_level_up_keeps_model_dependency_visible(self):
        snapshots = saves()
        snapshots[0]['heroes'][0].update(level=1, xp=90)
        snapshots[1]['heroes'][0].update(level=2, xp=10)
        rows = [sample(0, 100, 90, level=1), sample(30, 200, 95, level=1), sample(60, 300, 10, level=2)]
        r = self.report(snapshots, rows)
        self.assertEqual(r['windows'][0]['xp'][0]['save_gain'], 20)
        self.assertEqual(r['windows'][0]['xp'][0]['status'], 'matched')
        self.assertEqual(r['summary']['level_up_windows'], 1)
        self.assertIn('threshold model', ' '.join(r['windows'][0]['flags']))

    def test_save_rollback_is_explicit(self):
        snapshots = saves()
        snapshots[-1]['aggregates'][2]['value'] = 900
        self.assertEqual(self.report(snapshots)['windows'][0]['gold']['status'], 'counter_reset')

    def test_saved_reconciled_outcome_cannot_validate_itself(self):
        w = self.report(runs=[runtime_run(final_utc=None, outcome='clear', outcome_source='save-counters')])['windows'][0]
        self.assertEqual(w['runs']['status'], 'counts_differ')
        self.assertEqual(w['runs']['runtime']['1109'], {'clears': 0, 'fails': 1})

    def test_partial_runs_count_markers_without_prorating_rewards(self):
        w = self.report(runs=[runtime_run(partial_start=True, gold_gain_est=9999)])['windows'][0]
        self.assertEqual(w['runs']['status'], 'counts_agree')
        self.assertEqual(w['runs']['partial_starts'], 1)
        self.assertEqual(w['gold']['sample_rises'], 200)

    def test_stage_change_still_checked_but_not_used_for_stage_rate(self):
        snapshots = saves()
        snapshots[-1]['current_stage'] = 1110
        w = self.report(snapshots)['windows'][0]
        self.assertEqual(w['gold']['status'], 'matched')
        self.assertFalse(w['stage_rate_eligible'])

    def test_absolute_residual_cannot_cancel(self):
        snapshots = saves()
        third = copy.deepcopy(snapshots[-1])
        third.update(last_saved_utc=utc(120), play_time=120, gold=400, save_id=3)
        third['heroes'][0]['xp'] = 120
        third['aggregates'][2]['value'] = 1300
        snapshots[-1]['gold'] = 200  # first +100 residual; second -100 residual
        rows = [sample(t, gold, t) for t, gold in ((0, 100), (30, 150), (60, 200), (90, 300), (120, 400))]
        totals = self.report(snapshots + [third], rows)['summary']['gold_totals_on_comparable_windows']
        self.assertEqual(totals['residual'], 0)
        self.assertEqual(totals['absolute_residual'], 200)


class QualityViewTests(unittest.TestCase):
    def test_real_store_pagination_filters_and_route_are_read_only(self):
        class Catalog:
            build_id = 'test'
            level_thresholds = THRESHOLDS
            stage_label = staticmethod(lambda key: f'Stage {key}')
            hero_name = staticmethod(lambda key: f'Hero {key}')
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'quality.db')
            snapshots = saves()
            third = copy.deepcopy(snapshots[-1])
            third.update(last_saved_utc=utc(120), play_time=120, gold=400, save_id=3)
            third['heroes'][0]['xp'] = 120
            third['aggregates'][2]['value'] = 1400  # save gross +200, sampled rises +100
            snapshots.append(third)
            store.insert('sessions', {'id': 1, 'build_id': 'test'})
            for s in snapshots:
                store.insert('saves', {'id': s['save_id'], 'sha256': str(s['save_id']),
                                      'observed_utc': s['last_saved_utc'], 'last_saved_utc': s['last_saved_utc']})
            for t, gold in ((0, 100), (30, 200), (59, 300), (60, 300), (90, 350), (120, 400)):
                s = sample(t, gold, t)
                s['mono'] = t
                s['heroes'] = json.dumps(s['heroes'])
                store.insert('samples', s)
            store.insert('runs', runtime_run())
            before = store.conn.total_changes
            try:
                with patch('tbh.views.quality.light_snapshot', side_effect=lambda store, i: snapshots[i - 1]):
                    first = quality_report(store, Catalog(), now=BASE + timedelta(seconds=130), limit=1)
                    self.assertEqual(first['summary']['windows'], 2)
                    self.assertTrue(first['pagination']['has_more'])
                    self.assertEqual(first['windows'][0]['gold']['status'], 'discrepancy')
                    second = quality_report(store, Catalog(), now=BASE + timedelta(seconds=130), offset=1, limit=1)
                    self.assertEqual(second['windows'][0]['gold']['status'], 'matched')
                    only = quality_report(store, Catalog(), now=BASE + timedelta(seconds=130), status='matched')
                    self.assertEqual(only['pagination']['filtered_total'], 1)
                    self.assertEqual(only['summary'], first['summary'])  # filter never changes headline denominators
                    app = SimpleNamespace(store=store, catalog=Catalog())
                    with self.assertRaises(ValueError):
                        api_quality(app, {'hours': ['nan']}, {}, None)
                self.assertEqual(store.conn.total_changes, before)
            finally:
                store.conn.close()

    def test_empty_database_is_not_perfect_coverage_and_api_validation(self):
        class Catalog:
            build_id = 'test'
            level_thresholds = THRESHOLDS
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'quality.db')
            try:
                report = quality_report(store, Catalog(), now=BASE)
                self.assertEqual(report['windows'], [])
                self.assertIsNone(report['summary']['coverage_fraction'])
                for hours in (0, -1, float('nan'), float('inf'), 169):
                    with self.assertRaises(ValueError):
                        quality_report(store, Catalog(), hours=hours)
                with self.assertRaises(ValueError):
                    quality_report(store, Catalog(), status='garbage')
                with self.assertRaises(ValueError):
                    quality_report(store, Catalog(), limit=201)
                json.dumps(report, allow_nan=False)
            finally:
                store.conn.close()


if __name__ == '__main__':
    unittest.main()
