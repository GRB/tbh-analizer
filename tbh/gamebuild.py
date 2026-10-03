"""Identify the installed game build from Steam metadata and binary hashes."""
import hashlib
import json
import re
from pathlib import Path

HASHED_FILES = ('GameAssembly.dll', 'TaskBarHero_Data/il2cpp_data/Metadata/global-metadata.dat')


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(chunk):
            digest.update(block)
    return digest.hexdigest().upper()


def read_acf(path):
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    return dict(re.findall(r'^\s*"(\w+)"\s+"([^"]*)"', text, re.M))


class BuildIdentity:
    """Hashes are cached by size+mtime; a changed binary always produces a new identity."""

    def __init__(self, settings):
        self.settings = settings
        self.cache_path = settings.data_dir / 'hash-cache.json'

    def _cached_hash(self, path, cache):
        stat = path.stat()
        key = str(path)
        entry = cache.get(key)
        if entry and entry['size'] == stat.st_size and entry['mtime_ns'] == stat.st_mtime_ns:
            return entry['sha256']
        digest = sha256_file(path)
        cache[key] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest}
        return digest

    def identify(self):
        install = self.settings.install_dir
        result = {'install_dir': str(install), 'build_id': None, 'files': {}, 'problems': []}
        try:
            acf = read_acf(self.settings.app_manifest)
            result['build_id'] = acf.get('buildid')
            result['steam_state_flags'] = acf.get('StateFlags')
        except OSError as exc:
            result['problems'].append(f'unreadable appmanifest: {exc}')
        cache = {}
        if self.cache_path.exists():
            cache = json.loads(self.cache_path.read_text(encoding='utf-8'))
        for relative in HASHED_FILES:
            path = install / relative
            try:
                result['files'][Path(relative).name] = self._cached_hash(path, cache)
            except OSError as exc:
                result['problems'].append(f'{relative}: {exc}')
        self.cache_path.write_text(json.dumps(cache, indent=1), encoding='utf-8')
        return result
