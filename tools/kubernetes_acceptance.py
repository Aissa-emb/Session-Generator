"""Finite offline acceptance on an explicitly selected, disposable kind cluster.

Example: python tools/kubernetes_acceptance.py --kind /path/to/kind
  --kubeconfig /tmp/kind-kubeconfig --cluster sg-image-acceptance
  --image session-generator:kubernetes-test --output /tmp/kubernetes-result.json

Requires Docker, kubectl and kind; no company context or real credentials are
used. The cluster must already exist. Only a generated fixture namespace is
created/deleted. kind's default CNI does not enforce NetworkPolicy: a separate
kernel firewall is installed in each fixture pod network namespace before the
test entrypoint is allowed to initialize the dry controller.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess  # nosec B404: finite local Kubernetes acceptance commands, argv lists only
import time
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(command, data=None, timeout=120):
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, timeout=timeout, check=False)  # nosec B603
    if result.returncode:
        # Commands contain only known fake values; redact the fixture keys too.
        detail = (result.stderr or result.stdout)[-2000:]
        detail = re.sub(r'kubernetes-fixture-(?:trace|symbol)-v[12]', '[fixture credential]', detail)
        raise RuntimeError('Command failed: ' + command[0] + ': ' + detail)
    return result.stdout


def until(fn, message, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.5)
    raise RuntimeError(message)


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.namespace = 'sg-fixture-' + uuid.uuid4().hex[:10]
        self.node = args.cluster + '-control-plane'
        self.base = ['kubectl', '--kubeconfig', str(Path(args.kubeconfig).resolve()), '--context', 'kind-' + args.cluster]
        self.pod = None
        self.report = {'checks': [], 'image': args.image, 'cluster': 'isolated kind', 'telemetry': 'offline only',
                       'storage': 'kind local-path RWO; company CSI/RWOP remains a deployment check'}

    def kubectl(self, *args, data=None, namespace=True, timeout=120):
        return run(self.base + (['-n', self.namespace] if namespace else []) + list(args), data, timeout)

    def execute(self, *args):
        return self.kubectl('exec', self.pod, '--', *args)

    def check(self, label):
        self.report['checks'].append(label)
        print('PASS: ' + label, flush=True)

    def guard_context(self):
        if not re.fullmatch(r'sg-[a-z0-9-]+', self.args.cluster):
            raise RuntimeError('Use a disposable kind cluster named sg-*.')
        cfg = json.loads(self.kubectl('config', 'view', '--minify', '-o', 'json', namespace=False))
        server = cfg['clusters'][0]['cluster']['server']
        if not re.fullmatch(r'https://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]+', server):
            raise RuntimeError('Acceptance refuses any non-loopback Kubernetes API endpoint.')
        node = json.loads(run(['docker', 'inspect', self.node]))[0]
        if node['Config']['Labels'].get('io.x-k8s.kind.cluster') != self.args.cluster:
            raise RuntimeError('The target node is not owned by this kind cluster.')
        mappings = node['NetworkSettings']['Ports'].get('6443/tcp') or []
        if str(urlsplit(server).port) not in {mapping['HostPort'] for mapping in mappings}:
            raise RuntimeError('Kubeconfig does not point to the dedicated kind API port.')
        cluster_nodes = json.loads(self.kubectl('get', 'nodes', '-o', 'json', namespace=False))['items']
        internal_ips = {address['address'] for item in cluster_nodes if item['metadata']['name'] == self.node
                        for address in item['status']['addresses'] if address['type'] == 'InternalIP'}
        docker_ips = {network['IPAddress'] for network in node['NetworkSettings']['Networks'].values()}
        if not internal_ips.intersection(docker_ips):
            raise RuntimeError('Kubernetes node identity does not match the dedicated kind container.')
        image = json.loads(run(['docker', 'image', 'inspect', self.args.image]))[0]
        self.report['image_id'] = image['Id']

    def manifest(self):
        ref = lambda key: {'valueFrom': {'secretKeyRef': {'name': 'fixture-credentials', 'key': key}}}
        env = {key: {'value': value} for key, value in {
            'SG_CLOUD_RUNNER': '1', 'SG_WEB_APP_ID': 'web01', 'SG_ANDROID_APP_ID': 'and01',
            'SG_IOS_APP_ID': 'ios01', 'SG_RATE_PER_MINUTE': '60', 'SG_REQUIRE_AUTH': '1',
            'SG_RUNNER_REGION': 'kubernetes-acceptance', 'SG_DATA_DIR': '/var/lib/session-generator',
            'SG_SECRET_DIR': '/var/lib/session-generator/secrets', 'TMPDIR': '/var/lib/session-generator',  # nosec B105: directory, not a secret
        }.items()}
        env.update(SG_CHRONOSPHERE_TENANT=ref('orgName'), SG_CHRONOSPHERE_TOKEN=ref('apiToken'),
                   SG_SYMBOL_UPLOAD_TOKEN=ref('symbols'))
        probe = lambda path: {'httpGet': {'path': path, 'port': 8000,
                            'httpHeaders': [{'name': 'Host', 'value': 'localhost'}]}, 'timeoutSeconds': 3, 'periodSeconds': 2}
        container = {'name': 'generator', 'image': self.args.image, 'imagePullPolicy': 'Never',
                     'args': ['python', '-m', 'tools.kubernetes_smoke', 'serve'],
                     'env': [{'name': key, **value} for key, value in env.items()],
                     'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                         'capabilities': {'drop': ['ALL']}},
                     'volumeMounts': [{'name': 'state', 'mountPath': '/var/lib/session-generator'},
                                      {'name': 'tmp', 'mountPath': '/tmp'}],  # nosec B108: isolated pod emptyDir, not a host shared path
                     'startupProbe': {**probe('/readyz'), 'failureThreshold': 120},
                     'readinessProbe': probe('/readyz'), 'livenessProbe': {**probe('/livez'), 'failureThreshold': 5},
                     'resources': {'requests': {'cpu': '250m', 'memory': '256Mi'},
                                   'limits': {'cpu': '2', 'memory': '2Gi'}}}
        pod = {'automountServiceAccountToken': False, 'terminationGracePeriodSeconds': 90,
               'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True,
                                   'fsGroup': 10001, 'fsGroupChangePolicy': 'OnRootMismatch',
                                   'seccompProfile': {'type': 'RuntimeDefault'}},
               'containers': [container], 'volumes': [{'name': 'state', 'persistentVolumeClaim': {'claimName': 'fixture-state'}},
                                                      {'name': 'tmp', 'emptyDir': {'sizeLimit': '256Mi'}}]}
        deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'kubernetes-smoke'},
                      'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
                               'selector': {'matchLabels': {'app': 'kubernetes-smoke'}},
                               'template': {'metadata': {'labels': {'app': 'kubernetes-smoke'}}, 'spec': pod}}}
        return deployment

    def apply(self, resource):
        self.kubectl('apply', '-f', '-', data=resource)

    def secret(self, version=1):
        self.apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'fixture-credentials'},
                    'stringData': {'orgName': 'kubernetes-fixture.chronosphere.io:443',
                                   'apiToken': f'kubernetes-fixture-trace-v{version}',
                                   'symbols': f'kubernetes-fixture-symbol-v{version}'}})

    def start_pod(self, previous=None):
        def running():
            pods = json.loads(self.kubectl('get', 'pods', '-l', 'app=kubernetes-smoke', '-o', 'json'))['items']
            if previous and any(p['metadata']['name'] == previous for p in pods):
                return None  # Recreate must finish shutdown before we release the next fixture.
            candidates = [p for p in pods if p['metadata']['name'] != previous and
                          not p['metadata'].get('deletionTimestamp') and
                          any(s.get('state', {}).get('running') for s in p.get('status', {}).get('containerStatuses', []))]
            return candidates[0]['metadata']['name'] if len(candidates) == 1 else None
        self.pod = until(running, 'Fixture container did not start; inspect missing keys/PVC/image.', 180)
        # Locate exactly this sandbox on exactly this local kind node.
        sandboxes = json.loads(run(['docker', 'exec', self.node, 'crictl', 'pods', '-o', 'json']))['items']
        sandbox = next(p for p in sandboxes if p['metadata']['name'] == self.pod and
                       p['metadata']['namespace'] == self.namespace and p['state'] == 'SANDBOX_READY')
        info = json.loads(run(['docker', 'exec', self.node, 'crictl', 'inspectp', sandbox['id']]))
        pid = str(int(info['info']['pid']))
        for binary in ('iptables', 'ip6tables'):
            prefix = ['docker', 'exec', self.node, 'nsenter', '-t', pid, '-n', binary, '-w']
            for arguments in [ ['-N', 'SG_FIXTURE_OUTPUT'], ['-A', 'SG_FIXTURE_OUTPUT', '-o', 'lo', '-j', 'ACCEPT'],
                    ['-A', 'SG_FIXTURE_OUTPUT', '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
                    ['-A', 'SG_FIXTURE_OUTPUT', '-j', 'REJECT'], ['-I', 'OUTPUT', '1', '-j', 'SG_FIXTURE_OUTPUT'] ]:
                run(prefix + arguments)
        self.execute('python', '-c', "from pathlib import Path; Path('/tmp/kubernetes-egress-locked').touch()")
        self.kubectl('wait', '--for=condition=Ready', 'pod/' + self.pod, '--timeout=180s', timeout=190)
        return self.pod

    def inspect(self):
        return json.loads(self.execute('python', '-m', 'tools.kubernetes_smoke', 'inspect'))

    def wait_state(self, predicate, message):
        return until(lambda: (state if predicate(state := self.inspect()) else None), message)

    def control(self, action):
        return json.loads(self.execute('python', '-m', 'server.cloud', action))

    def replace(self):
        old = self.pod
        logs = self.kubectl('logs', self.pod)
        if re.search(r'kubernetes-fixture-(?:trace|symbol)-v[12]', logs):
            raise RuntimeError('A fixture credential appeared in ordinary container logs.')
        started = time.monotonic()
        self.kubectl('patch', 'deployment', 'kubernetes-smoke', '--type=merge', '-p', json.dumps(
            {'spec': {'template': {'metadata': {'annotations': {'fixture-revision': uuid.uuid4().hex}}}}}))
        self.start_pod(previous=old)
        self.report.setdefault('replacement_seconds', []).append(round(time.monotonic() - started, 2))
        return self.inspect()

    def lifecycle(self):
        started = time.monotonic()
        initial = self.wait_state(lambda s: s['sessions_generated'] and s['sessions_generated'] >= 3,
                                  'Dry worker did not generate three sessions.')
        require(initial['private_credentials'] == 2 and initial['streams'] == 1 and initial['dry_run'],
                'Unexpected fixture stream, credential snapshot or delivery mode.')
        self.check('Real Secret env injection, tenant :443, three-platform dry worker, single controller, private credentials')
        self.check('Nonroot/read-only filesystem, liveness/readiness probes, API auth and Host admission')
        after = self.replace()
        require(after['owner'] == initial['owner'] and after['stream_id'] == initial['stream_id'], 'Ownership changed after replacement.')
        require(after['run_id'] == initial['run_id'] and after['previous_shutdown']['desired'] == 'running',
                'Maintenance shutdown lost the active run or operator intent.')
        after = self.wait_state(lambda s: (s['sessions_generated'] or 0) > initial['sessions_generated'], 'Maintenance replacement lost progress.')
        self.check('SIGTERM drains real workers; replacement preserves active run, counters and owner-only PVC snapshots')
        self.control('pause')
        after = self.replace()
        require(after['desired'] == 'paused' and after['previous_shutdown']['desired'] == 'paused', 'Operator pause was lost.')
        self.wait_state(lambda s: s['status'] == 'paused' and s['worker_status'] != 'running', 'Pause did not survive replacement.')
        self.check('Operator pause survives pod replacement without starting a worker')
        self.control('resume')
        resumed = self.wait_state(lambda s: s['run_id'] != initial['run_id'] and (s['sessions_generated'] or 0) >= 2,
                                  'Explicit resume did not start a new dry activation.')
        self.control('stop')
        self.replace()
        after = self.wait_state(lambda s: s['status'] == 'stopped', 'Operator stop did not survive replacement.')
        require(after['run_id'] == resumed['run_id'] and after['previous_shutdown']['desired'] == 'stopped', 'Operator stop was lost.')
        self.check('Explicit resume works; operator stop survives replacement')
        self.secret(version=2)
        require(self.inspect()['secret_version'] == 1, 'Existing process env changed without replacement.')
        after = self.replace()
        require(after['secret_version'] == 2 and after['desired'] == 'stopped' and after['private_credentials'] == 2,
                'Rotation replaced immutable credentials or resumed stopped work.')
        self.control('resume')
        rotated = self.wait_state(lambda s: s['private_credentials'] == 4 and (s['sessions_generated'] or 0) >= 1,
                                  'Replacement did not snapshot rotated fixture credentials.')
        require(rotated['owner'] == initial['owner'] and rotated['streams'] == 1, 'Rotation duplicated the cloud stream.')
        self.execute('python', '-c', "import os,signal; from pathlib import Path; "
                     "pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() and "
                     "p.joinpath('cmdline').exists() and b'\\x00-m\\x00server.worker\\x00' in p.joinpath('cmdline').read_bytes()]; "
                     "len(pids)==1 or (_ for _ in ()).throw(RuntimeError('Expected one dry worker')); os.kill(pids[0],signal.SIGKILL)")
        recovered = self.wait_state(lambda s: s['run_id'] != rotated['run_id'] and (s['sessions_generated'] or 0) >= 1,
                                    'Interrupted worker did not recover within the bounded retry path.')
        self.check('Forced worker exit starts a new activation; no claim of lossless or exactly-once recovery')
        self.control('stop')
        self.check('Secret rotation takes effect only after replacement; immutable old/new credentials remain private')
        self.report.update(status='passed', duration_seconds=round(time.monotonic() - started, 2),
                           final_dry_sessions=recovered['sessions_generated'], live_deliveries=0,
                           architecture=recovered['architecture'], state_bytes=recovered['state_bytes'],
                           memory_current_bytes=recovered.get('memory_current_bytes'))

    def missing_key(self):
        deployment = self.manifest()
        container = deployment['spec']['template']['spec']['containers'][0]
        broken = json.loads(json.dumps(container))
        for variable in broken['env']:
            if variable['name'] == 'SG_CHRONOSPHERE_TOKEN':
                variable['valueFrom']['secretKeyRef']['key'] = 'missing-required-key'
        self.apply({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'missing-key'},
                    'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                             'containers': [{key: value for key, value in broken.items()
                                             if key not in ('volumeMounts', 'startupProbe', 'readinessProbe', 'livenessProbe')}]}})
        def blocked():
            pod = json.loads(self.kubectl('get', 'pod', 'missing-key', '-o', 'json'))
            return any(s.get('state', {}).get('waiting', {}).get('reason') == 'CreateContainerConfigError'
                       for s in pod.get('status', {}).get('containerStatuses', []))
        until(blocked, 'Missing required Secret key was not rejected.')
        self.kubectl('delete', 'pod', 'missing-key', '--wait=true')
        self.check('Missing required Secret key prevents container startup')

    def run(self):
        self.guard_context()
        run([self.args.kind, 'load', 'docker-image', '--name', self.args.cluster, self.args.image], timeout=300)
        self.kubectl('create', 'namespace', self.namespace, namespace=False)
        try:
            self.secret()
            self.apply({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'fixture-state'},
                        'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'standard',
                                 'resources': {'requests': {'storage': '2Gi'}}}})
            self.missing_key()
            self.apply(self.manifest())
            self.start_pod()
            self.lifecycle()
        finally:
            self.kubectl('delete', 'namespace', self.namespace, '--wait=true', '--timeout=150s', namespace=False, timeout=160)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', default='kind')
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--cluster', default='sg-image-acceptance')
    parser.add_argument('--image', default='session-generator:kubernetes-test')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    test = Acceptance(args)
    test.run()
    if args.output:
        args.output.write_text(json.dumps(test.report, indent=2) + '\n')
    print(json.dumps(test.report, indent=2))


if __name__ == '__main__':
    main()
