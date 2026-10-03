"""Failed fights and what would win them: attribute points (refund is free), gear you own, levels.

Read-only: it plans, the player decides. Built on analysis/challenge.py; every number states whether it
is measured, validated or an estimate. Needs a live combat reading (current stats and modifiers).
"""
import json
import time
from datetime import datetime, timedelta, timezone

from ..analysis import actboss, power
from ..analysis import bossprofile
from ..analysis.challenge import SAFETY, best_points, boss_target, fight, score
from ..analysis.impact import stats_at
from ..analysis.threat import hit
from ..analysis.progress import xp_to_level
from ..analysis.threat import live_heroes
from ..catalog.catalog import num
from ..snapshots import snapshots_since
from .analytics import front_hero, recent_rates
from .items import PARTS, containers, stat_name
from .power import PowerContext, item_raw

# Stats the fight model can value: survival (hit formula) and base-attack output. Points in other
# passives (cooldowns, healing, elemental or skill damage) are never moved: the model cannot price them.
MODELLED = {'MaxHp', 'Armor', 'DamageAbsorption', 'AttackDamage', 'AttackSpeed', 'CriticalChance', 'CriticalDamage'}
LEVELS_AHEAD = 15
# With a learned profile, retry when the simulated party deals this much of the boss HP before the wipe:
# damage per second varied 5.9k-9.1k between the three 2210 fights (Quick Loader timing, crits).
RETRY_REACH = 1.25
# What one item roll of each stat does to the fight, per hero: the stats worth looking for on gear.
STAT_STEPS = [('MaxHp', 'FLAT', 50, '+50 Max HP'), ('Armor', 'FLAT', 100, '+100 Armor'),
              ('DamageAbsorption', 'FLAT', 5, '+5 Damage Absorption'), ('AttackDamage', 'FLAT', 10, '+10 Attack Damage'),
              ('AttackSpeed', 'ADDITIVE', 0.1, '+10% Attack Speed'), ('CriticalChance', 'FLAT', 0.05, '+5% Critical Chance')]
ACT_BOSS_DAYS = 7      # how far back to look for the last failed act boss


def act_boss_steps(t, result):
    """'To beat this boss you need to:' as ordered steps: the boss's own attacks first (learned from your
    fights), then points, gear you own, stats to look for and when to retry."""
    now, retry, mech = t['now'], t.get('retry'), t.get('mechanics')
    steps = []
    for b in (mech or {}).get('bursts') or []:
        dead = [h for h in b['heroes'] if not h['survives']]
        what = (f"×{b['skill']['value']:g} hit on the back line at {b['at_s']:.1f} s"
                + (f", then every {b['period_s']:.0f} s" if b.get('period_s') else '') if b.get('skill') else
                f"burst at {b['at_s']:.1f} s")
        seen = ', '.join(f"{name} {n}/{b['fights']}" for name, n in b['killed'].items())
        if dead:
            steps.append(f"Survive its {what} (it killed {seen} in your fights): "
                         + '; '.join(f"{h['hero']} needs +{h['hp_needed']} Max HP (takes {h['hit']:.0f}, has {h['hp']:.0f})"
                                     for h in sorted(dead, key=lambda h: h['hp_needed'])))
        elif b['heroes']:
            steps.append(f"Its {what} no longer kills anyone with today's stats (it killed {seen} before)")
    if not mech or not mech.get('bursts'):
        cheap = sorted((h for h in now['heroes'] if h['hp_for_next_hit'] > 0), key=lambda h: h['hp_for_next_hit'])
        if cheap:
            steps.append('Survive more boss hits (cheapest first): ' + '; '.join(
                f"{h['name']} +{h['hp_for_next_hit']} Max HP → {h['hits'] + 1} hits instead of {h['hits']}" for h in cheap))
    if t['respec']['changes']:
        steps.append('Move attribute points for this fight (refund is free, put them back after): ' + '; '.join(
            f"{c['hero']} {c['stat']} {c['from']}→{c['to']}" for c in t['respec']['changes']))
    if t['gear']['swaps']:
        steps.append('Equip gear you already own: ' + '; '.join(
            f"{g['item']} (lvl {g['level']}) on {g['hero']}"
            + (' (a different copy, better rolls)' if g['instead_of'] == g['item'] else
               f" instead of {g['instead_of']}" if g['instead_of'] else '')
            for g in t['gear']['swaps']))
    if t['stat_rolls']:
        steps.append('On new gear, look for: ' + '; '.join(
            f"{r['stat']} on {r['hero']} (+{r['margin_pct']:.0f}%)" for r in t['stat_rolls'][:4]))
    if t.get('winnable_with_plan'):
        steps.append('The model predicts a win with these steps; verify in a new fight')
    elif retry:
        steps.append(f"The model suggests retrying at level {retry['level']}" + (f" (≈{retry['hours']:.1f} h of farming)" if retry.get('hours') else '')
                     + ' with the points of that level' + (' and gear' if retry['gear'] else ''))
    elif t.get('missing_factor'):
        steps.append(f"Not reachable in the next 15 levels with the gear you own: about ×{t['missing_factor']:.2f} more "
                     'is missing, so it depends on better gear (the stats above)')
    return steps


class Party:
    """The party's modifiers with point plans and gear swaps applied, and the fight model on top."""

    def __init__(self, app, snapshot, locale):
        self.ctx = PowerContext(app, snapshot, locale)
        self.catalog, self.snapshot, self.locale = app.catalog, snapshot, locale
        reading = self.ctx.reading or {}
        self.k = reading.get('armor_constants')
        self.live = {h['hero_key']: h for h in live_heroes(reading) if h['hero_key'] in self.ctx.heroes}
        self.levels = {h['hero_key']: h['level'] for h in snapshot['heroes']}
        self._steady(reading)
        self._passives(snapshot)

    def _steady(self, reading):
        """Modifiers without temporary buffs (attack-count or timed, e.g. Quick Loader x2 attack speed ~30%
        of the time): a plan must not change with the moment it was read. Permanent auras stay."""
        self.mods = {}
        for hero in reading.get('heroes') or []:
            key = hero['hero_key']
            if key not in self.ctx.heroes:
                continue
            mods = list(self.ctx.heroes[key]['modifiers'])
            for buff in hero.get('buffs') or []:
                if (buff.get('expiry') or {}).get('kind') not in ('attack_count', 'duration'):
                    continue
                for m in buff.get('modifiers') or []:
                    match = next((i for i, x in enumerate(mods) if x['layer'] == 'dynamic' and x['source'] == m['source']
                                  and x['stat'] == m['stat'] and x['mode'] == m['mode'] and abs(x['value'] - m['value']) < 1e-6), None)
                    if match is not None:
                        mods.pop(match)
            self.mods[key] = mods

    @property
    def ready(self):
        return bool(self.ctx.available and self.k and self.live)

    def _passives(self, snapshot):
        """Passive attributes per party hero: levels now, and the ones the model can value."""
        attrs = self.catalog.index('AttributeInfoData', 'AttributeKey')
        passive = self.catalog.index('PassiveSkillInfoData', 'PassiveSkillKey')
        self.info, self.plan_now, raw_by_hero = {}, {}, {}
        # Every allocated attribute (passives, active skills) counts towards unlocking later groups.
        self.group_of, self.all_levels = {}, {}
        self.thresholds = {g['AttributeGroupKey']: int(g['RequiredAllocatedPoint'])
                           for g in self.catalog.table('AttributeGroupInfoData')}
        for key, row in attrs.items():
            hero = int(row['HeroKey'])
            if hero in self.ctx.heroes:
                self.group_of[(hero, key)] = row['GroupKey']
                level = snapshot['attributes'].get(int(key)) or snapshot['attributes'].get(key) or 0
                if level:
                    self.all_levels.setdefault(hero, {})[key] = level
            if hero not in self.ctx.heroes or row['ATTRIBUTETYPE'] != 'PASSIVESKILL' or row['Value'] not in passive:
                continue
            p = passive[row['Value']]
            level = snapshot['attributes'].get(int(key)) or snapshot['attributes'].get(key) or 0
            self.info[(hero, key)] = {'stat': p['STATTYPE'], 'mode': p['MODTYPE'], 'raw': float(p['Value']),
                                      'max': int(row['MaxLevel']), 'name': stat_name(self.catalog, p['STATTYPE'], self.locale)}
            self.plan_now.setdefault(hero, {})[key] = level
            raw = raw_by_hero.setdefault(hero, {})
            raw[(p['STATTYPE'], p['MODTYPE'])] = raw.get((p['STATTYPE'], p['MODTYPE']), 0) + level * float(p['Value'])
        # Same check as gear and runes (D007): the save's levels must equal the game's PASSIVE modifiers.
        self.scales = power.learn_scales([(power.sums(self.ctx.heroes[h]['modifiers'], 'PASSIVE'), raw)
                                          for h, raw in raw_by_hero.items()])
        self.options = {h: {a: self.info[(h, a)]['max'] for a in levels
                            if self.info[(h, a)]['stat'] in MODELLED
                            and (self.info[(h, a)]['stat'], self.info[(h, a)]['mode']) in self.scales}
                        for h, levels in self.plan_now.items()}

    def valid(self, plan):
        """Group G needs RequiredAllocatedPoint points: counted strictly as points in earlier groups,
        which also satisfies a rule that counts all of the hero's points."""
        for hero, levels in plan.items():
            spent = {**self.all_levels.get(hero, {}), **levels}
            for attr, level in spent.items():
                group = self.group_of.get((hero, attr))
                need = self.thresholds.get(group, 0)
                if level and need and sum(lv for a, lv in spent.items()
                                          if self.group_of.get((hero, a), '') < group) < need:
                    return False
        return True

    def _mods(self, hero, levels):
        return [{'stat': i['stat'], 'mode': i['mode'], 'value': lv * i['raw'] * self.scales[(i['stat'], i['mode'])],
                 'source': 'PASSIVE', 'layer': 'base'}
                for a, lv in levels.items() if lv for i in [self.info[(hero, a)]] if a in self.options[hero]]

    def heroes(self, plan=None, gear=None, extra=None):
        """Fight-model heroes with a point plan, gear swaps {hero: [(current item, new item)]} and extra
        item modifiers {hero: [modifier]} (a hypothetical stat roll)."""
        out = []
        for hero, base in self.live.items():
            mods = self.mods.get(hero) or self.ctx.heroes[hero]['modifiers']
            if extra and extra.get(hero):
                mods = power.apply(mods, 'ITEM', (), extra[hero])
            if plan and plan.get(hero) != self.plan_now.get(hero):
                mods = power.apply(mods, 'PASSIVE', self._mods(hero, self.plan_now[hero]), self._mods(hero, plan[hero]))
            for old, new in (gear or {}).get(hero, []):
                remove, _ = power.to_mods(item_raw(old, self.ctx.enums) or {} if old else {}, self.ctx.gear_scales, 'ITEM')
                add, _ = power.to_mods(item_raw(new, self.ctx.enums) or {}, self.ctx.gear_scales, 'ITEM')
                mods = power.apply(mods, 'ITEM', remove, add)
            final = {**base['final'], **power.final_stats(mods)}
            before_buffs = power.final_stats([m for m in mods if m['layer'] == 'base'])
            out.append({**base, 'final': final, 'base': before_buffs, 'max_hp': final.get('MaxHp') or base['max_hp']})
        return out


def _measured_dps(runs, stage=None):
    """Median single-target damage per second and the party output it was measured at: on the target
    boss itself when fights against it were recorded, else on stage bosses."""
    def rows_for(match):
        return sorted([(r['combat']['boss']['dps'], r['combat'].get('party_offence'))
                       for r in runs if match(r) and ((r.get('combat') or {}).get('boss') or {}).get('dps')
                       and r['combat'].get('party_offence')][-20:])
    own = rows_for(lambda r: r['stage_key'] == stage) if stage else []
    rows = own or rows_for(lambda r: True)
    return (rows[len(rows) // 2] + ('this boss' if own else 'stage bosses',)) if rows else (None, None, None)


def _gear_options(party, max_level):
    """Owned gear each party hero could wear at `max_level` (slot and weapon type checked)."""
    weapons = {int(k): {'MAIN_WEAPON': h['MainWeaponGearType'], 'SUB_WEAPON': h['SubWeaponGearType']}
               for k, h in party.catalog.heroes.items()}
    pool = [it for c in containers(party.snapshot, party.catalog, party.locale).values() for it in c['items']
            if it.get('type') == 'GEAR' and it.get('parts') and item_raw(it, party.ctx.enums) is not None]
    out = {}
    for hero in party.live:
        for part in PARTS:
            need = weapons.get(hero, {}).get(part)
            fits = [it for it in pool if it['parts'] == part and (it['level'] or 0) <= max_level
                    and (not need or it['gear_type'] == need)]
            if fits:
                out[(hero, part)] = fits
    return out


def _best_gear(party, evaluate_gear, max_level):
    """Greedy: per slot, the owned item that most improves the fight score (one swap per slot)."""
    swaps, used, best = {}, set(), evaluate_gear({})
    for (hero, part), items in _gear_options(party, max_level).items():
        current = party.ctx.worn.get(hero, {}).get(part)
        choice = None
        for it in items:
            if it['unique_id'] in used or (current and it['unique_id'] == current['unique_id']):
                continue
            trial = {**swaps, hero: swaps.get(hero, []) + [(current, it)]}
            value = evaluate_gear(trial)
            if value > best + 1e-9 and (choice is None or value > choice[0]):
                choice = (value, it)
        if choice:
            best = choice[0]
            swaps.setdefault(hero, []).append((current, choice[1]))
            used.add(choice[1]['unique_id'])
    return swaps, best


def _profile(store, catalog, stage, target, party, front):
    """Learn the boss's attacks from the recorded fights against it (bossprofile), and check the simulation
    on each recorded fight with the others only (leave-one-out): the accuracy shown with the plan."""
    events = [(e['utc'], json.loads(e['payload'])) for e in
              store.query("SELECT utc, payload FROM events WHERE kind = 'hero_stats' ORDER BY utc")]
    fights = []
    for r in store.query('SELECT id, started_utc, outcome, combat FROM runs WHERE stage_key = ? AND combat IS NOT NULL '
                         'ORDER BY id', (stage,)):
        stats = stats_at(events, r['started_utc']) or {}
        heroes = [{'hero_key': int(key), 'final': v, 'base': v, 'max_hp': v.get('MaxHp'), 'resistances': None}
                  for key, v in stats.items() if int(key) in party.live]
        if heroes:
            fights.append({'id': r['id'], 'utc': r['started_utc'], 'combat': json.loads(r['combat']), 'heroes': heroes,
                           'won': r['outcome'] == 'clear'})
    if not fights:
        return None, []
    profile = bossprofile.fit_bursts(bossprofile.learn(fights), target, fights, party.k, front)
    check = []
    for f in fights:
        boss = f['combat'].get('boss')
        if not boss:
            continue
        others = [g for g in fights if g is not f]
        learned = bossprofile.fit_bursts(bossprofile.learn(others), target, others, party.k, front)
        sim = bossprofile.simulate(target, learned, f['heroes'], party.k, front)
        if sim:
            check.append({'utc': f['utc'], 'predicted_won': sim['won'], 'predicted_left': sim['boss_hp_left'],
                          'won': bool(boss.get('killed')), 'left': boss.get('hp_left_fraction')})
    return profile, check


def _mechanics(catalog, target, profile, party, front, locale):
    """The boss's learned attacks in words, and the HP each back-line hero needs to survive each burst now."""
    if not profile:
        return None
    skills = catalog.index('SkillInfoData', 'SkillKey')
    heroes = {h['hero_key']: h for h in party.heroes()}
    bursts = []
    for b in profile['bursts']:
        need = []
        skill = b.get('skill')
        for key, h in heroes.items():
            if key == front or not skill:
                continue
            one = hit(target['damage'] * skill['value'], h, target['level'], party.k, target['monster_key'])
            if one:
                need.append({'hero': catalog.hero_name(key, locale), 'hit': one['hit'], 'hp': h['max_hp'],
                             'survives': h['max_hp'] > one['hit'], 'hp_needed': max(0, int(one['hit'] - h['max_hp']) + 1)})
        name = catalog.localize((skills.get(str(skill['skill_key'])) or {}).get('SkillNameKey'), locale) if skill else None
        bursts.append({'at_s': b['at_s'], 'period_s': b.get('period_s'), 'fights': b['fights'],
                       'killed': {catalog.hero_name(k, locale): v for k, v in b['killed'].items()},
                       'survived': {catalog.hero_name(k, locale): v for k, v in b['survived'].items()},
                       'skill': skill and {**skill, 'name': name}, 'ambiguous': b.get('ambiguous'), 'heroes': need})
    contact = sorted(profile['contact_s'])
    return {'fights': profile['fights'], 'won': profile['won'], 'attack_interval_s': profile['attack_interval_s'],
            'contact_s': contact[len(contact) // 2] if contact else None,
            'dps': [r['dps'] for r in profile['dps']], 'bursts': bursts}


def _sim_view(catalog, result, locale):
    if not result:
        return None
    return {'won': result['won'], 'kill_s': result['kill_s'], 'boss_hp_left': result['boss_hp_left'], 'reach': result['reach'],
            'deaths': sorted(({'name': catalog.hero_name(k, locale), 'at_s': v} for k, v in result['deaths'].items()),
                             key=lambda d: d['at_s'])}


def _describe(party, target, result):
    if not result:
        return None
    return {'survive_s': result['survive_s'], 'kill_s': result['kill_s'], 'margin': result['margin'],
            'heroes': [{**h, 'name': party.catalog.hero_name(h['hero_key'], party.locale)} for h in result['heroes']]}


def _plan_changes(party, plan):
    out = []
    for hero, levels in plan.items():
        for a, lv in levels.items():
            before = party.plan_now[hero].get(a, 0)
            if lv == before:
                continue
            info = party.info[(hero, a)]
            per = info['raw'] * party.scales[(info['stat'], info['mode'])]
            out.append({'hero': party.catalog.hero_name(hero, party.locale), 'stat': info['name'], 'attribute': a,
                        'per_level': per if info['mode'] == 'FLAT' else per * 100, 'percent': info['mode'] != 'FLAT',
                        'from': before, 'to': lv})
    return sorted(out, key=lambda c: (c['hero'], c['to'] - c['from']))


def _target(app, catalog):
    """The last act boss the party tried and failed, unless it was beaten since: {'stage', 'failed_utc'} or None."""
    since = (datetime.now(timezone.utc) - timedelta(days=ACT_BOSS_DAYS)).isoformat()
    attempts = sorted(actboss.attempts(snapshots_since(app.store, since), catalog), key=lambda a: a['by_utc'])
    failed = [a for a in attempts if a['fails']]
    if not failed:
        return None
    last = failed[-1]
    if any(a['stage_key'] == last['stage_key'] and a['clears'] and a['by_utc'] > last['by_utc'] for a in attempts):
        return None
    return {'stage': last['stage_key'], 'failed_utc': last['by_utc'],
            'attempts': sum(a['fails'] for a in attempts if a['stage_key'] == last['stage_key'])}


def _failed_fights(store, catalog, locale):
    since = (datetime.now(timezone.utc) - timedelta(days=ACT_BOSS_DAYS)).isoformat()
    out = []
    act_bosses = actboss.act_boss_stages(catalog)
    for r in store.query("SELECT * FROM runs WHERE outcome = 'fail' AND ended_utc >= ? ORDER BY ended_utc DESC LIMIT 50", (since,)):
        if r['stage_key'] not in act_bosses:
            continue
        c = json.loads(r['combat']) if r['combat'] else None
        deaths = sorted(((h.get('died_s'), catalog.hero_name(k, locale)) for k, h in ((c or {}).get('heroes') or {}).items()
                         if h.get('deaths')), key=lambda d: (d[0] is None, d[0] or 0))
        hits = {catalog.hero_name(k, locale): h.get('hp_drops') for k, h in ((c or {}).get('heroes') or {}).items()}
        out.append({'stage_key': r['stage_key'], 'label': catalog.stage_label(r['stage_key'], locale), 'ended_utc': r['ended_utc'],
                    'duration_s': r['duration_s'], 'recorded': bool(c), 'deaths': [{'name': n, 'at_s': s} for s, n in deaths],
                    'hits_taken': hits, 'boss': (c or {}).get('boss'), 'wipe_s': (c or {}).get('wipe_s'),
                    'first_hit_s': (c or {}).get('first_hit_s')})
    return out


CACHE_SECONDS = 60   # the plan search takes seconds; stats and saves change on a slower scale
_cache = {}


def challenges(app, locale='en-US'):
    """Cached per server for CACHE_SECONDS (Suggestions refreshes every 5 s)."""
    hit = _cache.get((id(app), locale))
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    result = _challenges(app, locale)
    _cache[(id(app), locale)] = (time.monotonic(), result)
    return result


def _challenges(app, locale='en-US'):
    from .analytics import decorate_run
    catalog, store = app.catalog, app.store
    snapshot = app.save() if hasattr(app, 'save') else None
    failed = _failed_fights(store, catalog, locale)
    if not snapshot:
        return {'available': False, 'reason': 'no save read yet', 'failed': failed}
    party = Party(app, snapshot, locale)
    if not party.ready:
        return {'available': False, 'reason': 'needs a live reading of the game (stats and modifiers)', 'failed': failed}
    runs = [decorate_run(r, catalog, locale) for r in store.query('SELECT * FROM runs ORDER BY id DESC LIMIT 400')]
    dps, dps_offence, dps_source = _measured_dps([r for r in reversed(runs)])
    front = front_hero(runs)
    rates = recent_rates(store, catalog, 30, locale) or {}
    targets = []
    picked = _target(app, catalog)
    for stage, why in ([(picked['stage'], 'last failed act boss')] if picked else []):
        own = _measured_dps([r for r in reversed(runs)], stage)
        if own[2] == 'this boss':
            dps, dps_offence, dps_source = own
        target = boss_target(catalog, stage)
        if not target:
            continue

        profile, check = _profile(store, catalog, stage, target, party, front)
        simulate = bool(profile and profile['dps'])

        def run(plan=None, gear=None, extra=None):
            return fight(target, party.heroes(plan, gear, extra), party.k, dps, dps_offence)

        def sim(plan=None, gear=None, extra=None):
            return bossprofile.simulate(target, profile, party.heroes(plan, gear, extra), party.k, front) if simulate else None

        def value(plan=None, gear=None, extra=None):
            if simulate:
                result = sim(plan, gear, extra)
                return result['reach'] if result else -1.0
            return score(run(plan, gear, extra))

        def wins(plan=None, gear=None):
            if simulate:
                result = sim(plan, gear)
                return bool(result and result['won'] and result['reach'] >= RETRY_REACH), result
            result = run(plan, gear)
            return bool(result and result['margin'] is not None and result['margin'] >= SAFETY), result
        now, now_sim = run(), sim()
        base_value = value()
        # Value of one stat roll per hero, relative to now.
        rolls = []
        for hero in party.live:
            for stat, mode, amount, label in STAT_STEPS:
                extra = {hero: [{'stat': stat, 'mode': mode, 'value': amount, 'source': 'ITEM', 'layer': 'base'}]}
                trial = value(extra=extra)
                if trial > 0 and base_value > 0:
                    rolls.append({'hero': catalog.hero_name(hero, locale), 'stat': label,
                                  'margin_pct': (trial / base_value - 1) * 100,
                                  'hits': {catalog.hero_name(h['hero_key'], locale): h['hits']
                                           for h in (run(extra=extra) or {}).get('heroes', [])}})
        rolls.sort(key=lambda r: -r['margin_pct'])
        points, _ = best_points(party.options, party.plan_now, lambda p: value(plan=p), valid=party.valid)
        gear, _ = _best_gear(party, lambda g: value(plan=points, gear=g),
                             max_level=min(party.levels[h] for h in party.live))
        both, both_sim = run(points, gear), sim(points, gear)
        retry = None
        for extra in range(1, LEVELS_AHEAD + 1):
            level = min(party.levels[h] for h in party.live) + extra
            plan, _ = best_points(party.options, party.plan_now, lambda p: value(plan=p, gear=gear),
                                  extra_points=extra, max_moves=0, valid=party.valid)
            future_gear, _ = _best_gear(party, lambda g: value(plan=plan, gear=g), max_level=level)
            ok, result = wins(plan, future_gear)
            if ok:
                per_hero = (rates.get('xp_per_h') or {})
                needs = []
                for h in party.live:
                    hero = next(x for x in snapshot['heroes'] if x['hero_key'] == h)
                    need = xp_to_level(hero['level'], hero['xp'], level, catalog.level_thresholds)
                    rate = (per_hero.get(str(h)) or {}).get('value')
                    needs.append(need / rate if need is not None and rate else None)
                retry = {'level': level, 'margin': result['reach'] if simulate else result.get('margin'),
                         'hours': max(needs) if needs and None not in needs else None,
                         'points': _plan_changes(party, plan),
                         'gear': [{'hero': catalog.hero_name(h, locale), 'item': new['name'], 'level': new['level'],
                                   'slot': new['parts'], 'instead_of': old['name'] if old else None}
                                  for h, swaps in future_gear.items() for old, new in swaps]}
                break
        won_now, _ = wins()
        won_plan, _ = wins(points, gear)
        targets.append({
            'stage': stage, 'why': why, 'label': catalog.stage_label(stage, locale), 'act_boss': target['act_boss'],
            'boss_name': catalog.localize((catalog.monsters.get(str(target['monster_key'])) or {}).get('MonsterNameStringKey'), locale),
            'boss_hp': target['hp'], 'boss_damage': target['damage'], 'attacks_per_s': target['attacks_per_s'],
            'skill_factor': target['skill_factor'],
            'now': _describe(party, target, now),
            'respec': {'result': _describe(party, target, run(points)), 'changes': _plan_changes(party, points)},
            'gear': {'result': _describe(party, target, run(gear=gear)),
                     'swaps': [{'hero': catalog.hero_name(h, locale), 'item': new['name'], 'level': new['level'],
                                'slot': new['parts'], 'instead_of': old['name'] if old else None}
                               for h, swaps in gear.items() for old, new in swaps]},
            'both': _describe(party, target, both), 'retry': retry, 'stat_rolls': rolls[:10],
            'mode': 'simulation' if simulate else 'estimate',
            'simulation': {'now': _sim_view(catalog, now_sim, locale), 'plan': _sim_view(catalog, both_sim, locale)} if simulate else None,
            'mechanics': _mechanics(catalog, target, profile, party, front, locale), 'check': check,
            'winnable_now': won_now, 'winnable_with_plan': won_plan,
            # How much better the party must get for a safe win: damage dealt before the wipe (simulation) or
            # survival x damage (estimate).
            'missing_factor': ((RETRY_REACH / both_sim['reach']) if simulate and both_sim and both_sim['reach'] else
                               (SAFETY / both['margin'] if both and both['margin'] else None)),
        })
        targets[-1]['steps'] = act_boss_steps(targets[-1], {'safety': SAFETY})
    targets.sort(key=lambda t: (t['why'] != 'failed', t['stage']))
    return {
        'available': True, 'failed': failed, 'targets': targets, 'safety': SAFETY, 'last_failed': picked,
        'dps': dps, 'dps_offence': dps_offence, 'dps_source': dps_source, 'front': catalog.hero_name(front, locale) if front else None,
        'unmodelled_points': sorted({f"{catalog.hero_name(h, locale)}: {i['name']}" for (h, a), i in party.info.items()
                                     if a not in party.options.get(h, {}) and party.plan_now[h].get(a)}),
        'note': ('A fight is a race: the party must take the boss HP before the boss takes every hero. Boss hits use the '
                 'formula checked on real hits; ceil(HP / hit) matched the 6-7 hits the Priest took in the failed 2210 '
                 'fights. Boss attack speed and skill damage come from the game data and are not validated yet; healing, '
                 'block and dodge are not counted (real survival is longer). Kill time uses the damage per second measured '
                 'on stage bosses, scaled by base-attack output (estimate). Points are only moved between passives the '
                 'model can price (HP, armor, absorption, damage, speed, crit); refund is free in the game. '
                 f'A retry is suggested when the estimate wins by ×{SAFETY:g}.'),
    }
