"""Act boss attempts, derived from save counters.

Observed on 1.2.8 (2026-10-01): during the fight the runtime stage key is the act boss stage
(1310, wave 0 -> 1 when the boss dies). Until that day the reader failed on act boss stages
(empty WaveAmount), so older fights exist only in the save: StageClear/StageFail move for the
ACTBOSS stage, ActBossKill moves for the boss monster, and `currentStage` is the act boss stage
when the save is written mid-fight. Such an attempt is located only to the window between two saves.
"""
from datetime import datetime

from ..save.model import aggregate, stage_counters

ACT_BOSS_TYPES = ('ACTBOSS', 'CONTAMINACTBOSS')
ACT_BOSS_KILL = 17
GOLD_EARNED = 2
OVERLAP_SECONDS = 1.0   # a runtime run overlapping a window by less than this only touches its boundary


def act_boss_stages(catalog):
    return {int(k) for k, row in catalog.stages.items() if row['STAGETYPE'] in ACT_BOSS_TYPES} if catalog else set()


def _kills(snapshot):
    return {row['subkey']: row['value'] for row in snapshot['aggregates']
            if row['type'] == ACT_BOSS_KILL and row['content'] == 0 and row['subkey']}


def attempts(snapshots, catalog):
    """One entry per act boss stage whose clear/fail counters moved between consecutive saves."""
    stages = act_boss_stages(catalog)
    ordered = sorted((s for s in snapshots if s.get('last_saved_utc')), key=lambda s: s['last_saved_utc'])
    found = []
    for a, b in zip(ordered, ordered[1:]):
        ca, cb = stage_counters(a), stage_counters(b)
        ka, kb = _kills(a), _kills(b)
        kills = {m: v - ka.get(m, 0) for m, v in kb.items() if v != ka.get(m, 0)}
        ga, gb = aggregate(a, GOLD_EARNED), aggregate(b, GOLD_EARNED)
        for stage in sorted(stages & (set(ca) | set(cb))):
            clears = cb.get(stage, {}).get('clears', 0) - ca.get(stage, {}).get('clears', 0)
            fails = cb.get(stage, {}).get('fails', 0) - ca.get(stage, {}).get('fails', 0)
            if clears <= 0 and fails <= 0:
                continue
            boss = catalog.stages[str(stage)].get('BossMonsterKey')
            found.append({
                'stage_key': stage, 'clears': max(clears, 0), 'fails': max(fails, 0),
                'boss_kills': kills.get(int(boss), 0) if boss else None,
                'after_utc': a['last_saved_utc'], 'by_utc': b['last_saved_utc'],
                'window_s': round((datetime.fromisoformat(b['last_saved_utc'])
                                   - datetime.fromisoformat(a['last_saved_utc'])).total_seconds(), 1),
                # Gold earned by everything in the window, not only the boss.
                'window_gold_earned': gb - ga if ga is not None and gb is not None else None,
            })
    return found


def in_progress(snapshot, catalog):
    """Act boss stage the latest save was written in, or None."""
    stage = snapshot.get('current_stage') if snapshot else None
    return int(stage) if stage is not None and int(stage) in act_boss_stages(catalog) else None


def overlaps(run, attempt):
    """Seconds a runtime run shares with an attempt's save window."""
    start = max(datetime.fromisoformat(run['started_utc']), datetime.fromisoformat(attempt['after_utc']))
    end = min(datetime.fromisoformat(run['ended_utc']), datetime.fromisoformat(attempt['by_utc']))
    return (end - start).total_seconds()


def match_runtime(attempt_list, runtime_runs):
    """Set `observed_run_id` on attempts the runtime recorded as a run of the same stage."""
    for attempt in attempt_list:
        matches = [r['id'] for r in runtime_runs if r['stage_key'] == attempt['stage_key']
                   and r.get('ended_utc') and overlaps(r, attempt) > 0]
        # One runtime row cannot explain several attempts in the same save window.
        count = attempt.get('clears', 0) + attempt.get('fails', 0)
        attempt['observed_run_id'] = matches[0] if len(matches) == 1 and count == 1 else None
    return attempt_list


def contaminated(run, attempt_list):
    """True when an unobserved act boss fight may lie inside the run: its time and gold were recorded as the stage's."""
    if not run.get('ended_utc'):
        return False
    return any(overlaps(run, a) > OVERLAP_SECONDS for a in attempt_list if not a.get('observed_run_id'))
