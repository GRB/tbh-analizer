"""Confirm run outcomes with the game's StageClear/StageFail counters.

A clear is counted when the stage finishes (runtime marker: final wave), a fail when the
attempt restarts. Runs are assigned to the save window containing that moment. The
outcome is only confirmed when the counts in the window leave no ambiguity.
"""
from datetime import datetime

from ..save.model import stage_counters
from .runs import is_act_boss_run, is_first_clear_run


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


# A first clear saves on the stage advance, before our next sample shows the marker: observed
# 2026-10-01, save 08:35:08.843 and marker sample 08:35:08.875. Placing the moment a few seconds
# earlier puts the run in the save window that counted it. A save inside that lead only leaves
# the run undecided: the counts below must still match exactly.
FIRST_CLEAR_SAVE_LEAD = 5.0


def outcome_moment(run):
    moment = _ts(run.get('final_utc') or run.get('ended_utc'))
    if moment is not None and is_first_clear_run(run):
        moment -= FIRST_CLEAR_SAVE_LEAD
    return moment


def reconcile(runs, snapshots):
    """Return {run_id: (outcome, source)} for runs whose outcome the counters determine."""
    ordered = sorted((s for s in snapshots if s.get('last_saved_utc')), key=lambda s: s['last_saved_utc'])
    decided = {}
    candidates = [r for r in runs if r.get('end_reason') == 'wave_reset' or is_act_boss_run(r) or is_first_clear_run(r)]
    for a, b in zip(ordered, ordered[1:]):
        ta, tb = _ts(a['last_saved_utc']), _ts(b['last_saved_utc'])
        inside = [r for r in candidates if outcome_moment(r) is not None and ta < outcome_moment(r) <= tb]
        ca, cb = stage_counters(a), stage_counters(b)
        by_stage = {}
        for run in inside:
            by_stage.setdefault(run['stage_key'], []).append(run)
        changed = {k for k in set(ca) | set(cb) if ca.get(k) != cb.get(k)}
        if changed - set(by_stage):
            continue  # counters moved for a stage with no observed run: window not trustworthy
        for stage, group in by_stage.items():
            clears = cb.get(stage, {}).get('clears', 0) - ca.get(stage, {}).get('clears', 0)
            fails = cb.get(stage, {}).get('fails', 0) - ca.get(stage, {}).get('fails', 0)
            if clears < 0 or fails < 0 or clears + fails != len(group):
                continue
            if fails == 0:
                decided.update({r['id']: ('clear', 'save-counters') for r in group})
            elif clears == 0:
                decided.update({r['id']: ('fail', 'save-counters') for r in group})
            else:
                guessed = sum(1 for r in group if r.get('final_utc'))
                if guessed == clears:
                    decided.update({r['id']: ('clear' if r.get('final_utc') else 'fail', 'save-counters+marker')
                                    for r in group})
    return decided
