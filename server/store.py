"""SQLite persistence shared by API, MCP and isolated job processes."""
import hashlib
import os
from pathlib import Path
import secrets
import time
import uuid
from sqlalchemy import JSON, Column, Float, Index, Integer, MetaData, String, Table, create_engine, event, select, literal_column, func

metadata = MetaData()
TABLES = {}
for name in ('targets', 'scenario_specs', 'runs', 'receipts', 'verifications', 'dashboard_setup',
             'api_tokens', 'streams', 'settings', 'copilot_turns', 'preparations'):
    TABLES[name] = Table(name, metadata, Column('id', String, primary_key=True),
                        Column('created', Float, nullable=False), Column('data', JSON, nullable=False))
for table in ('receipts', 'verifications'):
    Index(table + '_run_created', func.json_extract(TABLES[table].c.data, literal_column("'$.run_id'")), TABLES[table].c.created)
Index('api_tokens_hash', func.json_extract(TABLES['api_tokens'].c.data, literal_column("'$.hash'")))
events = Table('run_events', metadata, Column('id', Integer, primary_key=True, autoincrement=True),
               Column('run_id', String, index=True), Column('created', Float), Column('data', JSON))
Index('run_events_run_cursor', events.c.run_id, events.c.id)
versions = Table('spec_versions', metadata, Column('spec_id', String, primary_key=True),
                 Column('version', Integer, primary_key=True), Column('created', Float), Column('data', JSON))


def identifier():
    return uuid.uuid4().hex


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('SG_DATA_DIR', '.state/server')).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / 'control.sqlite3'
        if path.is_symlink():
            raise ValueError('Database path cannot be a symbolic link.')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.engine = create_engine(f'sqlite:///{path}', connect_args={'check_same_thread': False, 'timeout': 30})
        @event.listens_for(self.engine, 'connect')
        def configure(dbapi, _):
            dbapi.execute('PRAGMA journal_mode=WAL')
            dbapi.execute('PRAGMA synchronous=FULL')
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            # create_all skips indexes on tables that already exist. Install these
            # additive indexes explicitly when opening an owner's existing database.
            for table in ('receipts', 'verifications'):
                conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {table}_run_created ON {table}(json_extract(data, '$.run_id'), created)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS api_tokens_hash ON api_tokens(json_extract(data, '$.hash'))")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS run_events_run_cursor ON run_events(run_id, id)")
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS ack_deliveries (run_id TEXT, delivery_id TEXT, good INTEGER, spans INTEGER, PRIMARY KEY(run_id, delivery_id))")
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS ack_sessions (run_id TEXT, group_id TEXT, expected INTEGER, received INTEGER, sdk_sessions INTEGER, PRIMARY KEY(run_id, group_id))")

        # Importing the ASGI app must not leave an unused connection open. Runtime
        # connections remain pooled until the lifespan/worker closes this Store.
        self.engine.dispose()

    def close(self):
        self.engine.dispose()

    def write_live_receipts(self, run_id, rows):
        # Receipt history and its cumulative acknowledgement totals commit atomically.
        # Live delivery IDs are unique and are never replayed after a worker restart.
        prepared = []
        sessions = spans = 0
        for original in rows:
            row = dict(original)
            sessions += row.pop('_sessions_acknowledged', 0)
            if row['target'] == 'otlp' and row['kind'] == 'traces' and row['status'] in ('accepted', 'partial'):
                spans += row.get('accepted', 0)
            row.setdefault('id', identifier())
            row.setdefault('created', time.time())
            prepared.append(dict(id=row['id'], created=row['created'], data=row))
        with self.engine.connect() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            conn.execute(TABLES['receipts'].insert(), prepared)
            data = dict(conn.execute(select(TABLES['runs'].c.data).where(TABLES['runs'].c.id == run_id)).scalar_one())
            totals = dict(data.get('delivery_totals') or {'sessions_acknowledged': 0, 'backend_spans_acknowledged': 0})
            totals['sessions_acknowledged'] += sessions
            totals['backend_spans_acknowledged'] += spans
            data['delivery_totals'] = totals
            conn.execute(TABLES['runs'].update().where(TABLES['runs'].c.id == run_id).values(data=data))
            conn.commit()

    def retain_live_history(self, run_id, receipts=10000, event_count=2000):
        # Indexed, bounded tails; cumulative totals survive pruning. SQLite reuses freed pages.
        with self.engine.begin() as conn:
            conn.exec_driver_sql("""DELETE FROM receipts WHERE json_extract(data, '$.run_id') = ? AND created <
                (SELECT created FROM receipts WHERE json_extract(data, '$.run_id') = ? ORDER BY created DESC LIMIT 1 OFFSET ?)""",
                (run_id, run_id, receipts - 1))
            conn.exec_driver_sql("""DELETE FROM run_events WHERE run_id = ? AND id <
                (SELECT id FROM run_events WHERE run_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                (run_id, run_id, event_count - 1))

    def get(self, table, id):
        with self.engine.connect() as conn:
            row = conn.execute(select(TABLES[table].c.data).where(TABLES[table].c.id == id)).first()
        if not row:
            raise KeyError(f'{table} item was not found')
        return row[0]

    def list(self, table):
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(select(TABLES[table].c.data).order_by(TABLES[table].c.created.desc()))]

    def list_for(self, table, key, value, limit=None, created_from=None):
        t = TABLES[table]
        field = func.json_extract(t.c.data, literal_column("'$.run_id'")) if key == 'run_id' else t.c.data[key].as_string()
        statement = select(t.c.data).where(field == value).order_by(t.c.created.desc())
        if created_from is not None:
            statement = statement.where(t.c.created >= created_from)
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(statement)]

    def put(self, table, data):
        data = dict(data)
        data.setdefault('id', identifier())
        data.setdefault('created', time.time())
        t = TABLES[table]
        from sqlalchemy.dialects.sqlite import insert
        with self.engine.begin() as conn:
            stmt = insert(t).values(id=data['id'], created=data['created'], data=data)
            conn.execute(stmt.on_conflict_do_update(index_elements=['id'], set_={'data': data}))
        return data

    def update(self, table, id, **changes):
        # Read-modify-write under an immediate transaction, including across worker processes.
        with self.engine.connect() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            row = conn.execute(select(TABLES[table].c.data).where(TABLES[table].c.id == id)).first()
            if not row:
                conn.rollback()
                raise KeyError(f'{table} item was not found')
            data = dict(row[0], **changes)
            conn.execute(TABLES[table].update().where(TABLES[table].c.id == id).values(data=data))
            conn.commit()
        return data

    def delete(self, table, id):
        with self.engine.begin() as conn:
            conn.execute(TABLES[table].delete().where(TABLES[table].c.id == id))

    def spec_version(self, id, version=None):
        if version is None:
            return self.get('scenario_specs', id)
        with self.engine.connect() as conn:
            row = conn.execute(select(versions.c.data).where(versions.c.spec_id == id, versions.c.version == version)).first()
        if not row:
            raise KeyError('Spec version was not found')
        return row[0]

    def save_spec(self, spec, author='user', id=None, expected_version=None):
        id = id or identifier()
        with self.engine.connect() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            t = TABLES['scenario_specs']
            prior = conn.execute(select(t.c.data).where(t.c.id == id)).first()
            current = prior[0]['version'] if prior else 0
            if expected_version is not None and current != expected_version:
                conn.rollback()
                raise ValueError('Spec changed since this edit; reload the current version.')
            data = {'id': id, 'version': current + 1, 'parent_version': current or None,
                    'spec': spec, 'author': author, 'created': time.time()}
            conn.execute(versions.insert().values(spec_id=id, version=current + 1, created=data['created'], data=data))
            if prior:
                conn.execute(t.update().where(t.c.id == id).values(data=data))
            else:
                conn.execute(t.insert().values(id=id, created=data['created'], data=data))
            conn.commit()
        return data

    def event(self, run_id, type, **data):
        from .audit import emit
        if type != 'progress':
            emit(type, run_id=run_id, **data)
        body = {'type': type, **data}
        with self.engine.begin() as conn:
            result = conn.execute(events.insert().values(run_id=run_id, created=time.time(), data=body))
        return result.inserted_primary_key[0]

    def events(self, run_id, after=0):
        with self.engine.connect() as conn:
            return [{'id': r.id, 'created': r.created, **r.data} for r in conn.execute(select(events).where(
                events.c.run_id == run_id, events.c.id > after).order_by(events.c.id).limit(500))]

    def create_token(self, name):
        if not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise ValueError('Token name must be 1–80 characters.')
        token = secrets.token_urlsafe(36)
        row = self.put('api_tokens', {'name': name, 'hash': hashlib.sha256(token.encode()).hexdigest(),
                                     'revoked': False, 'last_used': None})
        return {k: v for k, v in row.items() if k != 'hash'} | {'token': token}

    def authenticate(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.engine.connect() as conn:
            row = conn.execute(select(TABLES['api_tokens'].c.data).where(
                func.json_extract(TABLES['api_tokens'].c.data, literal_column("'$.hash'")) == digest)).scalar_one_or_none()
        if row and not row['revoked'] and secrets.compare_digest(row['hash'], digest):
            if time.time() - (row.get('last_used') or 0) >= 60:
                self.update('api_tokens', row['id'], last_used=time.time())
            return 'mcp:' + row['name']
        return None
