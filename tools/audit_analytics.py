"""Read-only audit of the local evidence. No historical DB writes.

Run: python -m tools.audit_analytics --output build/reports/analytics-audit.json
Only aggregate counts and diagnostics are exported; no account or item-instance data.
"""
import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tbh.analysis.economy import windows
from tbh.catalog.catalog import Catalog
from tbh.config import load_settings
from tbh.save.model import normalize
from tbh.snapshots import snapshots_since
from tbh.store import Store
from tbh.views.analytics import complete_runs, stage_comparison, unique_runs
from tbh.views.cube import cube_view
from tbh.views.items import containers, equipment
from tbh.views.suggestions import suggestions


class ReadOnlyStore:
    unpack = staticmethod(Store.unpack)

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        self.conn.row_factory = sqlite3.Row

    def query(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None


def audit(settings):
    store = ReadOnlyStore(settings.db_path)
    try:
        store.conn.execute('BEGIN')  # consistent snapshot while the collector keeps running
        catalog = Catalog.latest(settings)
        counts = {t: store.one(f'SELECT COUNT(*) n FROM {t}')['n']
                  for t in ('sessions', 'saves', 'samples', 'runs', 'events')}
        snapshots = snapshots_since(store, '0')
        ws = windows(snapshots, catalog.level_thresholds)
        runs = store.query('SELECT * FROM runs')
        eligible = complete_runs(store, catalog)
        comparison = stage_comparison(store, catalog, hours=None)
        latest = store.one('SELECT player_blob FROM saves ORDER BY last_saved_utc DESC LIMIT 1')
        snap = normalize(store.unpack(latest['player_blob'])) if latest else None
        issues, ownership, synthesis = Counter(), {}, []
        if snap:
            for name, row in containers(snap, catalog).items():
                ownership[name] = {k: row[k] for k in ('slots_unlocked', 'slots_used', 'quantity_total')}
                issues['unresolved_container_items'] += sum(not it['known'] for it in row['items'])
                issues['unknown_quantities'] += sum(it['quantity'] is None for it in row['items'])
            issues['unresolved_equipped_items'] = sum(not it['known'] for h in equipment(snap, catalog) for it in h['equipment'])
            for g in cube_view(snap, catalog)['synthesis']['groups']:
                synthesis.append({'type': g['synthesis_type'], 'grade': g['grade'], 'quantity': g['quantity'],
                                  'in_selected_range': g['in_selected_range'],
                                  'options': [{k: o[k] for k in ('tier', 'eligible_quantity', 'batches')}
                                              for o in g['options']]})
        collector = SimpleNamespace(latest_save=snap, latest_save_meta={}, last_sample=None,
                                    last_combat=None, combat=None, tracker=SimpleNamespace(run=None))
        app = SimpleNamespace(store=store, catalog=catalog, collector=collector, save=lambda: snap)
        cards = suggestions(app)['suggestions'] if snap else []
        return {
            'generated_utc': datetime.now(timezone.utc).isoformat(), 'catalog_build': catalog.build_id,
            'database_integrity': store.one('PRAGMA quick_check'), 'counts': counts,
            'catalog': {p.stem: len(catalog.table(p.stem)) for p in sorted((catalog.root / 'tables').glob('*.json'))},
            'history': {'first_save': snapshots[0]['last_saved_utc'] if snapshots else None,
                        'last_save': snapshots[-1]['last_saved_utc'] if snapshots else None},
            'sample_quality': store.query('SELECT quality, COUNT(*) n FROM samples GROUP BY quality'),
            'runs': {'unique': len(unique_runs(runs)), 'eligible_for_comparison': len(eligible),
                     'with_gaps': sum(bool(r['gaps']) for r in runs),
                     'with_anomalies': sum(bool(json.loads(r['anomalies'] or '[]')) for r in runs),
                     'outcomes': dict(Counter(r['outcome'] for r in runs)),
                     'outcome_sources': dict(Counter(r['outcome_source'] for r in runs)),
                     'unknown_gold': sum(r['gold_gain_est'] is None for r in runs)},
            'save_windows': {'total': len(ws), 'eligible': sum(w['valid'] for w in ws),
                             'exclusions': dict(Counter(reason for w in ws for reason in set(w['reasons'])))},
            'item_integrity': dict(issues), 'ownership': ownership, 'synthesis': synthesis,
            'suggestion_smoke_check': {'total': len(cards), 'areas': dict(Counter(c['area'] for c in cards)),
                                      'basis': dict(Counter(c['basis'] for c in cards))},
            'event_kinds': store.query('SELECT kind, COUNT(*) n FROM events GROUP BY kind'),
            'current_cohort': {'since': comparison['current_cohort_since'],
                               'stages': [{k: e[k] for k in ('stage', 'runs', 'gold_h', 'xp_h', 'gold_confidence', 'xp_confidence')}
                                          for e in comparison['current_evidence']]},
            'stage_evidence': [{k: e[k] for k in ('stage', 'party', 'runs', 'gold_runs', 'xp_runs',
                               'gold_h', 'xp_h', 'gold_confidence', 'xp_confidence', 'changes_since',
                               'changes_during', 'mixed_levels', 'current_build_comparable')}
                               for e in comparison['evidence']],
            'limits': ['Snapshot endpoints cannot prove an unchanged stage/party throughout a save window.',
                       'Sampling cannot identify simultaneous income/spending or every combat hit.',
                       'Standard errors measure repeatability, not correctness of game formulas.',
                       'Historical runs are not controlled experiments at the current loadout.'],
        }
    finally:
        store.conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(load_settings())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('counts', 'runs', 'save_windows', 'item_integrity')}, indent=2))


if __name__ == '__main__':
    main()
