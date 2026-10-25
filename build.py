"""Build the standalone w3-downgrade executable for this OS with PyInstaller (one file, no Python
needed to run it). PyInstaller does not cross-compile: build the Windows .exe on Windows and the
Linux binary on Linux.

    python -m pip install -r requirements-build.txt
    python build.py [--dist FOLDER]          (default: dist/ next to this script)
"""
import argparse
import os
from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dist', type=Path, default=HERE / 'dist', help='folder for the executable')
    args = ap.parse_args()
    PyInstaller.__main__.run([
        str(HERE / 'w3_downgrade_app.py'),
        '--name', 'w3-downgrade',
        '--onefile', '--console', '--noconfirm', '--clean',
        # the built-in skill template, found through sys._MEIPASS (see builtin_skill_template)
        '--add-data', f'{HERE / "skill_tree_404.json"}{os.pathsep}.',
        # Textual and Rich import parts of themselves lazily, which PyInstaller cannot follow
        '--collect-submodules', 'textual', '--collect-data', 'textual',
        '--collect-submodules', 'rich',
        '--exclude-module', 'tkinter',
        '--distpath', str(args.dist),
        '--workpath', str(HERE / 'build'),
        '--specpath', str(HERE / 'build'),
    ])


if __name__ == '__main__':
    main()
