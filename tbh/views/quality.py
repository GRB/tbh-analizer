"""Evidence quality API, sourced only from stored observations; read-only."""
import json
import math
from datetime import datetime, timedelta, timezone

from ..analysis.quality import MAX_STORED_GAP_SECONDS, SampleIndex, ledger
from ..analysis.xp_evidence import level_up_evidence
from ..analysis.spending import rune_spending
from ..analysis.farming import farming_report
from ..snapshots import light_snapshot
from .analytics import unique_runs


def quality_report(store, catalog, hours=24, offset=0, limit=50, status='all', now=None, export_all=False):
    if not math.isfinite(hours) or not 0 < hours <= 168:
        raise ValueError('hours must be greater than 0 and at most 168')
    if not 0 <= offset or not 1 <= limit <= 200:
        raise ValueError('offset must be nonnegative; limit must be between 1 and 200')
    if status not in ('all', 'attention', 'matched', 'discrepancy', 'boundary_uncertain', 'incomplete', 'missing', 'counter_reset'):
        raise ValueError('unknown quality filter')
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    # Fixed high-water marks keep appends from mixing the read scope. Nothing writes historical rows.
    marks = {t: (store.one(f'SELECT MAX(id) AS n FROM {t}') or {}).get('n') or 0 for t in ('saves', 'samples', 'runs')}
    ids = store.query('SELECT id FROM saves WHERE last_saved_utc >= ? AND last_saved_utc <= ? '
                      'AND id <= ? ORDER BY last_saved_utc, id', (since, now.isoformat(), marks['saves']))
    snapshots = [light_snapshot(store, r['id']) for r in ids]
    samples, raw_runs, sessions = [], [], {}
    if snapshots:
        first = snapshots[0]['last_saved_utc']
        last = snapshots[-1]['last_saved_utc']
        lo = (datetime.fromisoformat(first) - timedelta(seconds=MAX_STORED_GAP_SECONDS)).isoformat()
        hi = min(now, datetime.fromisoformat(last) + timedelta(seconds=MAX_STORED_GAP_SECONDS)).isoformat()
        samples = store.query('SELECT id, session_id, utc, mono, stage_key, gold, heroes, quality FROM samples '
                              'WHERE utc >= ? AND utc <= ? AND id <= ? ORDER BY utc, id', (lo, hi, marks['samples']))
        sessions = {r['id']: r for r in store.query('SELECT id, build_id, game_assembly_sha256 FROM sessions')}
        for s in samples:
            s['heroes'] = json.loads(s['heroes'] or '[]')
            if str((sessions.get(s['session_id']) or {}).get('build_id')) != str(catalog.build_id):
                s['quality'] = 'incompatible_or_unknown_build'
        # Include later observed markers to expose save-boundary uncertainty, never shift their times.
        run_end = hi
        raw_runs = store.query('SELECT * FROM runs WHERE ended_utc >= ? AND started_utc <= ? '
                               'AND id <= ? ORDER BY id', (first, run_end, marks['runs']))
    runs = unique_runs(raw_runs)
    report = ledger(snapshots, samples, runs, catalog.level_thresholds)
    by_id = {s['save_id']: s for s in snapshots}
    for w in report['windows']:
        w['gold']['rune_spending'] = rune_spending(by_id[w['save_from']], by_id[w['save_to']], catalog,
                                                w['gold']['implied_spend'])
    report['farming'] = farming_report(snapshots, report['windows'], catalog.level_thresholds, runs)
    report['xp_validation'] = level_up_evidence(SampleIndex(samples), catalog.level_thresholds, catalog.build_id, sessions)
    for w in report['windows']:
        w['stage_label'] = catalog.stage_label(w['stage']) if w['stage'] else None
        for h in w['xp']:
            h['name'] = catalog.hero_name(h['hero_key'])

    def statuses(w):
        values = {w['gold']['status'], w['runs']['status'], *(h['status'] for h in w['xp'])}
        if 'counts_differ' in values:
            values.add('discrepancy')
        return values

    def match(w):
        values = statuses(w)
        agreed = (values <= {'matched', 'counts_agree'} and
                  w['runs']['interval_reconciliation']['status'] in ('unique_correspondence', 'no_events'))
        return status == 'all' or (status == 'attention' and not agreed) or (status == 'matched' and agreed) or (
            status not in ('all', 'attention', 'matched') and status in values)
    filtered = [w for w in reversed(report['windows']) if match(w)]
    if export_all:
        offset, limit = 0, len(filtered)
    report.update({'generated_utc': now.isoformat(), 'hours': hours, 'catalog_build': catalog.build_id,
                   'scope': {'requested_since': since, 'first_save': snapshots[0]['last_saved_utc'] if snapshots else None,
                             'last_save': snapshots[-1]['last_saved_utc'] if snapshots else None,
                             'save_count': len(snapshots), 'sample_count': len(samples), 'run_count': len(runs),
                             'duplicates_ignored': len(raw_runs) - len(runs),
                             'incompatible_samples': sum(s['quality'] == 'incompatible_or_unknown_build' for s in samples),
                             'last_save_age_s': (now - datetime.fromisoformat(snapshots[-1]['last_saved_utc'])).total_seconds() if snapshots else None,
                             'last_sample_age_s': (now - datetime.fromisoformat(samples[-1]['utc'])).total_seconds() if samples else None},
                   'pagination': {'offset': offset, 'limit': limit, 'filtered_total': len(filtered),
                                  'status': status, 'has_more': offset + limit < len(filtered)},
                   'windows': filtered[offset:offset + limit]})
    return report
