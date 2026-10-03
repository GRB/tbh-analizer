"""Pure interpretation of combat readings (tbh/runtime/combat.py).

Formulas come from native code of build 25454993 and were checked against live caches.
They are inputs to the damage
path, not measured whole-hit damage: armor, avoidance and reductions surround them.
"""
from datetime import datetime

ELEMENTS = ('Fire', 'Cold', 'Lightning')
MAX_TIMELINE = 1500   # rows kept per act boss fight (5 per second: 5 minutes)
SOURCE_ORDER = ('BASE', 'ITEM', 'ATTRIBUTE', 'PASSIVE', 'Runes', 'AccountStatus', 'StatusEffect', 'BuffSkill', 'ENVIROUNMENT')


def resistance(individual, caps):
    """Effective resistance per damage attribute (D004), in percentage points, and the factor
    `max(0, 1 - R/100)` the damage helper applies. Unknown inputs give None, never 0."""
    if individual is None:
        return None
    result = {}
    for element in (*ELEMENTS, 'Chaos'):
        own = individual.get(element)
        if own is None:
            result[element] = None
            continue
        if element == 'Chaos':
            # This branch skips the AllElement addition and the elemental cap.
            value = own
        else:
            extra, cap = individual.get('AllElement'), (caps or {}).get(element)
            value = None if extra is None or cap is None else min(own + extra, cap)
        result[element] = None if value is None else {'resistance': value, 'damage_factor': max(0.0, 1 - value / 100)}
    return result


def armor_reduction(armor, damage, level, k, cap=None):
    """Fraction of a physical hit removed by armor (D005). `k`: the game's coefficients; `level`: stage
    level. Matched observed hits on 2026-10-01 (D009) where the reduction stayed below both hero caps
    (0.75 / 0.85); which cap applies is unknown, so `cap` None means "not capped by the hero"."""
    q = armor ** 2 / (k['bfmi'] * damage + armor + 0.01) / (k['bfmj'] + k['bfmk'] * level)
    r = q / (1 + q) if q <= k['bfmr'] else k['bfmq'] - k['bfms'] / (q + k['bfmt'])
    return min(r, cap if cap is not None else 1.0, k['bfmq'])


def physical_hit(damage, armor, absorption, level, k, cap=None):
    """HP lost to one physical hit: armor reduction, then Damage Absorption subtracted as a flat amount
    (D009: 37.4 -> 3.777 observed 127 times). Block, dodge and other reductions are not applied."""
    return max(0.0, damage * (1 - armor_reduction(armor, damage, level, k, cap)) - (absorption or 0.0))


def layer_value(seed, modifiers):
    """`(seed + ΣFLAT) * (1 + ΣADDITIVE) * Π(1 + MULTIPLICATIVE)`: matched every live
    observation of AttackDamage, AttackSpeed, MaxHp, Armor and IncreaseExpAmount (D001)."""
    flat = sum(m['value'] for m in modifiers if m['mode'] == 'FLAT')
    additive = sum(m['value'] for m in modifiers if m['mode'] == 'ADDITIVE')
    product = 1.0
    for m in modifiers:
        if m['mode'] == 'MULTIPLICATIVE':
            product *= 1 + m['value']
    return (seed + flat) * (1 + additive) * product


def origins(modifiers, stats):
    """Per stat: contributions grouped by source and mode, plus whether the formula above
    reproduces the read values for this stat (so the breakdown can be trusted as complete)."""
    if modifiers is None:
        return None
    by_stat = {}
    for mod in modifiers:
        by_stat.setdefault(mod['stat'], []).append(mod)
    result = {}
    for stat, mods in by_stat.items():
        sources = {}
        for mod in mods:
            entry = sources.setdefault(mod['source'], {'FLAT': 0.0, 'ADDITIVE': 0.0, 'MULTIPLICATIVE': 1.0, 'count': 0})
            if mod['mode'] == 'MULTIPLICATIVE':
                entry['MULTIPLICATIVE'] *= 1 + mod['value']
            elif mod['mode'] in ('FLAT', 'ADDITIVE'):
                entry[mod['mode']] += mod['value']
            entry['count'] += 1
        base = layer_value(0.0, [m for m in mods if m['layer'] == 'base'])
        final = layer_value(base, [m for m in mods if m['layer'] == 'dynamic'])
        read_base = (stats or {}).get('base', {}).get(stat)
        read_final = (stats or {}).get('final', {}).get(stat)
        result[stat] = {
            'sources': [{'source': s, **sources[s]} for s in sorted(sources, key=lambda s: (
                SOURCE_ORDER.index(s) if s in SOURCE_ORDER else len(SOURCE_ORDER), str(s)))],
            'reconstructed': final,
            'matches': read_base is not None and read_final is not None
                       and _close(base, read_base) and _close(final, read_final),
        }
    return result


def _close(a, b, rel=1e-4):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def per_attack(final):
    """Attack damage x average crit multiplier: the expected damage of one base attack before enemy
    defences (skills, multi-strike and damage types are not in it)."""
    ad = final.get('AttackDamage')
    if not ad:
        return None
    chance = min(max(final.get('CriticalChance') or 0.0, 0.0), 1.0)
    return ad * (1 + chance * ((final.get('CriticalDamage') or 1.0) - 1))


def offence(final):
    """Expected base-attack damage x attack speed: an estimate of base-attack output
    (skills, damage types and enemy defences are not in it)."""
    hit, aspd = per_attack(final), final.get('AttackSpeed')
    return hit * aspd if hit and aspd else None


def attack_shares(heroes):
    """Each hero's share of the party's estimated base-attack damage, from RunCombat.summary()['heroes'].

    An estimate (attacks counted by the game x expected hit before enemy defences), not measured
    damage: skills are left out. None for every hero when one hero has unvalued attacks, because the
    shares would then be wrong; [] when no attacks were recorded (older runs)."""
    rows = [(int(k), h) for k, h in (heroes or {}).items() if 'attacks' in h]
    if not rows or not any(h['attacks'] for _, h in rows):
        return []
    total = sum(h['attack_damage_est'] for _, h in rows)
    complete = total > 0 and not any(h['unvalued_attacks'] for _, h in rows)
    return sorted(({'hero_key': key, 'attacks': h['attacks'], 'damage_est': h['attack_damage_est'],
                    'share': h['attack_damage_est'] / total if complete else None} for key, h in rows),
                  key=lambda r: -r['damage_est'])


class RunCombat:
    """Survival, buff presence and party output of the open run, from the sampled combat readings.

    Presence is a fraction of samples, not exact uptime: reads are ~1 s apart and a missing
    sample says nothing about the time between. A death is a hero entering the game's dead-unit
    list (D009: dead heroes leave the party list; they never showed 0 HP or DIE); resurrection
    takes ~90 s, so readings ~1 s apart do not miss one. A dead hero counts as 0 HP.
    """

    def __init__(self, run_key, boss=None):
        """`boss`: {'monster_key', 'min_max_hp'} of the stage boss or act boss, so its HP and the damage the
        party deals to it can be followed (the boss can be the same monster as a normal one: max HP tells)."""
        self.run_key = run_key
        self.boss_hint = boss
        self.samples = 0
        self.heroes = {}
        self.party_offence = []   # per reading where every hero's stats were readable
        self.enemy_hp = {}        # enemy id -> last HP read
        self.damage_dealt = 0.0   # HP enemies lost between readings (kills between readings are not counted)
        self.first_utc = self.first_hit_utc = self.wipe_utc = None
        self.boss = None
        # Act boss fights keep a timeline: each boss has its own attacks, learned from these (bossprofile).
        self.timeline = [] if boss and boss.get('act') else None

    def feed(self, reading):
        if not reading or reading.get('heroes') is None:
            return
        self.samples += 1
        dead = reading.get('dead') or {}
        for key in dead:
            h = self._hero(key)
            if not h['_dead']:
                h['deaths'] += 1
                h['died_utc'] = h.get('died_utc') or reading.get('utc')
            h['_dead'] = True
            h['min_hp_fraction'] = 0.0
        if reading.get('dead') is not None:
            # Back in the party and out of the dead list: resurrected.
            for key in {x['hero_key'] for x in reading['heroes']} - set(dead):
                self._hero(key)['_dead'] = False
        self._fight(reading, dead)
        self._record(reading, dead)
        outputs = [offence((h.get('stats') or {}).get('final') or {}) for h in reading['heroes']]
        if outputs and None not in outputs:
            self.party_offence.append(sum(outputs))
        for hero in reading['heroes']:
            h = self._hero(hero['hero_key'])
            h['samples'] += 1
            # Readings where HP went down: who the enemies reach (the front hero takes almost all, D009).
            if hero['hp'] is not None and h['_last_hp'] is not None and hero['hp'] < h['_last_hp']:
                h['hp_drops'] += 1
            h['_last_hp'] = hero['hp']
            self._attacks(h, hero)
            if hero['hp'] is not None and hero['max_hp']:
                fraction = hero['hp'] / hero['max_hp']
                h['min_hp_fraction'] = fraction if h['min_hp_fraction'] is None else min(h['min_hp_fraction'], fraction)
            if hero['buffs'] is not None:
                h['buff_samples'] += 1
                for buff in hero['buffs']:
                    if 'quality' not in buff:
                        h['buffs'][buff['group']] = h['buffs'].get(buff['group'], 0) + 1

    def _attacks(self, h, hero):
        """Base attacks counted by the game (`Unit.beib`), valued at the hero's stats of this reading.

        The counter starts at 0 with each stage run and keeps counting across waves; when it goes down
        it was reset, so the new value is the attacks since then. Unreadable stats leave those attacks
        unvalued (`unvalued_attacks`), never counted as 0 damage."""
        count = hero.get('attack_count')
        if count is None:
            return
        last = h['_last_attacks']
        h['_last_attacks'] = count
        if last is None:
            return                      # first reading: attacks before it belong to no known window
        new = count - last if count >= last else count
        if new <= 0:
            return
        h['attacks'] += new
        hit = per_attack((hero.get('stats') or {}).get('final') or {})
        if hit is None:
            h['unvalued_attacks'] += new
        else:
            h['attack_damage_est'] += new * hit

    def _fight(self, reading, dead):
        """Damage dealt, the boss's HP over time and when the party was hit first and wiped."""
        utc = reading.get('utc')
        self.first_utc = self.first_utc or utc
        if utc and dead and not reading['heroes'] and self.wipe_utc is None:
            self.wipe_utc = utc
        for hero in reading['heroes']:
            last = self.heroes.get(hero['hero_key'], {}).get('_last_hp')
            if utc and self.first_hit_utc is None and last is not None and hero['hp'] is not None and hero['hp'] < last:
                self.first_hit_utc = utc
        for e in reading.get('enemies') or []:
            if e.get('id') is None or e['hp'] is None:
                continue
            before = self.enemy_hp.get(e['id'])
            if before is not None and e['hp'] < before:
                self.damage_dealt += before - e['hp']
            self.enemy_hp[e['id']] = e['hp']
            hint = self.boss_hint
            if hint and e['monster_key'] == hint['monster_key'] and (e['max_hp'] or 0) >= hint.get('min_max_hp', 0):
                b = self.boss
                if b is None or b['id'] != e['id']:
                    b = self.boss = {'id': e['id'], 'monster_key': e['monster_key'], 'max_hp': e['max_hp'],
                                     'first_utc': utc, 'hit_utc': None, 'last_utc': utc, 'hp': e['hp'], 'dead': False}
                if e['hp'] < b['hp'] and b['hit_utc'] is None:
                    b['hit_utc'] = b['last_utc']   # damage began between the previous reading and this one
                b['hp'], b['last_utc'] = e['hp'], utc
        if self.boss and not self.boss['dead'] and reading.get('enemies') is not None \
                and all(e.get('id') != self.boss['id'] for e in reading['enemies']):
            self.boss['dead'] = True       # gone from the field: killed (or the fight ended)
            self.boss['end_utc'] = utc

    def _record(self, reading, dead):
        """One timeline row: seconds since the run's first reading, boss HP, HP per hero (0 when dead)."""
        if self.timeline is None or not reading.get('utc') or len(self.timeline) >= MAX_TIMELINE:
            return
        t = (datetime.fromisoformat(reading['utc']) - datetime.fromisoformat(self.first_utc)).total_seconds()
        hp = {str(h['hero_key']): round(h['hp'], 2) for h in reading['heroes'] if h['hp'] is not None}
        hp.update({str(k): 0.0 for k in dead})
        boss = self.boss['hp'] if self.boss and self.boss.get('last_utc') == reading['utc'] else None
        self.timeline.append([round(t, 2), round(boss, 1) if boss is not None else None, hp])

    def _hero(self, key):
        return self.heroes.setdefault(key, {'samples': 0, 'min_hp_fraction': None, 'deaths': 0, 'hp_drops': 0,
                                            '_dead': False, '_last_hp': None, 'buffs': {}, 'buff_samples': 0,
                                            '_last_attacks': None, 'attacks': 0, 'unvalued_attacks': 0,
                                            'attack_damage_est': 0.0})

    def summary(self):
        values = sorted(self.party_offence)
        span = lambda a, b: (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() if a and b else None
        boss = None
        if self.boss:
            b = self.boss
            lost = b['max_hp'] - (0.0 if b['dead'] else b['hp'])
            seconds = span(b['hit_utc'], b.get('end_utc') or b['last_utc'])
            boss = {'monster_key': b['monster_key'], 'max_hp': b['max_hp'], 'killed': b['dead'],
                    'hp_left_fraction': 0.0 if b['dead'] else b['hp'] / b['max_hp'] if b['max_hp'] else None,
                    'damage': lost, 'seconds': seconds,
                    # Damage per second on one target: the reading interval (~1 s) bounds its precision.
                    'dps': lost / seconds if seconds and seconds >= 3 else None}
        return {'samples': self.samples, 'timeline': self.timeline,
                'damage_dealt': self.damage_dealt, 'boss': boss,
                'first_hit_s': span(self.first_utc, self.first_hit_utc), 'wipe_s': span(self.first_utc, self.wipe_utc),
                'hits_to_wipe_s': span(self.first_hit_utc, self.wipe_utc),
                'party_offence': values[len(values) // 2] if values else None,
                'min_hp_fraction': min((h['min_hp_fraction'] for h in self.heroes.values()
                                        if h['min_hp_fraction'] is not None), default=None),
                'deaths': sum(h['deaths'] for h in self.heroes.values()),
                'heroes': {
            key: {'samples': h['samples'], 'min_hp_fraction': h['min_hp_fraction'], 'deaths': h['deaths'],
                  'hp_drops': h['hp_drops'], 'died_s': span(self.first_utc, h.get('died_utc')),
                  'attacks': h['attacks'], 'unvalued_attacks': h['unvalued_attacks'],
                  'attack_damage_est': h['attack_damage_est'],
                  'buff_presence': {group: count / h['buff_samples'] for group, count in h['buffs'].items()}
                  if h['buff_samples'] else {}}
            for key, h in self.heroes.items()}}
