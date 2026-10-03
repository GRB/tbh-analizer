"""Read runtime state (stage, wave, combat state, gold, active hero XP) of the running game.

Every sample carries its quality. A value that cannot be read or validated is None
(unknown), never zero. Attach refuses to read when the binary hash has no layout.
"""
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import PROCESS_NAME
from ..gamebuild import BuildIdentity
from .layout import find_layout
from .process import ProcessReader, find_processes

STAGE_STATES = {0: 'NONE', 1: 'MONSTERSPAWN', 2: 'BATTLE', 3: 'REORGANIZATION'}
GOLD_CURRENCY_KEY = 100001
MAX_GOLD = 10 ** 15   # far above any balance seen; a torn ObscuredLong read lands near 2**63
EXPECTED_CLASS = {'wh.wb': 'wb', 'ob<StageManager>': 'ob`1', 'wh.ul': 'ul'}


def decode_int(hidden, key, bits=32):
    """ACTk ObscuredInt/Long as implemented by this build: (hidden - key) ^ key."""
    mask = (1 << bits) - 1
    value = ((hidden - key) & mask) ^ (key & mask)
    return value - (1 << bits) if value >> (bits - 1) else value


def decode_double(hidden, key):
    raw = struct.pack('<Q', hidden)
    permuted = int.from_bytes(bytes(raw[i] for i in (1, 0, 2, 3, 7, 4, 6, 5)), 'little')
    return struct.unpack('<d', struct.pack('<Q', permuted ^ key))[0]


class RuntimeUnavailable(Exception):
    pass


class GameRuntime:
    def __init__(self, settings, catalog):
        self.settings = settings
        self.catalog = catalog
        self.mem = None
        self.layout = None
        self.session = None

    # --- attach ----------------------------------------------------------
    def attach(self):
        self.detach()
        pids = find_processes(PROCESS_NAME)
        if not pids:
            raise RuntimeUnavailable('Game is not running')
        if len(pids) > 1:
            raise RuntimeUnavailable(f'Multiple instances ({pids}); multiple instances are not supported')
        mem = ProcessReader(pids[0])
        try:
            module = next((m for m in mem.modules() if m['name'].lower() == 'gameassembly.dll'), None)
            if not module:
                raise RuntimeUnavailable('GameAssembly.dll not loaded yet')
            identity = BuildIdentity(self.settings).identify()
            if Path(module['path']).resolve() != (self.settings.install_dir / 'GameAssembly.dll').resolve():
                raise RuntimeUnavailable(f"Process uses a different binary: {module['path']}")
            sha = identity['files'].get('GameAssembly.dll')
            layout = find_layout(self.settings, sha)
            if not layout:
                raise RuntimeUnavailable(f'No layout for GameAssembly {sha[:12] if sha else "?"}; '
                                         'generate one with `python -m tbh layout` from a dump of this build')
            self.mem, self.layout = mem, layout
            self.base = module['base']
            self.statics = {name: self._static_fields(name) for name in layout['typeinfo_rva']}
            self.session = {
                'pid': pids[0], 'process_created': str(mem.creation_time()),
                'build_id': identity['build_id'], 'game_assembly_sha256': sha,
                'layout_generated': layout['generated_utc'],
                'attached_utc': datetime.now(timezone.utc).isoformat(),
            }
            return self.session
        except Exception:
            mem.close()
            self.mem = None
            raise

    def detach(self):
        if self.mem:
            self.mem.close()
        self.mem, self.session = None, None

    def _class_name(self, klass):
        name_ptr = self.mem.scalar(klass + self.layout['il2cpp']['class_name']) if klass else None
        return self.mem.cstring(name_ptr) if name_ptr else None

    def _static_fields(self, name):
        klass = self.mem.scalar(self.base + self.layout['typeinfo_rva'][name])
        # A TypeInfo slot can hold an unresolved metadata token instead of a pointer.
        if not klass or self._class_name(klass) != EXPECTED_CLASS[name]:
            return None
        return self.mem.scalar(klass + self.layout['il2cpp']['class_static_fields'])

    # --- primitive reads ---------------------------------------------------
    def _f(self, cls, field):
        return self.layout['fields'][cls][field]

    def _obscured_int(self, address, typename='ObscuredInt'):
        bits = 64 if typename == 'ObscuredLong' else 32
        fmt = 'Q' if bits == 64 else 'I'
        hidden = self.mem.scalar(address + self._f(typename, 'hiddenValue'), fmt)
        key = self.mem.scalar(address + self._f(typename, 'currentCryptoKey'), fmt)
        return None if hidden is None or key is None else decode_int(hidden, key, bits)

    def _obscured_double(self, address):
        hidden = self.mem.scalar(address + self._f('ObscuredDouble', 'hiddenValue'))
        key = self.mem.scalar(address + self._f('ObscuredDouble', 'currentCryptoKey'))
        return None if hidden is None or key is None else decode_double(hidden, key)

    def _list(self, address, limit=64):
        il = self.layout['il2cpp']
        items = self.mem.scalar(address + il['list_items']) if address else None
        size = self.mem.scalar(address + il['list_size'], 'i') if address else None
        if not items or size is None or not 0 <= size <= limit:
            return []
        return [self.mem.scalar(items + il['array_data'] + i * 8) for i in range(size)]

    # --- composite reads -------------------------------------------------------
    def _read_stage(self, out, problems):
        statics = self.statics.get('wh.wb')
        if not statics:
            problems.append('stage: TypeInfo wb unresolved')
            return
        out['stage_key'] = self._obscured_int(statics + self._f('wh.wb', 'bgpw'))
        out['wave'] = self._obscured_int(statics + self._f('wh.wb', 'bgpx'))
        out['max_completed_stage'] = self._obscured_int(statics + self._f('wh.wb', 'bgpr'))
        out['last_cleared_stage'] = self._obscured_int(statics + self._f('wh.wb', 'bgps'))
        cache = self.mem.scalar(statics + self._f('wh.wb', 'bgqa'))
        info = self.mem.scalar(cache + self._f('wh.StageCache', 'bgqe')) if cache else None
        out['stage_cache_key'] = self.mem.scalar(info + self._f('StageInfoData', 'StageKey'), 'i') if info else None
        stage = self.catalog.stages.get(str(out['stage_key'])) if self.catalog else None
        if not stage:
            problems.append(f"stage {out['stage_key']} not found in the catalog")
            out['stage_key'] = None
        elif out['stage_cache_key'] not in (None, out['stage_key']):
            problems.append('stage atual e StageCache divergem')
        # Act boss stages have no WaveAmount (empty in the catalog): the range check does not apply.
        amount = int(stage['WaveAmount']) if stage and stage['WaveAmount'] else None
        if amount is not None and out['wave'] is not None and not 0 <= out['wave'] <= amount + 2:
            problems.append(f"wave {out['wave']} outside the stage range")
            out['wave'] = None

    def _read_manager(self, out, problems):
        statics = self.statics.get('ob<StageManager>')
        manager = self.mem.scalar(statics + self._f('ob<a>', 'bdwn')) if statics else None
        if not manager or self._class_name(self.mem.scalar(manager)) != 'StageManager':
            problems.append('StageManager unavailable')
            return
        state = self.mem.scalar(manager + self._f('StageManager', 'stageState'), 'i')
        out['stage_state'] = state if state in STAGE_STATES else None
        array = self.mem.scalar(manager + self._f('StageManager', 'HeroList'))
        il = self.layout['il2cpp']
        count = self.mem.scalar(array + il['array_length'], 'Q') if array else 0
        heroes = []
        if count and 0 < count <= 16:
            for i in range(count):
                hero = self.mem.scalar(array + il['array_data'] + i * 8)
                cache = self.mem.scalar(hero + self._f('Hero', 'cache')) if hero else None
                info = self.mem.scalar(cache + self._f('wj', 'bgtc')) if cache else None
                if not info:
                    continue
                key = self.mem.scalar(info + self._f('HeroInfoData', 'HeroKey'), 'i')
                if self.catalog and str(key) not in self.catalog.heroes:
                    problems.append(f'hero {key} not found in the catalog')
                    continue
                level = self._obscured_int(cache + self._f('wj', 'bgtv'))
                xp = self._obscured_double(cache + self._f('wj', 'bgtz'))
                if xp is not None and not (0 <= xp < 1e15):
                    xp = None
                heroes.append({'hero_key': key, 'level': level if level and 0 < level <= 200 else None,
                               'xp': xp, 'slot': i})
        out['heroes'] = heroes

    def _read_gold(self, out, problems):
        statics = self.statics.get('wh.ul')
        if not statics:
            problems.append('currency: TypeInfo ul unresolved')
            return
        for currency in self._list(self.mem.scalar(statics + self._f('wh.ul', 'bghx'))):
            info = self.mem.scalar(currency + self._f('wh.um', 'bghz')) if currency else None
            key = self.mem.scalar(info + self._f('CurrencyInfoData', 'CurrencyKey'), 'i') if info else None
            if key == GOLD_CURRENCY_KEY:
                # Hidden value and key are separate fields: a read that lands between the game's writes
                # decodes to garbage (7.78e18 once in 62 712 samples, 2026-10-01). Accept two equal reads only.
                address = currency + self._f('wh.um', 'bgic')
                value = self._obscured_int(address, 'ObscuredLong')
                if value != self._obscured_int(address, 'ObscuredLong') or value is None or not 0 <= value < MAX_GOLD:
                    problems.append('gold unreadable (changed during the read or out of range)')
                    value = None
                out['gold'] = value
                return
        problems.append('gold not found in the currency cache')

    def sample(self):
        if not self.mem or not self.mem.alive():
            raise RuntimeUnavailable('Processo terminou')
        # IL2CPP fills TypeInfo slots lazily: attaching during the game's boot finds them still
        # unresolved. Retry those each sample instead of staying partial until the next restart.
        for name, statics in self.statics.items():
            if not statics:
                self.statics[name] = self._static_fields(name)
        started = time.perf_counter()
        for attempt in range(3):
            out = {'stage_key': None, 'wave': None, 'stage_state': None, 'gold': None, 'heroes': [],
                   'max_completed_stage': None, 'last_cleared_stage': None}
            problems = []
            self._read_stage(out, problems)
            self._read_manager(out, problems)
            self._read_gold(out, problems)
            # Reads are not atomic: re-read the frame markers and retry when they moved.
            check = {}
            self._read_stage(check, [])
            if (check.get('stage_key'), check.get('wave')) == (out['stage_key'], out['wave']):
                break
        else:
            problems.append('inconsistent reading (frame changed during the read)')
        out['utc'] = datetime.now(timezone.utc).isoformat()
        out['mono'] = time.monotonic()
        out['read_ms'] = round((time.perf_counter() - started) * 1000, 2)
        out['attempts'] = attempt + 1
        out['problems'] = problems
        out['quality'] = 'ok' if not problems else 'partial'
        return out
