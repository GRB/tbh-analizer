"""Unit tests on synthetic data only: no saves, dumps or personal data are versioned."""
import json
import struct
import tempfile
import unittest
from pathlib import Path

from tbh.analysis.economy import save_window, stage_rates
from tbh.analysis.progress import xp_gained
from tbh.analysis.reconcile import reconcile
from tbh.analysis.runs import RunTracker
from tbh.runtime.layout import field_map
from tbh.runtime.reader import decode_double, decode_int
from tbh.save.es3 import decrypt, encrypt, load_save, SaveReadError
from tbh.save.model import normalize, stage_counters
from tbh.store import Store

THRESHOLDS = {1: 30.0, 2: 150.0, 3: 500.0, 19: 2042940.0, 20: 3064410.0}


class FakeCatalog:
    level_thresholds = THRESHOLDS
    stages = {'1109': {'WaveAmount': '13'}, '1110': {'WaveAmount': '13'}}


def encode_int(value, key, bits=32):
    mask = (1 << bits) - 1
    return (((value & mask) ^ key) + key) & mask


def encode_double(value, key):
    permuted = struct.unpack('<Q', struct.pack('<d', value))[0] ^ key
    raw = struct.pack('<Q', permuted)
    order = (1, 0, 2, 3, 7, 4, 6, 5)
    hidden = bytearray(8)
    for target, source in enumerate(order):
        hidden[source] = raw[target]
    return int.from_bytes(hidden, 'little')


class ObscuredTests(unittest.TestCase):
    def test_int_roundtrip(self):
        for value in (0, 1, 1109, -5, 2**31 - 1):
            for key in (0, 12345, 0xDEADBEEF):
                self.assertEqual(decode_int(encode_int(value, key), key), value)

    def test_long_roundtrip(self):
        key = 0x1234_5678_9ABC_DEF0
        self.assertEqual(decode_int(encode_int(90965, key, 64), key, 64), 90965)

    def test_double_roundtrip(self):
        key = 0x0F0E_0D0C_0B0A_0908
        for value in (0.0, 1365240.6468133926, 64524.69954395294):
            self.assertEqual(decode_double(encode_double(value, key), key), value)


class SaveTests(unittest.TestCase):
    def player(self, **common):
        base = {'version': '1.2.8', 'lastSavedTime': 639263614230359642, 'playTime': 100.0,
                'currentStageKey': 1109, 'arrangedHeroKey': [401, -1, 201], 'maxCompletedStage': 1210}
        base.update(common)
        return {
            'commonSaveData': base,
            'heroSaveDatas': [{'heroKey': 401, 'HeroLevel': 20, 'HeroExp': 10.5, 'equippedItemIds': [2**60 + 1, 0]}],
            'currenySaveDatas': [{'Key': 100001, 'Quantity': 500}],
            'itemSaveDatas': [{'ItemKey': 120003, 'UniqueId': 2**60 + 1}],
            'stashSaveDatas': [{'Index': 0, 'ItemUniqueId': 2**60 + 1, 'IsUnLock': True, 'Quantity': 4},
                               {'Index': 1, 'ItemUniqueId': 2**60 + 1, 'IsUnLock': True, 'Quantity': 3}],
            'aggregateSaveDatas': [{'Type': 13, 'SubKey': 1109, 'Value': 7, 'Content': 0},
                                   {'Type': 13, 'SubKey': 1109, 'Value': 7, 'Content': 1},
                                   {'Type': 13, 'SubKey': 0, 'Value': 9, 'Content': 0}],
            'newFieldFromFutureVersion': 1,
        }

    def test_es3_roundtrip_and_wrong_password(self):
        document = json.dumps({'PlayerSaveData': {'__type': 'string', 'value': json.dumps(self.player())}}).encode()
        blob = encrypt(document, 'secret', iv=bytes(range(16)))
        self.assertEqual(decrypt(blob, 'secret'), document)
        # CBC padding alone cannot authenticate a password (random data can have valid padding).
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'save.es3'
            path.write_bytes(blob)
            with self.assertRaises(SaveReadError):
                load_save(path, 'wrong')

    def test_big_ids_are_strings_and_quantities_per_slot(self):
        snap = normalize(json.dumps(self.player()))
        uid = str(2**60 + 1)
        self.assertIn(uid, snap['items'])
        self.assertEqual(snap['heroes'][0]['equipped'], [uid, None])
        self.assertEqual(sum(s['quantity'] for s in snap['stash'] if s['unique_id'] == uid), 7)
        self.assertEqual(snap['party'], [401, 201])
        self.assertEqual(snap['unmapped_keys'], ['newFieldFromFutureVersion'])

    def test_stage_counters_use_content_zero_and_skip_totals(self):
        snap = normalize(json.dumps(self.player()))
        self.assertEqual(stage_counters(snap), {1109: {'clears': 7, 'fails': 0}})


class ProgressTests(unittest.TestCase):
    def test_same_level(self):
        self.assertEqual(xp_gained(20, 100.0, 20, 150.0, THRESHOLDS), (50.0, 'same_level'))

    def test_level_up_counts_remaining_threshold(self):
        gain, kind = xp_gained(1, 20.0, 3, 10.0, THRESHOLDS)
        self.assertEqual(kind, 'level_up')
        self.assertEqual(gain, (30 - 20) + 150 + 10)

    def test_decrease_is_not_negative_xp(self):
        self.assertEqual(xp_gained(20, 150.0, 20, 100.0, THRESHOLDS), (None, 'xp_decreased'))


def sample(t, wave, gold=1000, stage=1109, xp=0.0, level=20):
    return {'utc': f'2026-09-30T10:{int(t) // 60:02d}:{int(t) % 60:02d}+00:00', 'mono': float(t),
            'stage_key': stage, 'wave': wave, 'gold': gold,
            'heroes': [{'hero_key': 401, 'level': level, 'xp': xp}]}


class RunTrackerTests(unittest.TestCase):
    def feed(self, samples):
        closed, events = [], []
        tracker = RunTracker(FakeCatalog(), closed.append, lambda k, u, p, r: events.append((k, p)))
        for s in samples:
            tracker.feed(s)
        return tracker, closed, events

    def test_waves_are_not_runs_and_reset_closes_run(self):
        samples = [sample(0, 5)] + [sample(t, w, 1000 + 10 * t, xp=float(t)) for t, w in
                                    [(10, 6), (20, 13), (30, 14), (32, 0), (40, 1), (50, 14), (52, 0)]]
        tracker, closed, _ = self.feed(samples)
        self.assertEqual(len(closed), 2)
        first, second = closed
        self.assertTrue(first['partial_start'])
        self.assertFalse(second['partial_start'])
        self.assertTrue(second['reached_final'])
        self.assertEqual(second['outcome'], 'clear')
        self.assertEqual(second['duration_s'], 20.0)
        self.assertEqual(second['xp']['401']['gain'], 20.0)
        self.assertIsNotNone(tracker.run)

    def test_spending_is_separate_from_income(self):
        samples = [sample(0, 0, 1000), sample(1, 1, 1200), sample(2, 1, 150), sample(3, 2, 300)]
        _, closed, events = self.feed(samples + [sample(4, 0, 300)])
        run = closed[-1]
        self.assertEqual(run['gold_gain_est'], 350)
        self.assertEqual(run['gold_spend_est'], 1050)
        self.assertEqual(events[0][0], 'gold_spend')

    def test_reset_before_final_wave_is_fail_heuristic(self):
        _, closed, _ = self.feed([sample(0, 0), sample(5, 3), sample(9, 0), sample(15, 4), sample(20, 0)])
        self.assertEqual(closed[-1]['outcome'], 'fail')
        self.assertEqual(closed[-1]['outcome_source'], 'runtime-heuristic')

    def test_stage_change_and_level_up(self):
        samples = [sample(0, 0, xp=3000000.0, level=20), sample(1, 1, xp=3064400.0, level=20),
                   sample(2, 2, xp=5.0, level=21), sample(3, 0, stage=1110, xp=6.0, level=21)]
        _, closed, events = self.feed(samples)
        self.assertEqual(closed[0]['end_reason'], 'stage_change')
        self.assertAlmostEqual(closed[0]['xp']['401']['gain'], 64400.0 + 10.0 + 5.0 + 1.0)
        self.assertEqual([e[0] for e in events], ['level_up'])

    def test_first_clear_advances_stage_with_last_cleared_marker(self):
        # Live frames use last_cleared_stage, database rows last_cleared: both must work.
        for field in ('last_cleared_stage', 'last_cleared'):
            mark = lambda s, cleared: dict(s, **{field: cleared})
            samples = [mark(sample(0, 0), 1108), mark(sample(5, 13), 1108),
                       mark(sample(9, 0, stage=1110), 1109), mark(sample(20, 13, stage=1110), 1109),
                       mark(sample(25, 0, stage=1109), 1109)]   # left 1110 without clearing it
            _, closed, _ = self.feed(samples)
            self.assertEqual([(r['end_reason'], r['outcome']) for r in closed],
                             [('stage_change', 'clear'), ('stage_change', 'unknown')])
            self.assertTrue(closed[0]['reached_final'])

    def test_long_gap_closes_run_as_unobserved(self):
        _, closed, _ = self.feed([sample(0, 0), sample(5, 1), sample(500, 3)])
        self.assertEqual(closed[0]['end_reason'], 'observation_gap')
        self.assertEqual(closed[0]['outcome'], 'unknown')


def snap(utc, clears, fails=0, gold_earned=0, gold=0, stage=1109, play=0.0, xp=0.0):
    return {'last_saved_utc': utc, 'play_time': play, 'current_stage': stage, 'party': [401], 'gold': gold,
            'heroes': [{'hero_key': 401, 'level': 20, 'xp': xp}], 'max_completed_stage': 1210,
            'aggregates': [{'type': 13, 'subkey': stage, 'content': 0, 'value': clears},
                           {'type': 14, 'subkey': stage, 'content': 0, 'value': fails},
                           {'type': 2, 'subkey': 0, 'content': 0, 'value': gold_earned},
                           {'type': 2, 'subkey': 1, 'content': 0, 'value': gold_earned}]}


class ReconcileTests(unittest.TestCase):
    def test_counts_confirm_outcomes(self):
        saves = [snap('2026-09-30T10:00:00+00:00', 10), snap('2026-09-30T10:02:00+00:00', 11, 1)]
        runs = [{'id': 1, 'stage_key': 1109, 'end_reason': 'wave_reset', 'final_utc': '2026-09-30T10:00:50+00:00',
                 'ended_utc': '2026-09-30T10:00:53+00:00'},
                {'id': 2, 'stage_key': 1109, 'end_reason': 'wave_reset', 'final_utc': None,
                 'ended_utc': '2026-09-30T10:01:40+00:00'}]
        self.assertEqual(reconcile(runs, saves), {1: ('clear', 'save-counters+marker'), 2: ('fail', 'save-counters+marker')})

    def test_first_clear_saved_just_before_the_marker_sample(self):
        saves = [snap('2026-09-30T10:00:00+00:00', 0, stage=1110), snap('2026-09-30T10:04:08+00:00', 1, stage=1110)]
        runs = [{'id': 1, 'stage_key': 1110, 'end_reason': 'stage_change', 'wave_amount': 13,
                 'final_utc': '2026-09-30T10:04:09+00:00', 'ended_utc': '2026-09-30T10:04:09+00:00'}]
        self.assertEqual(reconcile(runs, saves), {1: ('clear', 'save-counters')})

    def test_mismatch_leaves_runs_undecided(self):
        saves = [snap('2026-09-30T10:00:00+00:00', 10), snap('2026-09-30T10:02:00+00:00', 12)]
        runs = [{'id': 1, 'stage_key': 1109, 'end_reason': 'wave_reset', 'final_utc': '2026-09-30T10:00:50+00:00'}]
        self.assertEqual(reconcile(runs, saves), {})


class EconomyTests(unittest.TestCase):
    def test_window_separates_spending_and_rates(self):
        a = snap('2026-09-30T10:00:00+00:00', 10, gold_earned=1000, gold=60000, play=0, xp=100.0)
        b = snap('2026-09-30T10:05:00+00:00', 13, gold_earned=4000, gold=13000, play=300, xp=400.0)
        w = save_window(a, b, THRESHOLDS)
        self.assertTrue(w['valid'])
        self.assertEqual(w['gold_earned_total'], 3000)
        self.assertEqual(w['balance_delta'], -47000)
        self.assertEqual(w['implied_spend'], 50000)
        rates = stage_rates([w])[0]
        self.assertAlmostEqual(rates['gold_monster_per_h'], 36000)
        self.assertAlmostEqual(rates['xp_per_h']['401'], 3600)

    def test_offline_window_is_invalid(self):
        a = snap('2026-09-30T10:00:00+00:00', 10, play=0)
        b = snap('2026-09-30T12:00:00+00:00', 10, play=10)
        self.assertFalse(save_window(a, b, THRESHOLDS)['valid'])


class LayoutTests(unittest.TestCase):
    def test_field_map_parses_dump_class(self):
        dump = ('public static class wh.wb // TypeDefIndex: 983\n{\n\t// Fields\n'
                '\tprivate static ObscuredInt bgpw; // 0x80\n\tprivate static ObscuredInt bgpx; // 0x90\n\n'
                '\t// Properties\n\tpublic static int x { get; }\n}\n')
        self.assertEqual(field_map(dump, 'wh.wb'), {'bgpw': 0x80, 'bgpx': 0x90})


if __name__ == '__main__':
    unittest.main()
