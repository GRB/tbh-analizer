"""Catalog costs associated with observed rune-level increases, not purchase receipts."""
import math


def rune_spending(before, after, catalog, implied_spend):
    old, new = before.get('runes'), after.get('runes')
    rows, missing = [], []
    if old is None or new is None:
        missing.append('rune state missing')
    else:
        for key in sorted(set(old) | set(new)):
            start, end = old.get(key, 0), new.get(key, 0)
            if end < start:
                missing.append(f'rune {key}: level decreased')
                continue
            rune = catalog.runes.get(str(key), {})
            levels = catalog.rune_levels.get(rune.get('LevelDataKey'), {})
            for level in range(start + 1, end + 1):
                row = levels.get(level, {})
                try:
                    cost = float(row['CostValue'])
                except (KeyError, TypeError, ValueError):
                    cost = None
                if cost is None or not math.isfinite(cost) or cost < 0 or not row.get('CostItemKey'):
                    missing.append(f'rune {key} level {level}: cost missing or invalid')
                    continue
                rows.append({'rune_key': key, 'level': level, 'currency': row['CostItemKey'], 'cost': cost})
    gold = sum(r['cost'] for r in rows if r['currency'] == '100001')
    residual = implied_spend - gold if implied_spend is not None and not missing else None
    return {'levels': rows, 'catalog_gold_cost': gold, 'unpriced': missing,
            'spend_minus_catalog_cost': residual,
            'status': 'incomplete' if missing else 'no_increase' if not rows else
                      'consistent' if residual == 0 else 'difference',
            'note': 'Catalog costs of net rune-level increases. Agreement supports a spending explanation, '
                    'but does not establish purchase times, refunds or server charges. Income residuals are unchanged.'}
