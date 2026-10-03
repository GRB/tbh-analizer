"""Per-build memory layout manifest.

Generated once from an Il2CppDumper output of *this* installation (dump.cs + script.json)
and bound to the SHA-256 of GameAssembly.dll. A different binary means no layout: the
reader refuses to guess offsets from another version.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..gamebuild import BuildIdentity

# class name in dump.cs -> fields the reader needs
REQUIRED_FIELDS = {
    'wh.wb': ['bgpr', 'bgps', 'bgpt', 'bgpw', 'bgpx', 'bgqa'],
    'wh.StageCache': ['bgqe'],
    'StageInfoData': ['StageKey'],
    'StageManager': ['HeroList', 'stageState'],
    'ob<a>': ['bdwn'],
    'Hero': ['cache'],
    'wj': ['bgtc', 'bgtv', 'bgtw', 'bgtx', 'bgty', 'bgtz'],
    'HeroInfoData': ['HeroKey'],
    'wh.ul': ['bghx'],
    'wh.um': ['bghz', 'bgic'],
    'CurrencyInfoData': ['CurrencyKey'],
    'ObscuredInt': ['currentCryptoKey', 'hiddenValue'],
    'ObscuredLong': ['currentCryptoKey', 'hiddenValue'],
    'ObscuredDouble': ['currentCryptoKey', 'hiddenValue'],
}
REQUIRED_TYPEINFO = ['wh.wb', 'ob<StageManager>', 'wh.ul']
# Combat reader (tbh/runtime/combat.py). Kept apart from REQUIRED_FIELDS: without it only the combat view is missing.
COMBAT_FIELDS = {
    'StageManager': ['beza', 'bezp'],
    'DeadUnitData': ['currentResurrectionTime'],
    'Unit': ['UnitHealthController', 'BuffManager', 'state', 'b_attacking', 'beib', 'behj', 'behl', 'beht'],
    'ActiveSkill': ['skillCache', 'skillCastDistance'],
    'wo': ['bgus'],
    'SkillInfoData': ['SkillKey'],
    'Hero': ['cache'],
    'Monster': ['befl'],
    'pp': ['beih', 'beik', 'bein'],
    'wq': ['<bgvo>k__BackingField'],
    'bbb': ['bhmt', 'bhmu', 'bhmv'],
    'wv': ['bgwc', 'bgwd'],
    'wu': ['<bgvw>k__BackingField', '<bgvx>k__BackingField', '<bgvy>k__BackingField', '<bgvz>k__BackingField'],
    'wh.vk': ['bgmb'],
    'MonsterInfoData': ['MonsterKey'],
    'baj': ['bhlu', 'bhlx'],
    'bah': ['bhlk', 'bhll', 'bhlm', 'bhln', 'bhlo'],
    'bai': ['bhls', 'bhlt'],
    'bag': ['bhlh'],
    'wh.StageCache': ['bgqm'],
    # Armor formula coefficients (static readonly floats; D005, validated on hits in D009).
    'td': ['bfmi', 'bfmj', 'bfmk', 'bfml', 'bfmm', 'bfmq', 'bfmr', 'bfms', 'bfmt'],
}
# Enums whose numeric values the combat reader translates; read from the same dump.
COMBAT_ENUMS = ['StatType', 'MODTYPE', 'MODSOURCE', 'EDamageAttribute', 'DamageableType', 'EUNITSTATE']
# Meaning of obfuscated fields, as established by read-only observation.
SEMANTICS = {
    'wh.wb.bgpr': 'max completed stage (matches save maxCompletedStage)',
    'wh.wb.bgps': 'last cleared stage (matches save lastClearedStageKey)',
    'wh.wb.bgpw': 'current stage key',
    'wh.wb.bgpx': 'current wave (0..WaveAmount+1)',
    'wh.wb.bgqa': 'current StageCache',
    'wj.bgtz': 'hero XP within level (matched saved HeroExp)',
    'wj.bgtv': 'hero level candidate',
    'wh.um.bgic': 'currency quantity (ObscuredLong)',
}
# IL2CPP v31 Il2CppClass offsets used by the reader; verified on this build by the probes.
IL2CPP = {'class_name': 0x10, 'class_static_fields': 0xB8, 'array_length': 0x18, 'array_data': 0x20,
          'list_items': 0x10, 'list_size': 0x18}


def field_map(dump, name):
    match = re.search(r'(?:public|private|internal)[\w ]* (?:class|struct) ' + re.escape(name)
                      + r'(?=\s|:)[^\n]*\n\{(.*?)(?=\n\t// (?:Properties|Methods)|\n\})', dump, re.S)
    if not match:
        raise KeyError(f'class {name} not found in the dump')
    # Compiler backing fields keep their full name (`<bgvo>k__BackingField`).
    return {field: int(offset, 16) for field, offset in
            re.findall(r'(?<![\w<>])([<>\w]+); // 0x([0-9A-F]+)', match[1])}


def enum_values(dump, name):
    return {int(value): key for key, value in re.findall(r'public const ' + re.escape(name) + r' (\w+) = (-?\d+);', dump)}


def subclasses(dump, base):
    return sorted(set(re.findall(r'^public (?:sealed )?class (\w+) : ' + re.escape(base) + r'\b', dump, re.M)))


def combat_layout(dump, metadata=None):
    fields = {}
    for cls, needed in COMBAT_FIELDS.items():
        found = field_map(dump, cls)
        missing = [f for f in needed if f not in found]
        if missing:
            raise KeyError(f'{cls}: missing combat fields {missing}')
        fields[cls] = {f: found[f] for f in needed}
    td = next((x['Address'] for x in metadata or [] if x['Name'] == 'td_TypeInfo'), None)
    return {'fields': fields, 'enums': {name: enum_values(dump, name) for name in COMBAT_ENUMS}, 'td_typeinfo_rva': td,
            # Runtime classes of buffs (D002/D003); `bag` expires on an attack count (D003).
            'buff_classes': subclasses(dump, 'bah')}


def build_layout(settings, dump_dir):
    identity = BuildIdentity(settings).identify()
    dump_path = dump_dir / 'dump.cs'
    dump = dump_path.read_text(encoding='utf-8-sig')
    metadata = json.loads((dump_dir / 'script.json').read_text(encoding='utf-8'))['ScriptMetadata']
    fields = {}
    for cls, needed in REQUIRED_FIELDS.items():
        found = field_map(dump, cls)
        missing = [f for f in needed if f not in found]
        if missing:
            raise KeyError(f'{cls}: campos em falta {missing}')
        fields[cls] = {f: found[f] for f in needed}
    typeinfo = {}
    for name in REQUIRED_TYPEINFO:
        entry = next((x for x in metadata if x['Name'] == name + '_TypeInfo'), None)
        if not entry:
            raise KeyError(f'TypeInfo {name} not found')
        typeinfo[name] = entry['Address']
    layout = {
        'build_id': identity['build_id'],
        'game_assembly_sha256': identity['files'].get('GameAssembly.dll'),
        'metadata_sha256': identity['files'].get('global-metadata.dat'),
        'dump_sha256': hashlib.sha256(dump_path.read_bytes()).hexdigest().upper(),
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'fields': fields, 'typeinfo_rva': typeinfo, 'il2cpp': IL2CPP, 'semantics': SEMANTICS,
        'combat': combat_layout(dump, metadata),
        # Native decoders verified by disassembly of this binary.
        'actk': {'int': 'sub-xor', 'long': 'sub-xor', 'double': 'permute[1,0,2,3,7,4,6,5]+xor',
                 'decoder_rva': {'double': '0x759FC0', 'int': '0x72CF30', 'long': '0x725D30'}},
    }
    settings.layout_root.mkdir(parents=True, exist_ok=True)
    path = settings.layout_root / f"{identity['build_id']}-{layout['game_assembly_sha256'][:12]}.json"
    path.write_text(json.dumps(layout, indent=2), encoding='utf-8')
    return path, layout


# Layouts shipped with the code, one per published game build (offsets and field names only).
BUNDLED_LAYOUTS = Path(__file__).resolve().parent.parent / 'layouts'


def find_layout(settings, game_assembly_sha256):
    """The layout for this exact GameAssembly.dll: one generated locally first, then a bundled one."""
    paths = sorted(settings.layout_root.glob('*.json')) + sorted(BUNDLED_LAYOUTS.glob('*.json'))
    for path in paths:
        layout = json.loads(path.read_text(encoding='utf-8'))
        if layout.get('game_assembly_sha256') == game_assembly_sha256:
            return layout
    return None
