"""Exercise detached local start/reuse/status/stop using isolated state and no telemetry."""
import json
import os
from pathlib import Path
import socket
import subprocess  # nosec B404
import sys
import tempfile
from tools import studio


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        command = [sys.executable, str(studio.ROOT / 'tools/studio.py')]
        options = ['--port', str(port), '--state-dir', str(directory / 'launcher')]
        env = dict(os.environ, SG_DATA_DIR=str(directory / 'data'))
        def call(action):
            result = subprocess.run(command + [action] + options, env=env, capture_output=True, text=True, timeout=40)  # nosec B603: own CLI, generated local paths
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            return result.stdout
        try:
            call('start')
            first = studio.health(port)
            call('status')
            call('start')
            require(studio.health(port)['pid'] == first['pid'], 'Start created a duplicate server')
            owned = directory / 'launcher' / f'{port}.json'
            original = owned.read_text()
            owned.write_text(json.dumps({'pid': first['pid'], 'instance': 'not-owned'}))
            try:
                require(studio.stop(port, directory / 'launcher') == 1, 'Stop accepted mismatched ownership')
                require(studio.health(port)['pid'] == first['pid'], 'Unowned server was stopped')
            finally:
                owned.write_text(original)
            call('stop')
            require(studio.health(port) is None, 'Owned server did not stop')
            print('PASS: detached startup, reuse, status, ownership protection and clean shutdown; no telemetry')
        finally:
            if studio.health(port):
                call('stop')


if __name__ == '__main__':
    main()
