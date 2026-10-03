"""Act boss attempts from save counters (shape observed on 1.2.8, 2026-10-01: 1309 -> 1310 act boss -> 1309)."""
import unittest
from datetime import datetime, timedelta, timezone

from tbh.analysis import actboss
from tbh.analysis.reconcile import reconcile
from tbh.analysis.runs import RunTracker


class FakeCatalog:
    stages = {'1309': {'STAGETYPE': 'NORMAL', 'BossMonsterKey': '20111', 'WaveAmount': '18'},
              '1310': {'STAGETYPE': 'ACTBOSS', 'BossMonsterKey': '30901', 'WaveAmount': ''},
              '2110': {'STAGETYPE': 'ACTBOSS', 'BossMonsterKey': '10902', 'WaveAmount': ''}}
    level_thresholds = {}


T0 = datetime(2026, 10, 1, 9, 3, 50, tzinfo=timezone.utc)


def sample(seconds, stage, wave, gold):
    return {'utc': (T0 + timedelta(seconds=seconds)).isoformat(), 'mono': 100.0 + seconds,
            'stage_key': stage, 'wave': wave, 'gold': gold, 'heroes': []}


def snap(utc, stage, gold, counters, kills):
    """counters: {(type, stage): value} for StageClear(13)/StageFail(14); kills: {monster: value}."""
    rows = [{'type': 2, 'subkey': 0, 'content': c, 'value': gold} for c in (0, 1)]
    for (type_, key), value in counters.items():
        rows += [{'type': type_, 'subkey': key, 'content': c, 'value': value} for c in (0, 1)]
    for monster, value in kills.items():
        rows += [{'type': 17, 'subkey': monster, 'content': c, 'value': value} for c in (0, 1)]
    return {'last_saved_utc': f'2026-10-01T{utc}+00:00', 'current_stage': stage, 'aggregates': rows}


SAVES = [
    snap('07:51:33', 1309, 8869916, {(13, 1309): 21, (13, 1310): 1}, {30901: 1}),
    snap('07:54:07', 1309, 8906016, {(13, 1309): 22, (13, 1310): 1}, {30901: 1}),
    snap('07:54:35', 1310, 8911973, {(13, 1309): 22, (13, 1310): 2}, {30901: 2}),
    snap('07:57:35', 1309, 8952704, {(13, 1309): 22, (13, 1310): 2}, {30901: 2}),
]


class ActBossTests(unittest.TestCase):
    def test_attempt_found_between_saves(self):
        found = actboss.attempts(SAVES, FakeCatalog())
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual((a['stage_key'], a['clears'], a['fails'], a['boss_kills']), (1310, 1, 0, 1))
        self.assertEqual(a['after_utc'][11:19], '07:54:07')
        self.assertEqual(a['window_gold_earned'], 8911973 - 8906016)

    def test_content_one_duplicates_not_summed(self):
        self.assertEqual(actboss.attempts(SAVES, FakeCatalog())[0]['clears'], 1)

    def test_fail_counted(self):
        saves = [snap('21:19:12', 2109, 0, {(14, 2110): 0}, {}), snap('21:22:01', 2109, 0, {(14, 2110): 1}, {})]
        a = actboss.attempts(saves, FakeCatalog())[0]
        self.assertEqual((a['stage_key'], a['clears'], a['fails'], a['boss_kills']), (2110, 0, 1, 0))

    def test_in_progress_from_save_stage(self):
        self.assertEqual(actboss.in_progress(SAVES[2], FakeCatalog()), 1310)
        self.assertIsNone(actboss.in_progress(SAVES[1], FakeCatalog()))

    def test_run_with_fight_inside_is_contaminated(self):
        fights = actboss.attempts(SAVES, FakeCatalog())
        inside = {'started_utc': '2026-10-01T07:54:07.776+00:00', 'ended_utc': '2026-10-01T07:57:40.507+00:00'}
        boundary = {'started_utc': '2026-10-01T07:50:57.050+00:00', 'ended_utc': '2026-10-01T07:54:07.776+00:00'}
        self.assertTrue(actboss.contaminated(inside, fights))
        self.assertFalse(actboss.contaminated(boundary, fights))  # 0.15 s overlap: only the boundary


    def test_runtime_attempt_marks_save_attempt_observed(self):
        fights = actboss.match_runtime(actboss.attempts(SAVES, FakeCatalog()), [
            {'id': 9, 'stage_key': 1310, 'started_utc': '2026-10-01T07:54:09+00:00', 'ended_utc': '2026-10-01T07:54:39+00:00'}])
        self.assertEqual(fights[0]['observed_run_id'], 9)
        inside = {'started_utc': '2026-10-01T07:54:07.776+00:00', 'ended_utc': '2026-10-01T07:57:40.507+00:00'}
        self.assertFalse(actboss.contaminated(inside, fights))


class ActBossRunTests(unittest.TestCase):
    """Runtime sequence observed 2026-10-01 09:04: 1309 cleared, 1310 wave 0 -> 1, back to 1309."""

    def feed(self, samples):
        closed = []
        tracker = RunTracker(FakeCatalog(), closed.append)
        for s in samples:
            tracker.feed(s)
        return closed

    def test_act_boss_is_its_own_run(self):
        closed = self.feed([sample(0, 1309, 18, 0), sample(10, 1309, 19, 4000), sample(12, 1309, 0, 4000),
                            sample(15, 1310, 0, 4000), sample(37, 1310, 1, 9500), sample(41, 1309, 0, 9500)])
        boss = closed[-1]
        self.assertEqual((boss['stage_key'], boss['wave_amount'], boss['end_reason']), (1310, 0, 'stage_change'))
        self.assertEqual((boss['outcome'], boss['outcome_source']), ('clear', 'runtime-heuristic'))
        self.assertEqual(boss['gold_gain_est'], 5500)
        self.assertIsNotNone(boss['final_utc'])

    def test_act_boss_without_kill_is_a_fail(self):
        closed = self.feed([sample(0, 1309, 0, 0), sample(5, 1310, 0, 0), sample(60, 1309, 0, 0)])
        self.assertEqual(closed[-1]['outcome'], 'fail')

    def test_save_counters_confirm_act_boss_run(self):
        run = dict(self.feed([sample(0, 1309, 0, 0), sample(5, 1310, 0, 0), sample(20, 1310, 1, 10),
                              sample(25, 1309, 0, 10)])[-1], id=1)
        saves = [snap('09:03:00', 1309, 0, {(13, 1310): 1}, {30901: 1}), snap('09:04:30', 1310, 0, {(13, 1310): 2}, {30901: 2})]
        self.assertEqual(reconcile([run], saves), {1: ('clear', 'save-counters')})


if __name__ == '__main__':
    unittest.main()
