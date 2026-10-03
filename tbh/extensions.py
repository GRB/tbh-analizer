"""Optional modules that extend the server and the command line.

An extension is an importable module named in `local.json` ("extensions": ["name"]) or in the
TBH_EXTENSIONS environment variable (comma separated). It may define:

- `setup(app)` -> object with optional `start()`, `stop(reason)`, `status()` (merged into
  /api/status) and `web` = (folder, [scripts], [stylesheets]) served under /ext/<name>/.
  API routes are registered with `tbh.server.route` when the module is imported.
- `cli(subparsers)` to add commands to `python -m tbh`.

A missing or broken extension is logged and skipped: the core runs the same without it.
"""
import importlib
import logging

log = logging.getLogger('tbh.extensions')


def modules(names):
    loaded = []
    for name in names or []:
        try:
            loaded.append(importlib.import_module(name))
        except Exception as exc:   # an extension must never stop the core
            log.warning('extension %s not loaded: %s', name, exc)
    return loaded


class Loaded:
    def __init__(self, name, instance):
        self.name, self.instance = name, instance

    def call(self, method, *args):
        fn = getattr(self.instance, method, None)
        if not fn:
            return None
        try:
            return fn(*args)
        except Exception:
            log.exception('extension %s: %s failed', self.name, method)
            return None

    @property
    def web(self):
        folder, scripts, styles = getattr(self.instance, 'web', None) or (None, [], [])
        return {'folder': folder, 'scripts': list(scripts), 'styles': list(styles)}


def setup(app, names):
    out = []
    for module in modules(names):
        if not hasattr(module, 'setup'):
            continue
        try:
            out.append(Loaded(module.__name__, module.setup(app)))
        except Exception:
            log.exception('extension %s: setup failed', module.__name__)
    return out
