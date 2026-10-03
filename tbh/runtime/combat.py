"""Read combat state of the running game: party HP, named stats and their origins, buffs,
resistance inputs and the live enemy roster. Read-only; never invokes game code.

Field semantics were established by read-only observation and native code (D001-D006).
Every collection distinguishes unreadable (None) from proven empty ([] / {}): a list that
cannot be read is unknown, never "no buffs" or "no enemies".
"""
import math
import struct
import time
from datetime import datetime, timezone

# IL2CPP generic collection layouts of this build (il2cpp.h), checked around each read.
DICT_HEADER, DICT_ENTRIES = 0x18, 0x20      # entries ptr, count, freeList, freeCount, version
HASHSET_HEADER, HASHSET_VERSION = 0x18, 0x38  # slots ptr, count, lastIndex
LIST_HEADER = 0x10                          # items ptr, size, version
ARRAY_LENGTH, ARRAY_DATA = 0x18, 0x20
MAX_UNITS = 512
STAT_REFRESH_SECONDS = 10   # modifier lists cost ~25 ms per read and change on gear/buff events


class CombatUnavailable(Exception):
    pass


class CombatReader:
    def __init__(self, runtime):
        self.r = runtime
        combat = (runtime.layout or {}).get('combat')
        if not combat:
            raise CombatUnavailable('layout predates the combat reader: regenerate it with `python -m tbh layout`')
        self.f = combat['fields']
        # JSON object keys are strings; enum values are ints in memory.
        self.enums = {name: {int(k): v for k, v in values.items()} for name, values in combat['enums'].items()}
        self.buff_classes = set(combat['buff_classes'])
        self._stats = {}  # hero object -> (mono, modifier list); read less often than the rest
        self.td_rva = combat.get('td_typeinfo_rva')
        self._armor = None

    @property
    def m(self):
        return self.r.mem

    # --- primitives --------------------------------------------------------------
    def _read(self, obj, cls, field, fmt='Q'):
        return self.m.scalar(obj + self.f[cls][field], fmt) if obj else None

    def _cls(self, obj):
        return self.r._class_name(self.m.scalar(obj)) if obj else None

    def _enum(self, name, value):
        return self.enums[name].get(value, value)

    def dictionary(self, obj, values='f', limit=512):
        """int/enum-keyed Dictionary with float ('f') or reference ('Q') values; None if unreadable."""
        if self._cls(obj) != 'Dictionary`2':
            return None
        header = self.m.read(obj + DICT_HEADER, 24)
        if len(header) != 24:
            return None
        entries, count, _, free, _ = struct.unpack('<Qiiii', header)
        if not 0 <= free <= count <= limit:
            return None
        if count == 0:
            return {}
        capacity = self.m.scalar(entries + ARRAY_LENGTH) if entries else None
        if capacity is None or not count <= capacity <= 65536:
            return None
        stride, offset = (24, 16) if values == 'Q' else (16, 12)
        raw = self.m.read(entries + ARRAY_DATA, count * stride)
        if len(raw) != count * stride:
            return None
        result = {}
        for i in range(count):
            hash_code, _, key = struct.unpack_from('<iii', raw, i * stride)
            if hash_code < 0:   # removed entry
                continue
            value = struct.unpack_from('<' + values, raw, i * stride + offset)[0]
            if values == 'f' and not math.isfinite(value):
                return None
            result[key] = value
        if self.m.read(obj + DICT_HEADER, 24) != header or len(result) != count - free:
            return None
        return result

    def hashset(self, obj):
        if self._cls(obj) != 'HashSet`1':
            return None
        header = self.m.read(obj + HASHSET_HEADER, 16)
        if len(header) != 16:
            return None
        slots, count, last = struct.unpack('<Qii', header)
        if not 0 <= count <= last <= MAX_UNITS:
            return None
        if last == 0:
            return []
        capacity = self.m.scalar(slots + ARRAY_LENGTH) if slots else None
        if capacity is None or not last <= capacity <= 65536:
            return None
        version = self.m.scalar(obj + HASHSET_VERSION, 'i')
        raw = self.m.read(slots + ARRAY_DATA, last * 16)
        if len(raw) != last * 16:
            return None
        found = [struct.unpack_from('<Q', raw, i * 16 + 8)[0] for i in range(last)
                 if struct.unpack_from('<i', raw, i * 16)[0] >= 0]
        if len(found) != count or self.m.read(obj + HASHSET_HEADER, 16) != header \
                or self.m.scalar(obj + HASHSET_VERSION, 'i') != version:
            return None
        return found

    def refs(self, obj, limit=256):
        """List<T> of references; None if unreadable, [] if proven empty."""
        if self._cls(obj) != 'List`1':
            return None
        header = self.m.read(obj + LIST_HEADER, 16)
        if len(header) != 16:
            return None
        array, size, _ = struct.unpack('<Qii', header)
        capacity = self.m.scalar(array + ARRAY_LENGTH) if array else None
        if capacity is None or not 0 <= size <= min(limit, capacity):
            return None
        raw = self.m.read(array + ARRAY_DATA, size * 8) if size else b''
        if len(raw) != size * 8 or self.m.read(obj + LIST_HEADER, 16) != header:
            return None
        return list(struct.unpack('<' + 'Q' * size, raw))

    # --- composite ---------------------------------------------------------------
    def modifier(self, obj):
        if self._cls(obj) != 'wu':
            return None
        fields = self.f['wu']
        start = fields['<bgvw>k__BackingField']
        raw = self.m.read(obj + start, 16)
        if len(raw) != 16:
            return None
        values = {}
        for name, key in (('stat', '<bgvw>k__BackingField'), ('mode', '<bgvx>k__BackingField'),
                          ('value', '<bgvy>k__BackingField'), ('source', '<bgvz>k__BackingField')):
            values[name] = struct.unpack_from('<f' if name == 'value' else '<i', raw, fields[key] - start)[0]
        if not math.isfinite(values['value']):
            return None
        return {'stat': self._enum('StatType', values['stat']), 'mode': self._enum('MODTYPE', values['mode']),
                'value': values['value'], 'source': self._enum('MODSOURCE', values['source'])}

    def hero_key(self, hero):
        cache = self._read(hero, 'Hero', 'cache')
        info = self.m.scalar(cache + self.r._f('wj', 'bgtc')) if cache else None
        return self.m.scalar(info + self.r._f('HeroInfoData', 'HeroKey'), 'i') if info else None

    def health(self, unit):
        controller = self._read(unit, 'Unit', 'UnitHealthController')
        # The controller must point back at its unit (D001); otherwise the pointer is stale.
        if not controller or self._read(controller, 'pp', 'beih') != unit:
            return None, None
        hp, max_hp = self._read(controller, 'pp', 'beik', 'f'), self._read(controller, 'pp', 'bein', 'f')
        if hp is None or max_hp is None or not (math.isfinite(hp) and math.isfinite(max_hp)) or max_hp <= 0:
            return None, None
        return hp, max_hp

    def stat_holder(self, hero):
        cache = self._read(hero, 'Hero', 'cache')
        holder = self._read(cache, 'wq', '<bgvo>k__BackingField')
        return holder if self._cls(holder) == 'bbb' else None

    def stats(self, holder):
        """Base and final stat layers (D001), keyed by StatType name; None if unreadable."""
        layers = {}
        for name, field in (('base', 'bhmu'), ('final', 'bhmv')):
            values = self.dictionary(self._read(holder, 'bbb', field))
            if values is None:
                return None
            layers[name] = {self._enum('StatType', k): v for k, v in values.items() if k}
        return layers

    def modifiers(self, holder):
        """Every stat modifier with its origin; `base` layer seeds the final (`dynamic`) one."""
        owner = self._read(holder, 'bbb', 'bhmt')
        if self._cls(owner) != 'wv':
            return None
        result = []
        for layer, field in (('base', 'bgwc'), ('dynamic', 'bgwd')):
            groups = self.dictionary(self._read(owner, 'wv', field), 'Q')
            if groups is None:
                return None
            for group in groups.values():
                items = self.refs(group)
                if items is None:
                    return None
                result.extend({**mod, 'layer': layer} for mod in map(self.modifier, items) if mod)
        return result

    def resistances(self, unit):
        names = self.enums['EDamageAttribute']
        out = {}
        for name, field in (('individual', 'behj'), ('caps', 'behl')):
            values = self.dictionary(self._read(unit, 'Unit', field))
            out[name] = None if values is None else {names.get(k, str(k)): v for k, v in values.items()}
        return out

    def buffs(self, unit):
        manager = self._read(unit, 'Unit', 'BuffManager')
        if self._cls(manager) != 'baj' or self._read(manager, 'baj', 'bhlu') != unit:
            return None
        groups = self.dictionary(self._read(manager, 'baj', 'bhlx'), 'Q')
        if groups is None:
            return None
        result = []
        for group, buff in groups.items():
            kind = self._cls(buff)
            if kind not in self.buff_classes:
                result.append({'group': group, 'class': kind, 'quality': 'unsupported'})
                continue
            mods = self.refs(self._read(buff, 'bah', 'bhll'))
            caster = self._read(buff, 'bah', 'bhlm')
            entry = {'group': self._read(buff, 'bah', 'bhlk', 'i') or group, 'class': kind,
                     # Raw caster link; only meaningful for skill buffs (D002: environment buffs reuse it).
                     'caster_key': self.hero_key(caster) if self._cls(caster) == 'Hero' else None,
                     'modifiers': None if mods is None else [m for m in map(self.modifier, mods) if m],
                     'expiry': self.expiry(buff)}
            result.append(entry)
        return result

    def expiry(self, buff):
        """How the buff ends (D003/D006). No controller does not mean the buff is absent."""
        timer = self._read(buff, 'bah', 'bhlo')
        kind = self._cls(timer)
        if not timer:
            return {'kind': 'none'}
        if self._read(timer, 'bai', 'bhls') != buff:
            return {'kind': 'unknown'}
        if kind == 'bag':
            # Removed when the normal-attack count becomes strictly greater than this threshold.
            return {'kind': 'attack_count', 'threshold': self._read(timer, 'bag', 'bhlh', 'i')}
        if kind == 'bba':
            # Configured duration (input / 100, scaled by SkillDurationIncrease), not a countdown.
            value = self._read(timer, 'bai', 'bhlt', 'f')
            return {'kind': 'duration', 'configured_s': value if value is not None and math.isfinite(value) else None}
        if kind == 'baz':
            return {'kind': 'event'}
        return {'kind': 'unknown', 'class': kind}

    def skills(self, unit):
        """Active skill objects of a unit (D001): skill key and cast distance; None if unreadable.
        Their timer fields have no proven meaning, so cooldowns are not read."""
        items = self.refs(self._read(unit, 'Unit', 'beht'), limit=16)
        if items is None:
            return None
        result = []
        for obj in items:
            cache = self._read(obj, 'ActiveSkill', 'skillCache')
            info = self._read(cache, 'wo', 'bgus') if self._cls(cache) == 'wo' else None
            key = self._read(info, 'SkillInfoData', 'SkillKey', 'i')
            if key is None:
                return None
            distance = self._read(obj, 'ActiveSkill', 'skillCastDistance', 'f')
            result.append({'skill_key': key, 'class': self._cls(obj),
                           'cast_distance': distance if distance is not None and math.isfinite(distance) else None})
        return result

    def hero(self, obj, slot, now):
        hp, max_hp = self.health(obj)
        state = self._read(obj, 'Unit', 'state', 'i')
        attacking = self._read(obj, 'Unit', 'b_attacking', 'B')
        out = {'hero_key': self.hero_key(obj), 'slot': slot, 'hp': hp, 'max_hp': max_hp,
               'state': self._enum('EUNITSTATE', state) if state is not None else None,
               'attacking': bool(attacking) if attacking in (0, 1) else None,
               'attack_count': self._read(obj, 'Unit', 'beib', 'i'),
               'resistances': self.resistances(obj), 'buffs': self.buffs(obj), 'skills': self.skills(obj)}
        holder = self.stat_holder(obj)
        out['stats'] = self.stats(holder) if holder else None
        cached = self._stats.get(obj)
        if holder and (not cached or cached[1] is None or now - cached[0] >= STAT_REFRESH_SECONDS):
            cached = (now, self.modifiers(holder))
            self._stats[obj] = cached
        out['modifiers'], out['modifiers_age_s'] = (cached[1], round(now - cached[0], 1)) if cached else (None, None)
        return out

    def enemy(self, obj):
        cache = self._read(obj, 'Monster', 'befl')
        info = self._read(cache, 'wh.vk', 'bgmb') if self._cls(cache) == 'vk' else None
        hp, max_hp = self.health(obj)
        state = self._read(obj, 'Unit', 'state', 'i')
        # The object address identifies one enemy across readings of a fight (objects can be pooled
        # and reused later, so it is not an identity across fights).
        return {'id': str(obj), 'monster_key': self._read(info, 'MonsterInfoData', 'MonsterKey', 'i'), 'hp': hp,
                'max_hp': max_hp, 'state': self._enum('EUNITSTATE', state) if state is not None else None}

    def dead(self, manager):
        """{hero_key: seconds until resurrection} (D009). A dead hero leaves the party list instead of
        reaching 0 HP or state DIE, so this dictionary is the only death signal; the value counted down
        in real time from 90 s. None if unreadable."""
        entries = self.dictionary(self.m.scalar(manager + self.f['StageManager']['bezp']), 'Q')
        if entries is None:
            return None
        out = {}
        for key, data in entries.items():
            left = self._read(data, 'DeadUnitData', 'currentResurrectionTime', 'f') if self._cls(data) == 'DeadUnitData' else None
            out[key] = left if left is not None and math.isfinite(left) else None
        return out

    def armor_constants(self):
        """Coefficients of the armor formula (static readonly, read once per attach); None if unreadable."""
        if self._armor is None and self.td_rva is not None:
            klass = self.m.scalar(self.r.base + self.td_rva)
            # The TypeInfo slot can still hold an unresolved token: check the class name first.
            statics = self.m.scalar(klass + 0xB8) if klass and self.r._class_name(klass) == 'td' else None
            values = {k: self._read(statics, 'td', k, 'f') for k in self.f['td']} if statics else None
            if values and all(v is not None and math.isfinite(v) for v in values.values()):
                self._armor = values
        return self._armor

    def stage_level(self):
        statics = self.r.statics.get('wh.wb')
        cache = self.m.scalar(statics + self.r._f('wh.wb', 'bgqa')) if statics else None
        return self.r._obscured_int(cache + self.f['wh.StageCache']['bgqm']) if cache else None

    def sample(self):
        started, now = time.perf_counter(), time.monotonic()
        problems = []
        statics = self.r.statics.get('ob<StageManager>')
        manager = self.m.scalar(statics + self.r._f('ob<a>', 'bdwn')) if statics else None
        out = {'utc': datetime.now(timezone.utc).isoformat(), 'heroes': None, 'enemies': None, 'dead': None,
               'stage_level': self.stage_level(), 'armor_constants': self.armor_constants()}
        if not manager or self._cls(manager) != 'StageManager':
            problems.append('StageManager unavailable')
        else:
            array = self.m.scalar(manager + self.r._f('StageManager', 'HeroList'))
            size = self.m.scalar(array + ARRAY_LENGTH) if array else None
            if size is None or not 0 <= size <= 16:
                problems.append('party list unreadable')
            else:
                heroes = [self.m.scalar(array + ARRAY_DATA + i * 8) for i in range(size)]
                out['heroes'] = [self.hero(h, i, now) for i, h in enumerate(heroes) if self._cls(h) == 'Hero']
                live = set(heroes)
                self._stats = {k: v for k, v in self._stats.items() if k in live}
            out['dead'] = self.dead(manager)
            if out['dead'] is None:
                problems.append('dead-unit list unreadable')
            groups = self.dictionary(self.m.scalar(manager + self.f['StageManager']['beza']), 'Q')
            monster_type = next((k for k, v in self.enums['DamageableType'].items() if v == 'Monster'), None)
            if groups is None:
                problems.append('damageable groups unreadable')
            elif monster_type not in groups:
                out['enemies'] = []
            else:
                units = self.hashset(groups[monster_type])
                if units is None:
                    problems.append('enemy set unreadable')
                else:
                    out['enemies'] = [self.enemy(u) for u in units if self._cls(u) == 'Monster']
        for hero in out['heroes'] or []:
            for part in ('hp', 'buffs', 'stats'):
                if hero[part] is None:
                    problems.append(f"hero {hero['hero_key']}: {part} unreadable")
        out['problems'] = problems
        out['quality'] = 'ok' if not problems else 'partial'
        out['read_ms'] = round((time.perf_counter() - started) * 1000, 2)
        return out
