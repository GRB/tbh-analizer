"""Read-only ES3 decoding: AES-128-CBC, key = PBKDF2-SHA1(password, IV, 100, 16).

The save is never written. The JSON text is parsed by Python, whose integers are
exact, so instance IDs above 2**53 are preserved.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding


class SaveReadError(Exception):
    pass


@dataclass
class RawSave:
    path: str
    sha256: str
    size: int
    mtime_utc: str
    player_json: str   # PlayerSaveData JSON text exactly as stored
    version: str | None


def decrypt(raw, password):
    if len(raw) < 32 or (len(raw) - 16) % 16:
        raise SaveReadError('Incomplete or unencrypted save')
    iv = raw[:16]
    key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), iv, 100, 16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise SaveReadError('Invalid padding: incorrect password or file being written') from exc


def encrypt(plain, password, iv=None):
    """Only used by tests to build synthetic fixtures; never applied to game files."""
    iv = iv or os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), iv, 100, 16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(data) + encryptor.finalize()


def read_stable(path, attempts=4, pause=0.25):
    """Read bytes only when size/mtime are unchanged around the read."""
    for _ in range(attempts):
        before = os.stat(path)
        with open(path, 'rb') as stream:
            raw = stream.read()
        after = os.stat(path)
        unchanged = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        if unchanged and len(raw) == after.st_size:
            return raw, after
        time.sleep(pause)
    raise SaveReadError('Save changed while reading')


def load_save(path, password):
    if not password:
        raise SaveReadError('save settings not found in the game installation (check install_dir)')
    raw, stat = read_stable(path)
    try:
        document = json.loads(decrypt(raw, password).decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise SaveReadError('Invalid save JSON: incorrect password or corrupt file') from exc
    if not isinstance(document, dict):
        raise SaveReadError('Unknown save document schema')
    entry = document.get('PlayerSaveData')
    if not isinstance(entry, dict) or not isinstance(entry.get('value'), str):
        raise SaveReadError('PlayerSaveData missing or with an unknown schema')
    player_json = entry['value']
    try:
        player = json.loads(player_json)
        version = player.get('commonSaveData', {}).get('version')
    except (ValueError, AttributeError) as exc:
        raise SaveReadError('Unknown PlayerSaveData schema') from exc
    # AccountSaveData/SystemInfo are deliberately discarded: account data is not needed.
    return RawSave(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                   mtime_utc=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                   player_json=player_json, version=version)
