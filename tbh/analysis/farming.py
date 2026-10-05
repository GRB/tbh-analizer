"""Save-bounded farming episodes that retain failures and time across stage changes."""
from collections import Counter

from .economy import save_window


def farming_report(snapshots, windows, thresholds, runs=()):
    by_id = {s['save_id']: s for s in snapshots}
    episodes, excluded = [], Counter()
    current = None
    for row in sorted(windows, key=lambda w: w['start_utc']):
        a, b = by_id[row['save_from']], by_id[row['save_to']]
        w = save_window(a, b, thresholds)
        # A stage transition is part of farming. Never discard its time just because a wipe moved the party.
        reasons = [r for r in w['reasons'] if r not in ('stage changed', 'other stage played inside window')]
        if not row['coverage']['continuous']:
            reasons.append('runtime continuity unavailable')
        levels = lambda s: {h['hero_key']: h['level'] for h in s['heroes'] if h['hero_key'] in s['party']}
        if not a.get('power') or not b.get('power'):
            reasons.append('loadout unknown')
        if levels(a) != levels(b) or len(levels(a)) != len(a['party']) or any(v is None for v in levels(a).values()):
            reasons.append('levels changed or unknown')
        if row['gold']['status'] not in ('matched', 'discrepancy'):
            reasons.append('gold endpoints or evidence unavailable')
        if reasons:
            excluded.update(set(reasons))
            current = None
            continue
        cohort = (tuple(sorted(a['party'])), tuple(sorted(levels(a).items())), repr(sorted(a['power'].items())))
        if current is None or current['_cohort'] != cohort or current['save_to'] != a['save_id'] or current['_session'] != row['coverage']['session_id']:
            current = {'save_from': a['save_id'], 'save_to': a['save_id'], 'start_utc': row['start_utc'],
                       'end_utc': row['start_utc'], 'seconds': 0, 'windows': 0, 'stages': set(),
                       'party': sorted(a['party']), 'levels': levels(a), 'clears': 0, 'fails': 0,
                       'gross_gold': 0, 'net_gold': 0, 'xp': {str(k): 0 for k in a['party']},
                       '_cohort': cohort, '_session': row['coverage']['session_id']}
            episodes.append(current)
        current.update(save_to=b['save_id'], end_utc=row['end_utc'])
        current['seconds'] += row['seconds']
        current['windows'] += 1
        current['stages'].update([a['current_stage'], b['current_stage'], *w['all_clears'], *w['all_fails']])
        current['stages'].update(row.get('observed_stages', []))
        current['clears'] += sum(w['all_clears'].values())
        current['fails'] += sum(w['all_fails'].values())
        current['gross_gold'] += w['gold_earned_total']
        current['net_gold'] += w['balance_delta']
        for key in current['xp']:
            gain = (w['xp'].get(key) or {}).get('gain')
            current['xp'][key] = current['xp'][key] + gain if current['xp'][key] is not None and gain is not None else None
    for e in episodes:
        e.pop('_cohort')
        e['session_id'] = e.pop('_session')
        e['stages'] = sorted(s for s in e['stages'] if s is not None)
        e['gross_gold_h'] = e['gross_gold'] * 3600 / e['seconds']
        e['net_gold_h'] = e['net_gold'] * 3600 / e['seconds']
        e['xp_h'] = {k: v * 3600 / e['seconds'] if v is not None else None for k, v in e['xp'].items()}
    cycles = recovery_cycles(episodes, runs)
    return {'episodes': episodes, 'recovery_cycles': cycles, 'excluded_windows': dict(excluded),
            'summary': {'episodes': len(episodes), 'seconds': sum(e['seconds'] for e in episodes),
                        'with_failures': sum(e['fails'] > 0 for e in episodes),
                        'mixed_stage': sum(len(e['stages']) > 1 for e in episodes),
                        'completed_recoveries': sum(c['status'] == 'returned_and_cleared' for c in cycles),
                        'censored_recoveries': sum(c['status'] == 'not_observed_before_episode_end' for c in cycles)},
            'note': 'Save-bounded episodes with stable saved party, levels and loadout, and accepted runtime continuity. '
                    'Failures and stage-change time remain included. Mixed-stage income belongs to the whole route. '
                    'Endpoints are not complete-run boundaries; recovery beyond an episode is not measured. '
                    'Excluded time is reported, so these rates do not establish a global best stage.'}


def recovery_cycles(episodes, runs):
    """Failure to next recorded clear of the same stage, retaining unfinished observation."""
    from datetime import datetime
    cycles = []
    for e in episodes:
        rows = sorted((r for r in runs if r.get('session_id') == e['session_id']
                       and r.get('ended_utc') and e['start_utc'] <= r['started_utc'] < e['end_utc']
                       and r['ended_utc'] <= e['end_utc']), key=lambda r: r['started_utc'])
        i = 0
        while i < len(rows):
            first = rows[i]
            if first.get('outcome') != 'fail' or first.get('partial_start') or first.get('gaps'):
                i += 1
                continue
            end = next((j for j in range(i + 1, len(rows)) if rows[j]['stage_key'] == first['stage_key']
                        and rows[j].get('outcome') == 'clear' and not rows[j].get('gaps')), None)
            until = rows[end]['ended_utc'] if end is not None else e['end_utc']
            seconds = lambda a, b: (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
            cycles.append({'stage': first['stage_key'], 'failed_run_id': first['id'],
                           'return_clear_run_id': rows[end]['id'] if end is not None else None,
                           'status': 'returned_and_cleared' if end is not None else 'not_observed_before_episode_end',
                           'failure_and_recovery_s': seconds(first['started_utc'], until),
                           'after_failure_s': seconds(first['ended_utc'], until),
                           'until_utc': until, 'gold': None,
                           'note': 'Uses recorded run outcomes inside one accepted farming episode. Censored time is a lower bound; '
                                   'save-window income is not apportioned to this cycle.'})
            i = end + 1 if end is not None else len(rows)
    return cycles
