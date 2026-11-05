"""Bound live diagnostic history and the launcher's operational log.

Receipts/counts and bounded-run journals remain the authorities. Log rotation uses
copy/truncate because existing worker processes inherit the launcher's open stdout;
renaming alone would leave those processes writing an unbounded old file.
"""
import os
from pathlib import Path
import shutil
import stat
import threading


def rotate_manifest(path, max_bytes=16 * 1024**2):
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    archive = path.with_name(path.name + '.1')
    if path.is_symlink() or archive.is_symlink():
        raise ValueError('Manifest paths cannot be symbolic links.')
    os.replace(path, archive)


def trim_payloads(directory, keep=1000):
    path = Path(directory)
    if not path.exists():
        return
    files = sorted((p for p in path.glob('*.json') if p.name.split('-', 1)[0].isdigit()), key=lambda p: int(p.name.split('-', 1)[0]))
    for p in files[:-keep]:
        p.unlink()


def rotate_log(path, max_bytes=8 * 1024**2):
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    paths = [path.with_name(path.name + f'.{i}') for i in range(1, 4)]
    if path.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError('Log paths cannot be symbolic links.')
    for i in (2, 1):
        if paths[i-1].exists():
            os.replace(paths[i-1], paths[i])
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Log must be an owned regular file.')
        with os.fdopen(os.dup(fd), 'rb') as src:
            out = os.open(paths[0], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def start_log_rotation(path):
    stop = threading.Event()
    def rotate():
        while not stop.wait(5):
            try:
                rotate_log(path)
            except OSError:
                # Operational logs are secondary evidence; a rotation failure must not
                # interrupt active workers. The next interval retries.
                pass
    thread = threading.Thread(target=rotate, name='studio-log-rotation', daemon=True)
    thread.start()
    return stop, thread
