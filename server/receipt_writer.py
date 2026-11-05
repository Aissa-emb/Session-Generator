"""Live-only receipt batching. Bounded RunJournal intent/receipt commits stay synchronous.

Live workers never replay incomplete exports after a crash. Losing the final 200 ms
of observational receipts therefore cannot cause a resend. Flush on orderly exit;
writer failures propagate to the worker instead of silently dropping evidence.
"""
import queue
import threading
import time


class ReceiptWriter:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id
        self.queue = queue.Queue(maxsize=2048)
        self.error = None
        self.thread = threading.Thread(target=self._run, name='live-receipts', daemon=True)
        self.thread.start()

    def check(self):
        if self.error:
            raise RuntimeError('Live receipt persistence failed.') from self.error

    def put(self, receipt):
        while True:
            self.check()
            try:
                self.queue.put(receipt, timeout=.2)
                return
            except queue.Full:
                pass

    def _run(self):
        try:
            done = False
            while not done:
                batch = []
                item = self.queue.get()
                if item is None:
                    break
                batch.append(item)
                deadline = time.monotonic() + .2
                while len(batch) < 512:
                    try:
                        item = self.queue.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if item is None:
                        done = True
                        break
                    batch.append(item)
                self.store.write_live_receipts(self.run_id, batch)
        except Exception as error:
            self.error = error

    def close(self):
        self.put(None)
        self.thread.join()
        self.check()
