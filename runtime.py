"""Resolve the archive helper without searching the input folder or PATH."""
from __future__ import annotations
import os
from pathlib import Path


def default_base():
    """Use a private per-user folder, independent of the executable's location."""
    return Path.home() / 'XBOX360-Convert'


def find_7zip(explicit=None):
    """An explicit path/environment override is a trusted local configuration."""
    override = explicit or os.environ.get('XBOX360_7ZIP')
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ValueError('The 7-Zip path must be absolute')
        if candidate.name.casefold() != '7z.exe' or not candidate.is_file():
            raise FileNotFoundError('Choose an existing, trusted 7z.exe installation')
        return candidate.resolve()
    for name in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
        directory = os.environ.get(name)
        if directory:
            candidate = Path(directory) / '7-Zip' / '7z.exe'
            if candidate.is_absolute() and candidate.is_file():
                return candidate.resolve()
    raise FileNotFoundError(
        'Install 7-Zip from https://www.7-zip.org/ or pass '
        '--seven-zip with the absolute path to a trusted 7z.exe')
