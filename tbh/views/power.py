"""Live stat effects of gear swaps and rune levels for the party heroes (see analysis/power.py)."""
from datetime import datetime, timezone

from ..analysis import power, threat
from ..analysis.combat import origins
from .items import equipment, rune_totals, stat_name


def item_raw(item, enums):
    """Raw {(stat, mode): value} of one item, or None when it holds something we cannot map
    (a unique mod has no stat values in the save)."""
    if item.get('unique_mod') or item.get('chaotic'):
        return None
    raw = {}
    for st in item.get('stats') or []:
        if st.get('stat') and st['stat'] != 'NONE':
            key = (st['stat'], st.get('mod') or 'FLAT')
            if key[1] == 'MULTIPLICATIVE' or st.get('value') is None:
                return None  # raw sums cannot represent products of several modifiers
            raw[key] = raw.get(key, 0) + (st.get('value') or 0)
    for e in item.get('enchants') or []:
        stat, mode = enums['StatType'].get(e.get('stat')), enums['MODTYPE'].get(e.get('mod_type'))
        if not stat or not mode or mode == 'MULTIPLICATIVE' or e.get('value') is None:
            return None
        raw[(stat, mode)] = raw.get((stat, mode), 0) + (e.get('value') or 0)
    return raw


def _add(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out


class PowerContext:
    """Built per request from the latest combat reading and save; empty when either is missing."""

    def __init__(self, app, snapshot, locale='en-US'):
        self.catalog, self.locale = app.catalog, locale
        collector = app.collector
        reading = self.reading = collector.last_combat if collector else None
        reader = collector.combat if collector else None
        if reading:
            utc = reading.get('utc')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(utc)).total_seconds() if utc else None
            if age is None or not 0 <= age <= 30 or reading.get('quality') == 'error':
                reading = self.reading = None
        self.heroes = {h['hero_key']: h for h in (reading or {}).get('heroes') or [] if h.get('modifiers')}
        self.heroes = {key: h for key, h in self.heroes.items()
                       if all(v['matches'] for v in (origins(h['modifiers'], h.get('stats')) or {}).values())}
        self.enums = reader.enums if reader else None
        self.gear_scales, self.rune_scales, self.worn, self.rune_raw = {}, {}, {}, {}
        self.xp_inputs = None
        if not self.heroes or not self.enums or not snapshot:
            return
        gear_obs = []
        for hero in equipment(snapshot, self.catalog, locale):
            self.worn[hero['hero_key']] = {e['slot_part']: e for e in hero['equipment']}
            live = self.heroes.get(hero['hero_key'])
            raws = [item_raw(e, self.enums) for e in hero['equipment']]
            if live and None not in raws:
                total = {}
                for raw in raws:
                    total = _add(total, raw)
                gear_obs.append((power.sums(live['modifiers'], 'ITEM'), total))
        self.gear_scales = power.learn_scales(gear_obs)
        runes = rune_totals(snapshot, self.catalog, locale)
        raw = self.rune_raw = {power.RUNE_TARGETS[s]: (t.get('value') or 0) for s, t in runes.items() if s in power.RUNE_TARGETS}
        xp_rune = ((runes.get('IncreaseExpAmount') or {}).get('effect') or {}).get('value')
        pet = (self.catalog.index('PetInfoData', 'PetKey').get(str(snapshot.get('pet'))) or {}).get('StatDataKey')
        pet_xp = sum(float(r['Value']) for r in self.catalog.table('PetStatInfoData')
                     if r['PetStatKey'] == pet and r['STATTYPE'] == 'IncreaseExpAmount')
        self.xp_inputs = None if xp_rune is None else (xp_rune, pet_xp)
        # Only the (stat, mode) the runes feed: other account bonuses (pet...) are not modelled here.
        self.rune_scales = power.learn_scales(
            [({k: v for k, v in power.sums(h['modifiers'], 'AccountStatus').items() if k in raw}, raw)
             for h in self.heroes.values()])

    @property
    def available(self):
        return bool(self.heroes)

    def name(self, stat):
        return stat_name(self.catalog, stat, self.locale)

    def hero_name(self, hero_key):
        return self.catalog.hero_name(hero_key, self.locale)

    def gear(self, hero_key, current, candidate):
        """Effect of wearing `candidate` instead of `current` (None = empty slot), or None if unknown."""
        live = self.heroes.get(hero_key)
        if not live:
            return None
        old = item_raw(current, self.enums) if current else {}
        new = item_raw(candidate, self.enums)
        if old is None or new is None:
            return None
        remove, unknown_old = power.to_mods(old, self.gear_scales, 'ITEM')
        add, unknown_new = power.to_mods(new, self.gear_scales, 'ITEM')
        effect = power.change(live['modifiers'], 'ITEM', remove, add)
        effect['unknown'] = sorted({self.name(k[0]) for k in unknown_old + unknown_new})
        effect['defence'] = self.defence(hero_key, effect)
        return effect

    def rune_part(self):
        """{(stat, mode): value} the owned runes add to every hero, for keys whose scale is proven."""
        return {key: raw * self.rune_scales[key] for key, raw in self.rune_raw.items() if key in self.rune_scales and raw}

    def defence(self, hero_key, effect):
        """Boss hit of the current stage on this hero before and after a change (analysis/threat.py)."""
        live, reading = self.heroes.get(hero_key), self.reading or {}
        if not live or not reading.get('armor_constants') or not reading.get('stage_key'):
            return None
        final = (live.get('stats') or {}).get('final') or {}
        after = {**final, **{s: v['after'] for s, v in effect['stats'].items()}}
        out = {}
        for name, stats in (('before', final), ('after', after)):
            t = threat.stage_threat(self.catalog, reading['stage_key'], {
                'final': stats, 'resistances': live.get('resistances'), 'max_hp': stats.get('MaxHp')}, reading['armor_constants'])
            boss = (t or {}).get('boss')
            if not boss or not boss.get('hits_to_die'):
                return None
            out[f'hit_{name}'], out[f'hits_{name}'] = boss['hit'], boss['hits_to_die']
        return out if abs(out['hit_after'] - out['hit_before']) > 1e-6 or abs(out['hits_after'] - out['hits_before']) > 1e-6 else None

    def rune(self, stat, raw_step):
        """{hero_key: effect} of one more rune level, or None when the rune's scale is not proven."""
        target = power.RUNE_TARGETS.get(stat)
        if not target or target not in self.rune_scales or not raw_step:
            return None
        add = [{'stat': target[0], 'mode': target[1], 'value': raw_step * self.rune_scales[target],
                'source': 'AccountStatus', 'layer': 'base'}]
        out = {}
        for key, h in self.heroes.items():
            effect = power.change(h['modifiers'], 'AccountStatus', (), add)
            effect['defence'] = self.defence(key, effect)
            out[key] = effect
        return out

    def xp_pct(self, effect):
        """XP change per kill (%) of a change in the hero XP stat, from the measured XP formula."""
        xp = effect['stats'].get('IncreaseExpAmount')
        if not xp or self.xp_inputs is None:
            return None
        before, after = (power.xp_factor(*self.xp_inputs, xp[k]) for k in ('before', 'after'))
        return (after / before - 1) * 100

    def text(self, effect):
        parts = power.summary(effect, self.name)
        xp = self.xp_pct(effect)
        if xp is not None:
            parts += f" (≈{xp:+.1f}% XP for this hero)"
        if effect.get('offence_pct') is not None:
            parts += f"{'; ' if parts else ''}base-attack output {effect['offence_pct']:+.1f}% (estimate)"
        guard = effect.get('defence')
        if guard:
            parts += (f"{'; ' if parts else ''}stage boss hit {guard['hit_before']:.0f}→{guard['hit_after']:.0f} HP "
                      f"({guard['hits_before']:.1f}→{guard['hits_after']:.1f} hits to die)")
        if effect.get('unknown'):
            parts += f"{'; ' if parts else ''}not counted (scale unknown): {', '.join(effect['unknown'])}"
        return parts
