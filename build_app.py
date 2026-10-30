#!/usr/bin/env python3
"""Build a local macOS .app. Python and Seeknal remain external prerequisites."""
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sys

ROOT = Path(__file__).resolve().parent
DEST = ROOT / 'dist' / 'Seeknal Studio.app'


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This bundle builder targets macOS; use launch.py on other systems.')
    if DEST.exists():
        raise SystemExit(f'{DEST} already exists; move it aside before building a new bundle.')
    executable = DEST / 'Contents' / 'MacOS' / 'Seeknal Studio'
    resource = DEST / 'Contents' / 'Resources' / 'studio'
    resource.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    for name in (
        'server.py', 'engine_bridge.py', 'agent_runtime.py', 'agent_worker.py',
        'project_service.py', 'environment_service.py', 'repl_service.py', 'repl_worker.py',
        'runtime_resolver.py', 'launch.py', 'index.html', 'styles.css', 'app.js',
        'remote_workspace.py', 'remote_host.py', 'remote_gateway.py', 'studio_lint.py', 'requirements-runtime.lock.txt',
    ):
        shutil.copy2(ROOT / name, resource / name)
    runtime_config = ROOT / '.runtime' / 'runtime-config.json'
    if not runtime_config.is_file():
        raise SystemExit('Managed Seeknal runtime config is missing; run python3 runtime_setup.py --python <Python-3.11-or-newer> first.')
    (resource / '.runtime').mkdir()
    shutil.copy2(runtime_config, resource / '.runtime' / 'runtime-config.json')
    shutil.copytree(ROOT / 'demo', resource / 'demo', ignore=shutil.ignore_patterns('target', '__pycache__', '.seeknal'))
    (resource / 'bin').mkdir()
    binary = Path(os.environ.get('SEEKNAL_WEBFRAME', ROOT.parent / 'webframe-evaluation' / 'bin' / 'webframe'))
    shutil.copy2(binary, resource / 'bin' / 'webframe')
    executable.write_text('#!/bin/sh\nAPP_RESOURCES="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources/studio" && pwd)"\nexec ' + shlex.quote(sys.executable) + ' "$APP_RESOURCES/launch.py" "$@"\n')
    executable.chmod(0o755)
    with (DEST / 'Contents' / 'Info.plist').open('wb') as out:
        plistlib.dump({'CFBundleName':'Seeknal Studio','CFBundleDisplayName':'Seeknal Studio','CFBundleIdentifier':'dev.seeknal.studio.local','CFBundleExecutable':'Seeknal Studio','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.1.0','NSHighResolutionCapable':True},out)
    print(f'Built {DEST}\nLocal development bundle: uses the pinned managed Seeknal 2.14.0 runtime; unsigned.')


if __name__ == '__main__':
    main()
