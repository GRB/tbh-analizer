"""Observed level transitions, with native-code evidence kept separate from consistency."""
from collections import Counter
from .progress import xp_gained

AUDITED_BUILD = '25454993'
AUDITED_SHA256 = 'ADE9DE3DDCD244105F21D15D5F9DB1C3AF439098C9F6C2CA1CB0A8B1F6E810E1'


def level_up_evidence(index, thresholds, build_id, sessions):
    events = []
    for session, (rows, _) in index.sessions.items():
        identity = sessions.get(session, {})
        verified = (str(identity.get('build_id')) == AUDITED_BUILD and
                    str(identity.get('game_assembly_sha256', '')).upper() == AUDITED_SHA256)
        for a, b in zip(rows, rows[1:]):
            before = {h['hero_key']: h for h in a.get('heroes') or []}
            for h in b.get('heroes') or []:
                old = before.get(h['hero_key'], {})
                la, lb = old.get('level'), h.get('level')
                if not isinstance(la, int) or not isinstance(lb, int) or lb <= la:
                    continue
                gain, kind = xp_gained(la, old.get('xp'), lb, h.get('xp'), thresholds)
                status = ('incomplete' if not index.valid_pair(a, b) else
                          'invalid' if gain is None else 'consistent')
                events.append({'session_id': session, 'hero_key': h['hero_key'],
                               'before_sample_id': a.get('id'), 'after_sample_id': b.get('id'),
                               'before_utc': a['utc'], 'after_utc': b['utc'],
                               'level_before': la, 'level_after': lb,
                               'xp_before': old.get('xp'), 'xp_after': h.get('xp'),
                               'accounted_gain': gain, 'kind': kind, 'status': status,
                               'native_identity_matches': verified})
    return {'native_rule': {'status': 'confirmed_for_catalog_build' if str(build_id) == AUDITED_BUILD else 'unverified_build',
                            'build_id': AUDITED_BUILD, 'game_assembly_sha256': AUDITED_SHA256,
                            'source': 'native code of the audited build',
                            'rule': 'Subtract the current level threshold, increment level, repeat; keep the remainder.'},
            'observed_transitions': len(events), 'statuses': dict(Counter(e['status'] for e in events)),
            'native_identity_matched_transitions': sum(e['native_identity_matches'] for e in events),
            'events': events,
            'note': 'Native code validates carry-over arithmetic independently of save/runtime agreement. '
                    'Observed gains still use thresholds; they are not independent measurements of rewards. '
                    'Counts are per collector and hero, not deduplicated gameplay events. '
                    'Offline rewards, cap behavior and every XP award path are not validated here.'}
