"""Run continuity across server restarts, duplicate runs and the single-collector lock (no game)."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tbh import instance
from tbh.analysis.runs import RunTracker
from tbh.collector import open_run_to_resume, persist_run
from tbh.store import Store
from tbh.views.analytics import unique_runs

T0 = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
HEROES = [{'hero_key': 201, 'level': 24, 'xp': 1000.0}]


def sample(seconds, wave, gold, mono_base=1000.0, stage=1308):
    return {'utc': (T0 + timedelta(seconds=seconds)).isoformat(), 'mono': mono_base + seconds, 'stage_key': stage,
            'wave': wave, 'gold': gold, 'heroes': [dict(h, xp=h['xp'] + seconds) for h in HEROES]}


class TrackerResumeTests(unittest.TestCase):
    def test_restart_continues_the_open_run(self):
        # Old process: a run ends at t=100 (wave reset), the next one is open when it is killed at t=108.
        old = [sample(0, 17, 0), sample(100, 0, 1000), sample(105, 5, 1500), sample(108, 9, 2000)]
        # New process (different monotonic base) samples again 12 s later and sees the reset at t=128.
        new = [sample(120, 11, 2200, mono_base=50.0), sample(125, 12, 2600, mono_base=50.0),
               sample(128, 0, 3000, mono_base=50.0)]
        closed = []
        tracker = RunTracker(None, closed.append)
        tracker.resume(old[1:], new[0], partial=False)
        for s in new:
            tracker.feed(s)
        self.assertEqual(len(closed), 1)
        run = closed[0]
        self.assertFalse(run['partial_start'])
        self.assertEqual(run['end_reason'], 'wave_reset')
        self.assertAlmostEqual(run['duration_s'], 28.0)        # t=100 -> t=128 despite the new monotonic base
        self.assertEqual(run['gold_gain_est'], 2000)
        self.assertEqual(run['gaps'], 1)                         # the restart shows up as one gap


class OpenRunToResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.tmp.name) / 't.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, attached_s, pid=7, created='2026-09-30T20:00:00'):
        values = {'pid': pid, 'process_created': created, 'build_id': 'b', 'game_assembly_sha256': 'x',
                  'layout_generated': 'l', 'attached_utc': (T0 + timedelta(seconds=attached_s)).isoformat()}
        return self.store.insert('sessions', values), values

    def feed(self, session_id, samples):
        closed = []
        tracker = RunTracker(None, closed.append)
        for s in samples:
            s['id'] = self.store.insert('samples', {'session_id': session_id, 'utc': s['utc'], 'mono': s['mono'],
                                                    'stage_key': s['stage_key'], 'wave': s['wave'], 'gold': s['gold'],
                                                    'heroes': json.dumps(s['heroes'])})
            s['session_id'] = session_id
            tracker.feed(s)
        for run in closed:
            persist_run(self.store, run)
        return tracker

    def old_session(self):
        sid, _ = self.session(0)
        return sid, self.feed(sid, [sample(0, 17, 0), sample(100, 0, 1000), sample(160, 9, 2000)])

    def test_same_process_recent_sample_resumes_from_the_boundary(self):
        self.old_session()
        sid, values = self.session(170)
        rows, partial = open_run_to_resume(self.store, sid, values)
        self.assertFalse(partial)
        self.assertEqual([r['wave'] for r in rows], [0, 9])

    def test_other_process_or_long_gap_does_not_resume(self):
        self.old_session()
        sid, values = self.session(170, pid=8)
        self.assertIsNone(open_run_to_resume(self.store, sid, values))
        sid, values = self.session(160 + 121)
        self.assertIsNone(open_run_to_resume(self.store, sid, values))

    def test_clean_stop_run_is_superseded(self):
        sid, tracker = self.old_session()
        persist_run(self.store, tracker.stop('unobserved_end'))
        self.assertEqual(self.store.one("SELECT count(*) n FROM runs WHERE end_reason = 'unobserved_end'")['n'], 1)
        new_sid, values = self.session(170)
        rows, partial = open_run_to_resume(self.store, new_sid, values)
        self.assertEqual([r['wave'] for r in rows], [0, 9])
        self.assertEqual(self.store.one("SELECT count(*) n FROM runs WHERE end_reason = 'unobserved_end'")['n'], 0)

    def test_second_restart_after_a_resumed_run(self):
        # A: run closes at t=100, next one open. B resumes it; it closes at t=200 (keeps session A's id). C restarts.
        a_sid, _ = self.old_session()
        b_sid, values = self.session(170)
        rows, partial = open_run_to_resume(self.store, b_sid, values)
        closed = []
        tracker = RunTracker(None, closed.append)
        b_samples = [sample(170, 11, 2200), sample(200, 0, 3000), sample(230, 6, 3500)]
        for i, s in enumerate(b_samples):
            s['id'] = self.store.insert('samples', {'session_id': b_sid, 'utc': s['utc'], 'mono': s['mono'],
                                                    'stage_key': s['stage_key'], 'wave': s['wave'], 'gold': s['gold'],
                                                    'heroes': json.dumps(s['heroes'])})
            s['session_id'] = b_sid
            if i == 0:
                tracker.resume(rows, s, partial)
            tracker.feed(s)
        for run in closed:
            persist_run(self.store, run)
        self.assertEqual(closed[0]['session_id'], a_sid)  # the continued run belongs to A's first sample
        c_sid, values = self.session(240)
        rows, partial = open_run_to_resume(self.store, c_sid, values)
        self.assertFalse(partial)
        self.assertEqual([r['wave'] for r in rows], [0, 6])  # from the t=200 boundary, not all of B again


class DuplicateTests(unittest.TestCase):
    def test_same_run_from_concurrent_sessions_is_kept_once(self):
        start = T0.isoformat()
        rows = [{'id': 105, 'session_id': 4, 'stage_key': 1309, 'started_utc': start},
                {'id': 106, 'session_id': 5, 'stage_key': 1309, 'started_utc': (T0 + timedelta(seconds=1)).isoformat()},
                {'id': 107, 'session_id': 6, 'stage_key': 1309, 'started_utc': start},
                {'id': 108, 'session_id': 4, 'stage_key': 1309, 'started_utc': (T0 + timedelta(minutes=4)).isoformat()}]
        self.assertEqual([r['id'] for r in unique_runs(rows)], [105, 108])


class LockTests(unittest.TestCase):
    def test_second_process_cannot_collect(self):
        folder = Path(tempfile.mkdtemp())
        code = (f"import os,pathlib,time;from tbh import instance;"
                f"print(instance.acquire(pathlib.Path(r'{folder}')), os.getpid(), flush=True);time.sleep(3)")
        first = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        try:
            ok, pid = first.stdout.readline().split()
            self.assertEqual(ok, 'True')
            self.assertFalse(instance.acquire(folder))
            self.assertEqual(instance.owner(folder), int(pid))
        finally:
            first.wait()
        self.assertTrue(instance.acquire(folder))


if __name__ == '__main__':
    unittest.main()
