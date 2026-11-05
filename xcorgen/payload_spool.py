"""Private, disposable batch payload staging; no unpickling or remote input.

Complete realism preflight still precedes the first POST. Payloads are kept on disk
in delivery order so a large plan does not retain every session in RAM.
The durable RunJournal, not this scratch database, controls replay safety.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import zlib


class PayloadSpool:
    MAX_BYTES = 4 * 1024**3

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='sg-payloads-')
        self.path = os.path.join(self.directory.name, 'payloads.sqlite3')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=OFF')
        self.db.execute('PRAGMA cache_size=-4096')
        self.db.execute('CREATE TABLE payloads (ordinal INTEGER PRIMARY KEY, app INTEGER, due REAL, body BLOB)')
        self.bytes = 0
        self.count = 0
        return self

    def add(self, delivery):
        body = zlib.compress(json.dumps(delivery, separators=(',', ':')).encode(), level=1)
        self.bytes += len(body)
        if self.bytes > self.MAX_BYTES:
            raise ValueError('Batch payload staging exceeds 4 GiB. Split this run into smaller batches; no payloads were sent.')
        if self.count % 1000 == 0 and shutil.disk_usage(self.directory.name).free < 512 * 1024**2:
            raise ValueError('Batch staging needs at least 512 MiB of free disk space; no payloads were sent.')
        self.db.execute('INSERT INTO payloads(app,due,body) VALUES (?,?,?)',
                        (delivery['target'] == 'embrace', delivery['due'], body))
        self.count += 1

    def __iter__(self):
        self.db.execute('CREATE INDEX payload_order ON payloads(app,due,ordinal)')
        self.db.commit()
        for body, in self.db.execute('SELECT body FROM payloads ORDER BY app,due,ordinal'):
            yield json.loads(zlib.decompress(body))

    def __exit__(self, *_):
        self.db.close()
        self.directory.cleanup()
