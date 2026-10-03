"""Current-state views: live runtime frame, open run, heroes."""
import time
from datetime import datetime, timezone

from ..catalog.catalog import num
from ..analysis import actboss
from ..analysis.progress import xp_shares
from .analytics import STATE_NAMES, act_boss_attempts, complete_runs, decorate_run, recent_rates, since_iso


def _age(iso):
    if not iso:
        return None
    return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds(), 1)


def hero_progress(catalog, level, xp):
    need = catalog.level_thresholds.get(level) if level else None
    return {'xp_for_next': need, 'progress': xp / need if need and xp is not None else None}


def live(app, locale='en-US'):
    collector, catalog = app.collector, app.catalog
    sample = collector.last_sample if collector else None
    save = collector.latest_save if collector else None
    result = {'runtime': None, 'open_run': None, 'save': None, 'rates': recent_rates(app.store, catalog, 10, locale)}
    if sample:
        stage = catalog.stages.get(str(sample['stage_key'])) if sample['stage_key'] else None
        result['runtime'] = {
            'age_s': _age(sample['utc']), 'utc': sample['utc'], 'quality': sample['quality'], 'problems': sample['problems'],
            'stage_key': sample['stage_key'], 'stage_label': catalog.stage_label(sample['stage_key'], locale) if stage else None,
            'wave': sample['wave'], 'wave_amount': num(stage['WaveAmount']) if stage else None,
            'state': STATE_NAMES.get(sample['stage_state']), 'gold': sample['gold'],
            'max_completed_stage': sample['max_completed_stage'], 'last_cleared_stage': sample['last_cleared_stage'],
            'heroes': [{**h, 'name': catalog.hero_name(h['hero_key'], locale),
                        **hero_progress(catalog, h['level'], h['xp'])} for h in sample['heroes']],
        }
    run = collector.tracker.run if collector else None
    if run:
        elapsed = time.monotonic() - run['start_mono']
        result['open_run'] = {
            'stage_key': run['stage_key'], 'started_utc': run['started_utc'], 'elapsed_s': round(elapsed, 1),
            'partial_start': run['partial_start'], 'max_wave': run['max_wave'], 'wave_amount': run['wave_amount'],
            'gold_gain_est': run['gold_gain_est'], 'gold_spend_est': run['gold_spend_est'],
            'xp': {k: {**v, 'name': catalog.hero_name(k, locale)} for k, v in run['xp'].items()},
        }
    if save:
        meta = collector.latest_save_meta or {}
        result['save'] = {'last_saved_utc': save['last_saved_utc'], 'age_s': _age(save['last_saved_utc']),
                          'gold': save['gold'], 'stage': save['current_stage'],
                          'stage_label': catalog.stage_label(save['current_stage'], locale),
                          'party': save['party'], 'read_utc': meta.get('read_utc'), 'pending': save['pending']}
        fighting = actboss.in_progress(save, catalog)
        result['save']['act_boss_stage'] = fighting
        result['save']['act_boss_label'] = catalog.stage_label(fighting, locale) if fighting else None
    recent = act_boss_attempts(app.store, catalog, since_iso(24), locale)
    result['act_boss'] = {'last': recent[-1] if recent else None, 'count_24h': sum(a['clears'] + a['fails'] for a in recent)}
    return result


def heroes(app, locale='en-US'):
    catalog = app.catalog
    save = app.collector.latest_save if app.collector else None
    if not save:
        return []
    live_heroes = {h['hero_key']: h for h in (app.collector.last_sample or {}).get('heroes', [])}
    skills = catalog.index('SkillInfoData', 'SkillKey')
    attributes = catalog.index('AttributeInfoData', 'AttributeKey')
    shares = xp_shares([decorate_run(r, catalog, locale) for r in complete_runs(app.store, catalog, 24)])
    combat = {h['hero_key']: h for h in ((app.collector.last_combat or {}).get('heroes') or [])}
    result = []
    for hero in save['heroes']:
        info = catalog.heroes.get(str(hero['hero_key'])) or {}
        runtime = live_heroes.get(hero['hero_key'])
        level = runtime['level'] if runtime and runtime.get('level') else hero['level']
        xp = runtime['xp'] if runtime and runtime.get('xp') is not None else hero['xp']
        allocated = []
        for key, lv in save['attributes'].items():
            attr = attributes.get(str(key))
            if attr and attr['HeroKey'] == str(hero['hero_key']) and lv:
                allocated.append({'key': key, 'level': lv, 'type': attr['ATTRIBUTETYPE'], 'value': num(attr['Value']),
                                  'max_level': num(attr['MaxLevel'])})
        result.append({
            'hero_key': hero['hero_key'], 'name': catalog.hero_name(hero['hero_key'], locale),
            'class': info.get('ClassType'), 'unlocked': hero['unlocked'], 'in_party': hero['hero_key'] in save['party'],
            'level': level, 'xp': xp, 'xp_source': 'runtime' if runtime else 'save',
            **hero_progress(catalog, level, xp),
            'ability_points': hero['ability_points'], 'allocated_points': hero['allocated_points'],
            'skills': [{'key': k, 'name': catalog.localize(skills[str(k)]['SkillNameKey'], locale) if str(k) in skills else None}
                       for k in hero['skills'] if k and k > 0],
            'attributes': allocated,
            # Measured XP relative to the party mean, and the hero's XP stat as the game reads it now.
            'xp_share': (shares or {}).get('heroes', {}).get(str(hero['hero_key'])),
            'xp_share_runs': shares['runs'] if shares and str(hero['hero_key']) in shares['heroes'] else None,
            'xp_stat': ((combat.get(hero['hero_key']) or {}).get('stats') or {}).get('final', {}).get('IncreaseExpAmount'),
            'base': {k: num(info.get(k)) for k in ('AttackDamage', 'AttackSpeed', 'CriticalChance', 'CriticalDamage',
                                                   'MaxHp', 'Armor', 'MovementSpeed')},
        })
    return result
