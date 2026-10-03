"""Local HTTP API + static web UI. Binds to loopback; the browser never touches memory or saves."""
import csv
import io
import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import extensions, instance, lifetime
from .catalog.catalog import Catalog
from .catalog.extract import extract_catalog
from .collector import Collector
from .gamebuild import BuildIdentity
from .runtime.layout import find_layout
from .store import Store
from .views import analytics, challenge, combat, cube, impact, items, live, quality, suggestions

log = logging.getLogger('tbh.server')
WEB_ROOT = Path(__file__).resolve().parent.parent / 'web'


class App:
    def __init__(self, settings, collect=True):
        self.settings = settings
        self.identity = BuildIdentity(settings).identify()
        build_id = self.identity['build_id']
        self.catalog = Catalog.latest(settings, build_id)
        if not self.catalog or self.catalog.build_id != build_id:
            log.info('catalog missing for build %s: extracting from installation', build_id)
            extract_catalog(settings)
            self.catalog = Catalog.latest(settings, build_id)
        self.layout = find_layout(settings, self.identity['files'].get('GameAssembly.dll'))
        self.store = Store(settings.db_path)
        self.collector = Collector(settings, self.store, self.catalog)
        if collect and not instance.acquire(settings.data_dir):
            # Another server owns collection for this database.
            owner = instance.owner(settings.data_dir)
            log.warning('another server (pid %s) is collecting: this one serves the API read-only', owner)
            self.collector.status.update({'runtime': 'disabled', 'save': 'disabled',
                                          'runtime_reason': f'another server (pid {owner}) is collecting',
                                          'save_reason': f'another server (pid {owner}) is collecting'})
            collect = False
        self.active = collect
        self.extensions = extensions.setup(self, settings.extensions)
        if collect:
            self.collector.start()
            for ext in self.extensions:
                ext.call('start')

    def stop(self, reason):
        for ext in self.extensions:
            ext.call('stop', reason)
        self.collector.stop()

    def save(self):
        snapshot = self.collector.latest_save
        if not snapshot:
            raise LookupError('save not read yet: ' + str(self.collector.status.get('save_reason')))
        return snapshot

    def status(self):
        last = self.store.one('SELECT utc FROM samples ORDER BY id DESC LIMIT 1')
        counts = {t: self.store.one(f'SELECT COUNT(*) AS n FROM {t}')['n']
                  for t in ('samples', 'saves', 'runs', 'events', 'sessions')}
        status = {
            'identity': self.identity,
            'catalog': {'build_id': self.catalog.build_id, 'extracted_utc': self.catalog.manifest['extracted_utc'],
                        'sources': self.catalog.manifest['sources'], 'tables': len(self.catalog.manifest['tables']),
                        'rows': sum(self.catalog.manifest['tables'].values())},
            'catalog_matches_build': self.catalog.build_id == self.identity['build_id'],
            'layout': {'available': bool(self.layout),
                       'generated_utc': self.layout['generated_utc'] if self.layout else None,
                       'semantics': self.layout['semantics'] if self.layout else None},
            'collector': self.collector.status, 'session': self.collector.runtime.session,
            'last_sample_utc': last['utc'] if last else None, 'counts': counts,
            'save_meta': {k: v for k, v in (self.collector.latest_save_meta or {}).items() if k != 'signature'},
            'mode': 'read-only',
        }
        for ext in self.extensions:
            status.update(ext.call('status') or {})
        return status


def _json_default(value):
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return None
    return str(value)


ROUTES = []


def route(method, pattern):
    def register(fn):
        ROUTES.append((method, re.compile('^' + pattern + '$'), fn))
        return fn
    return register


def q(query, name, default=None, cast=str):
    values = query.get(name)
    if not values or values[0] == '':
        return default
    return cast(values[0])


@route('GET', '/api/status')
def api_status(app, query, body, match):
    return app.status()


@route('GET', '/api/live')
def api_live(app, query, body, match):
    return live.live(app)


@route('GET', '/api/combat')
def api_combat(app, query, body, match):
    return combat.combat(app)


@route('GET', '/api/act-boss')
def api_act_boss(app, query, body, match):
    return challenge.challenges(app)


@route('GET', '/api/heroes')
def api_heroes(app, query, body, match):
    snapshot = app.save()
    return {'heroes': live.heroes(app), 'equipment': items.equipment(snapshot, app.catalog)}


@route('GET', '/api/suggestions')
def api_suggestions(app, query, body, match):
    return suggestions.suggestions(app, q(query, 'hours', 168, float))


@route('GET', '/api/runs')
def api_runs(app, query, body, match):
    stage = q(query, 'stage', None, int)
    runs = analytics.list_runs(app.store, app.catalog, stage, q(query, 'limit', 200, int))
    since = min((r['started_utc'] for r in runs), default=analytics.since_iso(72))
    fights = analytics.act_boss_rows(app.store, app.catalog, stage, since)
    return sorted(runs + fights, key=lambda r: r['ended_utc'] or '', reverse=True)


@route('GET', '/api/stages')
def api_stages(app, query, body, match):
    return analytics.stage_comparison(app.store, app.catalog, q(query, 'hours', 72, float),
                                      reading=app.collector.last_combat if app.collector else None)


@route('GET', '/api/economy')
def api_economy(app, query, body, match):
    return analytics.economy(app.store, app.catalog, q(query, 'hours', 24, float))


@route('GET', '/api/quality')
def api_quality(app, query, body, match):
    return quality.quality_report(app.store, app.catalog, q(query, 'hours', 24, float),
                                  q(query, 'offset', 0, int), q(query, 'limit', 50, int),
                                  q(query, 'status', 'all'))


@route('GET', '/api/inventory')
def api_inventory(app, query, body, match):
    snapshot = app.save()
    return {'containers': items.containers(snapshot, app.catalog), 'last_saved_utc': snapshot['last_saved_utc'],
            'pending': snapshot['pending'],
            'note': 'Latest save state (the game saves about once a minute and on each clear). Quantities per slot.'}


@route('GET', '/api/chests')
def api_chests(app, query, body, match):
    return items.chests(app.save(), app.catalog)


@route('GET', '/api/cube')
def api_cube(app, query, body, match):
    return cube.cube_view(app.save(), app.catalog)


@route('GET', '/api/runes')
def api_runes(app, query, body, match):
    snapshot = app.save()
    return {'totals': list(items.rune_totals(snapshot, app.catalog).values()),
            'nodes': items.rune_tree(snapshot, app.catalog), 'gold': snapshot['gold'],
            'pet': items.pet_view(snapshot, app.catalog),
            'note': ('Totals sum the Value of every owned level; this matches the in-game Stat List '
                     '(checked 2026-09-30).')}


@route('GET', '/api/runes/impact')
def api_rune_impact(app, query, body, match):
    return impact.purchases(app.store, app.catalog, q(query, 'hours', 72, float))


@route('GET', '/api/catalog/items')
def api_catalog_items(app, query, body, match):
    text = (q(query, 'q', '') or '').lower()
    kind = q(query, 'type')
    limit = q(query, 'limit', 100, int)
    out = []
    for key, row in app.catalog.items.items():
        if kind and row['ITEMTYPE'] != kind:
            continue
        name = app.catalog.item_name(key) or ''
        if text and text not in name.lower() and text not in key:
            continue
        out.append(items.describe_item(app.catalog, {'unique_id': None, 'item_key': key}))
        if len(out) >= limit:
            break
    return out


@route('GET', '/api/catalog/stages')
def api_catalog_stages(app, query, body, match):
    from .analysis.economy import theoretical_stage_table
    return theoretical_stage_table(app.catalog)


@route('GET', '/api/events')
def api_events(app, query, body, match):
    kind = q(query, 'kind')
    sql, params = 'SELECT * FROM events', []
    if kind:
        sql += ' WHERE kind = ?'
        params.append(kind)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(q(query, 'limit', 200, int))
    rows = app.store.query(sql, params)
    for row in rows:
        row['payload'] = json.loads(row['payload']) if row['payload'] else None
    return rows


@route('GET', '/api/extensions')
def api_extensions(app, query, body, match):
    """Scripts and stylesheets the web UI loads from enabled extensions."""
    out = {'scripts': [], 'styles': []}
    for ext in app.extensions:
        web = ext.web
        out['scripts'] += [f'ext/{ext.name}/{f}' for f in web['scripts']]
        out['styles'] += [f'ext/{ext.name}/{f}' for f in web['styles']]
    return out


@route('GET', '/api/export/runs.csv')
def api_export_runs(app, query, body, match):
    rows = analytics.list_runs(app.store, app.catalog, limit=100000)
    stream = io.StringIO()
    fields = ['id', 'stage_key', 'stage_label', 'started_utc', 'ended_utc', 'duration_s', 'partial_start',
              'max_wave', 'reached_final', 'end_reason', 'outcome', 'outcome_source', 'gold_gain_est',
              'gold_spend_est', 'party', 'samples', 'gaps']
    writer = csv.DictWriter(stream, fieldnames=fields + ['xp'], extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, 'xp': json.dumps({k: v['gain'] for k, v in row['xp'].items()})})
    return ('text/csv; charset=utf-8', stream.getvalue().encode('utf-8'))


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = 'TBHAnalizer/1'

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, value):
        self._send(status, 'application/json; charset=utf-8',
                   json.dumps(value, default=_json_default, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def _origin_ok(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        return origin is None or origin in (f'http://{host}',)

    def _dispatch(self, method):
        url = urlparse(self.path)
        if not url.path.startswith('/api/'):
            return self._static(url.path) if method == 'GET' else self._json(405, {'error': 'method'})
        body = {}
        if method in ('POST', 'PUT'):
            if not self._origin_ok() or 'application/json' not in (self.headers.get('Content-Type') or ''):
                return self._json(403, {'error': 'invalid origin or content-type'})
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}') if length else {}
        for route_method, pattern, fn in ROUTES:
            match = pattern.match(url.path)
            if match and route_method == method:
                try:
                    result = fn(self.app, parse_qs(url.query), body, match)
                except LookupError as exc:
                    return self._json(404 if isinstance(exc, KeyError) else 503, {'error': str(exc)})
                except ValueError as exc:
                    return self._json(400, {'error': str(exc)})
                except Exception as exc:  # report, keep serving
                    log.exception('route error %s', url.path)
                    return self._json(500, {'error': f'{type(exc).__name__}: {exc}'})
                if isinstance(result, tuple):
                    return self._send(200, *result)
                return self._json(200, result)
        return self._json(404, {'error': 'unknown route'})

    def _static(self, path):
        root, rel = WEB_ROOT, path.lstrip('/') or 'index.html'
        if rel.startswith('ext/'):
            name, _, rel = rel[4:].partition('/')
            folder = next((e.web['folder'] for e in self.app.extensions if e.name == name), None)
            if not folder:
                return self._json(404, {'error': 'unknown file'})
            root = Path(folder).resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            if root != WEB_ROOT:
                return self._json(404, {'error': 'unknown file'})
            target = WEB_ROOT / 'index.html'
        content_type = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if content_type.startswith('text/') or content_type.endswith('javascript'):
            content_type += '; charset=utf-8'
        self._send(200, content_type, target.read_bytes())

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')


def log_readiness(app):
    """What `tbh status` checks, written once at startup: build, catalog and memory layout."""
    identity = app.identity
    log.info('game build %s%s', identity['build_id'],
             f" (problems: {', '.join(identity['problems'])})" if identity['problems'] else '')
    log.info('catalog %s', 'ok' if app.catalog.build_id == identity['build_id'] else
             f'from build {app.catalog.build_id}, not the installed one')
    if app.layout:
        log.info('memory layout ok (generated %s)', app.layout['generated_utc'])
    else:
        log.warning('no memory layout for this GameAssembly.dll: live readings are off until one exists '
                    '(python -m tbh layout --dump <folder>); saves and stored history still work')


def serve(settings, collect=True, parent_watch=True):
    app = App(settings, collect)
    Handler.app = app
    httpd = ThreadingHTTPServer((settings.host, settings.port), Handler)
    httpd.daemon_threads = True
    if parent_watch:
        # Killing the terminal must not leave a server holding the port and the collector lock.
        def launcher_gone(reason):
            log.warning('%s: shutting down', reason)
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        lifetime.watch_launcher(launcher_gone)
    log_readiness(app)
    log.info('TBH Analizer at http://%s:%s', settings.host, settings.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop('server shutting down')
        httpd.server_close()
