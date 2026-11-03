"""Controlled in-memory book transfer state; no external financial operations."""
from threading import Lock


class Ledger:
    def __init__(self):
        self._lock=Lock()
        self.intents={}
        self.keys={}
        self.outbox={}
        self.transactions={}

    def accept(self, user, reference, key, amount=10000):
        with self._lock:
            identity=(user,key)
            if identity in self.keys:
                prior=self.intents[self.keys[identity]]
                if prior['reference']!=reference or prior['amount']!=amount:
                    raise ValueError('Idempotency key conflicts with the original command')
                return dict(prior)
            if reference in self.intents:
                raise ValueError('Operation reference already belongs to an intent')
            row={'reference':reference,'owner':user,'amount':amount,'state':'Pending'}
            # One atomic transition creates intent, idempotency record and durable outbox.
            self.intents[reference]=row
            self.keys[identity]=reference
            self.outbox[reference]={'destination':'money.transfers.requested','reference':reference}
            return dict(row)

    def post(self, reference):
        with self._lock:
            row=self.intents[reference]
            if reference not in self.transactions:
                self.transactions[reference]={'debit':-row['amount'],'credit':row['amount']}
            return {'destination':'money.transfers.posted','reference':reference}

    def complete(self, reference):
        with self._lock:
            if reference not in self.transactions:raise ValueError('Completion before ledger commit')
            self.intents[reference]['state']='Completed'

    def status(self, user, reference):
        with self._lock:
            row=self.intents[reference]
            if row['owner']!=user:raise ValueError('Operation belongs to another synthetic user')
            return row['state']
