"""Segment runtime samples into runs.

A run is one attempt at a stage. Boundaries observed on 1.2.8:
  * wave index decreases at the same stage (e.g. boss wave 14 -> 0): previous attempt ended;
  * stage key changes (an act boss fight is its own stage key between two runs of the stage played);
  * the observation stops (detach or a long gap): the run is closed as `unobserved_end`.
The first run of an observation session is always `partial_start` — its start was not seen.
Waves and BATTLE/REORGANIZATION transitions are *not* run boundaries.

Gold per run is an estimate from 1-s balance samples: positive steps are counted as
income and negative steps as spending. Simultaneous income and spending between two
samples cannot be separated; save counters are the exact reference (see economy.py).
"""
from datetime import datetime

from .progress import party_signature, xp_gained

ACT_BOSS_TYPES = ('ACTBOSS', 'CONTAMINACTBOSS')
GAP_WARN_SECONDS = 10.0
GAP_CLOSE_SECONDS = 120.0
SPEND_EVENT_MIN = 1


def _last_cleared(sample):
    """Live samples carry `last_cleared_stage`; rows replayed from the database `last_cleared`."""
    return sample.get('last_cleared_stage', sample.get('last_cleared'))


def is_first_clear_run(run):
    """A run that ended by advancing to another stage with the last-cleared marker set."""
    return run.get('end_reason') == 'stage_change' and bool(run.get('final_utc')) and not is_act_boss_run(run)


def is_act_boss_run(run):
    """An act boss attempt seen from its start, ended by returning to a stage."""
    return run.get('wave_amount') == 0 and run.get('end_reason') == 'stage_change' and not run.get('partial_start')


class RunTracker:
    def __init__(self, catalog, on_run_end=None, on_event=None):
        self.catalog = catalog
        self.thresholds = catalog.level_thresholds if catalog else {}
        self.on_run_end = on_run_end or (lambda run: None)
        self.on_event = on_event or (lambda kind, utc, payload, run: None)
        self.run = None
        self.prev = None
        self.first_run = True

    # --- helpers -------------------------------------------------------------
    def _wave_amount(self, stage_key):
        row = self.catalog.stages.get(str(stage_key)) if self.catalog else None
        if row and row.get('STAGETYPE') in ACT_BOSS_TYPES:
            return 0  # observed 2026-10-01 (1310): wave 0 during the fight, 1 once the boss is dead
        return int(row['WaveAmount']) if row and row['WaveAmount'] else None

    def _start(self, sample, partial):
        self.run = {
            'session_id': sample.get('session_id'), 'stage_key': sample['stage_key'],
            'started_utc': sample['utc'], 'start_mono': sample['mono'],
            'ended_utc': None, 'end_mono': None, 'partial_start': partial,
            'max_wave': sample['wave'], 'wave_amount': self._wave_amount(sample['stage_key']), 'final_utc': None,
            'gold_start': sample.get('gold'), 'gold_end': sample.get('gold'),
            'gold_gain_est': 0, 'gold_spend_est': 0, 'gold_unknown_steps': 0,
            'xp': {str(h['hero_key']): {'gain': 0.0, 'level_start': h.get('level'), 'level_end': h.get('level'),
                                         'complete': True, 'level_ups': 0}
                   for h in sample.get('heroes') or []},
            'party': party_signature(sample.get('heroes') or []),
            'samples': 1, 'gaps': 0, 'anomalies': [],
            'first_sample_id': sample.get('id'), 'last_sample_id': sample.get('id'),
        }
        self.first_run = False

    def _close(self, reason, end_sample):
        run = self.run
        if not run:
            return None
        run['ended_utc'] = end_sample['utc']
        run['end_mono'] = end_sample['mono']
        run['duration_s'] = round(run['end_mono'] - run['start_mono'], 3)
        run['end_reason'] = reason
        run['reached_final'] = run['final_utc'] is not None
        run['outcome'], run['outcome_source'] = self._heuristic_outcome(run)
        self.run = None
        self.on_run_end(run)
        return run

    @staticmethod
    def _heuristic_outcome(run):
        """Runtime-only guess, replaced later by save reconciliation when counts allow it."""
        if is_act_boss_run(run):
            return ('clear' if run['reached_final'] else 'fail'), 'runtime-heuristic'
        if is_first_clear_run(run):
            return 'clear', 'runtime-heuristic'
        if run['end_reason'] != 'wave_reset':
            return 'unknown', 'none'
        if run['reached_final']:
            return 'clear', 'runtime-heuristic'
        return 'fail', 'runtime-heuristic'

    def _accumulate(self, prev, sample):
        run = self.run
        run['samples'] += 1
        run['last_sample_id'] = sample.get('id')
        if sample['wave'] is not None and sample['stage_key'] == run['stage_key']:
            run['max_wave'] = sample['wave'] if run['max_wave'] is None else max(run['max_wave'], sample['wave'])
            # Observed on 1.2.8: wave WaveAmount+1 appears right after the boss reward (stage done).
            amount = run['wave_amount']
            if amount is not None and sample['wave'] >= amount + 1 and run['final_utc'] is None:
                run['final_utc'] = sample['utc']
        # Observed 2026-10-01 (2201 -> 2202): clearing a stage for the first time advances to the next
        # stage without ever showing wave WaveAmount+1; the last-cleared stage switches to this stage
        # in that same frame. A replay of a cleared stage leaves it unchanged (no marker, not a fail).
        cleared, cleared_before = _last_cleared(sample), _last_cleared(prev)
        if cleared == run['stage_key'] and cleared_before not in (None, cleared) and run['final_utc'] is None:
            run['final_utc'] = sample['utc']
        if prev.get('gold') is not None and sample.get('gold') is not None:
            delta = sample['gold'] - prev['gold']
            if delta > 0:
                run['gold_gain_est'] += delta
            elif delta < 0:
                run['gold_spend_est'] += -delta
                if -delta >= SPEND_EVENT_MIN:
                    self.on_event('gold_spend', sample['utc'], {'amount': -delta, 'stage_key': sample['stage_key'],
                                                                 'wave': sample['wave'], 'balance_after': sample['gold']}, run)
        else:
            run['gold_unknown_steps'] += 1
        if sample.get('gold') is not None:
            run['gold_end'] = sample['gold']
        before = {str(h['hero_key']): h for h in prev.get('heroes') or []}
        for hero in sample.get('heroes') or []:
            key = str(hero['hero_key'])
            entry = run['xp'].get(key)
            if entry is None:
                run['xp'][key] = entry = {'gain': 0.0, 'level_start': hero.get('level'), 'level_end': hero.get('level'),
                                          'complete': False, 'level_ups': 0}
                run['anomalies'].append({'kind': 'hero_joined', 'hero': key, 'utc': sample['utc']})
            old = before.get(key)
            if not old:
                entry['complete'] = False
                continue
            gain, kind = xp_gained(old.get('level'), old.get('xp'), hero.get('level'), hero.get('xp'), self.thresholds)
            if gain is None:
                entry['complete'] = False
                run['anomalies'].append({'kind': kind, 'hero': key, 'utc': sample['utc']})
                continue
            entry['gain'] += gain
            entry['level_end'] = hero.get('level')
            if kind == 'level_up':
                entry['level_ups'] += hero['level'] - old['level']
                self.on_event('level_up', sample['utc'], {'hero_key': hero['hero_key'], 'from': old['level'],
                                                           'to': hero['level'], 'xp_counted': gain}, run)
        missing = set(before) - {str(h['hero_key']) for h in sample.get('heroes') or []}
        for key in missing:
            if key in run['xp']:
                run['xp'][key]['complete'] = False
            run['anomalies'].append({'kind': 'hero_left', 'hero': key, 'utc': sample['utc']})
        # Preserve the starting party; changed membership makes this run unsuitable for comparisons.
        current_party = party_signature(sample.get('heroes') or [])
        if current_party != run['party']:
            run['anomalies'].append({'kind': 'party_changed', 'utc': sample['utc']})

    # --- public API ------------------------------------------------------------
    def resume(self, samples, now_sample, partial):
        """Continue a run left open by a previous process watching the same game process.

        `samples` are that process's stored samples from the open run's first sample onwards.
        Monotonic clocks are per process, so they are re-based on `now_sample` by wall time;
        the restart itself becomes an ordinary gap between the last stored and the next sample."""
        if not samples:
            return
        now = datetime.fromisoformat(now_sample['utc'])
        rebased = [dict(x, mono=now_sample['mono'] - (now - datetime.fromisoformat(x['utc'])).total_seconds())
                   for x in samples]
        self._start(rebased[0], partial=partial)
        self.prev = rebased[0]
        for sample in rebased[1:]:
            self.feed(sample)

    def feed(self, sample):
        """Feed one sample (dict with utc, mono, stage_key, wave, gold, heroes). Returns closed runs."""
        closed = []
        if sample.get('stage_key') is None or sample.get('wave') is None:
            if self.run:
                self.run['gaps'] += 1
            return closed  # unreadable frame: keep the run open, count as a gap
        prev = self.prev
        if self.run is None:
            self._start(sample, partial=True)
            self.prev = sample
            return closed
        elapsed = sample['mono'] - prev['mono']
        if elapsed > GAP_CLOSE_SECONDS:
            closed.append(self._close('observation_gap', prev))
            self._start(sample, partial=True)
            self.prev = sample
            return closed
        if elapsed > GAP_WARN_SECONDS:
            self.run['gaps'] += 1
        # Changes between prev and sample happened before the boundary was observed.
        self._accumulate(prev, sample)
        if sample['stage_key'] != self.run['stage_key']:
            closed.append(self._close('stage_change', sample))
            self._start(sample, partial=False)
        elif prev['wave'] is not None and sample['wave'] < prev['wave']:
            closed.append(self._close('wave_reset', sample))
            self._start(sample, partial=False)
        self.prev = sample
        return closed

    def stop(self, reason='unobserved_end'):
        """Close the open run when observation ends (detach, shutdown)."""
        if self.run and self.prev:
            run = self._close(reason, self.prev)
            self.prev = None
            return run
        self.prev = None
        return None
