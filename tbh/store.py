"""SQLite persistence. One connection per thread; WAL so the API reads while the collector writes."""
import json
import sqlite3
import threading
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, pid INTEGER, process_created TEXT, build_id TEXT,
  game_assembly_sha256 TEXT, layout_generated TEXT, attached_utc TEXT,
  detached_utc TEXT, detach_reason TEXT);
CREATE TABLE IF NOT EXISTS samples(
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, utc TEXT NOT NULL, mono REAL NOT NULL,
  stage_key INTEGER, wave INTEGER, stage_state INTEGER, gold INTEGER, heroes TEXT,
  max_completed INTEGER, last_cleared INTEGER, quality TEXT, problems TEXT, read_ms REAL);
CREATE INDEX IF NOT EXISTS samples_session ON samples(session_id, id);
CREATE INDEX IF NOT EXISTS samples_utc ON samples(utc);
CREATE TABLE IF NOT EXISTS saves(
  id INTEGER PRIMARY KEY, observed_utc TEXT NOT NULL, sha256 TEXT UNIQUE NOT NULL,
  file_mtime_utc TEXT, last_saved_utc TEXT, last_saved_ticks TEXT, version TEXT,
  play_time REAL, gold INTEGER, stage INTEGER, party TEXT, player_blob BLOB);
CREATE INDEX IF NOT EXISTS saves_time ON saves(last_saved_utc);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, session_id INTEGER, stage_key INTEGER, started_utc TEXT, ended_utc TEXT,
  start_mono REAL, end_mono REAL, duration_s REAL, partial_start INTEGER, max_wave INTEGER,
  wave_amount INTEGER, reached_final INTEGER, final_utc TEXT, end_reason TEXT, outcome TEXT, outcome_source TEXT,
  gold_start INTEGER, gold_end INTEGER, gold_gain_est INTEGER, gold_spend_est INTEGER,
  xp TEXT, party TEXT, samples INTEGER, gaps INTEGER, anomalies TEXT, first_sample_id INTEGER,
  last_sample_id INTEGER);
CREATE INDEX IF NOT EXISTS runs_stage ON runs(stage_key, ended_utc);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, utc TEXT NOT NULL, session_id INTEGER, run_id INTEGER,
  kind TEXT NOT NULL, payload TEXT);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, utc);
"""
# Columns added after the first release: (table, column, declaration)
MIGRATIONS = [('runs', 'combat', 'TEXT')]


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self.write_lock = threading.Lock()
        self.ensure(SCHEMA, MIGRATIONS)

    def ensure(self, schema, migrations=()):
        """Create tables (idempotent DDL) and add columns introduced later; extensions add their own."""
        with self.write_lock:
            self.conn.executescript(schema)
            for table, column, declaration in migrations:
                columns = {row[1] for row in self.conn.execute(f'PRAGMA table_info({table})')}
                if column not in columns:
                    self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
            self.conn.commit()

    @property
    def conn(self):
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._local.conn = conn
        return conn

    def query(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params)]

    def one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def insert(self, table, values):
        columns = ', '.join(values)
        marks = ', '.join('?' for _ in values)
        with self.write_lock, self.conn:
            cursor = self.conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', list(values.values()))
        return cursor.lastrowid

    def update(self, table, row_id, values, key='id'):
        assignments = ', '.join(f'{k} = ?' for k in values)
        with self.write_lock, self.conn:
            self.conn.execute(f'UPDATE {table} SET {assignments} WHERE {key} = ?', [*values.values(), row_id])

    def event(self, utc, kind, payload, session_id=None, run_id=None):
        return self.insert('events', {'utc': utc, 'session_id': session_id, 'run_id': run_id,
                                      'kind': kind, 'payload': json.dumps(payload)})

    # --- saves -------------------------------------------------------------
    def has_save(self, sha256):
        return self.one('SELECT id FROM saves WHERE sha256 = ?', (sha256,)) is not None

    @staticmethod
    def pack(text):
        return zlib.compress(text.encode('utf-8'), 6)

    @staticmethod
    def unpack(blob):
        return zlib.decompress(blob).decode('utf-8') if blob else None
