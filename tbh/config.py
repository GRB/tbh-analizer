"""Local paths and settings. Session values (PID, addresses) never belong here."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STEAM_APP_ID = '3678970'
PROCESS_NAME = 'TaskBarHero.exe'


@dataclass
class Settings:
    install_dir: Path = Path(r'C:\Program Files (x86)\Steam\steamapps\common\TaskbarHero')
    save_dir: Path = Path(os.path.expandvars(r'%USERPROFILE%\AppData\LocalLow\TesseractStudio\TaskbarHero'))
    data_dir: Path = PROJECT_ROOT / 'build' / 'app'
    es3_password: str | None = None
    host: str = '127.0.0.1'
    port: int = 8765
    sample_interval: float = 1.0
    save_poll_interval: float = 5.0
    heartbeat_seconds: float = 30.0
    extensions: list = field(default_factory=list)   # optional modules loaded by the server and CLI
    extra: dict = field(default_factory=dict)        # the whole local.json, for extensions' own sections

    @property
    def save_file(self):
        return self.save_dir / 'SaveFile_Live.es3'

    @property
    def db_path(self):
        return self.data_dir / 'tbh.sqlite3'

    @property
    def catalog_root(self):
        return self.data_dir / 'catalog'

    @property
    def layout_root(self):
        return self.data_dir / 'layouts'

    @property
    def app_manifest(self):
        # steamapps/common/<dir> -> steamapps/appmanifest_<id>.acf
        return self.install_dir.parent.parent / f'appmanifest_{STEAM_APP_ID}.acf'


def load_settings(path=None):
    """Read `build/app/local.json` (unversioned) and TBH_* environment overrides."""
    settings = Settings()
    path = Path(path) if path else settings.data_dir / 'local.json'
    values = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    for key in ('install_dir', 'save_dir', 'data_dir'):
        if key in values:
            setattr(settings, key, Path(os.path.expandvars(values[key])))
    for key in ('es3_password', 'host', 'port', 'sample_interval', 'save_poll_interval', 'heartbeat_seconds'):
        if key in values:
            setattr(settings, key, values[key])
    settings.extensions = list(values.get('extensions') or [])
    settings.extra = values
    env = {'TBH_ES3_PASSWORD': 'es3_password', 'TBH_PORT': 'port', 'TBH_DATA_DIR': 'data_dir'}
    for name, key in env.items():
        if os.environ.get(name):
            value = os.environ[name]
            setattr(settings, key, int(value) if key == 'port' else Path(value) if key == 'data_dir' else value)
    if os.environ.get('TBH_EXTENSIONS') is not None:
        settings.extensions = [name for name in os.environ['TBH_EXTENSIONS'].split(',') if name.strip()]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
