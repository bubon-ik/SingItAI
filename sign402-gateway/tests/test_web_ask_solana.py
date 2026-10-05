"""Offline direct delegated Solana billing and durable budget holds."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sign402_gateway import ask_solana as m
from sign402_gateway.solana_allowance import SolanaAllowanceStore
from sign402_gateway.agent_allowance import AllowanceError, AllowanceUnavailable

ACCOUNT='solana:owner'
AGENT='agent-fixture'
NOW=1_800_000_000

def paid():
    return {'ok':True,'payer':AGENT,'owner':'owner','transaction':'solana-tx','amountAtomic':'1130',
        'body':{'choices':[{'message':{'content':'Hi'}}],'usage':{'buyer_cost_micro':100},'billing':{
            'version':1,'mode':'actual_usage','markupBps':3000,'maxChargeAtomic':'3000','settlementFeeAtomic':'1000',
            'providerCostAtomic':'100','markupAtomic':'30','totalAtomic':'1130','currency':'USDC'}}}

class SolanaAskTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        store=SolanaAllowanceStore(self.root/'allowance.db');store.set_limits(ACCOUNT,'owner',10000,3000,NOW+3600,NOW)
        self.lane=SimpleNamespace(store=store,_spend_lock=threading.Lock(),now=lambda:NOW,max_per_purchase=10000,max_daily=10000,
            _seen={},bridge=SimpleNamespace(rpc='https://unused.test'),owner=lambda _: 'owner',
            agent_key=Mock(return_value=(AGENT,'not-a-real-key')),_call=Mock(side_effect=AssertionError('No funding operation allowed')),
            chain_state=Mock(return_value={'owner':{'delegatedToAgent':'9000','amount':'10000'},'agent':{'usdcAtomic':'0','solLamports':'0'}}))
        self.server=SimpleNamespace(solana_allowance=self.lane)
        self.helper=Mock(side_effect=self.run_helper)
        self.confirmed=Mock(return_value=True)
        for p in [patch.object(m,'_journal_dir',return_value=self.root),patch.object(m,'_run',self.helper),
                  patch.object(m,'_confirmed',self.confirmed),
                  patch.dict(m.os.environ,{'SINGIT_ASK_QUOTE_TOKEN':'test-only-token-'*3})]:
            p.start();self.addCleanup(p.stop)
    def run_helper(self,lane,**p):
        if p.get('verify'):return {'ok':True,'verified':True}
        if p.get('reconcile'):
            self.reconciled=p['reconcile'];return self.chain_says
        Path(p['checkpoint']).write_text(json.dumps({'payer':AGENT,'owner':'owner','requestId':p['requestId'],
            'amountAtomic':'1130','memo':'singit-ask:'+p['requestId'],'feePayer':'facilitator','lastValidBlockHeight':'500'}))
        return paid()
    chain_says={'ok':False}
    def lose_answer(self):
        def lost(lane,**p):
            if p.get('reconcile'):return self.run_helper(lane,**p)
            self.run_helper(lane,**p);return {'ok':False,'submitted':True}
        self.helper.side_effect=lost
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
        self.helper.side_effect=self.run_helper
    def payments(self):return [c for c in self.helper.call_args_list if 'checkpoint' in c.kwargs]
    def pay(self):return m.pay(self.server,Mock(),ACCOUNT,{'messages':[]})
    def test_direct_owner_payment_works_without_agent_usdc_or_sol(self):
        amount,_,tx=self.pay();self.assertEqual((amount,tx),(1130,'solana-tx'))
        # The chain is checked here, in Python, not by a second Node process.
        self.assertFalse([c for c in self.helper.call_args_list if 'verify' in c.kwargs])
        self.assertEqual(self.confirmed.call_args.kwargs,{'signature':'solana-tx','owner':'owner','agent':AGENT,
            'amount':1130,'memo':self.confirmed.call_args.kwargs['memo'],'fee_payer':'facilitator'})
        self.lane._call.assert_not_called();self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),1130)
        self.assertFalse(list(self.root.glob('*.submitted')))
        self.assertEqual(self.helper.call_args_list[0].kwargs['owner'],'owner')
    def test_preparation_failure_releases_hold_without_payment(self):
        self.helper.side_effect=None;self.helper.return_value={'ok':False,'submitted':False}
        with self.assertRaisesRegex(AllowanceUnavailable,'No payment was submitted'):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),0);self.assertFalse(list(self.root.glob('*.json')))
    def test_uncertain_payment_survives_restart_and_counts_against_other_purchases(self):
        def lost(lane,**p):
            if p.get('reconcile'):return {'ok':False}  # an unreadable checkpoint proves nothing
            Path(p['checkpoint']).write_text('{}');return {'ok':False}
        self.helper.side_effect=lost
        with self.assertRaises(AllowanceError):self.pay()
        reopened=SolanaAllowanceStore(self.root/'allowance.db');self.assertEqual(reopened.spent_since(ACCOUNT,0),3000)
        with self.assertRaisesRegex(AllowanceError,'previous Solana'):self.pay()
        self.assertEqual(len(self.payments()),1)
        self.assertEqual(reopened.spent_since(ACCOUNT,0),3000)
    def test_wrong_invoice_keeps_hold(self):
        def wrong(lane,**p):
            output=self.run_helper(lane,**p)
            if 'checkpoint' in p:output['body']['billing']['totalAtomic']='1131'
            return output
        self.helper.side_effect=wrong
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
    def test_wrong_request_checkpoint_keeps_hold(self):
        def wrong(lane,**p):
            output=self.run_helper(lane,**p)
            if 'checkpoint' in p:
                f=Path(p['checkpoint']);cp=json.loads(f.read_text());cp['requestId']='other';f.write_text(json.dumps(cp))
            return output
        self.helper.side_effect=wrong
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
    def test_unverified_transfer_retains_receipt_for_review(self):
        self.confirmed.return_value=False
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
        state=json.loads(next(self.root.glob('*.json')).read_text());self.assertEqual(state['txId'],'solana-tx')
    def test_merchant_refusal_before_settlement_frees_the_account_at_once(self):
        def refused(lane,**p):
            self.run_helper(lane,**p);return {'ok':False,'submitted':True,'settled':False}
        self.helper.side_effect=refused
        with self.assertRaisesRegex(AllowanceUnavailable,'Nothing was charged'):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),0);self.assertFalse(list(self.root.glob('*.json')))
        self.helper.side_effect=self.run_helper
        self.assertEqual(self.pay()[0],1130)
    def test_lost_answer_whose_blockhash_died_unpaid_is_released_and_next_question_paid(self):
        self.lose_answer();self.chain_says={'ok':True,'state':'unpaid'}
        self.assertEqual(self.pay()[0],1130)
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),1130)
        self.assertEqual(self.reconciled['lastValidBlockHeight'],'500');self.assertEqual(self.reconciled['owner'],'owner')
        proof=json.loads(next(self.root.glob('reconciled/*/proof.json')).read_text());self.assertEqual(proof['outcome'],'unpaid')
    def test_lost_answer_found_paid_on_chain_counts_its_actual_charge(self):
        self.lose_answer();self.chain_says={'ok':True,'state':'paid','transaction':'found-tx'}
        m.recover(self.server,ACCOUNT)
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),1130)
        self.assertFalse(list(self.root.glob('*.json')));self.assertFalse(list(self.root.glob('*.submitted')))
    def test_lost_answer_still_confirming_keeps_the_block(self):
        self.lose_answer();self.chain_says={'ok':True,'state':'pending'}
        with self.assertRaisesRegex(AllowanceError,'still confirming'):m.recover(self.server,ACCOUNT)
        with self.assertRaisesRegex(AllowanceError,'still confirming'):self.pay()
        self.assertEqual(len(self.payments()),1);self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
    def test_crash_before_submission_releases_the_hold(self):
        journal=self.root/(m.hashlib.sha256(ACCOUNT.encode()).hexdigest()+'-solana.json')
        self.lane.store.reserve_metered('ask-crashed',ACCOUNT,3000,10000,NOW)
        journal.write_text(json.dumps({'account':ACCOUNT,'holdId':'ask-crashed','requestId':'r'}))
        self.assertEqual(self.pay()[0],1130);self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),1130)
    def test_revoked_grant_or_daily_limit_prevents_payment(self):
        self.lane.chain_state.return_value['owner']['delegatedToAgent']='0'
        with self.assertRaises(AllowanceError):self.pay()
        self.helper.assert_not_called()
        self.lane.store.add_spend(ACCOUNT,8000,'other','other-tx',NOW)
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),8000)
    def test_atomic_partial_settlement_and_invalid_amounts(self):
        store=self.lane.store
        store.reserve_metered('hold',ACCOUNT,3000,4000,NOW)
        for amount in [-1,3001,True]:
            with self.assertRaises(AllowanceError):store.settle_metered('hold',amount,'tx',NOW)
        self.assertEqual(store.spent_since(ACCOUNT,0),3000)
        store.settle_metered('hold',2130,'tx',NOW)
        self.assertEqual(store.spent_since(ACCOUNT,0),2130)
        with self.assertRaises(AllowanceError):store.reserve_metered('second',ACCOUNT,3000,4000,NOW)
        with self.assertRaises(AllowanceError):store.settle_metered('hold',2130,'tx',NOW)



OWNER, PAYER, FEE = 'Owner1111', 'Agent2222', 'Cdp3333'
OWNER_ATA, MERCHANT_ATA = 'OwnerUsdc', 'MerchantUsdc'


def chain_tx(amount=1130, memo='singit-ask:abc', err=None):
    keys = [{'pubkey': FEE, 'signer': True}, {'pubkey': PAYER, 'signer': True},
            {'pubkey': OWNER_ATA, 'signer': False}, {'pubkey': MERCHANT_ATA, 'signer': False}]
    def bal(i, owner, n): return {'accountIndex': i, 'mint': m.USDC, 'owner': owner,
                                  'uiTokenAmount': {'amount': str(n), 'decimals': 6}}
    return {'meta': {'err': err, 'preTokenBalances': [bal(2, OWNER, 5000), bal(3, m.PAY_TO, 0)],
                     'postTokenBalances': [bal(2, OWNER, 5000 - amount), bal(3, m.PAY_TO, amount)]},
            'transaction': {'signatures': ['sig'], 'message': {'accountKeys': keys, 'instructions': [
                {'program': 'spl-token', 'parsed': {'type': 'transferChecked', 'info': {
                    'source': OWNER_ATA, 'destination': MERCHANT_ATA, 'authority': PAYER, 'mint': m.USDC,
                    'tokenAmount': {'amount': '1130'}}}},
                {'program': 'spl-memo', 'parsed': memo}]}}}


class ChainCheckTests(unittest.TestCase):
    ARGS = dict(signature='sig', owner=OWNER, agent=PAYER, amount=1130, memo='singit-ask:abc', fee_payer=FEE)

    def test_the_payment_is_this_requests_and_nothing_else(self):
        self.assertTrue(m._verified(chain_tx(), **self.ARGS))
        for change in ({'amount': 1131}, {'memo': 'singit-ask:other'}, {'owner': PAYER}, {'agent': OWNER},
                       {'fee_payer': OWNER}, {'signature': 'other'}):
            self.assertFalse(m._verified(chain_tx(), **{**self.ARGS, **change}), change)
        self.assertFalse(m._verified(chain_tx(err={'InstructionError': [0, 'x']}), **self.ARGS))
        self.assertFalse(m._verified(chain_tx(amount=1000), **self.ARGS))  # moved less than the transfer says
        self.assertFalse(m._verified(None, **self.ARGS))

    def test_a_lagging_node_is_asked_again_and_a_missing_payment_is_not_confirmed(self):
        lane = SimpleNamespace(bridge=SimpleNamespace(rpc='https://rpc.test'))
        with patch.object(m, '_transaction', side_effect=[None, chain_tx()]), patch.object(m.time, 'sleep'):
            self.assertTrue(m._confirmed(lane, **self.ARGS))
        with patch.object(m, '_transaction', return_value=None), patch.object(m.time, 'sleep') as slept:
            self.assertFalse(m._confirmed(lane, **self.ARGS))
        self.assertEqual(slept.call_count, 5)


if __name__ == '__main__':
    unittest.main()
