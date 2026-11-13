"""Finite cloud-controller acceptance, only in a non-root network-disabled container.

Run with --network none, a read-only root and private writable SG_DATA_DIR. The
controller is explicitly dry-run even though its input configuration is enabled.
"""
import argparse
import json
import os
from pathlib import Path
import time

from server.cloud import CloudController, record, request_control, status
from server.service import Service
from server.store import Store


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def until(predicate, message, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-stopped', action='store_true', help='verify saved state after replacing the first container')
    args = parser.parse_args()
    require(os.getuid() == 10001, 'Run the smoke check with the image non-root user.')
    # Some kernels expose disabled tunnel devices even with --network none.
    # Refuse active non-loopback interfaces or any IPv4 route.
    for interface in Path('/sys/class/net').iterdir():
        flags = interface / 'flags'
        if interface.name != 'lo' and flags.is_file():
            require(not (int(flags.read_text(), 16) & 1), 'Use docker --network none.')
    require(len(Path('/proc/net/route').read_text().strip().splitlines()) <= 1, 'Use docker --network none.')
    require(not any(os.environ.get(name) for name in ('SG_CHRONOSPHERE_TOKEN', 'SG_SYMBOL_UPLOAD_TOKEN',
            'SG_CHRONOSPHERE_TOKEN_FILE', 'SG_SYMBOL_UPLOAD_TOKEN_FILE')), 'Do not pass real credentials.')
    try:
        Path('/app/cloud-smoke-must-be-read-only').write_text('invalid')
    except OSError:
        pass
    else:
        raise RuntimeError('The application filesystem must be read-only.')
    environment = dict(SG_CLOUD_RUNNER='1', SG_WEB_APP_ID='web01', SG_ANDROID_APP_ID='and01',
                       SG_IOS_APP_ID='ios01', SG_RATE_PER_MINUTE='60')
    store = Store()
    require(bool(record(store)) == args.verify_stopped, 'Use fresh state initially, then reuse it only with --verify-stopped.')
    if args.verify_stopped:
        require(all(run['dry_run'] for run in store.list('runs')), 'Do not use a live runner data directory.')
        require({app['embrace_app_id'] for app in record(store).get('summary', {}).get('apps', [])} ==
                {'web01', 'and01', 'ios01'}, 'Use only the isolated smoke-test fixture state.')
    else:
        require(not store.list('runs') and not store.list('streams'), 'Use an empty test data directory.')
    svc = Service(store, launch_jobs=True)
    controller = CloudController(svc, environ=environment, dry_run=True)

    def close(current):
        current.close()
        until(lambda: not current.worker_busy(), 'Worker did not finish its drain.', timeout=70)
        for process in current.svc.processes.values():
            process.wait(timeout=5)

    try:
        if args.verify_stopped:
            previous = record(store)
            require(previous['desired'] == 'stopped', 'Previous container did not preserve operator stop.')
            require(controller.start(), 'Replacement container did not acquire ownership.')
            until(lambda: status(store)['status'] == 'stopped', 'Replacement container resumed stopped work.')
            time.sleep(2.5)
            require(not controller.worker_busy(), 'Replacement container started a stopped worker.')
            require(record(store)['run_id'] == previous['run_id'], 'Replacement container created duplicate work.')
            require(len(store.list('streams')) == 1, 'Replacement container duplicated the stream.')
            print('PASS: replacing container preserved stopped stream and prevented duplicate workers')
            return
        require(controller.start(), 'First controller must acquire ownership.')
        until(lambda: record(store) and record(store).get('run_id'), 'Cloud run was not created.')
        until(lambda: status(store).get('sessions_generated', 0) >= 3, 'Dry worker generated no traffic.')
        state = status(store)
        require(state['dry_run'] is True, 'Cloud smoke must never create live runs.')
        require(state['summary']['destination'] == 'sessions_only', 'Unexpected destination.')
        require({app['platform'] for app in state['summary']['apps']} == {'web', 'android', 'ios'},
                'Not all configured platforms were enabled.')
        owner, stream_id, initial_run = state['owner'], state['stream_id'], state['run_id']
        other = CloudController(Service(store, launch_jobs=True), environ=environment, dry_run=True)
        require(other.start() is False, 'A second controller acquired the same deployment.')
        require(len(store.list('streams')) == 1, 'Duplicate stream created.')

        # Closing the process for maintenance drains without changing operator intent.
        close(controller)
        require(record(store)['desired'] == 'running', 'Maintenance shutdown became an operator pause.')
        svc = Service(store, launch_jobs=True)
        controller = CloudController(svc, environ=environment, dry_run=True)
        require(controller.start(), 'Replacement controller did not acquire released ownership.')
        until(lambda: status(store).get('worker_status') == 'running', 'Maintenance restart did not recover.')
        require(record(store)['owner'] == owner and record(store)['stream_id'] == stream_id,
                'Maintenance restart changed owned stream identity.')
        require(record(store)['run_id'] == initial_run, 'Clean maintenance restart duplicated the run.')

        # Operator pause remains paused after a new controller is created.
        request_control(svc, 'pause')
        # Ordinary pause respects future delivery times; maintenance close flushes
        # pending work while preserving that explicit operator pause.
        close(controller)
        svc = Service(store, launch_jobs=True)
        controller = CloudController(svc, environ=environment, dry_run=True)
        require(controller.start(), 'Paused controller did not restart.')
        until(lambda: status(store)['status'] == 'paused', 'Operator pause was not preserved.')
        time.sleep(2.5)
        require(not controller.worker_busy(), 'Paused restart started a worker.')
        require(record(store)['run_id'] == initial_run, 'Paused restart created a run.')

        request_control(svc, 'resume')
        until(lambda: status(store)['run_id'] != initial_run and status(store).get('worker_status') == 'running',
              'Explicit resume did not start a new activation.')
        request_control(svc, 'stop')
        until(lambda: status(store)['status'] == 'stopped' and not controller.worker_busy(), 'Stop did not drain.')
        stopped_run = record(store)['run_id']
        close(controller)
        svc = Service(store, launch_jobs=True)
        controller = CloudController(svc, environ=environment, dry_run=True)
        require(controller.start(), 'Stopped controller did not restart.')
        until(lambda: status(store)['status'] == 'stopped', 'Operator stop was not preserved.')
        time.sleep(2.5)
        require(not controller.worker_busy(), 'Stopped restart started a worker.')
        require(record(store)['run_id'] == stopped_run, 'Stopped restart created a run.')
        require(len(store.list('streams')) == 1, 'Lifecycle changes duplicated the stream.')
        require(all(run['dry_run'] for run in store.list('runs')), 'A run was allowed to deliver remotely.')
        print(json.dumps({'status': 'passed', 'network': 'disabled', 'filesystem': 'read_only',
                          'platforms': ['web', 'android', 'ios'], 'streams': 1,
                          'checks': ['real_worker', 'single_controller', 'maintenance_restart',
                                     'operator_pause_restart', 'explicit_resume', 'operator_stop_restart']}, indent=2))
    finally:
        if record(store):
            request_control(svc, 'stop')
        close(controller)
        store.close()


if __name__ == '__main__':
    main()
