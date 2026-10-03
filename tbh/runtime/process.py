"""Windows process access limited to PROCESS_QUERY_INFORMATION | PROCESS_VM_READ.

No write, injection, thread creation or method invocation exists in this module.
"""
import ctypes as c
import struct
from ctypes import wintypes as w

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPMODULE = 0x8
TH32CS_SNAPMODULE32 = 0x10
INVALID_HANDLE = c.c_void_p(-1).value
MAX_READ = 8 * 1024 * 1024


class _Module(c.Structure):
    _fields_ = [('size', w.DWORD), ('id', w.DWORD), ('pid', w.DWORD),
                ('global_usage', w.DWORD), ('process_usage', w.DWORD),
                ('base', c.c_void_p), ('bytes', w.DWORD), ('handle', w.HMODULE),
                ('name', w.WCHAR * 256), ('path', w.WCHAR * 260)]


class _Process(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                ('heap', c.c_void_p), ('module', w.DWORD), ('threads', w.DWORD),
                ('parent', w.DWORD), ('priority', c.c_long), ('flags', w.DWORD),
                ('exe', w.WCHAR * 260)]


class _FileTime(c.Structure):
    _fields_ = [('low', w.DWORD), ('high', w.DWORD)]


_k = c.WinDLL('kernel32', use_last_error=True)
for _name, (_args, _res) in {
    'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
    'CloseHandle': ([w.HANDLE], w.BOOL),
    'ReadProcessMemory': ([w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL),
    'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
    'Module32FirstW': ([w.HANDLE, c.POINTER(_Module)], w.BOOL),
    'Module32NextW': ([w.HANDLE, c.POINTER(_Module)], w.BOOL),
    'Process32FirstW': ([w.HANDLE, c.POINTER(_Process)], w.BOOL),
    'Process32NextW': ([w.HANDLE, c.POINTER(_Process)], w.BOOL),
    'GetProcessTimes': ([w.HANDLE] + [c.POINTER(_FileTime)] * 4, w.BOOL),
    'GetExitCodeProcess': ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
}.items():
    _fn = getattr(_k, _name)
    _fn.argtypes, _fn.restype = _args, _res


def find_processes(exe_name):
    snap = _k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE:
        raise c.WinError(c.get_last_error())
    found = []
    try:
        entry = _Process()
        entry.size = c.sizeof(entry)
        ok = _k.Process32FirstW(snap, c.byref(entry))
        while ok:
            if entry.exe.lower() == exe_name.lower():
                found.append(entry.pid)
            ok = _k.Process32NextW(snap, c.byref(entry))
    finally:
        _k.CloseHandle(snap)
    return found


class ProcessReader:
    def __init__(self, pid):
        self.pid = pid
        self.handle = _k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.handle:
            raise c.WinError(c.get_last_error())

    def close(self):
        if self.handle:
            _k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def alive(self):
        code = w.DWORD()
        return bool(self.handle and _k.GetExitCodeProcess(self.handle, c.byref(code)) and code.value == 259)

    def creation_time(self):
        """Process start as FILETIME integer: identifies one launch, unlike a reusable PID."""
        times = [_FileTime() for _ in range(4)]
        if not _k.GetProcessTimes(self.handle, *[c.byref(t) for t in times]):
            return None
        return (times[0].high << 32) | times[0].low

    def modules(self):
        snap = _k.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap == INVALID_HANDLE:
            raise c.WinError(c.get_last_error())
        try:
            entry = _Module()
            entry.size = c.sizeof(entry)
            ok = _k.Module32FirstW(snap, c.byref(entry))
            while ok:
                yield {'name': entry.name, 'base': entry.base, 'size': entry.bytes, 'path': entry.path}
                ok = _k.Module32NextW(snap, c.byref(entry))
        finally:
            _k.CloseHandle(snap)

    def read(self, address, size):
        if not address or not 0 < size <= MAX_READ:
            return b''
        buf = c.create_string_buffer(size)
        count = c.c_size_t()
        if _k.ReadProcessMemory(self.handle, c.c_void_p(address), buf, size, c.byref(count)):
            return buf.raw[:count.value]
        return b''

    def scalar(self, address, fmt='Q'):
        size = struct.calcsize('<' + fmt)
        raw = self.read(address, size)
        return struct.unpack('<' + fmt, raw)[0] if len(raw) == size else None

    def cstring(self, address, limit=64):
        raw = self.read(address, limit) if address else b''
        return raw.split(b'\0')[0].decode('ascii', errors='replace') if raw else None
