"""Export the same evidence ledger as /api/quality, without starting a collector."""
import argparse
import json
from pathlib import Path

from tbh.catalog.catalog import Catalog
from tbh.config import load_settings
from tbh.views.quality import quality_report
from .audit_analytics import ReadOnlyStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=float, default=168)
    parser.add_argument('--output', type=Path, default=Path('build/reports/quality-ledger.json'))
    args = parser.parse_args()
    settings = load_settings()
    store = ReadOnlyStore(settings.db_path)
    try:
        store.conn.execute('BEGIN')
        report = quality_report(store, Catalog.latest(settings), hours=args.hours, limit=200, export_all=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        print(json.dumps({'summary': report['summary'], 'scope': report['scope'], 'output': str(args.output)}, indent=2))
    finally:
        store.conn.close()


if __name__ == '__main__':
    main()
