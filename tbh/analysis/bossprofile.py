"""Each act boss fights differently: learn its attacks from the recorded fights and simulate a fight.

Learned per boss (only from fights the collector recorded, `runs.combat`):
- base attack interval: front-hero HP losses over the time it was being hit;
- base hit size vs the hit formula (D009), when a timeline (5 readings/s, since 2026-10-02) has them;
- bursts: moments where several heroes die or lose HP together, timed from the start of the fight
  (2210: the back line died at 9.08 / 9.08 / 9.11 s in three fights). Which heroes a burst killed and
  which survived it, with their max HP then, bound its damage;
- damage per second on the boss, and the party output it was measured at.

The simulation replays a fight second by second with those attacks and the party's current stats:
base attacks on the front hero (then the next), bursts at their learned times, party damage shared by
hero output and lost as heroes die. It reports who dies when and the boss HP left, the number the
recorded fights can be compared with. Every learned value carries how many fights support it.
"""
import math
import statistics

from .combat import offence
from .threat import hit

TOGETHER_S = 0.4       # deaths or HP losses this close count as one hit on several heroes
BURST_MATCH_S = 0.6    # bursts in different fights within this many seconds are the same attack
STEP_S = 0.05
CONTACT_S = 4.5        # seconds from the fight start to the first hit when no fight was recorded
MAX_FIGHT_S = 120


def _died(fight):
    return {int(k): h['died_s'] for k, h in (fight['combat'].get('heroes') or {}).items() if h.get('died_s') is not None}


def fit_bursts(profile, target, fights, k, front):
    """Which boss skill each burst is: the catalog skill multiplier (x a base hit, armor applied) that agrees
    with every hero it killed and every hero it left alive, and that skill's period. Unresolved bursts keep
    only their observed kills."""
    for b in profile['bursts']:
        fits = []
        for skill in target.get('skills') or []:
            ok = True
            for f in fights:
                died, at = _died(f), b['at_s']
                if not any(abs(t - at) <= BURST_MATCH_S for t in died.values()):
                    continue
                for h in f['heroes']:
                    if h['hero_key'] == front:
                        continue
                    base = hit(target['damage'] * skill['value'], h, target['level'], k, target['monster_key'])
                    if not base:
                        ok = False
                        break
                    when = died.get(h['hero_key'])
                    killed = when is not None and abs(when - at) <= BURST_MATCH_S
                    if killed != (base['hit'] >= (h.get('max_hp') or 0)):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                fits.append(skill)
        # Several skills agree with what was seen: assume the strongest (worst case) and say so.
        b['skill'] = max(fits, key=lambda x: x['value']) if fits else None
        b['candidates'] = [s['skill_key'] for s in fits]
        b['ambiguous'] = len(fits) > 1
        # A cooldown skill repeats every `every` s; a count skill every `every` base attacks.
        if b['skill']:
            b['period_s'] = (b['skill']['every'] if b['skill']['trigger'] == 'COOLDOWN'
                             else b['skill']['every'] * (profile.get('attack_interval_s') or 1 / target['attacks_per_s']))
    return profile


def learn(fights):
    """Profile of one boss from `fights`: [{'combat': runs.combat, 'heroes': [fight-model heroes at the time],
    'won': bool, 'hit_ratios': [observed / predicted base hit]}]. Missing pieces stay None or empty."""
    out = {'fights': len(fights), 'won': sum(1 for f in fights if f['won']), 'attack_interval_s': None,
           'interval_fights': 0, 'hit_ratio': None, 'contact_s': [], 'bursts': [], 'dps': []}
    intervals, ratios, moments = [], [], []
    for f in fights:
        c, died = f['combat'], _died(f)
        if c.get('first_hit_s') is not None:
            out['contact_s'].append(c['first_hit_s'])
        front = max((c.get('heroes') or {}).items(), key=lambda kv: kv[1].get('hp_drops') or 0, default=(None, {}))
        drops, start = front[1].get('hp_drops') or 0, c.get('first_hit_s')
        end = died.get(int(front[0])) if front[0] is not None else None
        if end is None and start is not None and (c.get('boss') or {}).get('seconds'):
            end = start + c['boss']['seconds']
        if drops >= 3 and start is not None and end and end > start:
            # HP-loss readings miss hits that land within one reading interval: an upper bound on the interval.
            intervals.append((end - start) / drops)
        boss = c.get('boss') or {}
        # Damage per second per unit of party output, on one basis: output from stats before buffs (`base`)
        # at the time of the fight, weighted by how long each hero was alive while the boss was hit (a dead
        # hero deals nothing; the simulation removes it again). Buffs come and go inside the measured dps.
        hit_from, hit_to = boss.get('hit_s'), boss.get('end_s')
        if hit_from is None and start is not None and boss.get('seconds'):
            hit_from, hit_to = start, start + boss['seconds']
        output = 0.0
        for h in f['heroes']:
            share = 1.0
            death = died.get(h['hero_key'])
            if death is not None and hit_from is not None and hit_to and hit_to > hit_from:
                share = min(1.0, max(0.0, (death - hit_from) / (hit_to - hit_from)))
            output += share * (offence(h.get('base') or h['final']) or 0)
        if boss.get('dps') and output:
            out['dps'].append({'dps': boss['dps'], 'offence': output, 'killed': boss.get('killed'),
                               'hp_left': boss.get('hp_left_fraction')})
        ratios.extend(f.get('hit_ratios') or [])
        moments.extend((t, f) for t in died.values())
    if intervals:
        out['attack_interval_s'], out['interval_fights'] = statistics.median(intervals), len(intervals)
    if ratios:
        out['hit_ratio'] = statistics.median(ratios)
    # A burst: deaths at the same moment of the fight (from its start) in two or more fights, or of two or
    # more heroes in one fight. Base attacks kill at moments that vary with the party's HP; bursts do not.
    for t, f in sorted(moments, key=lambda m: m[0]):
        if any(abs(b['at_s'] - t) <= BURST_MATCH_S for b in out['bursts']):
            continue
        near = [(u, g) for u, g in moments if abs(u - t) <= BURST_MATCH_S]
        in_fights = {id(g) for _, g in near}
        if len(near) < 2:
            continue
        at = statistics.median(u for u, _ in near)
        killed, survived = {}, {}
        for g in fights:
            died, hp = _died(g), {h['hero_key']: h.get('max_hp') for h in g['heroes']}
            if id(g) not in in_fights:
                continue
            for hero, max_hp in hp.items():
                when = died.get(hero)
                if when is not None and abs(when - at) <= BURST_MATCH_S:
                    killed.setdefault(hero, []).append(max_hp)
                elif when is None or when > at + BURST_MATCH_S:
                    survived.setdefault(hero, []).append(max_hp)
        out['bursts'].append({
            'at_s': at, 'fights': len(in_fights),
            'killed': {h: len(v) for h, v in killed.items()}, 'survived': {h: len(v) for h, v in survived.items()},
            # It killed this hero at up to this max HP; it left this hero alive at this max HP or more.
            'lethal_hp': {h: max(x for x in v if x) for h, v in killed.items() if any(v)},
            'survived_hp': {h: min(x for x in v if x) for h, v in survived.items() if any(v)}})
    return out


def _alive_order(heroes, front):
    rest = [h for h in heroes if h['hero_key'] != front]
    return ([h for h in heroes if h['hero_key'] == front] + rest) if front else heroes


def simulate(target, profile, heroes, k, front=None):
    """Replay a fight with the learned attacks and the party's stats now. Returns who dies when, the boss HP
    left at the wipe (0 = won), the kill time if won, and `reach`: party damage before the wipe / boss HP."""
    interval = (profile or {}).get('attack_interval_s') or 1 / (target['attacks_per_s'] * target['skill_factor'])
    ratio = (profile or {}).get('hit_ratio') or 1.0
    dps_rows = (profile or {}).get('dps') or []
    if not dps_rows:
        return None
    per_output = statistics.median(r['dps'] / r['offence'] for r in dps_rows)
    order = _alive_order(heroes, front)
    hp = {h['hero_key']: h.get('max_hp') or h['final'].get('MaxHp') for h in order}
    base_hit = {}
    for h in order:
        one = hit(target['damage'], h, target['level'], k, target['monster_key'])
        if not one:
            return None
        base_hit[h['hero_key']] = one['hit'] * ratio
    output = {h['hero_key']: offence(h.get('base') or h['final']) or 0 for h in order}
    boss_hp, dealt, t, next_attack, deaths, killed_at = target['hp'], 0.0, 0.0, 0.0, {}, None
    bursts = sorted((profile or {}).get('bursts') or [], key=lambda b: b['at_s'])
    done = set()
    # The boss starts attacking at first contact; recorded fights put it ~4-5 s after the start.
    contact = statistics.median((profile or {}).get('contact_s') or [CONTACT_S])
    next_attack = contact
    while t < MAX_FIGHT_S and any(v > 0 for v in hp.values()):
        alive = [h for h in hp if hp[h] > 0]
        if t >= contact:
            dmg = per_output * sum(output[h] for h in alive) * STEP_S
            dealt += dmg
            if killed_at is None and dealt >= boss_hp:
                killed_at = t
        if t >= next_attack:
            front_alive = next((h['hero_key'] for h in order if hp[h['hero_key']] > 0), None)
            if front_alive is not None:
                hp[front_alive] -= base_hit[front_alive]
            next_attack += interval
        for i, b in enumerate(list(bursts)):
            if i not in done and t >= b['at_s']:
                done.add(i)
                for hero in alive:
                    if hero == front:
                        continue      # the bursts seen hit the back line, the front hero is busy with the boss
                    if b.get('skill'):
                        h = next(x for x in order if x['hero_key'] == hero)
                        one = hit(target['damage'] * b['skill']['value'], h, target['level'], k, target['monster_key'])
                        hp[hero] -= one['hit'] if one else 0
                    else:
                        lethal = b['lethal_hp'].get(hero)
                        if lethal is not None and hp[hero] <= lethal + 1e-9:
                            hp[hero] = 0      # it killed this hero before at this much HP or more
                if b.get('period_s'):
                    bursts.append({**b, 'at_s': b['at_s'] + b['period_s']})
        for h in list(hp):
            if hp[h] <= 0 and h not in deaths:
                deaths[h] = round(t, 2)
        t += STEP_S
    wiped = all(v <= 0 for v in hp.values())
    return {'deaths': deaths, 'won': killed_at is not None and (not wiped or killed_at <= max(deaths.values() or [0])),
            'kill_s': killed_at, 'boss_hp_left': max(0.0, 1 - dealt / boss_hp) if killed_at is None else 0.0,
            'reach': dealt / boss_hp, 'wipe_s': max(deaths.values()) if wiped and deaths else None,
            'interval_s': interval, 'hit_ratio': ratio}
