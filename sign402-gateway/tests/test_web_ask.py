"""Offline integration: actual-usage Ask selection and billing, without a top-up."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sign402_gateway import web_ask, web_internal, web_venice
from sign402_gateway.agent_allowance import AllowanceError, AllowanceUnavailable
from sign402_gateway.chat_store import ChatStore

ACCOUNT = "wallet:0x1111111111111111111111111111111111111111"
SOLANA_ACCOUNT = "solana:4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu"

class AskTests(unittest.TestCase):
    def setUp(self):
        store = ChatStore(":memory:"); self.addCleanup(store.close)
        self.server = SimpleNamespace(chat_service=SimpleNamespace(store=store),user_event_store=Mock())
        self.gw = SimpleNamespace(_purchases_paused=lambda:False,_enforce_user_purchase_rate=Mock())
        self.pay = Mock(return_value=(1130,{"choices":[{"message":{"content":"Hello"}}],
            "usage":{"prompt_tokens":20,"completion_tokens":5},"billing":{"totalAtomic":"1130"}},"0x"+"a"*64))
        for p in [patch.object(web_ask.ask_metered,"pay",self.pay),patch.object(web_internal,"_limits_from_limiter")]:
            p.start();self.addCleanup(p.stop)

    def chat(self,messages=None,account=ACCOUNT):
        return web_ask.chat(self.server,self.gw,account,messages or [{"role":"user","content":"Hi"}])

    def test_one_answer_records_actual_cost_and_tokens(self):
        status,answer=self.chat()
        self.assertEqual((status,answer["text"],answer["costAtomic"]),(200,"Hello",1130))
        self.assertEqual((answer["promptTokens"],answer["completionTokens"]),(20,5))
        self.assertEqual(answer["billingMode"],"actual_usage")
        self.pay.assert_called_once()
        args=self.pay.call_args.args
        self.assertEqual(args[2],ACCOUNT)
        self.assertEqual(args[3]["messages"][-1],{"role":"user","content":"Hi"})
        self.assertIn("connected network is Base",args[3]["messages"][0]["content"])
        self.assertEqual(args[3]["max_tokens"],1200)
        self.assertNotIn("topUpUsd",answer)

    def test_solana_funding_context_overrides_stale_history_without_erasing_question(self):
        original = [{"role":"system","content":"Current state: owner has USDC"},
                    {"role":"assistant","content":"Top up Agent funds and SOL before chatting"},
                    {"role":"user","content":"hello"}]
        with patch.object(web_ask.ask_solana,"pay",return_value=(1130,self.pay.return_value[1],"solana-tx")) as sol:
            self.chat(original,account=SOLANA_ACCOUNT)
            sent=sol.call_args.args[3]["messages"]
        self.assertEqual(sent[-1],original[-1]);self.assertEqual(sent[1],original[1])
        self.assertIn("directly from the user's wallet",sent[0]["content"])
        self.assertIn("Zero USDC or SOL on the agent is normal",sent[0]["content"])
        self.assertIn("not an unsolicited wallet status",sent[0]["content"])
        self.assertEqual(original[0]["content"],"Current state: owner has USDC")
        self.pay.assert_not_called()

    def test_payment_context_and_latest_question_survive_history_trimming(self):
        original=[{"role":"user","content":str(i)+"x"*800} for i in range(40)]
        with patch.object(web_ask.ask_solana,"pay",return_value=(1130,self.pay.return_value[1],"solana-tx")) as sol:
            self.chat(original,account=SOLANA_ACCOUNT)
            sent=sol.call_args.args[3]["messages"]
        self.assertLessEqual(len(sent),40)
        self.assertLessEqual(sum(len(m["content"].encode("utf-16-le"))//2 for m in sent),24000)
        self.assertIn("connected network is Solana",sent[0]["content"])
        self.assertEqual(sent[-1],original[-1])

    def test_pause_prevents_payment(self):
        self.gw._purchases_paused=lambda:True
        with self.assertRaises(AllowanceUnavailable):self.chat()
        self.pay.assert_not_called()

    def test_failure_or_lost_answer_is_not_retried(self):
        self.pay.side_effect=AllowanceError("unresolved")
        with self.assertRaises(AllowanceError):self.chat()
        self.pay.assert_called_once()
        self.pay.reset_mock();self.pay.side_effect=None;self.pay.return_value=(1130,{"choices":[]},"tx")
        with self.assertRaises(AllowanceError):self.chat()
        self.pay.assert_called_once()

    def test_solana_uses_its_own_payment_lane_without_base_fallback(self):
        with patch.object(web_ask.ask_solana,"pay",return_value=(2130,self.pay.return_value[1],"solana-tx")) as sol:
            status,reply=self.chat(account=SOLANA_ACCOUNT)
            self.assertEqual((status,reply["costAtomic"]),(200,2130))
            sol.assert_called_once()
        self.pay.assert_not_called()
        web_ask.choose(self.server,SOLANA_ACCOUNT)
        self.assertTrue(web_ask.selected(self.server,SOLANA_ACCOUNT))
        self.assertFalse(web_ask.selected(self.server,ACCOUNT))
        self.assertEqual(web_ask.usage(SOLANA_ACCOUNT)[1]["settlementFeeAtomic"],1000)

    def test_model_choice_is_per_account_and_moves_no_money(self):
        self.server.chat_service.store.set_model(ACCOUNT,"existing-venice-model")
        self.assertFalse(web_ask.selected(self.server,ACCOUNT))
        status,result=web_venice.choose_model(self.server,ACCOUNT,web_ask.MODEL)
        self.assertEqual((status,result["chosen"]),(200,web_ask.MODEL))
        self.assertTrue(web_ask.selected(self.server,ACCOUNT))
        self.assertEqual(self.server.chat_service.store.get_session(ACCOUNT).model,"existing-venice-model")
        self.assertFalse(web_ask.selected(self.server,ACCOUNT+"2"));self.pay.assert_not_called()
        self.assertEqual(web_ask.usage()[1]["billingMode"],"actual_usage")

    def test_picker_discloses_markup_fee_and_ceiling(self):
        result=web_ask.listing({"models":[],"categories":[],"chosen":"existing"},ACCOUNT)
        self.assertEqual((result["chosen"],result["provider"]),("existing","Venice"))
        model=result["models"][0]
        self.assertEqual((model["markupPercent"],model["settlementFeeUsd"],model["maxChargeUsd"]),(30,"0.001","0.003"))
        self.assertNotIn("pricePerAnswerUsd",model)
        self.assertEqual(web_ask.listing({"models":[],"categories":[],"chosen":"existing"},SOLANA_ACCOUNT)["models"][0]["settlementFeeUsd"],"0.001")

    def test_too_long_question_is_refused_before_payment(self):
        with self.assertRaises(ValueError):self.chat([{"role":"user","content":"🙂"*12001}])
        self.pay.assert_not_called()

if __name__ == '__main__':unittest.main()
