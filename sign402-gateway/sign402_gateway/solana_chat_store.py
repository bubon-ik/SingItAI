"""Private, durable Solana chat budgets and payment journal; no prompts or keys."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

ACTIVE = ('approving', 'paying', 'uncertain', 'credit_pending')


class SolanaChatStore:
    def __init__(self, path, now=time.time):
        self.path, self.now = Path(path), now
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS preferences (user_id TEXT PRIMARY KEY, chain TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, payer TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', cap INTEGER NOT NULL DEFAULT 0,
                    expires INTEGER NOT NULL DEFAULT 0, policy_hash TEXT NOT NULL DEFAULT '',
                    credit INTEGER, credit_checked INTEGER);
                CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    payer TEXT NOT NULL, quote TEXT NOT NULL, amount INTEGER NOT NULL,
                    state TEXT NOT NULL, started INTEGER, transaction_id TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_payment ON payments(user_id)
                    WHERE state IN ('approving','paying','uncertain','credit_pending');
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def chain(self, user_id, value=None):
        with self.db() as db:
            if value is not None:
                if value not in ('base', 'solana'):
                    raise ValueError('Invalid chain')
                db.execute('INSERT INTO preferences VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET chain=excluded.chain', (str(user_id), value))
            row = db.execute('SELECT chain FROM preferences WHERE user_id=?', (str(user_id),)).fetchone()
            return row['chain'] if row else 'base'

    def user(self, user_id, payer=None):
        with self.db() as db:
            if payer:
                db.execute('INSERT OR IGNORE INTO users(user_id,payer) VALUES (?,?)', (str(user_id), payer))
            row = db.execute('SELECT * FROM users WHERE user_id=?', (str(user_id),)).fetchone()
            if row and payer and row['payer'] != payer:
                raise ValueError('Wallet binding changed')
            return dict(row) if row else None

    def get_session(self, user_id):
        return SimpleNamespace(model=(self.user(user_id) or {}).get('model', ''))

    def set_model(self, user_id, model):
        with self.db() as db:
            db.execute('UPDATE users SET model=? WHERE user_id=?', (model, str(user_id)))

    def policy(self, user_id, payer, cap, expires, digest):
        with self.db() as db:
            db.execute('UPDATE users SET cap=?,expires=?,policy_hash=? WHERE user_id=? AND payer=?', (cap, expires, digest, str(user_id), payer))

    def credit(self, user_id, amount):
        with self.db() as db:
            db.execute('UPDATE users SET credit=?,credit_checked=? WHERE user_id=?', (amount, int(self.now()), str(user_id)))

    def _spent(self, db, user_id):
        day = int(self.now()) // 86400 * 86400
        return db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE user_id=? AND started>=? AND state IN ('approving','paying','uncertain','credit_pending','confirmed')", (str(user_id), day)).fetchone()[0]

    def spent(self, user_id):
        with self.db() as db:
            return self._spent(db, user_id)

    def save_quote(self, user_id, quote):
        amount = int(quote['amountAtomic'])
        with self.db() as db:
            db.execute("INSERT INTO payments(id,user_id,payer,quote,amount,state) VALUES (?,?,?,?,?,'quoted')", (quote['quoteId'], str(user_id), quote['payer'], json.dumps(quote), amount))

    def payment(self, user_id, quote_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM payments WHERE user_id=? AND id=?', (str(user_id), quote_id)).fetchone()
            return self._payment(row)

    def latest(self, user_id, *, active=False):
        with self.db() as db:
            condition = " AND state IN ('approving','paying','uncertain','credit_pending')" if active else ''
            row = db.execute('SELECT * FROM payments WHERE user_id=?' + condition + ' ORDER BY rowid DESC LIMIT 1', (str(user_id),)).fetchone()
            return self._payment(row)

    @staticmethod
    def _payment(row):
        return {**dict(row), 'quote': json.loads(row['quote'])} if row else None

    def claim(self, user_id, quote_id):
        """Reserve the whole top-up atomically before requesting phone approval."""
        with self.db() as db:
            row = db.execute('SELECT * FROM payments WHERE id=? AND user_id=?', (quote_id, str(user_id))).fetchone()
            user = db.execute('SELECT * FROM users WHERE user_id=?', (str(user_id),)).fetchone()
            if not row or row['state'] != 'quoted' or not user or user['expires'] <= self.now():
                return False
            if row['payer'] != user['payer'] or self._spent(db, user_id) + row['amount'] > user['cap']:
                return False
            if db.execute("SELECT 1 FROM payments WHERE user_id=? AND state IN ('approving','paying','uncertain','credit_pending')", (str(user_id),)).fetchone():
                return False
            db.execute("UPDATE payments SET state='approving',started=? WHERE id=?", (int(self.now()), quote_id))
            return True

    def begin_payment(self, user_id, quote_id):
        # An approval can straddle midnight. Charge the submission day's cap.
        with self.db() as db:
            row = db.execute('SELECT * FROM payments WHERE user_id=? AND id=?', (str(user_id), quote_id)).fetchone()
            user = db.execute('SELECT * FROM users WHERE user_id=?', (str(user_id),)).fetchone()
            day = int(self.now()) // 86400 * 86400
            if not row or row['state'] != 'approving' or not user or user['expires'] <= self.now():
                return False
            spent = self._spent(db, user_id) - (row['amount'] if row['started'] >= day else 0)
            if spent + row['amount'] > user['cap']:
                return False
            db.execute("UPDATE payments SET state='paying',started=? WHERE id=?", (int(self.now()), quote_id))
            return True

    def update(self, user_id, quote_id, state, transaction=None):
        if state not in (*ACTIVE, 'confirmed', 'failed', 'declined', 'cancelled'):
            raise ValueError('Invalid state')
        with self.db() as db:
            db.execute('UPDATE payments SET state=?,transaction_id=COALESCE(?,transaction_id) WHERE user_id=? AND id=?', (state, transaction, str(user_id), quote_id))
