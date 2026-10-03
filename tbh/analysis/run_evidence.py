"""Counter-constrained interval matching, separate from historical run outcomes.

Uniqueness is conditional on the recorded events and counters, never proof of complete
capture. Unknown stage departures cannot be turned into victories by matching counters.
"""
from collections import Counter, defaultdict
from datetime import datetime


def ts(value):
    return datetime.fromisoformat(value).timestamp()


def match_component(options, capacities, forbidden=None):
    """Find a capacity-respecting assignment; identical counter units are not distinct."""
    if sum(capacities.values()) != len(options):
        return None
    owners = defaultdict(list)

    def place(event, seen):
        for target in options[event]:
            if (event, target) == forbidden or target in seen:
                continue
            seen.add(target)
            if len(owners[target]) < capacities[target]:
                owners[target].append(event)
                return True
            for old in list(owners[target]):
                if place(old, seen):
                    owners[target].remove(old)
                    owners[target].append(event)
                    return True
        return False

    for event in options:
        if not place(event, set()):
            return None
    return {event: target for target, events in owners.items() for event in events}


def reconcile_intervals(windows, runs, index):
    """Attach reproducible classifications without changing strict residuals or stored rows."""
    import bisect
    starts = [ts(w['start_utc']) for w in windows]
    ends = [ts(w['end_utc']) for w in windows]
    capacities, links, evidence = {}, defaultdict(set), {}
    issues = defaultdict(list)
    blocked_stages = defaultdict(set)
    for i, window in enumerate(windows):
        for stage, counts in window['runs']['save'].items():
            for kind, count in counts.items():
                if count > 0:
                    capacities[(i, str(stage), kind)] = count
        if window['runs']['status'] in ('missing', 'counter_reset', 'incomplete'):
            issues[i].append('save counters or runtime continuity incomplete')
            blocked_stages[i].add(None)
    options = {}
    for run in runs:
        moment = run.get('final_utc') or run.get('ended_utc')
        if not moment or not windows:
            continue
        at = ts(moment)
        raw_window = bisect.bisect_left(ends, at)
        rows, times, marker_session = index.marker_rows(run, at)
        pos = bisect.bisect_left(times, at)
        valid = (0 < pos < len(rows) and times[pos] == at
                 and index.valid_pair(rows[pos - 1], rows[pos])
                 and rows[pos - 1].get('stage_key') == run['stage_key'])
        relevant = bool(run.get('final_utc') or run.get('end_reason') in ('wave_reset', 'stage_change'))
        if not relevant:
            continue
        if not valid:
            if raw_window < len(windows) and starts[raw_window] < at:
                issues[raw_window].append(f"run {run['id']}: no valid same-session marker bracket")
                blocked_stages[raw_window].add(str(run['stage_key']))
            continue
        lo = times[pos - 1]
        touched = list(range(bisect.bisect_right(ends, lo), bisect.bisect_left(starts, at)))
        kind = 'clears' if run.get('final_utc') else 'fails' if run.get('end_reason') == 'wave_reset' else None
        record = {'run_id': run['id'], 'stage_key': run['stage_key'], 'kind': kind or 'unknown_departure',
                  'before_utc': rows[pos - 1]['utc'], 'observed_utc': moment,
                  'session_id': marker_session, 'run_session_id': run.get('session_id'), 'candidate_save_windows': [
                      [windows[i]['save_from'], windows[i]['save_to']] for i in touched]}
        evidence[run['id']] = record
        for i in touched:
            links[i].add(run['id'])
        if kind is None or lo < starts[0] or at > ends[-1]:
            for i in touched:
                issues[i].append(f"run {run['id']}: " + ('outcome not independently observed' if kind is None else 'interval crosses report scope'))
                blocked_stages[i].add(str(run['stage_key']))
            continue
        options[run['id']] = [(i, str(run['stage_key']), kind) for i in touched]
        for target in options[run['id']]:
            capacities.setdefault(target, 0)

    # Connected components ensure a marker cannot be reused independently in adjacent windows.
    reverse = defaultdict(set)
    for event, targets in options.items():
        for target in targets:
            reverse[target].add(event)
    classifications, assignments = {}, {}
    pending = set(capacities)
    while pending:
        targets, events, todo = set(), set(), [min(pending)]
        while todo:
            target = todo.pop()
            if target in targets:
                continue
            targets.add(target)
            for event in reverse[target] - events:
                events.add(event)
                todo.extend(options[event])
        pending.difference_update(targets)
        local = {event: options[event] for event in sorted(events)}
        capacity = {target: capacities[target] for target in targets}
        assigned = match_component(local, capacity)
        affected = {target[0] for target in targets}
        blocked = any(None in blocked_stages[i] or stage in blocked_stages[i] for i, stage, _ in targets)
        if blocked or assigned is None:
            status = 'insufficient_evidence'
            if blocked:
                for i in affected:
                    issues[i].append('linked interval component includes incomplete evidence')
            if assigned is None:
                for i in affected:
                    issues[i].append('no complete assignment satisfies recorded counters and intervals')
        elif any(match_component(local, capacity, (event, target)) is not None
                 for event, target in assigned.items()):
            status = 'ambiguous'
        else:
            status = 'unique_correspondence'
            assignments.update(assigned)
        for i in affected:
            classifications.setdefault(i, set()).add(status)
    for event, target in assignments.items():
        w = windows[target[0]]
        evidence[event]['assigned_save_window'] = [w['save_from'], w['save_to']]
    for i, window in enumerate(windows):
        states = classifications.get(i, set())
        status = ('insufficient_evidence' if issues[i] or 'insufficient_evidence' in states else
                  'ambiguous' if 'ambiguous' in states else
                  'unique_correspondence' if states else 'no_events')
        window['runs']['interval_reconciliation'] = {
            'status': status, 'reasons': sorted(set(issues[i])),
            'evidence': [evidence[event] for event in sorted(links[i])],
            'assignments': [{'run_id': event, 'stage_key': target[1], 'outcome': target[2],
                             'save_from': window['save_from'], 'save_to': window['save_to']}
                            for event, target in sorted(assignments.items()) if target[0] == i],
            'note': 'Unique only within recorded counters and accepted intervals. Unknown departures, '
                    'missing capture and historical outcomes are not independent proof of a result.'}
    return dict(Counter(w['runs']['interval_reconciliation']['status'] for w in windows))
