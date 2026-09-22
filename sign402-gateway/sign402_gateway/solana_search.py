"""Opt-in Exa x402 search. Durable consent/holds; no stored queries or results."""
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from .solana_chat import SolanaChatError, USDC, usd
from .solana_wallets import SOLANA_NETWORK
from .web_search import classify, Verdict

ENDPOINT = 'https://api.exa.ai/search'
PER_CALL = 20_000
PER_DAY = 200_000
CALLS = 20
DAYS = 30


def needs_search(text):
    # Explicit research and freshness requests, including the bot's Russian UI users.
    return (text.strip().upper() in {'SOL', 'BTC', 'ETH'} or classify(text) == Verdict.SEARCH or bool(re.search(
        r'\b(search|look up|browse|latest|today|current|news)\b|'
        r'найди|поищи|погугли|свежи[ей]|актуальн|новост|сегодня|в интернете|\bкурс(?:а|у|ом|е|ы|ов)?\b|погод|сколько стоит', text, re.I)))


class SearchStore:
    def __init__(self, chat_store, now=time.time):
        self.db, self.now = chat_store.db, now
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS exa_proposals (user_id TEXT PRIMARY KEY, digest TEXT NOT NULL, terms TEXT NOT NULL, deadline INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS exa_policies (user_id TEXT PRIMARY KEY, digest TEXT NOT NULL, terms TEXT NOT NULL, enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS exa_payments (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payer TEXT NOT NULL, amount INTEGER NOT NULL, started INTEGER NOT NULL, state TEXT NOT NULL, transaction_id TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS exa_one_pending ON exa_payments(user_id) WHERE state IN ('paying','uncertain');
            ''')

    def propose(self, uid, terms):
        raw = json.dumps(terms, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO exa_proposals VALUES (?,?,?,?)', (str(uid), digest, raw, int(self.now()) + 600))
        return digest

    def proposal(self, uid, digest):
        with self.db() as db:
            row = db.execute('SELECT * FROM exa_proposals WHERE user_id=? AND digest=? AND deadline>?', (str(uid), digest, int(self.now()))).fetchone()
            return json.loads(row['terms']) if row else None

    def approve(self, uid, digest):
        with self.db() as db:
            row = db.execute('SELECT * FROM exa_proposals WHERE user_id=? AND digest=? AND deadline>?', (str(uid), digest, int(self.now()))).fetchone()
            if not row:
                raise SolanaChatError('EXA_REVIEW_REQUIRED', 'Search budget review expired. Review it again.')
            db.execute('INSERT OR REPLACE INTO exa_policies VALUES (?,?,?,1)', (str(uid), digest, row['terms']))
            db.execute('DELETE FROM exa_proposals WHERE user_id=?', (str(uid),))

    def disable(self, uid):
        with self.db() as db:
            db.execute('UPDATE exa_policies SET enabled=0 WHERE user_id=?', (str(uid),))
            db.execute('DELETE FROM exa_proposals WHERE user_id=?', (str(uid),))

    def _policy(self, db, uid):
        row = db.execute('SELECT * FROM exa_policies WHERE user_id=?', (str(uid),)).fetchone()
        return dict(json.loads(row['terms']), policyHash=row['digest'], enabled=bool(row['enabled'])) if row else None

    def policy(self, uid):
        with self.db() as db:
            return self._policy(db, uid)

    def _spent(self, db, uid):
        day = int(self.now()) // 86400 * 86400
        row = db.execute("SELECT COALESCE(SUM(amount),0),COUNT(*) FROM exa_payments WHERE user_id=? AND started>=? AND state IN ('paying','uncertain','confirmed')", (str(uid), day)).fetchone()
        return tuple(row)

    def spent(self, uid):
        with self.db() as db:
            return self._spent(db, uid)

    def latest(self, uid, quote_id='', *, pending=False):
        with self.db() as db:
            clause, args = (' AND id=?', (str(uid), quote_id)) if quote_id else ('', (str(uid),))
            if pending:
                clause += " AND state IN ('paying','uncertain')"
            row = db.execute('SELECT * FROM exa_payments WHERE user_id=?' + clause + ' ORDER BY rowid DESC LIMIT 1', args).fetchone()
            return dict(row) if row else None

    def reserve(self, uid, quote):
        with self.db() as db:
            policy = self._policy(db, uid)
            if not policy or not policy['enabled'] or policy['expiresAt'] <= self.now():
                raise SolanaChatError('EXA_CONSENT_REQUIRED', 'Enable web search and approve its separate budget first.')
            if db.execute("SELECT 1 FROM exa_payments WHERE user_id=? AND state IN ('paying','uncertain')", (str(uid),)).fetchone():
                raise SolanaChatError('EXA_PAYMENT_PENDING', 'Check the pending search payment before searching again.')
            amount = int(quote['amountAtomic'])
            if any(quote[k] != policy[k] for k in ('payer', 'recipient', 'network', 'asset', 'endpoint')) or amount <= 0 or amount > policy['maxPerCallAtomic'] or quote['expiresAt'] <= self.now() * 1000:
                raise SolanaChatError('EXA_TERMS_CHANGED', 'Search terms changed. Review a new search budget.')
            spent, count = self._spent(db, uid)
            if spent + amount > policy['dailyCapAtomic'] or count >= policy['maxCallsPerDay']:
                raise SolanaChatError('EXA_LIMIT_REACHED', 'Your daily web search limit is reached. It resets at 00:00 UTC.')
            db.execute("INSERT INTO exa_payments VALUES (?,?,?,?,?,'paying',NULL)", (quote['quoteId'], str(uid), quote['payer'], amount, int(self.now())))
            # A reservation cannot cross midnight into the next day's allowance.
            policy['expiresAt'] = min(policy['expiresAt'], (int(self.now()) // 86400 + 1) * 86400)
            return policy

    def update(self, uid, qid, state, transaction=None):
        if state not in ('uncertain', 'confirmed', 'failed', 'cancelled'):
            raise ValueError('Invalid search payment state')
        with self.db() as db:
            db.execute("UPDATE exa_payments SET state=?,transaction_id=COALESCE(?,transaction_id) WHERE user_id=? AND id=? AND state IN ('paying','uncertain')", (state, transaction, str(uid), qid))


class SolanaSearch:
    def __init__(self, chat, *, enabled=True):
        self.chat, self.enabled, self.now = chat, enabled, chat.now
        self.store = SearchStore(chat.store, chat.now)

    def _available(self):
        if not self.enabled:
            raise SolanaChatError('EXA_DISABLED', 'Solana web search is temporarily unavailable.')
        self.chat._paid_allowed()

    def status(self, uid):
        policy = self.store.policy(uid)
        spent, count = self.store.spent(uid)
        active = bool(self.enabled and policy and policy['enabled'] and policy['expiresAt'] > self.now())
        pending = self.store.latest(uid, pending=True)
        return {'ok': True, 'chain': 'solana', 'enabled': active, 'available': self.enabled,
                'remainingAtomic': max(0, PER_DAY - spent), 'callsRemaining': max(0, CALLS - count),
                'pending': bool(pending), 'telegramText': (
                    f'Web search: {"on" if active else "off"}\nExa · x402 · USDC on Solana\n'
                    f'Limit: {usd(PER_CALL)} USDC per search, {usd(PER_DAY)} USDC and {CALLS} searches / UTC day.\n'
                    f'Remaining today: {usd(max(0, PER_DAY - spent))} USDC · {max(0, CALLS - count)} searches.\n'
                    'Search is charged separately from Venice credit. Freshness and explicit search requests can search automatically within your approved budget.\n'
                    + ('A search payment is unresolved. Check its status; do not pay again.' if pending else ''))}

    def prepare(self, uid):
        self._available()
        user = self.chat._user(uid)
        terms = self.chat._call(uid, 'exa-terms')
        if terms.get('payer') != user['payer'] or terms.get('network') != SOLANA_NETWORK or terms.get('asset') != USDC or terms.get('endpoint') != ENDPOINT or not terms.get('recipient') or not 0 < int(terms['amountAtomic']) <= PER_CALL:
            raise SolanaChatError('EXA_INVALID_TERMS', 'Could not validate Exa payment terms.')
        price = int(terms.pop('amountAtomic'))
        terms.update(purpose='exa_solana_search', maxPerCallAtomic=PER_CALL, dailyCapAtomic=PER_DAY,
                     maxCallsPerDay=CALLS, expiresAt=int(self.now()) + DAYS * 86400)
        digest = self.store.propose(uid, terms)
        return {'ok': True, 'chain': 'solana', 'approvalHash': digest, 'telegramText': (
            f'Review web search budget\n\nExa · x402 · Solana mainnet\nCurrent price: {usd(price)} USDC / search (3 results).\n'
            f'Approve up to {usd(PER_CALL)} USDC per search, {usd(PER_DAY)} USDC and {CALLS} searches / UTC day for {DAYS} days.\n'
            f'From your wallet: {terms["payer"]}\nTo Exa: {terms["recipient"]}\nUSDC mint: {USDC}\nEndpoint: {ENDPOINT}\nNetwork fee: sponsored.\n\n'
            'Searches run automatically within this budget when your question needs the web. Charges are separate from Venice credit. '
            'Confirm on your linked phone. This approval moves no money. Turn it off in AI settings at any time.')}

    def approve(self, uid, digest):
        self._available()
        terms = self.store.proposal(uid, digest)
        if not terms or terms['payer'] != self.chat._user(uid)['payer']:
            raise SolanaChatError('EXA_REVIEW_REQUIRED', 'Review the search budget before requesting approval.')
        result = self.chat.approvals.request_hash_approval(telegram_user_id=str(uid), action_type='sign402_exa_search_policy',
            wallet_chain='solana', commitment_hash=digest, context_lines=[
                'Exa web search — automatic x402 payments on Solana',
                f'Up to {usd(PER_CALL)} USDC per search; {usd(PER_DAY)} USDC and {CALLS} searches / UTC day for {DAYS} days.',
                f'From: {terms["payer"]}', f'To: {terms["recipient"]}', f'USDC mint: {USDC}', f'Endpoint: {ENDPOINT}',
                'Network fee: sponsored. Separate from Venice credit.',
                'No per-search approval within these limits.',
                'Turn off in AI settings. This approval moves no money.'])
        if not result.get('ok') or not result.get('approved'):
            return {'ok': False, 'chain': 'solana', 'telegramText': result.get('telegramText') or 'Search budget was not approved.'}
        if result.get('approvedHash') != digest:
            raise SolanaChatError('EXA_APPROVAL_MISMATCH', 'Approval did not match this search budget. Nothing changed.')
        self._available()
        self.store.approve(uid, digest)
        return {'ok': True, 'chain': 'solana', 'telegramText': 'Web search enabled. Send your question again. Exa uses your separately approved Solana search budget.'}

    def handle(self, operation, uid, payload):
        if operation == 'search':
            return self.status(uid)
        if operation == 'search-disable':
            self.store.disable(uid)
            return {'ok': True, 'chain': 'solana', 'telegramText': 'Web search is off. Your existing receipts remain available.'}
        if operation == 'search-prepare':
            return self.prepare(uid)
        if operation == 'search-approve':
            return self.approve(uid, str(payload.get('approvalHash') or ''))
        if operation == 'search-payment':
            return self.reconcile(uid, str(payload.get('quoteId') or ''), str(payload.get('transaction') or ''))
        raise SolanaChatError('EXA_INVALID_OPERATION', 'Unsupported search operation.')

    def search(self, uid, query):
        self._available()
        policy = self.store.policy(uid)
        if not policy or not policy['enabled'] or policy['expiresAt'] <= self.now():
            raise SolanaChatError('EXA_CONSENT_REQUIRED', 'This question needs web search. Review and approve its separate budget, then send your question again.')
        if self.store.latest(uid, pending=True):
            raise SolanaChatError('EXA_PAYMENT_PENDING', 'Check the pending search payment before searching again.')
        # Confirm usable AI credit before buying results that the model cannot use.
        balance = self.chat._call(uid, 'balance')
        if not balance.get('canConsume'):
            raise SolanaChatError('TOP_UP_REQUIRED', 'Top up Venice before paying for a web search.')
        quote = self.chat._call(uid, 'exa-quote', query=query)
        authorization = self.store.reserve(uid, quote)
        try:
            self._available()
            result = self.chat._call(uid, 'exa-search', quoteId=quote['quoteId'], approvalHash=quote['approvalHash'], query=query, authorization=authorization)
        except Exception:
            # Synchronous bridge has exited (or was killed on timeout). A missing
            # attempt here is proof that this worker never submitted a payment.
            try:
                status = self.chat._call(uid, 'exa-status', quoteId=quote['quoteId'])
                state = status['state'] if status.get('attempted') and status['state'] in ('confirmed', 'failed') else 'uncertain' if status.get('attempted') else 'cancelled'
                self.store.update(uid, quote['quoteId'], state, status.get('transaction'))
            except Exception:
                self.store.update(uid, quote['quoteId'], 'uncertain')
            raise SolanaChatError('EXA_SEARCH_INTERRUPTED', 'Web search did not complete. Check search payment status before trying again.') from None
        self.store.update(uid, quote['quoteId'], result['state'], result.get('transaction'))
        if result['state'] != 'confirmed' or not result.get('delivered'):
            raise SolanaChatError('EXA_RESULTS_UNAVAILABLE', 'Search results are unavailable. Check search payment status. A confirmed payment is still charged; no automatic retry was made.')
        # Bound/sanitize again at the Python boundary; excerpts never authorize money.
        sources = []
        for hit in result.get('results', [])[:3]:
            url = str(hit.get('url') or '')
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or len(url) > 2000 or any(ord(c) < 32 for c in url):
                    continue
            except ValueError:
                continue
            sources.append({'url': url, 'title': str(hit.get('title') or '')[:200], 'text': str(hit.get('text') or '')[:1200]})
        return {'sources': sources, 'webCostAtomic': int(quote['amountAtomic']), 'webTransaction': result.get('transaction'),
                'webQuoteId': quote['quoteId'], 'webFooter': f'Exa web search: {usd(int(quote["amountAtomic"]))} USDC · Solana · x402'}

    def reconcile(self, uid, quote_id='', transaction=''):
        row = self.store.latest(uid, quote_id)
        if not row:
            return {'ok': True, 'chain': 'solana', 'telegramText': 'No search payment found.'}
        status = self.chat._call(uid, 'exa-status', quoteId=row['id'])
        if status.get('attempted'):
            result = self.chat._call(uid, 'exa-reconcile', quoteId=row['id'], transaction=transaction or None)
            self.store.update(uid, row['id'], result['state'], result.get('transaction'))
            row = self.store.latest(uid, row['id'])
        # Missing Node attempt after a restart is not proof a previous child is
        # dead. Keep the reservation until an operator can establish that fact.
        text = f'Exa x402 · USDC on Solana\nSearch: {row["id"]}\nPayment: {row["state"]}\nAmount: {usd(row["amount"])} USDC'
        if row['transaction_id']:
            text += f'\nTransaction: {row["transaction_id"]}\nhttps://solscan.io/tx/{row["transaction_id"]}'
        text += ('\nResults are not stored or re-purchased by this status check.' if row['state'] == 'confirmed' else '\nNo payment was completed.' if row['state'] in ('cancelled', 'failed') else '\nUnresolved: do not pay again. If no receipt is available, contact support with the search ID.')
        return {'ok': True, 'chain': 'solana', 'paymentState': row['state'], 'telegramText': text}
