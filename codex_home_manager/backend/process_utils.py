from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def list_windows_processes() -> list[dict[str, str]]:
    if os.name != "nt":
        return []

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot_handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot_handle == ctypes.c_void_p(-1).value:
        return []

    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    processes: list[dict[str, str]] = []
    try:
        has_entry = kernel32.Process32FirstW(snapshot_handle, ctypes.byref(entry))
        while has_entry:
            processes.append(
                {
                    "imageName": entry.szExeFile,
                    "pid": str(entry.th32ProcessID),
                    "parentPid": str(entry.th32ParentProcessID),
                }
            )
            has_entry = kernel32.Process32NextW(snapshot_handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot_handle)
    return processes
