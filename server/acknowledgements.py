"""Incremental, duplicate-safe acknowledgement counts for bounded runs.

These are display aggregates. The engine's FULL-synchronous journal remains the
only authority for whether a delivery can be sent or resumed.
"""
import hashlib
import json


def apply(conn, run_id, row, totals):
    delivery_id = row.get('delivery_id') or row['id']
    previous = conn.exec_driver_sql('SELECT good, spans FROM ack_deliveries WHERE run_id=? AND delivery_id=?',
                                   (run_id, delivery_id)).first()
    old_good, old_spans = previous or (0, 0)
    good = int(row['status'] == 'accepted' and not row.get('rejected', 0))
    spans = row.get('accepted', 0) if row['target'] == 'otlp' and row['kind'] == 'traces' and row['status'] in ('accepted', 'partial') else 0
    # A later duplicate failure cannot erase a known accepted delivery.
    good, spans = max(good, old_good), max(spans, old_spans)
    totals['backend_spans_acknowledged'] += spans - old_spans
    conn.exec_driver_sql('INSERT OR REPLACE INTO ack_deliveries VALUES (?,?,?,?)', (run_id, delivery_id, good, spans))
    evidence = row.get('session_receipt') or {}
    keys, expected = evidence.get('keys', []), evidence.get('expected_payloads', 0)
    if row['target'] != 'embrace' or not keys or not expected:
        return
    group = hashlib.sha256(json.dumps(sorted(set(keys))).encode()).hexdigest()
    prior = conn.exec_driver_sql('SELECT expected, received, sdk_sessions FROM ack_sessions WHERE run_id=? AND group_id=?', (run_id, group)).first()
    need, received, count = prior or (expected, 0, len(set(keys)))
    before = received == need and need > 0
    if need != expected:
        need = -1  # inconsistent evidence can never be reported as acknowledged
    received += good - old_good
    after = received == need and need > 0
    totals['sessions_acknowledged'] += (int(after) - int(before)) * count
    conn.exec_driver_sql('INSERT OR REPLACE INTO ack_sessions VALUES (?,?,?,?,?)', (run_id, group, need, received, count))


def write(store, row):
    from .store import TABLES, identifier
    from sqlalchemy import select
    import time
    row = dict(row)
    row.setdefault('id', identifier())
    row.setdefault('created', time.time())
    run_id = row['run_id']
    with store.engine.connect() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        run = dict(conn.execute(select(TABLES['runs'].c.data).where(TABLES['runs'].c.id == run_id)).scalar_one())
        totals = dict(run.get('delivery_totals') or {'sessions_acknowledged': 0, 'backend_spans_acknowledged': 0})
        if 'delivery_totals' not in run:
            # One-time bootstrap for existing jobs; subsequent polls and writes are constant cost.
            for data, in conn.exec_driver_sql("SELECT data FROM receipts WHERE json_extract(data, '$.run_id')=? ORDER BY created", (run_id,)):
                apply(conn, run_id, json.loads(data), totals)
        apply(conn, run_id, row, totals)
        conn.execute(TABLES['receipts'].insert().values(id=row['id'], created=row['created'], data=row))
        run['delivery_totals'] = totals
        conn.execute(TABLES['runs'].update().where(TABLES['runs'].c.id == run_id).values(data=run))
        conn.commit()
