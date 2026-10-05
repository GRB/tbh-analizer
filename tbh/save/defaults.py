"""The game's save settings (Easy Save 3 `ES3Defaults` asset), read from the local installation.

The asset is a MonoBehaviour in `resources.assets` without a type tree, so its fields are read in
their serialized order: location, path, encryption type, compression type, encryption password.
Nothing is written or kept on disk.
"""
import struct

ASSET_NAME = 'ES3Defaults'
HEADER = 28          # m_GameObject PPtr (12), m_Enabled (4, aligned), m_Script PPtr (12)
AES = 1              # ES3.EncryptionType


class _Reader:
    def __init__(self, raw, pos=0):
        self.raw, self.pos = raw, pos

    def int(self):
        value, = struct.unpack_from('<i', self.raw, self.pos)
        self.pos += 4
        return value

    def str(self):
        size = self.int()
        if not 0 <= size <= len(self.raw) - self.pos:
            raise ValueError('string out of range')
        value = self.raw[self.pos:self.pos + size].decode('utf-8')
        self.pos += (size + 3) & ~3
        return value


def parse(raw):
    """{'name', 'location', 'path', 'encryption', 'compression', 'password'} from the raw MonoBehaviour,
    or None when it is not the ES3 settings asset."""
    try:
        r = _Reader(raw, HEADER)
        if r.str() != ASSET_NAME:
            return None
        return {'name': ASSET_NAME, 'location': r.int(), 'path': r.str(), 'encryption': r.int(),
                'compression': r.int(), 'password': r.str()}
    except (ValueError, struct.error, UnicodeError):
        return None


def read_save_settings(game_data_dir):
    """ES3 settings of the installed game, or None when they cannot be found."""
    import UnityPy
    path = game_data_dir / 'resources.assets'
    if not path.exists():
        return None
    for obj in UnityPy.load(str(path)).objects:
        if obj.type.name != 'MonoBehaviour':
            continue
        found = parse(obj.get_raw_data())
        if found:
            return found if found['encryption'] == AES and found['password'] and found['path'].endswith('.es3') else None
    return None
