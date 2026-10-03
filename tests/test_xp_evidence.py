import unittest
from tbh.analysis.progress import xp_gained
from tbh.analysis.quality import SampleIndex
from tbh.analysis.xp_evidence import level_up_evidence, AUDITED_SHA256
from tests.test_quality import sample


class XPEvidenceTests(unittest.TestCase):
    def test_remainder_and_multiple_levels(self):
        self.assertEqual(xp_gained(1, 25, 3, 7, {1: 30, 2: 150}), (162, 'level_up'))
        self.assertEqual(xp_gained(1, 25, 2, 0, {1: 30}), (5, 'level_up'))

    def test_invalid_inputs_do_not_publish_nonfinite_gains(self):
        for bad in (float('nan'), float('inf'), -1, True):
            self.assertIsNone(xp_gained(1, bad, 2, 0, {1: 30})[0])
            self.assertIsNone(xp_gained(1, 0, 2, 0, {1: bad})[0])
        self.assertIsNone(xp_gained(1.5, 0, 2, 0, {1: 30})[0])

    def test_consistency_is_separate_from_native_identity(self):
        rows = [sample(0, 0, 25, level=1), sample(1, 0, 7, level=3)]
        sessions = {1: {'build_id': '25454993', 'game_assembly_sha256': AUDITED_SHA256}}
        d = level_up_evidence(SampleIndex(rows), {1: 30, 2: 150}, '25454993', sessions)
        self.assertEqual(d['statuses'], {'consistent': 1})
        self.assertEqual(d['native_identity_matched_transitions'], 1)
        self.assertEqual(d['events'][0]['accounted_gain'], 162)
        d = level_up_evidence(SampleIndex(rows), {1: 30, 2: 150}, 'other', {})
        self.assertEqual(d['native_rule']['status'], 'unverified_build')
        self.assertEqual(d['native_identity_matched_transitions'], 0)

    def test_gap_and_missing_threshold_remain_visible(self):
        rows = [sample(0, 0, 25, level=1), sample(60, 0, 7, level=3)]
        d = level_up_evidence(SampleIndex(rows), {}, '25454993', {})
        self.assertEqual(d['statuses'], {'incomplete': 1})
        self.assertIsNone(d['events'][0]['accounted_gain'])

    def test_different_sessions_are_not_joined(self):
        rows = [sample(0, 0, 25, level=1), sample(1, 0, 7, session=2, level=3)]
        self.assertEqual(level_up_evidence(SampleIndex(rows), {1: 30}, '25454993', {})['observed_transitions'], 0)
