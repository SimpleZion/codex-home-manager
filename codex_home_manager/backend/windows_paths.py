from __future__ import annotations

import ntpath
import os
from pathlib import Path


def strip_windows_extended_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def windows_extended_path(path: str | os.PathLike[str]) -> Path:
    value = os.fspath(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return Path(value)
    absolute = os.path.abspath(value)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def canonical_windows_path(path: str | os.PathLike[str], *, resolve: bool = True) -> str:
    value = os.fspath(path)
    if resolve:
        value = os.path.realpath(value)
    else:
        value = os.path.abspath(value)
    value = strip_windows_extended_prefix(value)
    return ntpath.normcase(ntpath.normpath(value))


def windows_path_key(path: str | os.PathLike[str]) -> str:
    return canonical_windows_path(path, resolve=True).casefold()


def windows_path_is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    candidate = canonical_windows_path(path, resolve=True)
    boundary = canonical_windows_path(root, resolve=True)
    try:
        return ntpath.commonpath([candidate, boundary]) == boundary
    except ValueError:
        return False


def canonical_path(path: str | os.PathLike[str]) -> Path:
    return Path(canonical_windows_path(path, resolve=True))
