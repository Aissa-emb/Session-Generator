"""Test-only Kubernetes entrypoint: real API/workers, exclusively offline payloads.

Run through tools/kubernetes_acceptance.py, never as a production command. The
host harness installs a kernel OUTPUT firewall in this pod's network namespace
before releasing its startup gate. There is deliberately no production env flag
that silently changes a live controller into a dry run.
"""
import argparse
from contextlib import asynccontextmanager
import errno
import json
import os
from pathlib import Path
import platform
import socket
import stat
import time
import urllib.error
import urllib.request


FIXTURE_TOKENS = {
    'kubernetes-fixture-trace-v1', 'kubernetes-fixture-trace-v2',
    'kubernetes-fixture-symbol-v1', 'kubernetes-fixture-symbol-v2',
}
FIXTURE_IDS = {'web01', 'and01', 'ios01'}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def environment():
    require(os.getuid() == 10001, 'Use the image nonroot user.')
    require(os.environ.get('SG_DATA_DIR') == '/var/lib/session-generator', 'Use the isolated fixture PVC.')
    require(os.environ.get('SG_CLOUD_RUNNER') == '1', 'Fixture cloud input was not injected.')
    require({os.environ.get('SG_' + platform + '_APP_ID') for platform in ('WEB', 'ANDROID', 'IOS')} == FIXTURE_IDS,
            'Only the three fixture app IDs are allowed.')
    require(os.environ.get('SG_CHRONOSPHERE_TENANT') == 'kubernetes-fixture.chronosphere.io:443',
            'Only the fixture tenant is allowed.')
    require(os.environ.get('SG_CHRONOSPHERE_TOKEN') in FIXTURE_TOKENS and
            os.environ.get('SG_SYMBOL_UPLOAD_TOKEN') in FIXTURE_TOKENS, 'Only fixture Secret values are allowed.')
    require(not any(os.environ.get(name) for name in
            ('SG_CHRONOSPHERE_TOKEN_FILE', 'SG_SYMBOL_UPLOAD_TOKEN_FILE', 'SG_CONFIG_FILE',
             'CHRONOSPHERE_API_TOKEN', 'CHRONOSPHERE_API_TOKEN_FILE', 'OTLP_ENDPOINT', 'OTLP_HEADERS')),
            'Unexpected legacy credentials or transport configuration.')
    root_mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()
                   if line.split()[4] == '/']
    require(root_mounts and 'ro' in root_mounts[-1][5].split(','), 'Root filesystem is not mounted read-only.')
    try:
        Path('/app/kubernetes-must-be-read-only').write_text('invalid')
    except OSError as error:
        require(error.errno in (errno.EROFS, errno.EACCES), 'Unexpected filesystem failure.')
    else:
        raise RuntimeError('Root filesystem is writable.')


def snapshot():
    from server import cloud
    from server.store import Store
    from xcorgen.security import read_token_file
    store = Store()
    try:
        runs = store.list('runs')
        require(all(run['dry_run'] for run in runs), 'Fixture state contains live work.')
        state = cloud.status(store)
        serialized = json.dumps(state)
        require(not any(value in serialized for value in FIXTURE_TOKENS), 'Public status leaked a fixture credential.')
        token_files = list((store.directory / 'cloud-credentials').glob('*.token'))
        for path in token_files:
            require(stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == 10001,
                    'Credential file ownership changed after volume remount.')
            require(read_token_file(str(path)) in FIXTURE_TOKENS, 'Unexpected fixture credential content.')
        result = {key: state.get(key) for key in ('status', 'desired', 'owner', 'stream_id', 'run_id',
                  'worker_status', 'sessions_generated', 'dry_run', 'delivery_errors')}
        result.update(streams=len(store.list('streams')), runs=len(runs), private_credentials=len(token_files),
                      uid=os.getuid(), architecture=platform.machine(),
                      state_bytes=sum(path.stat().st_size for path in store.directory.rglob('*') if path.is_file()),
                      secret_version=2 if os.environ['SG_CHRONOSPHERE_TOKEN'].endswith('v2') else 1)
        memory = Path('/sys/fs/cgroup/memory.current')
        if memory.is_file():
            result['memory_current_bytes'] = int(memory.read_text())
        marker = store.directory / 'kubernetes-clean-shutdown.json'
        if marker.exists():
            result['previous_shutdown'] = json.loads(marker.read_text())
        return result
    finally:
        store.close()


def inspect():
    environment()
    value = snapshot()
    # Probe admission and API authentication are exercised on the real server.
    for path, expected, host in [('/livez', 200, 'localhost'), ('/readyz', 200, 'localhost'),
            ('/health', 200, 'localhost'), ('/api/cloud/status', 401, 'localhost'), ('/livez', 400, 'untrusted.invalid')]:
        request = urllib.request.Request('http://127.0.0.1:8000' + path, headers={'Host': host})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:  # nosec B310: fixed loopback URL and fixed paths
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        require(code == expected, 'Unexpected health/auth/Host response.')
    from server.cloud import CloudController
    from server.service import Service
    from server.store import Store
    store = Store()
    other = CloudController(Service(store, launch_jobs=True), dry_run=True)
    try:
        require(not other.start(), 'A competing controller acquired the same PVC.')
    finally:
        other.close()
        store.close()
    print(json.dumps(value))


def serve():
    environment()
    deadline = time.monotonic() + 120
    gate = Path('/tmp/kubernetes-egress-locked')  # nosec B108: isolated fixture pod emptyDir gate, not host tmp
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    require(gate.is_file(), 'Host did not install the isolated network firewall.')
    # These packets must be rejected locally by the test namespace firewall.
    for destination in ('1.1.1.1', '192.0.2.1'):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            result = connection.connect_ex((destination, 443))
            require(result in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES),
                    'Kernel egress isolation was not confirmed; refusing to start.')
    snapshot()  # Refuse existing live state before importing the application.
    from server import cloud
    original = cloud.CloudController

    class OfflineController(original):
        def __init__(self, svc, environ=None, dry_run=False):
            super().__init__(svc, environ=environ, dry_run=True)

    cloud.CloudController = OfflineController
    from server.app import app
    import uvicorn
    real_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def observed_lifespan(application):
        async with real_lifespan(application):
            yield
        # Uvicorn can re-raise SIGTERM after lifespan exit, so record evidence
        # here, after the real drain and before its original signal is restored.
        require(all(process.poll() is not None for process in application.state.service.processes.values()),
                'A worker survived graceful API shutdown.')
        result = snapshot()
        result.pop('previous_shutdown', None)
        path = Path(os.environ['SG_DATA_DIR']) / 'kubernetes-clean-shutdown.json'
        path.write_text(json.dumps(result))
        path.chmod(0o600)
        print('PASS: API shutdown drained workers and preserved durable operator intent', flush=True)

    app.router.lifespan_context = observed_lifespan
    uvicorn.run(app, host='0.0.0.0', port=8000, access_log=False, timeout_graceful_shutdown=75)  # nosec B104: isolated kind pod probes, no Service or ingress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('serve', 'inspect'))
    args = parser.parse_args()
    (serve if args.command == 'serve' else inspect)()


if __name__ == '__main__':
    main()
