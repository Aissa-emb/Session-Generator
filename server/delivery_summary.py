"""Run-wide acknowledgement counts. Transport receipts do not prove storage."""
import json
from .verification import counted_sessions, manifest_sessions


def session_keys(record):
    return [':'.join(str(row.get(k, '')) for k in ('target', 'app_id', 'device_id', 'session_id'))
            for row in counted_sessions([record])]


def annotate(deliveries, record):
    """Attach private accounting metadata after the durable payload hash is made."""
    app = [d for d in deliveries if d['target'] == 'embrace']
    evidence = {'keys': session_keys(record), 'expected_payloads': len(app)}
    for delivery in app:
        delivery['_session_receipt'] = evidence


def summarize(store, run):
    # Aggregate the full indexed history in SQLite, not the 500-row UI window
    # or a Python copy of every receipt in a long-running stream.
    with store.engine.connect() as conn:
        backend = conn.exec_driver_sql("""
            SELECT COALESCE(SUM(n), 0) FROM (
              SELECT MAX(COALESCE(json_extract(data, '$.accepted'), 0)) AS n
              FROM receipts WHERE json_extract(data, '$.run_id') = ?
                AND json_extract(data, '$.target') = 'otlp'
                AND json_extract(data, '$.kind') = 'traces'
                AND json_extract(data, '$.status') IN ('accepted', 'partial')
              GROUP BY COALESCE(json_extract(data, '$.delivery_id'), id))
            """, (run['id'],)).scalar_one()
        groups = conn.exec_driver_sql("""
            SELECT json_extract(data, '$.session_receipt.keys') AS keys
            FROM receipts WHERE json_extract(data, '$.run_id') = ?
              AND json_extract(data, '$.target') = 'embrace'
              AND json_type(data, '$.session_receipt.keys') = 'array'
            GROUP BY keys
            HAVING MIN(json_extract(data, '$.session_receipt.expected_payloads')) > 0
              AND MIN(json_extract(data, '$.session_receipt.expected_payloads')) =
                  MAX(json_extract(data, '$.session_receipt.expected_payloads'))
              AND MIN(json_extract(data, '$.session_receipt.expected_payloads')) =
                  COUNT(DISTINCT CASE WHEN json_extract(data, '$.status') = 'accepted'
                    AND COALESCE(json_extract(data, '$.rejected'), 0) = 0
                    THEN COALESCE(json_extract(data, '$.delivery_id'), id) END)
            """, (run['id'],)).all()
    acknowledged = set()
    for row in manifest_sessions(run['manifest_path']):
        if row.get('stage') == 'sent':
            acknowledged.update(session_keys(row))
    for keys, in groups:
        acknowledged.update(json.loads(keys))
    return {'sessions_acknowledged': len(acknowledged),
            'backend_spans_acknowledged': backend,
            'sessions_stored': None, 'backend_spans_stored': None,
            'storage_status': 'not_verified',
            'basis': 'local_output' if run['dry_run'] else 'destination_acknowledgement'}
