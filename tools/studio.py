#!/usr/bin/env python3
"""Start the local UI/API as an owned background process, without a LaunchAgent."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess  # nosec B404
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as response:  # nosec B310: fixed loopback HTTP
            return json.loads(response.read(16384))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def occupied(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) == 0


def record(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def start(port, directory, open_browser=False):
    url = f'http://127.0.0.1:{port}'
    existing = health(port)
    if existing and existing.get('service') == 'session-generator':
        print(f'Session Generator is already running at {url}')
        if open_browser:
            webbrowser.open(url)
        return 0
    if occupied(port):
        print(f'Port {port} is in use by another application. Choose a different --port.', file=sys.stderr)
        return 1
    python = ROOT / '.venv/bin/python'
    if not python.exists() or not (ROOT / 'apps/web/dist/index.html').exists():
        print('Run the setup commands in README.md first: install the server requirements and build apps/web.', file=sys.stderr)
        return 1
    instance = secrets.token_hex(16)
    log = directory / 'server.log'
    if log.is_symlink():
        raise ValueError('The server log cannot be a symbolic link.')
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'a') as output:
        child = subprocess.Popen([str(python), '-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', str(port)],
            cwd=ROOT, env=dict(os.environ, SG_STUDIO_INSTANCE=instance, SG_STUDIO_LOG=str(log)), stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)  # nosec B603: fixed interpreter/module, validated numeric port
    deadline = time.monotonic() + 25
    while child.poll() is None and time.monotonic() < deadline:
        current = health(port)
        if current and current.get('instance') == instance and current.get('pid') == child.pid:
            pidfile = directory / f'{port}.json'
            if pidfile.is_symlink():
                child.terminate()
                raise ValueError('The ownership record cannot be a symbolic link.')
            fd = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump({'pid': child.pid, 'instance': instance}, output)
            print(f'Session Generator is running at {url}\nIt stays running after this terminal closes. Log: {log}')
            if open_browser:
                webbrowser.open(url)
            return 0
        time.sleep(.2)
    if child.poll() is None:
        child.terminate()
    print(f'The server could not start. Check {log}', file=sys.stderr)
    return 1


def stop(port, directory):
    current = health(port)
    if not current:
        print('No responding server was found. No process was signalled.')
        return 1 if occupied(port) else 0
    owned = record(directory / f'{port}.json')
    if not owned or current.get('instance') != owned.get('instance') or current.get('pid') != owned.get('pid'):
        print('This server was not started by this launcher. No process was signalled.', file=sys.stderr)
        return 1
    if current.get('active_runs', 0):
        print('Stop active or paused runs in the UI before stopping the server.', file=sys.stderr)
        return 1
    os.kill(owned['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not occupied(port):
            (directory / f'{port}.json').unlink(missing_ok=True)
            print('Session Generator stopped.')
            return 0
        time.sleep(.2)
    print('Shutdown is still in progress. No forced kill was issued.')
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.state/studio')
    parser.add_argument('--open', action='store_true', help='Open the browser after start')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    directory = args.state_dir.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == 'status':
        current = health(args.port)
        if current and current.get('service') == 'session-generator':
            print(f"Running at http://127.0.0.1:{args.port} · {current.get('active_runs', 0)} active runs")
            return 0
        print('Session Generator is not running on this port.')
        return 1
    # Serialize double-clicks and concurrent start/stop calls for this launcher.
    lock = directory / 'launcher.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return start(args.port, directory, args.open) if args.action == 'start' else stop(args.port, directory)
    finally:
        os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
