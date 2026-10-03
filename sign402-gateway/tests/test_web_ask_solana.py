"""Offline Solana token billing, funding, durable budget holds and refunds."""
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
    return {'ok':True,'payer':AGENT,'transaction':'solana-tx','channelId':'channel-fixture','amountAtomic':'2130','refundAtomic':'870',
        'body':{'choices':[{'message':{'content':'Hi'}}],'usage':{'buyer_cost_micro':100},'billing':{
            'version':1,'mode':'actual_usage','markupBps':3000,'maxChargeAtomic':'3000','settlementFeeAtomic':'2000',
            'providerCostAtomic':'100','markupAtomic':'30','totalAtomic':'2130','currency':'USDC'}}}

class SolanaAskTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        store=SolanaAllowanceStore(self.root/'allowance.db');store.set_limits(ACCOUNT,'owner',10000,3000,NOW+3600,NOW)
        self.lane=SimpleNamespace(store=store,_spend_lock=threading.Lock(),now=lambda:NOW,max_per_purchase=10000,max_daily=10000,
            _seen={},bridge=SimpleNamespace(rpc='https://unused.test'),fee_payer_key=lambda:'not-a-real-key',owner=lambda _: 'owner',
            agent_key=Mock(return_value=(AGENT,'not-a-real-key')),_call=Mock(return_value={'state':'confirmed','transaction':'fund-tx'}),
            chain_state=Mock(return_value={'owner':{'delegatedToAgent':'9000','amount':'10000'},'agent':{'usdcAtomic':'0'}}))
        self.server=SimpleNamespace(solana_allowance=self.lane)
        self.helper=Mock(side_effect=self.run_helper)
        for p in [patch.object(m,'_journal_dir',return_value=self.root),patch.object(m,'_run',self.helper)]:
            p.start();self.addCleanup(p.stop)
    def run_helper(self,lane,**payload):
        if payload.get('checkOnly'):return {'ok':True,'ready':True}
        if payload.get('verify'):return {'ok':True,'verified':True}
        Path(payload['checkpoint']).write_text(json.dumps({'payer':AGENT,'channelId':'channel-fixture'}))
        return paid()
    def pay(self):return m.pay(self.server,Mock(),ACCOUNT,{'messages':[]})
    def test_funds_only_shortfall_and_counts_actual_usage(self):
        amount,_,tx=self.pay();self.assertEqual((amount,tx),(2130,'solana-tx'))
        self.lane._call.assert_called_once_with(ACCOUNT,'allowance-pull',fee_payer=True,owner='owner',amount='3000')
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),2130)
        self.assertFalse(list(self.root.glob('*.submitted')))
    def test_reuses_agent_refund_without_another_funding_transaction(self):
        self.lane.chain_state.return_value['agent']['usdcAtomic']='3500'
        self.pay();self.lane._call.assert_not_called()
    def test_no_recipient_account_stops_before_funding_and_releases_hold(self):
        self.helper.side_effect=None;self.helper.return_value={'ok':False,'error':'receiver_not_ready'}
        with self.assertRaises(AllowanceUnavailable):self.pay()
        self.lane._call.assert_not_called();self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),0)
        self.assertFalse(list(self.root.glob('*.json')))
    def test_uncertain_payment_survives_restart_and_counts_against_other_purchases(self):
        def lost(lane,**p):
            if p.get('checkOnly'):return {'ok':True,'ready':True}
            Path(p['checkpoint']).write_text('{}');return {'ok':False}
        self.helper.side_effect=lost
        with self.assertRaises(AllowanceError):self.pay()
        reopened=SolanaAllowanceStore(self.root/'allowance.db')
        self.assertEqual(reopened.spent_since(ACCOUNT,0),3000)
        calls=self.helper.call_count
        with self.assertRaisesRegex(AllowanceError,'previous Solana'):self.pay()
        self.assertEqual(self.helper.call_count,calls)
    def test_uncertain_funding_is_not_pulled_twice(self):
        self.lane._call.return_value={'state':'uncertain','transaction':'fund-tx'}
        with self.assertRaises(AllowanceError):self.pay()
        with self.assertRaises(AllowanceError):self.pay()
        self.lane._call.assert_called_once();self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
        self.assertEqual(self.helper.call_count,1)
    def test_confirmed_full_refund_releases_hold_without_charging(self):
        def refund(lane,**p):
            output=self.run_helper(lane,**p)
            if 'checkpoint' in p:return {**output,'ok':False,'refunded':True,'amountAtomic':'0','refundAtomic':'3000'}
            return output
        self.helper.side_effect=refund
        with self.assertRaisesRegex(AllowanceError,'entire Solana'):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),0);self.assertFalse(list(self.root.glob('*.json')))
    def test_wrong_invoice_or_refund_keeps_hold(self):
        def wrong(lane,**p):
            output=self.run_helper(lane,**p)
            if 'checkpoint' in p:output['refundAtomic']='869'
            return output
        self.helper.side_effect=wrong
        with self.assertRaises(AllowanceError):self.pay()
        self.assertEqual(self.lane.store.spent_since(ACCOUNT,0),3000)
    def test_revoked_grant_or_daily_limit_prevents_payment(self):
        self.lane.chain_state.return_value['owner']['delegatedToAgent']='0'
        with self.assertRaises(AllowanceError):self.pay()
        self.lane._call.assert_not_called()
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
