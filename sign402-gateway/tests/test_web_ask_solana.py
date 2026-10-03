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
        for p in [patch.object(m,'_journal_dir',return_value=self.root),patch.object(m,'_run',self.helper),
                  patch.dict(m.os.environ,{'SINGIT_ASK_QUOTE_TOKEN':'test-only-token-'*3})]:
            p.start();self.addCleanup(p.stop)
    def run_helper(self,lane,**p):
        if p.get('verify'):return {'ok':True,'verified':True}
        Path(p['checkpoint']).write_text(json.dumps({'payer':AGENT,'owner':'owner','requestId':p['requestId'],
            'amountAtomic':'1130','memo':'singit-ask:'+p['requestId'],'feePayer':'facilitator'}))
        return paid()
    def pay(self):return m.pay(self.server,Mock(),ACCOUNT,{'messages':[]})
    def test_direct_owner_payment_works_without_agent_usdc_or_sol(self):
        amount,_,tx=self.pay();self.assertEqual((amount,tx),(1130,'solana-tx'))
        self.lane._call.assert_not_called();self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),1130)
        self.assertFalse(list(self.root.glob('*.submitted')))
        self.assertEqual(self.helper.call_args_list[0].kwargs['owner'],'owner')
    def test_preparation_failure_releases_hold_without_payment(self):
        self.helper.side_effect=None;self.helper.return_value={'ok':False,'submitted':False}
        with self.assertRaisesRegex(AllowanceUnavailable,'No payment was submitted'):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),0);self.assertFalse(list(self.root.glob('*.json')))
    def test_uncertain_payment_survives_restart_and_counts_against_other_purchases(self):
        def lost(lane,**p):
            Path(p['checkpoint']).write_text('{}');return {'ok':False}
        self.helper.side_effect=lost
        with self.assertRaises(AllowanceError):self.pay()
        reopened=SolanaAllowanceStore(self.root/'allowance.db');self.assertEqual(reopened.spent_since(ACCOUNT,0),3000)
        calls=self.helper.call_count
        with self.assertRaisesRegex(AllowanceError,'previous Solana'):self.pay()
        self.assertEqual(self.helper.call_count,calls)
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
        def wrong(lane,**p):
            return {'ok':False} if p.get('verify') else self.run_helper(lane,**p)
        self.helper.side_effect=wrong
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
        state=json.loads(next(self.root.glob('*.json')).read_text());self.assertEqual(state['txId'],'solana-tx')
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

if __name__=='__main__':unittest.main()
