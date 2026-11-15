"""Package reviewed, Git-tracked runtime source only, without macOS metadata.

Usage: python3 deploy/package_live_source.py /tmp/northbound-source.tar
Review `git diff` first: this packages current working-tree bytes, not HEAD.
"""
import io
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ('core', 'reading_challenge', 'templates', 'static')


def package(destination):
    files = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', *PACKAGES], cwd=ROOT,
    ).decode().split('\0')
    with tarfile.open(destination, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for name in filter(None, files):
            path = ROOT / name
            if path.is_symlink() or any(part.startswith('.') for part in Path(name).parts):
                raise ValueError(f'Unexpected runtime path: {name}')
            if path.name.startswith('test') or '.test.' in path.name:
                continue
            content = path.read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = 0o444
            # Gunicorn polling requires changed mtimes on subsequent syncs.
            entry.mtime = int(path.stat().st_mtime)
            archive.addfile(entry, io.BytesIO(content))


if __name__ == '__main__':
    package(sys.argv[1])
