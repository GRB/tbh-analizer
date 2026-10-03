"""Extract static game data from the local installation into a per-build catalog.

Sources: CSV TextAssets in `sharedassets0.assets` and Unity Localization bundles.
No community catalog is consulted. Output lives under `build/app/catalog/<build_id>/`.
"""
import csv
import io
import json
from datetime import datetime, timezone

from ..gamebuild import BuildIdentity, sha256_file

LOCALES = ('en-US', 'pt-BR')


def _load_unitypy():
    try:
        import UnityPy
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit('UnityPy em falta: .venv\\Scripts\\pip install UnityPy') from exc
    return UnityPy


def extract_tables(data_dir):
    UnityPy = _load_unitypy()
    env = UnityPy.load(str(data_dir / 'sharedassets0.assets'))
    tables = {}
    for obj in env.objects:
        if obj.type.name != 'TextAsset':
            continue
        tree = obj.read_typetree()
        text = tree.get('m_Script', '')
        if isinstance(text, bytes):
            text = text.decode('utf-8')
        text = text.lstrip('﻿')
        reader = csv.DictReader(io.StringIO(text, newline=''))
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            continue
        tables[tree['m_Name']] = list(reader)
    return tables


def extract_localization(data_dir):
    UnityPy = _load_unitypy()
    bundle_dir = data_dir / 'StreamingAssets' / 'aa' / 'StandaloneWindows64'
    shared, locale_tables, sources = {}, [], []
    for path in sorted(bundle_dir.glob('localization-*.bundle')):
        name = path.name.lower()
        if not ('shared' in name or 'english' in name or 'portuguese' in name):
            continue
        sources.append(path)
        for obj in UnityPy.load(str(path)).objects:
            if obj.type.name != 'MonoBehaviour':
                continue
            tree = obj.read_typetree()
            if 'm_TableCollectionName' in tree:
                shared[obj.path_id] = {
                    'collection': tree['m_TableCollectionName'],
                    'keys': {e['m_Id']: e['m_Key'] for e in tree['m_Entries']},
                }
            elif 'm_LocaleId' in tree:
                locale_tables.append(tree)
    result = {locale: {} for locale in LOCALES}
    for table in locale_tables:
        locale = table['m_LocaleId']['m_Code']
        if locale not in result:
            continue
        info = shared.get(table['m_SharedData']['m_PathID'])
        if not info:
            continue
        target = result[locale].setdefault(info['collection'], {})
        for entry in table['m_TableData']:
            key = info['keys'].get(entry['m_Id'])
            if key is not None:
                target[key] = entry['m_Localized']
    return result, sources


def extract_catalog(settings):
    identity = BuildIdentity(settings).identify()
    build_id = identity['build_id'] or 'unknown'
    data_dir = settings.install_dir / 'TaskBarHero_Data'
    out = settings.catalog_root / build_id
    (out / 'tables').mkdir(parents=True, exist_ok=True)
    tables = extract_tables(data_dir)
    for name, rows in tables.items():
        (out / 'tables' / f'{name}.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    localization, bundles = extract_localization(data_dir)
    for locale, collections in localization.items():
        (out / f'localization-{locale}.json').write_text(json.dumps(collections, ensure_ascii=False), encoding='utf-8')
    sources = [data_dir / 'sharedassets0.assets', *bundles]
    manifest = {
        'build_id': build_id,
        'extracted_utc': datetime.now(timezone.utc).isoformat(),
        'identity': identity,
        'sources': {p.name: sha256_file(p) for p in sources},
        'tables': {name: len(rows) for name, rows in sorted(tables.items())},
        'localization': {loc: {c: len(v) for c, v in cols.items()} for loc, cols in localization.items()},
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    return manifest
