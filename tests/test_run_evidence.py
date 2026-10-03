import unittest

from tbh.analysis.run_evidence import match_component, reconcile_intervals
from tbh.analysis.quality import SampleIndex
from tests.test_quality import sample, runtime_run, utc


def window(a, b, clears=0, fails=0):
    return {'start_utc': utc(a), 'end_utc': utc(b), 'save_from': a, 'save_to': b,
            'runs': {'status': 'counts_differ', 'save': {'1109': {'clears': clears, 'fails': fails}}}}


class IntervalTests(unittest.TestCase):
    def check(self, windows, runs, samples):
        reconcile_intervals(windows, runs, SampleIndex(samples))
        return [w['runs']['interval_reconciliation'] for w in windows]

    def test_unique_adjacent_assignment_does_not_double_count(self):
        result = self.check([window(0, 60, 1), window(60, 120)],
                            [runtime_run(final_utc=utc(61))], [sample(59, 1, 1), sample(61, 1, 1)])
        self.assertEqual([r['status'] for r in result], ['unique_correspondence'] * 2)
        self.assertEqual([a['run_id'] for r in result for a in r['assignments']], [1])
        self.assertEqual(result[0]['assignments'][0]['save_to'], 60)

    def test_two_distinct_runs_can_be_ambiguous(self):
        result = self.check([window(0, 60, 1), window(60, 120, 1)],
                            [runtime_run(id=i, session_id=i, final_utc=utc(61)) for i in (1, 2)],
                            [sample(t, 1, 1, session=i) for i in (1, 2) for t in (59, 61)])
        self.assertTrue(all(r['status'] == 'ambiguous' for r in result))
        self.assertTrue(all(not r['assignments'] for r in result))

    def test_identical_counter_units_do_not_create_false_ambiguity(self):
        options = {1: ['a'], 2: ['a']}
        self.assertEqual(match_component(options, {'a': 2}), {1: 'a', 2: 'a'})
        self.assertIsNone(match_component(options, {'a': 2}, (1, 'a')))

    def test_plague_or_boss_departure_cannot_be_invented_clear_or_fail(self):
        for counts in ({'clears': 1}, {'fails': 1}, {}):
            result = self.check([window(0, 60, **counts), window(60, 120)],
                                [runtime_run(final_utc=None, ended_utc=utc(61), end_reason='stage_change')],
                                [sample(59, 1, 1), sample(61, 1, 1)])
            self.assertTrue(all(r['status'] == 'insufficient_evidence' for r in result))
            self.assertTrue(all(not r['assignments'] for r in result))

    def test_marker_in_different_session_is_insufficient(self):
        result = self.check([window(0, 60, 1)], [runtime_run()],
                            [sample(58, 1, 1, session=2), sample(59, 1, 1, session=2)])
        self.assertEqual(result[0]['status'], 'insufficient_evidence')

    def test_resumed_run_uses_explicit_terminal_sample_reference(self):
        rows = [sample(58, 1, 1, session=2), sample(59, 1, 1, session=2)]
        run = runtime_run(last_sample_id=rows[-1]['id'])
        result = self.check([window(0, 60, 1)], [run], rows)
        self.assertEqual(result[0]['status'], 'unique_correspondence')
        self.assertEqual(result[0]['evidence'][0]['session_id'], 2)
        self.assertEqual(result[0]['evidence'][0]['run_session_id'], 1)
        run['last_sample_id'] = rows[0]['id']
        result = self.check([window(0, 60, 1)], [run], rows)
        self.assertEqual(result[0]['status'], 'insufficient_evidence')

    def test_missing_event_extra_event_and_wrong_outcome_are_insufficient(self):
        for clears, fails in [(0, 0), (2, 0), (0, 1)]:
            result = self.check([window(0, 60, clears, fails)], [runtime_run()],
                                [sample(58, 1, 1), sample(59, 1, 1)])
            self.assertEqual(result[0]['status'], 'insufficient_evidence')

    def test_exact_save_boundary_belongs_to_preceding_window(self):
        result = self.check([window(0, 60, 1), window(60, 120)], [runtime_run(final_utc=utc(60))],
                            [sample(59, 1, 1), sample(60, 1, 1)])
        self.assertEqual(result[0]['status'], 'unique_correspondence')
        self.assertEqual(result[1]['status'], 'no_events')

    def test_scope_gap_and_counter_reset_prevent_unique_claim(self):
        for before, status in [(-1, 'counts_differ'), (1, 'counts_differ'), (58, 'counter_reset')]:
            w = window(0, 60, 1)
            w['runs']['status'] = status
            result = self.check([w], [runtime_run()], [sample(before, 1, 1), sample(59, 1, 1)])
            self.assertEqual(result[0]['status'], 'insufficient_evidence')
