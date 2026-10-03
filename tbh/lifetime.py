"""Tie the server's lifetime to whoever launched it.

Observed 2026-10-01: stopping the terminal run killed only the shell. The venv launcher
(`.venv\\Scripts\\python.exe`) and the real server it spawned kept running, holding port 8765
and the collector lock, so the next server came up read-only ("another pid serving").

At start the server walks up its ancestors: python launchers, then the first non-python
process (the shell). It keeps a handle to each — a handle stays valid even if the PID is
reused — and calls `on_exit` as soon as any of them ends. Detached launches, whose launcher
exits on purpose, opt out with `serve --no-parent-watch`.
"""
import ctypes as c
import logging
import os
import sys
import threading
from ctypes import wintypes as w

log = logging.getLogger(__name__)

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF
LAUNCHERS = {'python.exe', 'pythonw.exe', 'py.exe', 'pyw.exe'}


class _Process(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                ('heap', c.c_void_p), ('module', w.DWORD), ('threads', w.DWORD),
                ('parent', w.DWORD), ('priority', c.c_long), ('flags', w.DWORD),
                ('exe', w.WCHAR * 260)]


class _FileTime(c.Structure):
    _fields_ = [('low', w.DWORD), ('high', w.DWORD)]


if sys.platform == 'win32':
    _k = c.WinDLL('kernel32', use_last_error=True)
    for _name, (_args, _res) in {
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'CloseHandle': ([w.HANDLE], w.BOOL),
        'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
        'Process32FirstW': ([w.HANDLE, c.POINTER(_Process)], w.BOOL),
        'Process32NextW': ([w.HANDLE, c.POINTER(_Process)], w.BOOL),
        'GetProcessTimes': ([w.HANDLE] + [c.POINTER(_FileTime)] * 4, w.BOOL),
        'WaitForMultipleObjects': ([w.DWORD, c.POINTER(w.HANDLE), w.BOOL, w.DWORD], w.DWORD),
    }.items():
        _fn = getattr(_k, _name)
        _fn.argtypes, _fn.restype = _args, _res


def _processes():
    snap = _k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, c.c_void_p(-1).value):
        raise c.WinError(c.get_last_error())
    table = {}
    try:
        entry = _Process()
        entry.size = c.sizeof(entry)
        ok = _k.Process32FirstW(snap, c.byref(entry))
        while ok:
            table[entry.pid] = (entry.parent, entry.exe.lower())
            ok = _k.Process32NextW(snap, c.byref(entry))
    finally:
        _k.CloseHandle(snap)
    return table


def _created(handle):
    times = [_FileTime() for _ in range(4)]
    if not _k.GetProcessTimes(handle, *[c.byref(t) for t in times]):
        return None
    return (times[0].high << 32) | times[0].low


def ancestors(table, pid):
    """[(pid, exe)] from the parent up to and including the first non-python process."""
    chain, seen = [], {pid}
    parent = table.get(pid, (0, ''))[0]
    while parent and parent not in seen and parent in table:
        seen.add(parent)
        exe = table[parent][1]
        chain.append((parent, exe))
        if exe not in LAUNCHERS:
            break
        parent = table[parent][0]
    return chain


def watch_launcher(on_exit):
    """Call `on_exit(reason)` from a daemon thread when an ancestor process ends. Returns the watched list."""
    if sys.platform != 'win32':
        return []
    me = _k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, os.getpid())
    born = _created(me)
    _k.CloseHandle(me)
    handles, watched = [], []
    for pid, exe in ancestors(_processes(), os.getpid()):
        handle = _k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            break
        created = _created(handle)
        if created is None or born is None or created > born:  # PID already reused by a younger process
            _k.CloseHandle(handle)
            break
        handles.append(handle)
        watched.append((pid, exe))
    if not handles:
        log.info('parent watch: no live launcher found; the server runs until stopped')
        return []
    array = (w.HANDLE * len(handles))(*handles)

    def wait():
        index = _k.WaitForMultipleObjects(len(handles), array, False, INFINITE)
        if index == WAIT_FAILED:
            log.warning('parent watch failed: %s', c.WinError(c.get_last_error()))
            return
        pid, exe = watched[index] if index < len(watched) else (None, '?')
        on_exit(f'launcher process ended ({exe} pid {pid})')

    threading.Thread(target=wait, name='parent-watch', daemon=True).start()
    log.info('parent watch: exits with %s', ', '.join(f'{exe}:{pid}' for pid, exe in watched))
    return watched
