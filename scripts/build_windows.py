"""Build the app locally; never collect user data or USB files."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import importlib.metadata

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Xbox360 Converter'


def main():
    if os.name != 'nt':
        raise SystemExit('Build on Windows using a standard Python installation with Tkinter.')
    try:
        import PyInstaller
        import tkinter
    except ImportError:
        raise SystemExit('Install Python with Tkinter, then install requirements-build.txt.')
    # One-folder builds avoid unpacking the runtime on every app launch.
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir',
        '--windowed', '--noupx', '--name', NAME,
        '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'),
        '--specpath', str(ROOT / 'build'), str(ROOT / 'app.py'),
    ], cwd=ROOT, check=True)
    bundle = ROOT / 'dist' / NAME
    for name in ('README.md', 'SECURITY.md'):
        shutil.copy2(ROOT / name, bundle / name)
    for package in ('ttkbootstrap','Pillow'):
        dist=importlib.metadata.distribution(package)
        for entry in dist.files or []:
            if 'license' in entry.name.lower() and '.dist-info' in str(entry):
                target=bundle/'licenses'/package/entry.name
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(dist.locate_file(entry),target)
    print(f'Built: {bundle / (NAME + ".exe")}')
    print('Keep the entire application folder. The receiving PC still needs 7-Zip.')


if __name__ == '__main__':
    main()
