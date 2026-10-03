"""Read-only reconciliation ledger. Agreement is evidence, never a correctness certificate.

Save timestamps are compared with the nearest runtime readings, without interpolation,
value-based endpoint selection or prorating runs. Disagreement at an endpoint remains visible.
Only one collector session is used per window; concurrent collectors are never summed.
"""
import bisect
import math
from collections import Counter, defaultdict
from datetime import datetime

from .economy import save_window
from .progress import xp_gained
from .runs import is_act_boss_run, is_first_clear_run
from .run_evidence import reconcile_intervals
from ..save.model import stage_counters

ALIGNMENT_SECONDS = 2.0
MAX_STORED_GAP_SECONDS = 35.0  # collector stores unchanged state at least every 30 s by default
XP_ABS_TOLERANCE = 0.0001


def timestamp(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def known(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _equal(a, b):
    return known(a) and known(b) and math.isclose(a, b, rel_tol=0, abs_tol=XP_ABS_TOLERANCE)


class SampleIndex:
    def __init__(self, samples, max_gap_s=MAX_STORED_GAP_SECONDS):
        self.max_gap_s = max_gap_s
        grouped = defaultdict(list)
        for sample in samples:
            if sample.get('utc') and sample.get('session_id') is not None:
                grouped[sample['session_id']].append(dict(sample, t=timestamp(sample['utc'])))
        self.sessions = {}
        self.by_id = {s['id']: s for s in samples if s.get('id') is not None}
        for session, rows in grouped.items():
            rows.sort(key=lambda r: (r['t'], r.get('id', 0)))
            self.sessions[session] = (rows, [r['t'] for r in rows])

    def marker_rows(self, run, at):
        # Resumed runs retain their starting session. An explicit terminal sample ID
        # can identify the observing session without stitching clocks or guessing by time.
        terminal = self.by_id.get(run.get('last_sample_id'), {})
        session = run.get('session_id')
        if terminal.get('utc') and timestamp(terminal['utc']) == at:
            session = terminal.get('session_id')
        rows, times = self.sessions.get(session, ([], []))
        return rows, times, session

    @staticmethod
    def nearest(rows, times, at):
        i = bisect.bisect_left(times, at)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(rows)]
        j = min(candidates, key=lambda j: (abs(times[j] - at), times[j]), default=None)
        return j if j is not None and abs(times[j] - at) <= ALIGNMENT_SECONDS else None

    def valid_pair(self, a, b):
        elapsed = b['t'] - a['t']
        mono_a, mono_b = a.get('mono'), b.get('mono')
        return (0 < elapsed <= self.max_gap_s and a.get('quality') == b.get('quality') == 'ok'
                and known(mono_a) and known(mono_b) and mono_b > mono_a
                and abs((mono_b - mono_a) - elapsed) <= ALIGNMENT_SECONDS)

    def window(self, start, end):
        choices = []
        for session, (rows, times) in self.sessions.items():
            if not times or times[-1] < start - ALIGNMENT_SECONDS or times[0] > end + ALIGNMENT_SECONDS:
                continue
            left, right = self.nearest(rows, times, start), self.nearest(rows, times, end)
            lo = max(0, bisect.bisect_left(times, start) - 1)
            hi = min(len(rows), bisect.bisect_right(times, end) + 1)
            around = rows[lo:hi]
            covered = sum(max(0.0, min(end, b['t']) - max(start, a['t']))
                          for a, b in zip(around, around[1:])
                          if self.valid_pair(a, b))
            paired = left is not None and right is not None and right > left
            selected = rows[left:right + 1] if paired else []
            gaps = [b['t'] - a['t'] for a, b in zip(selected, selected[1:])]
            continuous = bool(paired and all(self.valid_pair(a, b) for a, b in zip(selected, selected[1:])))
            offsets = [rows[i]['t'] - at if i is not None else None for i, at in ((left, start), (right, end))]
            result = {'session_id': session, 'covered_s': covered, 'rows': selected,
                      'start': rows[left] if left is not None else None,
                      'end': rows[right] if right is not None else None,
                      'start_offset_s': offsets[0], 'end_offset_s': offsets[1],
                      'continuous': continuous, 'max_gap_s': max(gaps, default=None),
                      'sample_count': len(selected) if paired else sum(start <= r['t'] <= end for r in around)}
            choices.append(((paired, covered, -sum(abs(o) for o in offsets if o is not None)), result))
        return max(choices, key=lambda x: x[0])[1] if choices else {
            'session_id': None, 'covered_s': 0.0, 'rows': [], 'start': None, 'end': None,
            'start_offset_s': None, 'end_offset_s': None, 'continuous': False,
            'max_gap_s': None, 'sample_count': 0}


def _metric_status(value, runtime, residual, continuity, endpoints_match):
    if value is None or runtime is None:
        return 'missing'
    if not continuity:
        return 'incomplete'
    if not endpoints_match:
        return 'boundary_uncertain'
    return 'matched' if _equal(residual, 0) else 'discrepancy'


def gold_check(window, a, b, observation):
    rows = observation['rows']
    values = [r.get('gold') for r in rows]
    rises = falls = net = None
    if len(values) >= 2 and all(known(v) for v in values):
        rises = sum(max(y - x, 0) for x, y in zip(values, values[1:]))
        falls = sum(max(x - y, 0) for x, y in zip(values, values[1:]))
        net = values[-1] - values[0]
    earned = window['gold_earned_total']
    residual = earned - rises if known(earned) and rises is not None else None
    start = (observation['start'] or {}).get('gold')
    end = (observation['end'] or {}).get('gold')
    endpoint_match = known(start) and known(end) and start == a['gold'] and end == b['gold']
    status = _metric_status(earned, rises, residual, observation['continuous'], endpoint_match)
    if known(earned) and earned < 0:
        status = 'counter_reset'
    return {'status': status, 'save_earned': earned, 'sample_rises': rises, 'sample_falls': falls,
            'residual': residual, 'save_net': window['balance_delta'], 'sample_net': net,
            'implied_spend': window['implied_spend'], 'endpoints_match': endpoint_match,
            'spending_overlap_possible': bool(status == 'discrepancy' and residual > 0 and falls > 0),
            'start_balance_residual': a['gold'] - start if known(a['gold']) and known(start) else None,
            'end_balance_residual': b['gold'] - end if known(b['gold']) and known(end) else None,
            'sources': {k: window[k] for k in ('gold_monster', 'gold_alchemy', 'gold_offline')},
            'note': 'Residual = save gross income − sampled balance rises. Hidden simultaneous income/spending '
                    'is possible; implied spending is not an independently observed expense.'}


def _heroes(row):
    return {str(h['hero_key']): h for h in (row or {}).get('heroes') or []}


def xp_checks(a, b, observation, thresholds):
    old, new = _heroes(a), _heroes(b)
    result = []
    # Include heroes at either endpoint. Joining/leaving never silently disappears from the ledger.
    keys = sorted(set(map(str, a['party'])) | set(map(str, b['party'])))
    states = [_heroes(r) for r in observation['rows']]
    for key in keys:
        ha, hb = old.get(key, {}), new.get(key, {})
        value, kind = xp_gained(ha.get('level'), ha.get('xp'), hb.get('level'), hb.get('xp'), thresholds)
        gain, runtime_kinds = (0.0 if len(states) >= 2 else None), Counter()
        for sa, sb in zip(states, states[1:]):
            x, y = sa.get(key, {}), sb.get(key, {})
            delta, why = xp_gained(x.get('level'), x.get('xp'), y.get('level'), y.get('xp'), thresholds)
            runtime_kinds[why] += 1
            if delta is None or not known(delta):
                gain = None
            elif gain is not None:
                gain += delta
        start = _heroes(observation['start']).get(key, {})
        end = _heroes(observation['end']).get(key, {})
        aligned = (ha.get('level') == start.get('level') and hb.get('level') == end.get('level')
                   and _equal(ha.get('xp'), start.get('xp')) and _equal(hb.get('xp'), end.get('xp')))
        residual = value - gain if known(value) and gain is not None else None
        status = _metric_status(value, gain, residual, observation['continuous'], aligned)
        if kind in ('level_decreased', 'xp_decreased'):
            status = 'counter_reset'
        result.append({'hero_key': int(key), 'status': status, 'save_gain': value, 'sample_gain': gain,
                       'residual': residual, 'save_kind': kind, 'runtime_kinds': dict(runtime_kinds),
                       'level_before': ha.get('level'), 'level_after': hb.get('level'),
                       'endpoints_match': aligned,
                       'start_xp_residual': ha['xp'] - start['xp'] if known(ha.get('xp')) and known(start.get('xp')) else None,
                       'end_xp_residual': hb['xp'] - end['xp'] if known(hb.get('xp')) and known(end.get('xp')) else None})
    return result


def run_check(a, b, events, continuous):
    ca, cb = stage_counters(a), stage_counters(b)
    expected = {str(k): {f: cb.get(k, {}).get(f, 0) - ca.get(k, {}).get(f, 0) for f in ('clears', 'fails')}
                for k in set(ca) | set(cb)}
    expected = {k: v for k, v in expected.items() if any(v.values())}
    observed = {}
    for r in events:
        row = observed.setdefault(str(r['stage_key']), {'clears': 0, 'fails': 0})
        # Deliberately ignore outcome/outcome_source: reconciliation already copied save counters there.
        row['clears' if r.get('final_utc') else 'fails'] += 1
    keys = set(expected) | set(observed)
    residual = {k: {f: expected.get(k, {}).get(f, 0) - observed.get(k, {}).get(f, 0)
                    for f in ('clears', 'fails')} for k in sorted(keys)}
    reset = any(v < 0 for row in expected.values() for v in row.values())
    differs = any(v for row in residual.values() for v in row.values())
    status = ('counter_reset' if reset else 'missing' if not ca and not cb else
              'incomplete' if not continuous else 'counts_differ' if differs else 'counts_agree')
    return {'status': status, 'save': expected, 'runtime': observed, 'residual': residual,
            'run_ids': [r['id'] for r in events], 'partial_starts': sum(bool(r.get('partial_start')) for r in events),
            'first_clear_adjustments': 0,
            'note': 'Runtime markers/inferred resets, not the save-confirmed outcome field. '
                    'Marker timestamps are unshifted. Boundary brackets indicate possible timing ambiguity, '
                    'not a confirmed assignment or an explanation of every difference.'}


def boundary_markers(index, runs, save_times):
    """Bracket observed markers with preceding readings, without fitting save counters.

    A bracket is evidence of timing uncertainty only. An unmarked stage departure is
    also retained, but cannot independently establish either a clear or a failure.
    """
    result = defaultdict(list)
    for run in runs:
        at = timestamp(run.get('final_utc') or run.get('ended_utc'))
        if at is None:
            continue
        rows, times, marker_session = index.marker_rows(run, at)
        pos = bisect.bisect_left(times, at)
        if pos == 0 or pos >= len(rows) or times[pos] != at:
            continue
        before, after = rows[pos - 1], rows[pos]
        if not index.valid_pair(before, after) or before.get('stage_key') != run['stage_key']:
            continue
        if run.get('final_utc'):
            kind = 'clear_marker'
        elif run.get('end_reason') == 'wave_reset':
            kind = 'inferred_failure_reset'
        elif run.get('end_reason') == 'stage_change':
            kind = 'unmarked_stage_departure'
        else:
            continue
        first = bisect.bisect_right(save_times, before['t'])
        last = bisect.bisect_right(save_times, after['t'])
        for boundary in save_times[first:last]:
            result[boundary].append({'run_id': run['id'], 'stage_key': run['stage_key'], 'kind': kind,
                                     'before_utc': before['utc'], 'observed_utc': after['utc'],
                                     'interval_s': after['t'] - before['t'],
                                     'session_id': marker_session})
    return result


def ledger(snapshots, samples, runs, thresholds, max_gap_s=MAX_STORED_GAP_SECONDS):
    """All windows and aggregates. Runs must already be deduplicated by the view layer."""
    missing_times = sum(not s.get('last_saved_utc') for s in snapshots)
    snapshots = sorted((s for s in snapshots if s.get('last_saved_utc')), key=lambda s: timestamp(s['last_saved_utc']))
    index = SampleIndex(samples, max_gap_s)
    boundaries = boundary_markers(index, runs, [timestamp(s['last_saved_utc']) for s in snapshots])
    events = sorted(((timestamp(r.get('final_utc') or r['ended_utc']), r) for r in runs if r.get('ended_utc') and
                     (r.get('end_reason') == 'wave_reset' or is_first_clear_run(r) or is_act_boss_run(r))),
                    key=lambda x: x[0])
    moments = [t for t, _ in events]
    windows, invalid_intervals = [], 0
    for a, b in zip(snapshots, snapshots[1:]):
        start, end = timestamp(a['last_saved_utc']), timestamp(b['last_saved_utc'])
        if end <= start:
            invalid_intervals += 1
            continue
        w = save_window(a, b, thresholds)
        obs = index.window(start, end)
        selected = [r for _, r in events[bisect.bisect_right(moments, start):bisect.bisect_right(moments, end)]]
        gold = gold_check(w, a, b, obs)
        xp = xp_checks(a, b, obs, thresholds)
        run = run_check(a, b, selected, obs['continuous'])
        run['boundary_markers'] = {'start': boundaries.get(start, []), 'end': boundaries.get(end, [])}
        flags = list(dict.fromkeys(w['reasons']))
        if not obs['continuous']:
            flags.append('runtime endpoints, continuity or quality incomplete')
        if obs['rows'] and len({r.get('stage_key') for r in obs['rows']}) > 1:
            flags.append('stage changed in runtime samples')
        if any(v.get('save_kind') == 'level_up' for v in xp):
            flags.append('XP crosses a level; threshold model used on both sources')
        windows.append({'save_from': a.get('save_id'), 'save_to': b.get('save_id'),
                        'start_utc': w['start_utc'], 'end_utc': w['end_utc'], 'seconds': end - start,
                        'stage': w['stage'], 'stage_rate_eligible': w['valid'], 'flags': flags,
                        'coverage': {k: v for k, v in obs.items() if k not in ('rows', 'start', 'end')},
                        'gold': gold, 'xp': xp, 'runs': run})
    interval_statuses = reconcile_intervals(windows, runs, index)
    seconds = sum(w['seconds'] for w in windows)
    covered = sum(w['coverage']['covered_s'] for w in windows)
    gold_counts = Counter(w['gold']['status'] for w in windows)
    xp_counts = Counter(h['status'] for w in windows for h in w['xp'])
    run_counts = Counter(w['runs']['status'] for w in windows)
    comparable = [w for w in windows if w['gold']['status'] in ('matched', 'discrepancy')]
    comparable_seconds = sum(w['seconds'] for w in comparable)
    totals = {key: sum(w['gold'][key] for w in comparable) if comparable else None
              for key in ('save_earned', 'sample_rises', 'sample_falls', 'residual')}
    totals['absolute_residual'] = sum(abs(w['gold']['residual']) for w in comparable) if comparable else None
    return {'windows': windows, 'summary': {'windows': len(windows), 'seconds': seconds, 'covered_s': covered,
            'snapshots_without_time': missing_times, 'nonpositive_intervals': invalid_intervals,
            'coverage_fraction': covered / seconds if seconds else None,
            'gold_statuses': dict(gold_counts), 'xp_statuses': dict(xp_counts), 'run_statuses': dict(run_counts),
            'run_interval_statuses': interval_statuses,
            'run_windows_with_boundary_markers': sum(any(w['runs']['boundary_markers'].values()) for w in windows),
            'gold_comparable_seconds': comparable_seconds, 'gold_totals_on_comparable_windows': totals,
            'gold_differences_with_balance_drops': sum(w['gold']['spending_overlap_possible'] for w in windows),
            'level_up_windows': sum(any(h['save_kind'] == 'level_up' for h in w['xp']) for w in windows),
            'stage_rate_eligible_windows': sum(w['stage_rate_eligible'] for w in windows),
            'exclusions': dict(Counter(f for w in windows for f in w['flags']))},
            'method': {'alignment_seconds': ALIGNMENT_SECONDS, 'max_stored_gap_seconds': max_gap_s,
                       'xp_absolute_tolerance': XP_ABS_TOLERANCE,
                       'note': 'Nearest timestamp endpoints, never chosen to make values agree. No interpolation or '
                               'prorating. Accepted intervals require agreeing wall/monotonic clocks and known build/quality. '
                               'Coverage is time between acceptable stored readings, not continuous event capture. '
                               'XP agreement uses the same threshold model on both sources; it does not independently validate level-ups. '
                               'Run counts are assessed over the full save window; partial runs are not prorated for gold/XP.'}}
