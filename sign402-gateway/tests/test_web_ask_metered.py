"""The metered lane reserves a ceiling, counts actual spend and persists uncertainty."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sign402_gateway import ask_metered as m
from sign402_gateway.agent_allowance import AllowanceError

ACCOUNT='wallet:0x'+'11'*20
AGENT='0x'+'22'*20
TX='0x'+'aa'*32

def result():
    return {'ok':True,'payer':AGENT,'transaction':TX,'amountAtomic':'1130','body':{
        'choices':[{'message':{'content':'Hello'}}], 'usage':{'buyer_cost_micro':100},'billing':{
            'providerCostAtomic':'100','markupAtomic':'30','totalAtomic':'1130','settlementFeeAtomic':'1000',
            'maxChargeAtomic':'3000','markupBps':3000,'mode':'actual_usage','version':1,'currency':'USDC'}}}

def receipt(amount=1130):
    return {'status':'0x1','transactionHash':TX,'logs':[{'address':m.USDC,'data':hex(amount),'logIndex':'0x3',
        'topics':[m.TRANSFER_TOPIC,'0x'+AGENT[2:].zfill(64),'0x'+m.PAY_TO[2:].lower().zfill(64)]}]}

class MeteredTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);self.root=Path(directory.name)
        self.service=Mock();self.service._spend_lock=threading.Lock()
        self.service.lane_for.return_value={'per_purchase_cap':3000}
        self.service.agent_key.return_value=(AGENT,'test-key-not-real')
        self.service.evm.call.return_value=receipt();self.service.store.counted_settlements.return_value=set()
        self.server=SimpleNamespace(allowance=self.service,spending_policy=None)
        self.gw=Mock();self.gw._reserve_user_wallet_spend.return_value=('reservation',None,'claim')
        self.buyer=Mock(side_effect=self.success)
        for p in [patch.object(m,'_journal_dir',return_value=self.root),patch.object(m,'_run_buyer',self.buyer)]:
            p.start();self.addCleanup(p.stop)
    def success(self,key,body,checkpoint):
        checkpoint.write_text(json.dumps({'nonce':'1','ceilingAtomic':'3000'}));return result()
    def pay(self):return m.pay(self.server,self.gw,ACCOUNT,{'messages':[]})
    def test_reserve_maximum_but_ledger_and_memory_receive_only_actual_charge(self):
        amount,body,tx=self.pay();self.assertEqual((amount,tx),(1130,TX))
        self.assertEqual(self.gw._reserve_user_wallet_spend.call_args.args[2]['amountAtomic'],'3000')
        settled=self.gw._settle_user_wallet_spend.call_args
        self.assertEqual(settled.kwargs['actual_amount_atomic'],1130)
        self.assertEqual(settled.args[4]['amountAtomic'],'1130')
        self.assertEqual(self.gw._payment_from_requirements.call_args.args[0]['amountAtomic'],'1130')
        self.service.store.count_settlement.assert_called_once_with(TX,'0x3',ACCOUNT,m.PAY_TO,1130,m.URL,unittest.mock.ANY)
        self.assertEqual(list(self.root.glob('*.json')),[])
        self.gw._release_user_wallet_spend.assert_not_called()
    def test_failure_before_signature_releases_reservation_and_allows_next_request(self):
        self.buyer.return_value={'ok':False};self.buyer.side_effect=None
        with self.assertRaises(AllowanceError):self.pay()
        self.gw._release_user_wallet_spend.assert_called_once_with(self.server,'reservation')
        self.assertEqual(list(self.root.glob('*.json')),[])
    def test_uncertain_submission_keeps_hold_and_blocks_next_request(self):
        def lost(key,body,checkpoint):checkpoint.write_text('{}');return {'ok':False}
        self.buyer.side_effect=lost
        with self.assertRaises(AllowanceError):self.pay()
        self.gw._release_user_wallet_spend.assert_not_called()
        with self.assertRaisesRegex(AllowanceError,'previous Ask payment'):self.pay()
        self.buyer.assert_called_once()
        state=json.loads(next(self.root.glob('*.json')).read_text());self.assertEqual(state['reservationId'],'reservation')
        self.assertNotIn('test-key-not-real',json.dumps(state))
    def test_wrong_onchain_amount_stays_unresolved_and_never_enters_ledger(self):
        self.service.evm.call.return_value=receipt(3000)
        with self.assertRaises(AllowanceError):self.pay()
        self.gw._settle_user_wallet_spend.assert_not_called();self.gw._release_user_wallet_spend.assert_not_called()
    def test_fee_tampering_is_rejected(self):
        paid=result();paid['body']['billing']['markupBps']=4000
        with self.assertRaises(AllowanceError):m._validate_result(self.service,paid,AGENT,ACCOUNT)
    def test_reused_transaction_is_rejected(self):
        self.service.store.counted_settlements.return_value={(TX,'0x3')}
        with self.assertRaises(AllowanceError):m._validate_result(self.service,result(),AGENT,ACCOUNT)
    def test_small_per_purchase_limit_prevents_funding_or_signing(self):
        self.service.lane_for.return_value={'per_purchase_cap':2999}
        with self.assertRaises(AllowanceError):self.pay()
        self.buyer.assert_not_called();self.service._fund.assert_not_called()

if __name__=='__main__':unittest.main()
