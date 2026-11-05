"""Deterministic scheduler tests: the clock charges for generation, not CPU speed."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from server import worker
from server.store import Store
from xcorgen.spec import default_spec


class LiveScaleTests(unittest.TestCase):
    def exercise(self, seconds=10, cost=0, stall=0, pause_at=None, blocked_until=0, action='paused'):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            spec = default_spec()
            spec['traffic'] = dict(mode='live', rate_per_minute=600, duration_minutes=seconds / 60)
            version = store.save_spec(spec)
            clock = SimpleNamespace(now=1000., sleeps=0)
            clock.time = clock.monotonic = lambda: clock.now
            generated, saves, updates = [], [], []
            def sleep(dt):
                clock.now += dt
                clock.sleeps += 1
                if pause_at and clock.now >= 1000 + pause_at:
                    store.update('runs', 'r', desired_status=action)
            clock.sleep = sleep
            job = store.put('runs', dict(id='r', spec_id=version['id'], spec_version=1, started=1000.,
                    desired_status='running', status='running', dry_run=True, mode='live', destination='sessions_only',
                    manifest_path=str(Path(directory) / 'manifest.jsonl'), counters={}))
            class Runner:
                def __init__(self, *a, **kw):
                    self.n = 0
                    self.emitted = SimpleNamespace(last_observation={}, summary=lambda: {'sessions': self.n})
                def session(self, start):
                    generated.append(clock.now)
                    clock.now += cost + (stall if len(generated) == 1 else 0)
                    session = SimpleNamespace(app_id='abcde', persona={'device_id':'d'}, user_session_id=str(len(generated)),
                            start_ms=start, end_ms=start, properties={'business_unit':'retail'}, traces=False, calls=[], plan=SimpleNamespace(evidence=[]))
                    d = dict(target='embrace', kind='logs', due=start, payload={'resourceLogs': []})
                    return {'id':'a','platform':'web'}, session, '', [d], []
                def record(self, *a):
                    self.n += 1
                def save(self):
                    saves.append(clock.now)
            class Sender:
                def __init__(self, store, job, spec, callback=None, writer=None):
                    self.callback, self.writer = callback, writer
                    self.inflight = self.queued = 0
                def submit(self, d, block=True):
                    if clock.now < 1000 + blocked_until:
                        return None
                    receipt = dict(status='accepted', accepted=1, rejected=0, target='embrace', kind='logs', time_ms=clock.now*1000)
                    ack = self.callback(d, receipt)
                    self.writer.put(dict(receipt, run_id='r', _sessions_acknowledged=ack))
                    return object()
                def close(self):
                    pass
            original = store.update
            def update(table, ident, **changes):
                updates.append(changes)
                return original(table, ident, **changes)
            try:
                with patch.object(worker, 'time', clock), patch.object(worker, 'SpecRun', Runner), \
                     patch.object(worker, 'ReceiptSender', Sender), patch.object(worker, 'config', return_value={}), \
                     patch.object(worker, 'lint_payloads', return_value=[]), patch.object(worker.random, 'expovariate', return_value=.1), \
                     patch.object(store, 'update', side_effect=update):
                    try:
                        worker.live(store, job, version)
                    except (worker.JobPaused, worker.JobStopped):
                        self.assertIsNotNone(pause_at)
                return store.get('runs', 'r'), generated, saves, updates, clock.now
            finally:
                store.close()

    def test_high_rate_has_no_fixed_sleep_ceiling_and_throttles_bookkeeping(self):
        row, sessions, saves, updates, _ = self.exercise()
        self.assertTrue(99 <= len(sessions) <= 101, len(sessions))
        self.assertLessEqual(len(saves), 4)
        self.assertLessEqual(sum('counters' in c for c in updates), 12)
        self.assertGreater(row['achieved_rate_per_minute'], 580)
        self.assertEqual(row['delivery_totals']['sessions_acknowledged'], len(sessions))

    def test_schedule_catches_up_after_a_slow_pass(self):
        _, sessions, _, _, _ = self.exercise(stall=2)
        self.assertTrue(99 <= len(sessions) <= 101, len(sessions))
        self.assertGreater(sum(1002 <= t < 1002.2 for t in sessions), 10)

    def test_pause_remains_responsive_under_load(self):
        _, sessions, _, _, ended = self.exercise(cost=.05, pause_at=2)
        self.assertLess(ended, 1002.6)
        self.assertLessEqual(len(sessions), 25)

    def test_sender_saturation_is_reported_and_recovers(self):
        row, sessions, _, _, _ = self.exercise(blocked_until=3)
        self.assertTrue(row['capacity_limited'])
        self.assertEqual(row['capacity_reason'], 'sender_saturation')
        self.assertEqual(row['counters']['payloads'], len(sessions))

    def test_stop_remains_responsive_under_load_and_drains(self):
        row, sessions, _, _, ended = self.exercise(cost=.05, pause_at=2, action='stopped')
        self.assertLess(ended, 1002.6)
        self.assertEqual(row['counters']['payloads'], len(sessions))

    def test_excess_schedule_lag_is_bounded_and_reported(self):
        row, sessions, _, _, _ = self.exercise(seconds=50, stall=35)
        self.assertGreater(row['missed_arrivals'], 300)
        self.assertEqual(row['capacity_reason'], 'generation_cpu')
        self.assertLess(len(sessions), 160)

    def test_error_window_ignores_old_failures(self):
        health = worker.DeliveryHealth()
        for _ in range(30):
            health.add(True)
            for _ in range(30):
                health.add(False)
            self.assertIsNone(health.failure())
        for _ in range(10):
            health.add(True)
        self.assertIn('10 consecutive', health.failure())
        health = worker.DeliveryHealth()
        for i in range(200):
            health.add(i % 10 == 0)
        self.assertIn('20 of the last 200', health.failure())


if __name__ == '__main__':
    unittest.main()
