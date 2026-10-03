"""Stored save snapshots, reduced to the fields analyses need and cached per row id."""
import json
from pathlib import Path

from .analysis.impact import power_state
from .save.model import normalize

_cache = {}
LIGHT_KEYS = ('last_saved_utc', 'play_time', 'current_stage', 'party', 'gold', 'heroes', 'aggregates', 'runes',
              'max_completed_stage')


def store_save(store, raw, snapshot, observed_utc):
    """Insert a decoded save once (keyed by file SHA-256). Returns True when new."""
    if store.has_save(raw.sha256):
        return False
    store.insert('saves', {
        'observed_utc': observed_utc, 'sha256': raw.sha256, 'file_mtime_utc': raw.mtime_utc,
        'last_saved_utc': snapshot['last_saved_utc'], 'last_saved_ticks': snapshot['last_saved_ticks'],
        'version': snapshot['version'], 'play_time': snapshot['play_time'], 'gold': snapshot['gold'],
        'stage': snapshot['current_stage'], 'party': json.dumps(snapshot['party']),
        'player_blob': store.pack(raw.player_json)})
    return True


def light_snapshot(store, save_id):
    cache_key = (str(Path(store.path).resolve()), save_id)
    cached = _cache.get(cache_key)
    if cached:
        return cached
    row = store.one('SELECT player_blob FROM saves WHERE id = ?', (save_id,))
    snap = normalize(store.unpack(row['player_blob']))
    light = {k: snap[k] for k in LIGHT_KEYS}
    light['heroes'] = [{k: h[k] for k in ('hero_key', 'level', 'xp')} for h in snap['heroes']]
    light['power'] = power_state(snap)
    light['save_id'] = save_id
    _cache[cache_key] = light
    return light


def snapshots_since(store, since_iso, limit=None):
    sql = 'SELECT id FROM saves WHERE last_saved_utc >= ? ORDER BY last_saved_utc'
    rows = store.query(sql, (since_iso,))
    if limit:
        rows = rows[-limit:]
    return [light_snapshot(store, r['id']) for r in rows]
