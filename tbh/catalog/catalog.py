"""Read-only access to one extracted catalog. Values stay as extracted strings;
helpers convert only the columns they interpret."""
import json
from functools import cached_property


def num(value, default=None):
    if value in (None, ''):
        return default
    try:
        return int(value)
    except ValueError:
        return float(value)


class Catalog:
    def __init__(self, root):
        self.root = root
        self.manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        self.build_id = self.manifest['build_id']
        self._tables = {}
        self.text = {}
        for locale in ('pt-BR', 'en-US'):
            path = root / f'localization-{locale}.json'
            self.text[locale] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    @classmethod
    def latest(cls, settings, build_id=None):
        root = settings.catalog_root
        if build_id and (root / build_id / 'manifest.json').exists():
            return cls(root / build_id)
        candidates = sorted(p for p in root.glob('*') if (p / 'manifest.json').exists())
        return cls(candidates[-1]) if candidates else None

    def table(self, name):
        if name not in self._tables:
            path = self.root / 'tables' / f'{name}.json'
            self._tables[name] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        return self._tables[name]

    def index(self, name, key):
        return {row[key]: row for row in self.table(name)}

    def localize(self, key, locale='en-US'):
        """Resolve a localization key; returns None when the game has no entry."""
        if not key:
            return None
        for loc in (locale, 'en-US'):
            for collection in ('ItemTable', 'StringTable'):
                value = self.text.get(loc, {}).get(collection, {}).get(key)
                if value:
                    return value
        return None

    # --- indexed tables -------------------------------------------------
    @cached_property
    def items(self):
        return self.index('ItemInfoData', 'ItemKey')

    @cached_property
    def gears(self):
        return self.index('GearInfoData', 'GearKey')

    @cached_property
    def stages(self):
        return self.index('StageInfoData', 'StageKey')

    @cached_property
    def monsters(self):
        return self.index('MonsterInfoData', 'MonsterKey')

    @cached_property
    def heroes(self):
        return self.index('HeroInfoData', 'HeroKey')

    @cached_property
    def stage_levels(self):
        return self.index('StageLevelInfoData', 'StageLevel')

    @cached_property
    def level_thresholds(self):
        """XP needed to go from level L to L+1 (LevelInfoData.ExpForLevelUp)."""
        return {int(r['Level']): float(r['ExpForLevelUp']) for r in self.table('LevelInfoData')}

    @cached_property
    def runes(self):
        return self.index('RuneInfoData', 'RuneKey')

    @cached_property
    def rune_levels(self):
        result = {}
        for row in self.table('RuneLevelInfoData'):
            result.setdefault(row['LevelKey'], {})[int(row['Level'])] = row
        return result

    # --- names ------------------------------------------------------------
    def item_name(self, item_key, locale='en-US'):
        row = self.items.get(str(item_key))
        if not row:
            return None
        return self.localize(row['NameKey'], locale) or self.localize(f'ItemName_{item_key}', locale) or row['NameKey']

    def hero_name(self, hero_key, locale='en-US'):
        row = self.heroes.get(str(hero_key))
        return (self.localize(row['HeroNameKey'], locale) if row else None) or f'Hero {hero_key}'

    def stage_name(self, stage_key, locale='en-US'):
        row = self.stages.get(str(stage_key))
        return (self.localize(row['StageNameKey'], locale) if row else None) or f'Stage {stage_key}'

    def stage_label(self, stage_key, locale='en-US'):
        row = self.stages.get(str(stage_key))
        if not row:
            return f'Stage {stage_key}'
        return f"{row['Act']}-{row['StageNo']} {self.stage_name(stage_key, locale)} ({row['STAGEDIFFICULITY']})"
