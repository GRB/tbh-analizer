"""Command line: python -m tbh <command>."""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import extensions
from .config import load_settings


def cmd_serve(settings, args):
    from .server import serve
    if args.port:
        settings.port = args.port
    serve(settings, collect=not args.no_collect, parent_watch=not args.no_parent_watch)


def cmd_catalog(settings, args):
    from .catalog.extract import extract_catalog
    manifest = extract_catalog(settings)
    print(json.dumps({k: manifest[k] for k in ('build_id', 'sources', 'localization')}, indent=2))
    print(f"{len(manifest['tables'])} tables, {sum(manifest['tables'].values())} rows")


def cmd_layout(settings, args):
    from .runtime.layout import build_layout
    path, layout = build_layout(settings, Path(args.dump))
    print(f"layout {layout['build_id']} for GameAssembly {layout['game_assembly_sha256'][:12]}: {path}")


def cmd_status(settings, args):
    from .catalog.catalog import Catalog
    from .gamebuild import BuildIdentity
    from .runtime.layout import find_layout
    from .runtime.reader import GameRuntime, RuntimeUnavailable
    identity = BuildIdentity(settings).identify()
    catalog = Catalog.latest(settings, identity['build_id'])
    layout = find_layout(settings, identity['files'].get('GameAssembly.dll'))
    print('build', identity['build_id'], 'problems', identity['problems'] or 'none')
    print('catalog', catalog.build_id if catalog else 'missing', '| layout', 'ok' if layout else 'missing')
    runtime = GameRuntime(settings, catalog)
    try:
        session = runtime.attach()
        sample = runtime.sample()
        print('process', session['pid'], '| stage', sample['stage_key'], 'wave', sample['wave'],
              'gold', sample['gold'], '| heroes', [(h['hero_key'], h['level']) for h in sample['heroes']],
              '| quality', sample['quality'], sample['problems'] or '')
    except RuntimeUnavailable as exc:
        print('runtime unavailable:', exc)
    finally:
        runtime.detach()


def cmd_import_saves(settings, args):
    """Read existing save copies (rolling/daily/.bak) into history. Read-only on the files."""
    from .collector import reconcile_recent
    from .save.es3 import SaveReadError, load_save
    from .save.model import normalize
    from .snapshots import store_save
    from .store import Store
    store = Store(settings.db_path)
    folder = Path(args.folder) if args.folder else settings.save_dir
    added = skipped = 0
    for path in sorted(list(folder.glob('*.es3')) + list(folder.glob('*.es3.bak'))):
        try:
            raw = load_save(path, settings.es3_password)
        except (SaveReadError, OSError, ValueError) as exc:
            print('skipped', path.name, exc)
            skipped += 1
            continue
        if store_save(store, raw, normalize(raw.player_json), datetime.now(timezone.utc).isoformat()):
            added += 1
    print(f'{added} new snapshots, {skipped} skipped; {reconcile_recent(store, None)} runs reconciled')


def cmd_rebuild_runs(settings, args):
    from .catalog.catalog import Catalog
    from .collector import rebuild_runs, reconcile_recent
    from .store import Store
    store = Store(settings.db_path)
    catalog = Catalog.latest(settings)
    count = rebuild_runs(store, catalog)
    print(f'{count} runs rebuilt; {reconcile_recent(store, None)} reconciled')


def cmd_import_capture(settings, args):
    """Load a JSONL validation capture (runtime samples) as a past observation session."""
    import time
    from .store import Store
    store = Store(settings.db_path)
    rows = [json.loads(line) for line in Path(args.file).read_text(encoding='utf-8').splitlines() if line.strip()]
    runtime = [r for r in rows if r.get('kind') == 'runtime']
    if not runtime:
        print('no samples')
        return
    session = store.insert('sessions', {'pid': None, 'process_created': None, 'build_id': args.build,
                                        'attached_utc': runtime[0]['utc'], 'detached_utc': runtime[-1]['utc'],
                                        'detach_reason': f'imported from {Path(args.file).name}'})
    for r in runtime:
        store.insert('samples', {'session_id': session, 'utc': r['utc'], 'mono': r['mono'], 'stage_key': r['stage_key'],
                                 'wave': r['wave'], 'stage_state': r['stage_state'], 'gold': r['gold'],
                                 'heroes': json.dumps(r['heroes']), 'max_completed': r['max_completed_stage'],
                                 'last_cleared': r['last_cleared_stage'], 'quality': r['quality'],
                                 'problems': json.dumps(r['problems']) if r['problems'] else None,
                                 'read_ms': r['read_ms']})
    print(f'session {session}: {len(runtime)} samples imported ({time.strftime("%X")})')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tbh', description='TBH Analizer — local analysis of TBH: Task Bar Hero')
    parser.add_argument('--config', help='alternative local.json file')
    parser.add_argument('-v', '--verbose', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('serve', help='API + web portal + collector')
    p.add_argument('--port', type=int)
    p.add_argument('--no-collect', action='store_true', help='serve already collected data only')
    p.add_argument('--no-parent-watch', action='store_true',
                   help='keep running when the launching terminal/process ends (detached launches)')
    p.set_defaults(fn=cmd_serve)
    sub.add_parser('catalog', help='extract the catalog from the installation').set_defaults(fn=cmd_catalog)
    p = sub.add_parser('layout', help='generate the memory layout from a dump of this build')
    p.add_argument('--dump', required=True, help='folder with dump.cs and script.json')
    p.set_defaults(fn=cmd_layout)
    sub.add_parser('status', help='check build, catalog, layout and one read').set_defaults(fn=cmd_status)
    p = sub.add_parser('import-saves', help='import existing save copies into history')
    p.add_argument('--folder')
    p.set_defaults(fn=cmd_import_saves)
    sub.add_parser('rebuild-runs', help='rebuild runs from samples').set_defaults(fn=cmd_rebuild_runs)
    p = sub.add_parser('import-capture', help='import a JSONL validation capture')
    p.add_argument('file')
    p.add_argument('--build', default=None)
    p.set_defaults(fn=cmd_import_capture)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config')
    for module in extensions.modules(load_settings(pre.parse_known_args(argv)[0].config).extensions):
        if hasattr(module, 'cli'):
            module.cli(sub)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    args.fn(load_settings(args.config), args)


if __name__ == '__main__':
    sys.exit(main())
