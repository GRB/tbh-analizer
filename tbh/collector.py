"""Collector: observes the save file and the running game, persists observations,
segments runs and reconciles them with the save counters. Read-only towards the game."""
import json
import logging
import threading
import time
from datetime import datetime, timezone

from .analysis import actboss
from .analysis.combat import RunCombat
from .analysis.reconcile import reconcile
from .analysis.runs import GAP_CLOSE_SECONDS, RunTracker
from .runtime.combat import CombatReader, CombatUnavailable
from .runtime.process import find_processes
from .runtime.reader import GameRuntime, RuntimeUnavailable
from .config import PROCESS_NAME
from .save.es3 import SaveReadError, load_save
from .save.model import normalize
from .snapshots import snapshots_since, store_save

log = logging.getLogger('tbh.collector')
FAST_COMBAT_SECONDS = 0.2   # combat reading interval during act boss fights (~4 ms per reading)
RUN_COLUMNS = ('session_id', 'stage_key', 'started_utc', 'ended_utc', 'start_mono', 'end_mono', 'duration_s',
               'partial_start', 'max_wave', 'wave_amount', 'reached_final', 'final_utc', 'end_reason', 'outcome',
               'outcome_source', 'gold_start', 'gold_end', 'gold_gain_est', 'gold_spend_est', 'samples', 'gaps',
               'first_sample_id', 'last_sample_id')


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Collector:
    def __init__(self, settings, store, catalog):
        self.settings = settings
        self.store = store
        self.catalog = catalog
        self.runtime = GameRuntime(settings, catalog)
        self.tracker = RunTracker(catalog, self._run_closed, self._tracker_event)
        self.session_id = None
        self.stop_event = threading.Event()
        self.thread = None
        self.last_sample = None
        self.last_stored = None
        self.last_save_check = 0.0
        self.latest_save = None       # normalized snapshot of the newest save
        self.latest_save_meta = None
        self.status = {'runtime': 'idle', 'runtime_reason': None, 'save': 'idle', 'save_reason': None,
                       'started_utc': None, 'samples_stored': 0, 'last_error': None, 'combat_reason': None}
        self._next_attach = 0.0
        self._resume = None
        self.combat = None            # CombatReader for the attached session, if the layout has it
        self.last_combat = None       # latest combat reading (memory only)
        self.run_combat = None        # RunCombat of the open run
        self._stats_key = None        # party base stats last stored as a `hero_stats` event
        self._act_bosses = None       # act boss stage keys (catalog), loaded on first use

    # --- lifecycle -------------------------------------------------------------
    def start(self):
        self.status['started_utc'] = utcnow()
        # A previous process that died without detaching left its session open; its last
        # run was never closed and stays absent (not invented).
        with self.store.write_lock, self.store.conn:
            self.store.conn.execute("UPDATE sessions SET detached_utc = ?, detach_reason = 'collector interrupted' "
                                    "WHERE detached_utc IS NULL", (self.status['started_utc'],))
        self.thread = threading.Thread(target=self._loop, name='collector', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        self._detach('collector stopped')

    def _loop(self):
        self._poll_save(force=True)
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self._tick_runtime()
                if started - self.last_save_check >= self.settings.save_poll_interval:
                    self._poll_save()
            except Exception as exc:  # keep observing; record the failure
                log.exception('collector tick failed')
                self.status['last_error'] = f'{type(exc).__name__}: {exc}'
            deadline = started + self.settings.sample_interval
            # Act boss fights last ~20 s and each boss has its own attacks: read them 5x per second.
            while self._act_boss_fight() and time.monotonic() + FAST_COMBAT_SECONDS < deadline \
                    and not self.stop_event.wait(FAST_COMBAT_SECONDS):
                self._fast_combat()
            self.stop_event.wait(max(0.05, deadline - time.monotonic()))

    # --- runtime -----------------------------------------------------------------
    def _attach(self):
        if time.monotonic() < self._next_attach:
            return False
        try:
            session = self.runtime.attach()
        except (RuntimeUnavailable, OSError) as exc:
            self.status['runtime'] = 'unavailable'
            self.status['runtime_reason'] = str(exc)
            self._next_attach = time.monotonic() + 10
            return False
        self.session_id = self.store.insert('sessions', {k: session[k] for k in (
            'pid', 'process_created', 'build_id', 'game_assembly_sha256', 'layout_generated', 'attached_utc')})
        self.store.event(session['attached_utc'], 'attach', session, self.session_id)
        self.status['runtime'] = 'attached'
        self.status['runtime_reason'] = None
        try:
            self.combat, self.status['combat_reason'] = CombatReader(self.runtime), None
        except CombatUnavailable as exc:
            self.combat, self.status['combat_reason'] = None, str(exc)
        self.last_stored = None
        self._resume = open_run_to_resume(self.store, self.session_id, session)
        return True

    def _detach(self, reason):
        self.tracker.stop('unobserved_end')  # the open run is persisted by the callback
        if self.session_id:
            self.store.update('sessions', self.session_id, {'detached_utc': utcnow(), 'detach_reason': reason})
            self.store.event(utcnow(), 'detach', {'reason': reason}, self.session_id)
        self.runtime.detach()
        self.session_id = None
        self.combat = self.last_combat = self.run_combat = None
        self._stats_key = None

    def _tick_runtime(self):
        if not self.runtime.mem and not self._attach():
            return
        try:
            sample = self.runtime.sample()
        except RuntimeUnavailable as exc:
            self._detach(str(exc))
            self.status['runtime'] = 'unavailable'
            self.status['runtime_reason'] = str(exc)
            return
        sample['session_id'] = self.session_id
        self.last_sample = sample
        if self._should_store(sample):
            sample['id'] = self.store.insert('samples', {
                'session_id': self.session_id, 'utc': sample['utc'], 'mono': sample['mono'],
                'stage_key': sample['stage_key'], 'wave': sample['wave'], 'stage_state': sample['stage_state'],
                'gold': sample['gold'], 'heroes': json.dumps(sample['heroes']),
                'max_completed': sample['max_completed_stage'], 'last_cleared': sample['last_cleared_stage'],
                'quality': sample['quality'], 'problems': json.dumps(sample['problems']) if sample['problems'] else None,
                'read_ms': sample['read_ms']})
            self.status['samples_stored'] += 1
            self.last_stored = sample
            if self._resume:
                rows, partial = self._resume
                self._resume = None
                self.tracker.resume(rows, sample, partial)
            self.tracker.feed(sample)
        self._tick_combat(sample)

    def _tick_combat(self, sample):
        """Combat state is shown live and summarised for the open run; it never affects samples or runs."""
        if not self.combat:
            return
        try:
            reading = self.combat.sample()
        except Exception as exc:  # a combat read failure must not stop the collector
            log.warning('combat read failed: %s', exc)
            reading = {'utc': sample['utc'], 'heroes': None, 'enemies': None, 'stage_level': None,
                       'quality': 'error', 'problems': [f'{type(exc).__name__}: {exc}'], 'read_ms': None}
        reading['stage_key'] = sample['stage_key']
        self.last_combat = reading
        run = self.tracker.run
        key = (run['started_utc'], run['stage_key']) if run else None
        if not self.run_combat or self.run_combat.run_key != key:
            self.run_combat = RunCombat(key, boss_hint(self.catalog, run['stage_key'])) if key else None
        if self.run_combat:
            self.run_combat.feed(reading)
        self._store_stats(reading)

    def _act_boss_fight(self):
        stage = (self.last_sample or {}).get('stage_key')
        if stage is None or not self.combat or not self.run_combat:
            return False
        if self._act_bosses is None:
            self._act_bosses = actboss.act_boss_stages(self.catalog)
        return stage in self._act_bosses

    def _fast_combat(self):
        """An extra combat reading between samples, fed to the open run only (not shown, not stored alone)."""
        try:
            reading = self.combat.sample()
        except Exception as exc:
            log.warning('fast combat read failed: %s', exc)
            return
        reading['stage_key'] = (self.last_sample or {}).get('stage_key')
        self.run_combat.feed(reading)

    def _store_stats(self, reading):
        """Party stats before buffs, stored when they change (gear, runes, attributes, level-ups), so a power
        change can be shown as its stat effect right away (views/impact.py). Buffs are left out: they come
        and go every few seconds."""
        heroes = reading.get('heroes')
        if not heroes or any(not h.get('stats') for h in heroes):
            return
        payload = {str(h['hero_key']): {k: round(v, 6) for k, v in h['stats']['base'].items() if v}
                   for h in heroes}
        key = json.dumps(payload, sort_keys=True)
        if key != self._stats_key:
            self._stats_key = key
            self.store.event(reading['utc'], 'hero_stats', payload, self.session_id)

    def _should_store(self, sample):
        last = self.last_stored
        if last is None:
            return True
        keys = ('stage_key', 'wave', 'stage_state', 'gold', 'quality')
        if any(sample[k] != last[k] for k in keys):
            return True
        if [(h['hero_key'], h['level'], h['xp']) for h in sample['heroes']] != \
           [(h['hero_key'], h['level'], h['xp']) for h in last['heroes']]:
            return True
        return sample['mono'] - last['mono'] >= self.settings.heartbeat_seconds

    # --- runs ------------------------------------------------------------------
    def _run_closed(self, run):
        # The tracker closes a run before the next reading is fed, so run_combat still holds it.
        if self.run_combat and self.run_combat.run_key == (run['started_utc'], run['stage_key']):
            run['combat'] = self.run_combat.summary()
        persist_run(self.store, run)

    def _tracker_event(self, kind, utc, payload, run):
        # Events belong to a run that has no row yet; attach them when it closes.
        run.setdefault('_pending_events', []).append((kind, utc, payload))

    # --- saves -------------------------------------------------------------------
    def _poll_save(self, force=False):
        self.last_save_check = time.monotonic()
        path = self.settings.save_file
        try:
            stat = path.stat()
        except OSError as exc:
            self.status['save'], self.status['save_reason'] = 'unavailable', str(exc)
            return
        signature = (stat.st_size, stat.st_mtime_ns)
        if not force and self.latest_save_meta and self.latest_save_meta.get('signature') == signature:
            return
        try:
            raw = load_save(path, self.settings.es3_password)
        except (SaveReadError, OSError, ValueError) as exc:
            self.status['save'], self.status['save_reason'] = 'error', str(exc)
            return
        snapshot = normalize(raw.player_json)
        self.latest_save = snapshot
        self.latest_save_meta = {'signature': signature, 'sha256': raw.sha256, 'file_mtime_utc': raw.mtime_utc,
                                 'read_utc': utcnow()}
        self.status['save'], self.status['save_reason'] = 'ok', None
        if snapshot['unmapped_keys']:
            self.status['save_reason'] = f"new fields in the save: {snapshot['unmapped_keys']}"
        if store_save(self.store, raw, snapshot, utcnow()):
            reconcile_recent(self.store)


def boss_hint(catalog, stage_key):
    """Which enemy is the boss of a stage: its monster key and the max HP above which it is the boss
    (on normal stages the boss can also appear as an ordinary monster with x BossHpMultiplier less HP)."""
    stage = catalog.stages.get(str(stage_key)) if catalog and stage_key else None
    monster = catalog.monsters.get(stage.get('BossMonsterKey')) if stage else None
    scale = catalog.stage_levels.get(stage.get('StageLevel')) if stage else None
    if not monster or not scale or not monster.get('MaxLife'):
        return None
    normal = float(monster['MaxLife']) * int(scale['MonsterHpMultiplier']) / 1000
    act = stage['STAGETYPE'] != 'NORMAL'
    return {'monster_key': int(stage['BossMonsterKey']), 'min_max_hp': 0 if act else normal * 1.5, 'act': act}


def reconcile_recent(store, limit_saves=40, since='0'):
    snapshots = snapshots_since(store, since, limit_saves)
    if len(snapshots) < 2:
        return 0
    oldest = snapshots[0]['last_saved_utc']
    runs = store.query("SELECT * FROM runs WHERE ended_utc >= ? "
                       "AND (end_reason = 'wave_reset' OR (end_reason = 'stage_change' "
                       "AND (wave_amount = 0 OR final_utc IS NOT NULL))) "
                       "AND COALESCE(outcome_source, '') NOT LIKE 'save%'", (oldest,))
    decided = reconcile(runs, snapshots)
    for run_id, (outcome, source) in decided.items():
        store.update('runs', run_id, {'outcome': outcome, 'outcome_source': source})
    return len(decided)


def open_run_to_resume(store, session_id, session):
    """Samples of the run a previous server left open on this same game process, or None.

    A server restart used to lose the run in progress and record the next one as a partial
    start. When the previous session watched the same process (pid + creation time) and its last
    sample is recent enough to be an ordinary gap, the open run is replayed and continues.
    A run the previous server persisted as `unobserved_end` on a clean stop is superseded:
    runs are derived from samples, so that row (and its events) are replaced by the continued run."""
    prev = store.one('SELECT id FROM sessions WHERE id < ? AND pid = ? AND process_created = ? ORDER BY id DESC LIMIT 1',
                     (session_id, session['pid'], session['process_created']))
    if not prev:
        return None
    last = store.one('SELECT utc FROM samples WHERE session_id = ? ORDER BY id DESC LIMIT 1', (prev['id'],))
    if not last:
        return None
    age = (datetime.fromisoformat(session['attached_utc']) - datetime.fromisoformat(last['utc'])).total_seconds()
    if age > GAP_CLOSE_SECONDS:
        return None
    # By sample range, not runs.session_id: a run continued across a restart keeps the session of its
    # first sample, so "the last run of the previous session" may carry an older session id
    # (a session_id lookup replayed the whole session as a duplicate partial run, 2026-10-01).
    first = store.one('SELECT MIN(id) AS id FROM samples WHERE session_id = ?', (prev['id'],))
    run = store.one('SELECT id, end_reason, partial_start, first_sample_id, last_sample_id FROM runs '
                    'WHERE last_sample_id >= ? ORDER BY id DESC LIMIT 1', (first['id'],))
    if run and run['end_reason'] == 'unobserved_end':
        start, partial = run['first_sample_id'], bool(run['partial_start'])
        with store.write_lock, store.conn:
            store.conn.execute('DELETE FROM events WHERE run_id = ?', (run['id'],))
            store.conn.execute('DELETE FROM runs WHERE id = ?', (run['id'],))
    elif run:
        start, partial = run['last_sample_id'], False   # the open run began at the closing boundary
    else:
        start, partial = 0, True                        # the whole session was one run, start unseen
    rows = store.query('SELECT * FROM samples WHERE session_id = ? AND id >= ? ORDER BY id', (prev['id'], start or 0))
    for row in rows:
        row['heroes'] = json.loads(row['heroes'] or '[]')
    return (rows, partial) if rows else None


def persist_run(store, run):
    values = {k: run.get(k) for k in RUN_COLUMNS}
    values['partial_start'] = int(bool(run['partial_start']))
    values['reached_final'] = int(bool(run['reached_final']))
    values['xp'] = json.dumps(run['xp'])
    values['party'] = run['party']
    values['anomalies'] = json.dumps(run['anomalies'][:50]) if run['anomalies'] else None
    values['gold_gain_est'] = run['gold_gain_est'] if run['gold_unknown_steps'] == 0 else None
    values['combat'] = json.dumps(run['combat']) if run.get('combat') else None
    run['id'] = store.insert('runs', values)
    for kind, utc, payload in run.pop('_pending_events', []):
        store.event(utc, kind, payload, run.get('session_id'), run['id'])
    return run['id']


def rebuild_runs(store, catalog):
    """Recompute all runs from stored samples (after a segmentation change). Combat summaries cannot be
    recomputed (combat readings are not stored); they are kept for runs that start at the same time."""
    combat = {(r['started_utc'], r['stage_key']): r['combat']
              for r in store.query('SELECT started_utc, stage_key, combat FROM runs WHERE combat IS NOT NULL')}
    with store.write_lock, store.conn:
        store.conn.execute('DELETE FROM runs')
        store.conn.execute("DELETE FROM events WHERE kind IN ('gold_spend', 'level_up')")
    count = 0
    for session in store.query('SELECT id FROM sessions ORDER BY id'):
        closed = []
        tracker = RunTracker(catalog, closed.append,
                             lambda kind, utc, payload, run: run.setdefault('_pending_events', []).append((kind, utc, payload)))
        for row in store.query('SELECT * FROM samples WHERE session_id = ? ORDER BY id', (session['id'],)):
            row['heroes'] = json.loads(row['heroes'] or '[]')
            tracker.feed(row)
        tracker.stop('unobserved_end')
        for run in closed:
            kept = combat.get((run['started_utc'], run['stage_key']))
            run['combat'] = json.loads(kept) if kept else None
            persist_run(store, run)
            count += 1
    return count


def game_running():
    try:
        return bool(find_processes(PROCESS_NAME))
    except OSError:
        return False
