"""Venice chat from each user's managed Solana wallet.

Budgets authorize a ceiling, never an unattended top-up. Every payment also
requires a fresh, exact quote approved on the user's linked phone channel.
The Node journal is the submission authority; this journal reserves the budget.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from .solana_chat_store import SolanaChatStore
from .solana_wallets import SOLANA_NETWORK
from .venice_chat import ChatService, DEFAULT_MODEL

USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
ROOT = Path(__file__).resolve().parents[2] / 'solana-x402-service'


class SolanaChatError(Exception):
    def __init__(self, code, text):
        self.code, self.text = code, text
        super().__init__(text)


def atomic(value):
    try:
        amount = Decimal(str(value)) * 1000000
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return int(amount)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise SolanaChatError('INVALID_BALANCE', 'Venice balance is unavailable. Try again later.') from None


def usd(value):
    return str(Decimal(value) / 1000000) if value is not None else 'unavailable'


def expiry(quote):
    return datetime.fromisoformat(quote['expiresAt'].replace('Z', '+00:00')).timestamp()


class SolanaBridge:
    def __init__(self, wallets, directory, *, node=None, rpc=None):
        self.wallets, self.directory = wallets, Path(directory)
        runtime = Path.home() / '.local/share/singit-node24/bin/node'
        self.node = node or os.environ.get('SIGN402_SOLANA_NODE_BINARY') or (str(runtime) if runtime.is_file() else shutil.which('node')) or 'node'
        self.rpc = rpc or os.environ.get('SOLANA_RPC_URL') or 'https://api.mainnet-beta.solana.com'

    def __call__(self, user_id, payer, operation, **payload):
        # Isolate each authenticated identity and wallet, even if a caller
        # supplies someone else's quote id. Secrets travel only on stdin.
        directory = self.directory / hashlib.sha256(f'{user_id}:{payer}'.encode()).hexdigest()
        key = self.wallets.decrypt_private_key_for_future_signing(str(user_id), chain='solana')
        request = dict(payload, operation=operation, payer=payer, privateKey=key)
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        env.update(SINGIT_STATE_DIR=str(directory), SOLANA_RPC_URL=self.rpc, NODE_NO_WARNINGS='1')
        try:
            process = subprocess.run([self.node, str(ROOT / 'src/gateway.mjs')],
                input=json.dumps(request), capture_output=True, text=True, env=env,
                timeout=240, check=False)
            response = json.loads(process.stdout)
        except Exception:
            raise SolanaChatError('BRIDGE_UNAVAILABLE', 'Could not complete the Exa request. Check search payment status before trying again.' if operation.startswith('exa-') else 'Could not complete the Venice request. Check payment status before trying another top-up.') from None
        finally:
            request.pop('privateKey', None)
            key = None
        if not isinstance(response, dict) or not response.get('ok'):
            code = response.get('code', 'BRIDGE_FAILED') if isinstance(response, dict) else 'BRIDGE_FAILED'
            messages = {
                'TOP_UP_REQUIRED': 'Venice credit is insufficient. Review and approve a Solana top-up.',
                'INSUFFICIENT_USDC': 'Insufficient USDC in your Solana wallet. Open Wallet → Solana to add funds.',
                'QUOTE_EXPIRED': 'This quote expired. Request and review a fresh quote.',
                'TERMS_CHANGED': 'Venice changed the terms. Request and approve a fresh quote.',
                'PAYMENT_UNCERTAIN': 'The payment result is uncertain. Check payment status; do not pay again.',
                'TRANSACTION_REQUIRED': 'No transaction receipt is available yet. Contact support with the quote ID; do not pay again.',
            }
            if operation.startswith('exa-'):
                messages.update(EXA_RATE_LIMIT='Exa is rate limited. Try again later; no search was submitted.',
                    EXA_PAYMENT_UNCERTAIN='Search payment is uncertain. Check search payment status; do not pay again.',
                    EXA_PAYMENT_PENDING='Check your pending search payment before searching again.',
                    TRANSACTION_REQUIRED='No search receipt is available yet. Contact support with the search ID; do not pay again.')
            fallback = 'Exa could not complete this request. Check search payment status; no automatic retry was made.' if operation.startswith('exa-') else 'Venice could not complete this request. No automatic retry was made.'
            raise SolanaChatError(str(code), messages.get(code, fallback))
        return response['result']


class SolanaChatService:
    def __init__(self, *, store, wallets, bridge, approvals, catalogue=None,
                 default_model=DEFAULT_MODEL, now=time.time, purchases_paused=lambda: False):
        self.store, self.wallets, self.bridge, self.approvals = store, wallets, bridge, approvals
        self.now, self.purchases_paused = now, purchases_paused
        self.default_model = default_model
        self.enabled = True
        self.search = None
        self.catalogue = ChatService(store=store, client=None, wallet_service=wallets,
            daily_cap_atomic=5_000_000, default_model=default_model, catalogue=catalogue)
        self._locks = [threading.Lock() for _ in range(256)]

    @contextmanager
    def guard(self, user_id):
        lock = self._locks[int(hashlib.sha256(str(user_id).encode()).hexdigest(), 16) % len(self._locks)]
        if not lock.acquire(blocking=False):
            raise SolanaChatError('BUSY', 'Your previous Solana AI request is still running. Wait for its result.')
        try:
            yield
        finally:
            lock.release()

    def _user(self, user_id):
        status = self.wallets.wallet_status(str(user_id), chain='solana')
        payer = (status.get('wallet') or {}).get('address') if status.get('ok') else None
        if not payer:
            raise SolanaChatError('WALLET_REQUIRED', 'Create your personal Solana wallet first: /wallet solana')
        return self.store.user(user_id, payer)

    def _call(self, user_id, operation, **payload):
        return self.bridge(str(user_id), self._user(user_id)['payer'], operation, **payload)

    def _budget(self, user_id):
        user = self._user(user_id)
        if not user['policy_hash'] or user['expires'] <= self.now():
            raise SolanaChatError('POLICY_MISSING', 'Approve or renew your Solana AI budget first: /chat_budget')
        return user

    def _paid_allowed(self):
        if self.purchases_paused():
            raise SolanaChatError('PURCHASES_PAUSED', 'Payments and paid chat are temporarily paused.')

    def handle(self, path, user_id, payload):
        try:
            with self.guard(user_id):
                operation = path.rsplit('/', 1)[-1]
                if operation == 'start':
                    return self.start(user_id)
                if operation == 'end':
                    return {'ok': True}
                self._user(user_id)
                if operation == 'models':
                    if payload.get('model'):
                        return self.catalogue.set_model(user_id, str(payload['model']))
                    return self.catalogue.models(user_id, category=str(payload.get('category') or ''), query=str(payload.get('query') or ''), page=int(payload.get('page') or 0))
                if operation == 'payment':
                    return self.reconcile(user_id, str(payload.get('quoteId') or ''), str(payload.get('transaction') or ''))
                if operation.startswith('search'):
                    if self.search is None:
                        raise SolanaChatError('EXA_DISABLED', 'Solana web search is unavailable.')
                    return self.search.handle(operation, user_id, payload)
                self._paid_allowed()
                if operation == 'approve-policy':
                    return self.approve_policy(user_id, payload)
                if operation == 'quote':
                    return self.quote(user_id)
                if operation == 'pay':
                    return self.pay(user_id, payload)
                if operation == 'message':
                    return self.send(user_id, str(payload.get('text') or ''))
                raise SolanaChatError('UNSUPPORTED_OPERATION', 'Unsupported Solana AI operation.')
        except SolanaChatError as exc:
            return {'ok': False, 'chain': 'solana', 'state': exc.code, 'telegramText': exc.text}

    def start(self, user_id):
        try:
            user = self._user(user_id)
        except SolanaChatError as exc:
            return {'ok': True, 'chain': 'solana', 'availableChains': ['base', 'solana'], 'walletRequired': True,
                    'hasPolicy': False, 'telegramText': exc.text, 'model': self.default_model}
        balance_known = False
        try:
            balance = self._call(user_id, 'balance')
            self.store.credit(user_id, atomic(balance['balanceUsd']))
            balance_known = True
            user = self.store.user(user_id)
        except SolanaChatError:
            pass
        model = user['model'] or self.default_model
        try:
            chosen = self.catalogue._catalogue().resolve(model)
        except Exception:
            chosen = None
        cap = user['cap'] or 5_000_000
        remaining = max(0, cap - self.store.spent(user_id))
        pending = self.store.latest(user_id, active=True)
        return {'ok': True, 'chain': 'solana', 'availableChains': ['base', 'solana'],
                'wallet': user['payer'], 'hasPolicy': bool(user['policy_hash']),
                'model': model, 'modelLabel': chosen.label if chosen else model,
                'inputUsdPerMTok': chosen.input_usd_per_mtok if chosen else None,
                'outputUsdPerMTok': chosen.output_usd_per_mtok if chosen else None,
                'dailyCapAtomic': cap, 'dailyCapUsdc': usd(cap), 'remainingWindowAtomic': remaining,
                'remainingWindowUsdc': usd(remaining), 'outstandingAtomic': user['credit'],
                'outstandingUsdc': usd(user['credit']), 'creditFresh': balance_known,
                'policyExpiresAt': user['expires'], 'policyExpired': bool(user['expires'] and user['expires'] <= self.now()),
                'paused': bool(pending), 'pauseReason': 'Check your pending Solana payment.' if pending else '',
                'pendingQuoteId': pending['id'] if pending else None,
                'webSearch': self.search.status(user_id) if self.search else None}

    def approve_policy(self, user_id, payload):
        cap, days = int(payload.get('dailyCapAtomic') or 0), int(payload.get('days') or 0)
        if cap not in (5_000_000, 10_000_000, 20_000_000) or not 1 <= days <= 30:
            raise SolanaChatError('INVALID_BUDGET', 'Choose a $5, $10 or $20 daily top-up budget, valid for up to 30 days.')
        user = self._user(user_id)
        expires = int(self.now()) + days * 86400
        terms = dict(purpose='venice_solana_budget', payer=user['payer'], network=SOLANA_NETWORK,
                     asset=USDC, dailyCapAtomic=cap, expiresAt=expires)
        digest = hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        result = self.approvals.request_hash_approval(telegram_user_id=str(user_id),
            action_type='sign402_solana_chat_policy', wallet_chain='solana', commitment_hash=digest,
            context_lines=['Venice AI — Solana budget', f'Up to {usd(cap)} USDC / UTC day for {days} days',
                f'Wallet: {user["payer"]}', 'Every top-up also requires exact quote approval. This approval moves no money.'])
        if not result.get('ok') or not result.get('approved'):
            return {'ok': False, 'approved': False, 'telegramText': result.get('telegramText') or 'Budget was not approved.'}
        if result.get('approvedHash') != digest:
            raise SolanaChatError('APPROVAL_MISMATCH', 'The approval did not match this Solana budget. Nothing changed.')
        self.store.policy(user_id, user['payer'], cap, expires, digest)
        return {'ok': True, 'approved': True, 'chain': 'solana', 'telegramText': 'Solana AI budget approved. Each top-up requires its own approval.'}

    def quote(self, user_id):
        self._budget(user_id)
        if self.store.latest(user_id, active=True):
            raise SolanaChatError('PAYMENT_PENDING', 'Check the existing payment before requesting another top-up.')
        balance = self._call(user_id, 'balance')
        self.store.credit(user_id, atomic(balance['balanceUsd']))
        if balance['canConsume']:
            return {'ok': True, 'telegramText': 'You already have usable Venice credit on Solana. Send your question.'}
        previous = self.store.latest(user_id)
        if previous and previous['state'] == 'quoted' and expiry(previous['quote']) > self.now():
            return {'ok': True, 'chain': 'solana', 'quote': previous['quote']}
        quote = self._call(user_id, 'quote')
        quote['amountAtomic'] = atomic(quote['amountUsdc'])
        if quote['network'] != SOLANA_NETWORK or quote['asset'] != USDC or quote['payer'] != self._user(user_id)['payer'] or quote['amountAtomic'] <= 0 or quote['feePayer'] == quote['payer']:
            raise SolanaChatError('INVALID_QUOTE', 'The quote does not match your Solana wallet and USDC network.')
        if quote['amountAtomic'] + self.store.spent(user_id) > self._budget(user_id)['cap']:
            raise SolanaChatError('WINDOW_EXHAUSTED', 'This top-up exceeds your remaining daily Solana AI budget.')
        self.store.save_quote(user_id, quote)
        return {'ok': True, 'chain': 'solana', 'quote': quote}

    def pay(self, user_id, payload):
        quote_id = str(payload.get('quoteId') or '')
        payment = self.store.payment(user_id, quote_id)
        if not payment or payment['quote']['approvalHash'] != payload.get('approvalHash'):
            raise SolanaChatError('APPROVAL_REQUIRED', 'Review the exact quote before requesting approval.')
        quote = payment['quote']
        if expiry(quote) <= self.now():
            raise SolanaChatError('QUOTE_EXPIRED', 'This quote expired. Request and review a fresh quote.')
        self._budget(user_id)
        if not self.store.claim(user_id, quote_id):
            raise SolanaChatError('PAYMENT_PENDING', 'This payment cannot be started. Check payment status and your remaining budget.')
        try:
            result = self.approvals.request_hash_approval(telegram_user_id=str(user_id),
                action_type='sign402_venice_solana_topup', wallet_chain='solana', commitment_hash=quote['approvalHash'],
                context_lines=['One Venice AI top-up — x402 / Solana mainnet',
                    f'Total: {quote["amountUsdc"]} USDC; network fee sponsored',
                    f'From: {quote["payer"]}', f'To: {quote["recipient"]}',
                    f'USDC mint: {quote["asset"]}', f'Expires: {quote["expiresAt"]}'])
        except Exception:
            self.store.update(user_id, quote_id, 'cancelled')
            raise SolanaChatError('APPROVAL_UNAVAILABLE', 'Approval could not complete. No payment was submitted.') from None
        if not result.get('ok') or not result.get('approved'):
            self.store.update(user_id, quote_id, 'declined')
            return {'ok': False, 'telegramText': result.get('telegramText') or 'Top-up declined. Nothing was paid.'}
        if result.get('approvedHash') != quote['approvalHash']:
            self.store.update(user_id, quote_id, 'cancelled')
            raise SolanaChatError('APPROVAL_MISMATCH', 'The approval did not match this exact quote. No payment was submitted.')
        # Approval can take minutes. Recheck expiry, policy and pause before signing.
        try:
            self._paid_allowed()
            self._budget(user_id)
            if expiry(quote) <= self.now():
                raise SolanaChatError('QUOTE_EXPIRED', 'The quote expired during approval. Review a fresh quote.')
        except SolanaChatError:
            self.store.update(user_id, quote_id, 'cancelled')
            raise
        if not self.store.begin_payment(user_id, quote_id):
            self.store.update(user_id, quote_id, 'cancelled')
            raise SolanaChatError('WINDOW_EXHAUSTED', 'Your budget changed during approval. Review the budget before another top-up.')
        try:
            result = self._call(user_id, 'pay', quoteId=quote_id, approvalHash=quote['approvalHash'])
        except SolanaChatError as original:
            # The synchronous child has ended (or was killed and waited for).
            # Only a readable Node journal proving no submission frees the hold.
            try:
                status = self._call(user_id, 'status', quoteId=quote_id)
                self.store.update(user_id, quote_id, 'uncertain' if status['attempted'] else 'cancelled', status.get('transaction'))
            except SolanaChatError:
                self.store.update(user_id, quote_id, 'uncertain')
            raise original
        return self._receipt(user_id, quote_id, result)

    def reconcile(self, user_id, quote_id='', transaction=''):
        payment = self.store.payment(user_id, quote_id) if quote_id else self.store.latest(user_id)
        if not payment:
            return {'ok': True, 'telegramText': 'No Solana Venice top-up has been requested.'}
        quote_id = payment['id']
        status = self._call(user_id, 'status', quoteId=quote_id)
        if not status['attempted']:
            # An interrupted approval can never resume itself. A paying worker
            # may still have a child alive after a gateway restart: keep its hold.
            if payment['state'] == 'approving':
                self.store.update(user_id, quote_id, 'cancelled')
            pending = payment['state'] in ('paying', 'uncertain', 'credit_pending')
            return {'ok': True, 'telegramText': f'Quote: {quote_id}\n' + ('Payment is unresolved. Contact support; do not pay again.' if pending else 'No payment was submitted. Request a fresh quote when ready.')}
        result = self._call(user_id, 'reconcile', quoteId=quote_id, transaction=transaction or None)
        return self._receipt(user_id, quote_id, result)

    def _receipt(self, user_id, quote_id, result):
        state = result['state']
        balance = result.get('veniceBalance')
        ready = bool(state == 'confirmed' and balance and balance.get('canConsume'))
        previously_credited = self.store.payment(user_id, quote_id)['state'] == 'confirmed'
        if balance:
            self.store.credit(user_id, atomic(balance['balanceUsd']))
        self.store.update(user_id, quote_id, 'confirmed' if ready or (state == 'confirmed' and previously_credited) else 'credit_pending' if state == 'confirmed' else state, result.get('transaction'))
        text = f'Venice x402 · USDC on Solana\nQuote: {quote_id}\nOn-chain: {state}'
        if result.get('transaction'):
            text += f'\nTransaction: {result["transaction"]}\nhttps://solscan.io/tx/{result["transaction"]}'
        text += '\nVenice credit: ' + (f'${balance["balanceUsd"]}' if balance else 'unavailable')
        text += '\nThis top-up was already credited. Send a question or review a new top-up if the credit has been used.' if previously_credited else '\nCredit is ready. Send your question.' if ready else '\nCheck payment status again. Do not submit another top-up.' if state != 'failed' else '\nPayment failed on-chain; no top-up was recorded.'
        return {'ok': True, 'chain': 'solana', 'paymentState': state, 'creditReady': ready, 'transaction': result.get('transaction'), 'telegramText': text}

    def _complete(self, user_id, model, text, **options):
        # Each completion can spend Venice credit. Recheck before the second
        # one as well; record the decision's balance even if search is refused.
        self._paid_allowed()
        self._budget(user_id)
        result = self._call(user_id, 'chat', model=model, message=text, **options)
        credit = None
        try:
            if result.get('balanceRemaining') is not None:
                credit = atomic(result['balanceRemaining'])
        except SolanaChatError:
            pass
        self.store.credit(user_id, credit)
        return dict(result, credit=credit)

    @staticmethod
    def _answer(result, **web):
        return {'ok': True, 'chain': 'solana', 'text': result['text'], 'costAtomic': None,
                'outstandingAtomic': result.get('credit'), 'model': result.get('model'),
                'prefunded': False, **web}

    def send(self, user_id, text):
        if not text.strip() or len(text) > 12000:
            raise SolanaChatError('INVALID_MESSAGE', 'Send a question of up to 12,000 characters.')
        user = self._budget(user_id)
        if self.store.latest(user_id, active=True):
            raise SolanaChatError('PAYMENT_PENDING', 'Check your pending top-up before using Solana AI chat.')
        model = user['model'] or self.default_model
        first = self._complete(user_id, model, text, **({'offerSearch': True} if self.search else {}))
        if not self.search:
            return self._answer(first)
        from .solana_search import requested_search
        try:
            query = requested_search(first['text'])
            if query is None:
                return self._answer(first)
            self._budget(user_id)
            web = self.search.search(user_id, query)
        except SolanaChatError as error:
            # A model decision is a paid completion even if search never starts.
            credit = first['credit']
            note = f'Venice credit left: ${usd(credit)}.' if credit is not None else 'Venice credit balance is unavailable.'
            return {'ok': False, 'chain': 'solana', 'state': error.code,
                    'outstandingAtomic': credit,
                    'telegramText': error.text + '\n\nThe model’s search decision uses Venice credit. ' + note}
        if not web['sources']:
            return self._answer(dict(first, text='Exa returned no usable sources for this question. The search was charged; no further Venice answer was requested.'), **web)
        try:
            final = self._complete(user_id, model, text, sources=web['sources'])
        except SolanaChatError:
            # Preserve the paid results/receipt if completion or policy fails.
            return self._answer({'text': 'Exa search completed, but Venice could not answer. The search was charged. No automatic retry was made.'}, **web)
        try:
            another_search = requested_search(final['text']) is not None
        except SolanaChatError:
            another_search = True
        if another_search:
            final['text'] = 'The retrieved sources were insufficient for the model to answer. See the sources below. No second search was purchased.'
        return self._answer(final, **web)


def build_solana_chat(*, wallets, approvals, base_chat, purchases_paused):
    directory = Path(os.environ.get('SIGN402_SOLANA_CHAT_STATE_DIR') or Path.home() / '.sign402' / 'solana-chat')
    store = SolanaChatStore(directory / 'chat.sqlite3')
    service = SolanaChatService(store=store, wallets=wallets, approvals=approvals,
        bridge=SolanaBridge(wallets, directory / 'operations'),
        catalogue=base_chat.catalogue if base_chat else None,
        default_model=base_chat.default_model if base_chat else DEFAULT_MODEL,
        purchases_paused=purchases_paused)

    from .solana_search import SolanaSearch
    service.search = SolanaSearch(service, enabled=(
        os.environ.get('SIGN402_AI_SEARCH_ENABLED', '0').strip().lower() in {'1', 'true', 'on'} and
        os.environ.get('SIGN402_SOLANA_SEARCH_ENABLED', '1').strip().lower() not in {'0', 'false', 'off'}))
    service.enabled = os.environ.get("SIGN402_SOLANA_CHAT_ENABLED", "1").strip().lower() not in {"0", "false", "off"}
    return service
