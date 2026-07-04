import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import ANY, Mock

from sign402_gateway.bitrefill import TestBitrefillClient
from sign402_gateway.bitrefill_runner import (
    BitrefillFulfillmentRunner,
    BitrefillProductDetailsService,
    BitrefillPurchaseRunner,
    BitrefillQuoteService,
    BitrefillSearchService,
    BitrefillSettlementPreparationRunner,
    WalletBitrefillPurchaseRunner,
    lookup_bitrefill_order,
)
from sign402_gateway.commerce_store import BitrefillCommerceStore


class PendingThenDeliveredBitrefillClient:
    def __init__(self):
        self.buy_calls = 0
        self.refresh_calls = 0

    def buy_product(self, *, quote, recipient, checkpoint_callback=None):
        self.buy_calls += 1
        return {
            "ok": True,
            "provider": "bitrefill-live",
            "invoiceId": "invoice_1",
            "orderId": "order_1",
            "status": "created",
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": None,
            },
        }

    def refresh_purchase(self, provider_result, quote):
        self.refresh_calls += 1
        return {
            "ok": True,
            "provider": "bitrefill-live",
            "invoiceId": provider_result["invoiceId"],
            "orderId": provider_result["orderId"],
            "status": "delivered",
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": {"code": "READY-123"},
            },
        }


class FixedRealRatePricer:
    def price_for_usdc(self, target_usdc):
        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": str(target_usdc),
            "bufferedTargetUsdc": "0.11",
            "requiredSingit": "25000",
            "requiredSingitAtomic": "25000000000000000000000",
            "expectedUsdc": "0.111",
            "minUsdc": "0.109",
        }


class FakeFundingRunner:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, quote):
        self.calls.append(quote)
        if self.fail:
            raise RuntimeError("swap route failed")
        return {
            "ok": True,
            "txId": "0xSWAP",
            "expectedUsdc": quote.get("expectedUsdc", "0.11"),
        }


class BitrefillRunnerTests(unittest.TestCase):
    def test_catalog_services_search_and_return_product_details(self):
        client = TestBitrefillClient()

        search = BitrefillSearchService(bitrefill_client=client)(
            {"query": "phone", "country": "US", "includeTestProducts": True}
        )
        details = BitrefillProductDetailsService(bitrefill_client=client)(
            {"productId": "test-phone-refill", "country": "US"}
        )

        self.assertEqual(search["products"][0]["productId"], "test-phone-refill")
        self.assertEqual(details["recipientType"], "phone")

    def test_quote_service_quotes_selected_phone_refill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_phone",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote(
                {
                    "productId": "test-phone-refill",
                    "packageId": "1",
                    "country": "US",
                    "recipient": {"phone": "+12025550123"},
                }
            )

            self.assertEqual(quote["productId"], "test-phone-refill")
            self.assertEqual(quote["packageId"], "1")

    def test_quote_service_saves_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})

            self.assertEqual(quote["quoteId"], "quote_1")
            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTED")

    def test_quote_service_uses_configured_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
                ttl_seconds=900,
            )

            quote = service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})

            self.assertEqual(quote["expiresAtEpoch"], 1_719_000_900)
            self.assertIn("Quote expires in 900s", quote["quoteText"])

    def test_quote_service_can_use_real_rate_pricer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                real_rate_pricer=FixedRealRatePricer(),
                quote_id_provider=lambda: "quote_real_1",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote(
                {"productId": "test-gift-card-code", "packageId": "1", "country": "US"}
            )

            self.assertEqual(quote["pricingMode"], "bankr_real_rate")
            self.assertEqual(quote["singitAmount"], "25000")
            self.assertEqual(
                store.get_quote("quote_real_1")["quote"]["maxSingitAtomic"],
                "25000000000000000000000",
            )

    def test_runner_requires_firefly_before_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            firefly.approve_payment_hash.return_value = {
                "approved": False,
                "approvedHash": "",
                "raw": "<CANCEL",
            }
            bankr = Mock()

            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
            )

            result = runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertFalse(result["ok"])
            self.assertEqual(result["decision"], "rejected_by_firefly")
            bankr.assert_not_called()

    def test_runner_checks_reserve_before_firefly_or_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote(
                {"productId": "test-gift-card-link", "packageId": "1", "country": "US"}
            )
            firefly = Mock()
            bankr = Mock()
            guard = Mock(side_effect=ValueError("insufficient USDC reserve"))
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                pre_payment_guard=guard,
            )

            with self.assertRaisesRegex(ValueError, "insufficient USDC reserve"):
                runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            guard.assert_called_once_with(quote)
            firefly.approve_payment_hash.assert_not_called()
            bankr.assert_not_called()
            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTED")

    def test_runner_calls_bankr_after_firefly_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock(return_value={"ok": True, "status": 200, "body": {"ok": True, "orderId": "order_1"}})

            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={"email": "buyer@example.com"})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertTrue(result["ok"])
            self.assertEqual(result["fulfillmentToken"], "fulfill_secret_1")
            bankr.assert_called_once_with(
                "https://x402.bankr.bot/wallet/buy-bitrefill",
                request_body={"quoteId": "quote_1", "fulfillmentToken": "fulfill_secret_1"},
            )
            metadata = store.get_quote("quote_1")["metadata"]
            self.assertEqual(
                metadata["fulfillmentTokenHash"],
                hashlib.sha256(b"fulfill_secret_1").hexdigest(),
            )
            self.assertEqual(metadata["recipient"], {"email": "buyer@example.com"})
            self.assertNotIn("fulfill_secret_1", str(metadata))
            self.assertIn(
                "✅ Test Gift Card Link $1 is ready.",
                result["telegramText"],
            )
            self.assertIn("paid with SINGIT", result["telegramText"])
            self.assertNotIn("x402", result["telegramText"].lower())
            self.assertNotIn("invoice", result["telegramText"].lower())
            self.assertNotIn("usdc", result["telegramText"].lower())

    def test_runner_blocks_bitrefill_fulfillment_without_verified_singit_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock(return_value={"ok": True, "status": 200, "body": {"ok": True}, "transactionHash": None})
            fulfillment = Mock()
            settlement_verifier = Mock(side_effect=ValueError("SINGIT settlement transaction hash is missing"))
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
                settlement_verifier=settlement_verifier,
                fulfillment_runner=fulfillment,
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            with self.assertRaisesRegex(ValueError, "SINGIT settlement transaction hash is missing"):
                runner.buy({"quoteId": "quote_1"})

            bankr.assert_called_once()
            settlement_verifier.assert_called_once()
            fulfillment.assert_not_called()
            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
            self.assertEqual(
                record["metadata"]["singitSettlementError"],
                "SINGIT settlement transaction hash is missing",
            )

    def test_runner_fulfills_bitrefill_only_after_verified_singit_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr_result = {
                "ok": True,
                "status": 200,
                "body": {"ok": True, "quoteId": "quote_1", "status": "singit_settlement_requested"},
                "transactionHash": "0xSINGITTX",
            }
            bankr = Mock(return_value=bankr_result)
            settlement_verifier = Mock(return_value={"transactionHash": "0xSINGITTX", "amountAtomic": quote["maxSingitAtomic"]})
            fulfillment = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=TestBitrefillClient(),
                now_provider=lambda: 1_719_000_002,
            )
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
                settlement_verifier=settlement_verifier,
                fulfillment_runner=fulfillment,
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy({"quoteId": "quote_1"})

            self.assertTrue(result["ok"])
            self.assertEqual(result["decision"], "approved_and_executed")
            self.assertEqual(result["bitrefill"]["orderId"], store.get_quote("quote_1")["metadata"]["bitrefill"]["orderId"])
            self.assertEqual(store.get_quote("quote_1")["state"], "DELIVERED")
            settlement_verifier.assert_called_once_with(bankr_result=bankr_result, quote=quote)

    def test_runner_rejects_expired_quote_before_firefly_or_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock()
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_121,
            )

            with self.assertRaisesRegex(ValueError, "quote expired"):
                runner.buy({"quoteId": "quote_1"})

            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTE_EXPIRED")
            firefly.approve_payment_hash.assert_not_called()
            bankr.assert_not_called()

    def test_wallet_runner_fulfills_without_bankr_x402_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_wallet_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote(
                {"productId": "test-gift-card-link", "packageId": "1", "country": "US"}
            )
            funding = FakeFundingRunner()
            fulfillment = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=TestBitrefillClient(),
                funding_runner=funding,
                now_provider=lambda: 1_719_000_002,
            )
            approval = Mock()
            runner = WalletBitrefillPurchaseRunner(
                store=store,
                approval_client=approval,
                fulfillment_runner=fulfillment,
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "wallet_fulfill_secret_1",
            )
            expected_hash = runner.payment_hash_for_quote(
                quote,
                recipient={"email": "buyer@example.com"},
            )
            approval.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy(
                {
                    "quoteId": "quote_wallet_1",
                    "recipient": {"email": "buyer@example.com"},
                    "telegramUserId": "1045618308",
                }
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["decision"], "approved_and_fulfilled")
            self.assertEqual(result["fulfillmentToken"], "wallet_fulfill_secret_1")
            self.assertEqual(result["walletCheckout"]["paymentApprovalHash"], expected_hash)
            self.assertNotIn("bankr", result)
            self.assertEqual(len(funding.calls), 1)
            record = store.get_quote("quote_wallet_1")
            self.assertEqual(record["state"], "DELIVERED")
            self.assertEqual(record["metadata"]["recipient"], {"email": "buyer@example.com"})
            self.assertEqual(record["metadata"]["walletCheckout"]["approval"]["approved"], True)
            self.assertNotIn("wallet_fulfill_secret_1", str(record["metadata"]))
            self.assertEqual(approval.call_args.kwargs["telegram_user_id"], "1045618308")

    def test_wallet_runner_rejects_unconfirmed_checkout_before_fulfillment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_wallet_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote(
                {"productId": "test-gift-card-link", "packageId": "1", "country": "US"}
            )
            fulfillment = Mock()
            approval = Mock(return_value={"approved": False, "approvedHash": ""})
            runner = WalletBitrefillPurchaseRunner(
                store=store,
                approval_client=approval,
                fulfillment_runner=fulfillment,
                now_provider=lambda: 1_719_000_001,
            )

            result = runner.buy({"quoteId": "quote_wallet_1"})

            self.assertFalse(result["ok"])
            self.assertEqual(result["decision"], "rejected_by_user")
            fulfillment.assert_not_called()
            self.assertEqual(store.get_quote("quote_wallet_1")["state"], "USER_REJECTED")

    def test_runner_rejects_replay_of_non_quoted_order_before_firefly_or_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state("quote_1", "DELIVERED")
            firefly = Mock()
            bankr = Mock()
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(ValueError, "quote is not purchasable"):
                runner.buy({"quoteId": "quote_1"})

            firefly.approve_payment_hash.assert_not_called()
            bankr.assert_not_called()

    def test_runner_marks_reconciliation_required_when_bankr_call_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock(side_effect=RuntimeError("bankr timeout"))
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={})
            firefly.approve_payment_hash.return_value = {
                "approved": True,
                "approvedHash": expected_hash,
            }

            with self.assertRaisesRegex(RuntimeError, "bankr timeout"):
                runner.buy({"quoteId": "quote_1"})

            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
            self.assertEqual(record["metadata"]["bankrError"], "bankr timeout")

    def test_fulfillment_runner_buys_once_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"fulfill_secret_1").hexdigest()},
            )

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=TestBitrefillClient(),
                now_provider=lambda: 1_719_000_001,
            )
            result1 = runner.fulfill(
                {
                    "quoteId": "quote_1",
                    "fulfillmentToken": "fulfill_secret_1",
                    "recipient": {"email": "buyer@example.com"},
                }
            )

            self.assertTrue(result1["ok"])
            self.assertEqual(result1["quoteId"], "quote_1")
            self.assertIn("orderId", result1)
            self.assertNotIn("redemption", result1)
            self.assertNotIn("buyer@example.com", str(result1))
            with self.assertRaisesRegex(ValueError, "already fulfilled"):
                runner.fulfill(
                    {"quoteId": "quote_1", "fulfillmentToken": "fulfill_secret_1"}
                )

    def test_fulfillment_rejects_quote_that_expires_before_provider_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            bitrefill = Mock()

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_121,
            )

            with self.assertRaisesRegex(ValueError, "quote expired"):
                runner.fulfill({"quoteId": "quote_1"})

            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTE_EXPIRED")
            bitrefill.buy_product.assert_not_called()

    def test_fulfillment_rejects_token_not_bound_to_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            bitrefill = Mock()

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(ValueError, "invalid fulfillment token"):
                runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "wrong_token"})

            bitrefill.buy_product.assert_not_called()

    def test_fulfillment_uses_firefly_approved_recipient_from_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest(),
                    "recipient": {"email": "approved@example.com"},
                },
            )
            bitrefill = Mock(
                **{
                    "buy_product.return_value": {
                        "ok": True,
                        "orderId": "order_1",
                        "status": "delivered",
                    }
                }
            )

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )
            runner.fulfill(
                {
                    "quoteId": "quote_1",
                    "fulfillmentToken": "valid_token",
                    "recipient": {"email": "attacker@example.com"},
                }
            )

            bitrefill.buy_product.assert_called_once_with(
                quote=quote,
                recipient={"email": "approved@example.com"},
                checkpoint_callback=ANY,
            )

    def test_fulfillment_swaps_singit_before_bitrefill_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "productType": "gift_card",
                    "packageId": "1",
                    "packageValue": "1",
                    "priceUsd": "1.00",
                    "pricingMode": "bankr_real_rate",
                    "expectedUsdc": "1.10",
                    "maxSingitAtomic": "25000000000000000000000",
                    "singitAmount": "25000",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            funding = FakeFundingRunner()
            bitrefill = Mock(
                **{
                    "buy_product.return_value": {
                        "ok": True,
                        "orderId": "order_1",
                        "status": "delivered",
                    }
                }
            )
            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                funding_runner=funding,
                now_provider=lambda: 1_719_000_001,
            )

            runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            self.assertEqual(funding.calls[0]["quoteId"], "quote_1")
            bitrefill.buy_product.assert_called_once()
            self.assertEqual(store.get_quote("quote_1")["metadata"]["bankrSwap"]["txId"], "0xSWAP")

    def test_fulfillment_does_not_buy_bitrefill_when_swap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "productType": "gift_card",
                    "packageId": "1",
                    "packageValue": "1",
                    "priceUsd": "1.00",
                    "pricingMode": "bankr_real_rate",
                    "expectedUsdc": "1.10",
                    "maxSingitAtomic": "25000000000000000000000",
                    "singitAmount": "25000",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            bitrefill = Mock()
            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                funding_runner=FakeFundingRunner(fail=True),
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(RuntimeError, "swap route failed"):
                runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            bitrefill.buy_product.assert_not_called()
            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
            self.assertEqual(record["metadata"]["fundingError"], "swap route failed")

    def test_settlement_preparation_returns_real_rate_pricing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "productType": "gift_card",
                    "packageId": "1",
                    "packageValue": "1",
                    "priceUsd": "1.00",
                    "pricingMode": "bankr_real_rate",
                    "expectedUsdc": "1.10",
                    "maxSingitAtomic": "25000000000000000000000",
                    "singitAmount": "25000",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            runner = BitrefillSettlementPreparationRunner(
                store=store,
                now_provider=lambda: 1_719_000_001,
            )

            result = runner.prepare({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            self.assertEqual(result["pricingMode"], "bankr_real_rate")
            self.assertEqual(result["settleAmountAtomic"], "25000000000000000000000")

    def test_order_lookup_redacts_private_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "recipient": {"email": "private@example.com"},
                    "fulfillmentTokenHash": "a" * 64,
                    "bankr": {"stdout": "private diagnostic"},
                    "bitrefill": {"orderId": "order_1", "status": "delivered"},
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            result = lookup_bitrefill_order(store, "quote_1")

            self.assertEqual(result["quoteId"], "quote_1")
            self.assertEqual(result["state"], "DELIVERED")
            self.assertEqual(result["orderId"], "order_1")
            self.assertNotIn("private@example.com", str(result))
            self.assertNotIn("fulfillmentTokenHash", result)
            self.assertNotIn("metadata", result)

    def test_order_lookup_can_reveal_redemption_when_recipient_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "recipient": {"email": "buyer@example.com"},
                    "bitrefill": {
                        "orderId": "order_1",
                        "status": "delivered",
                        "redemption": {
                            "type": "bitrefill",
                            "label": "Bitrefill redemption",
                            "value": {"code": "SECRET-CODE"},
                        },
                    },
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            result = lookup_bitrefill_order(
                store,
                "quote_1",
                include_redemption=True,
                recipient={"email": "buyer@example.com"},
            )

            self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE")
            self.assertEqual(
                result["telegramText"],
                "✅ Test Gift Card Code $25 is ready. Your code is ready.",
            )

    def test_order_lookup_requires_fulfillment_token_when_no_recipient_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "productType": "gift_card",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"reveal_tok").hexdigest(),
                    "bitrefill": {
                        "orderId": "order_1",
                        "status": "delivered",
                        "redemption": {
                            "type": "bitrefill",
                            "label": "Bitrefill redemption",
                            "value": {"code": "SECRET-CODE"},
                        },
                    },
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            with self.assertRaises(ValueError):
                lookup_bitrefill_order(store, "quote_1", include_redemption=True)

            with self.assertRaises(ValueError):
                lookup_bitrefill_order(
                    store,
                    "quote_1",
                    include_redemption=True,
                    fulfillment_token="wrong_tok",
                )

            result = lookup_bitrefill_order(
                store,
                "quote_1",
                include_redemption=True,
                fulfillment_token="reveal_tok",
            )
            self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE")

    def test_pending_order_refreshes_without_repurchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-code", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest(),
                    "recipient": {"email": "buyer@example.com"},
                },
            )
            bitrefill = PendingThenDeliveredBitrefillClient()
            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )

            first_result = runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})
            refreshed = lookup_bitrefill_order(
                store,
                "quote_1",
                include_redemption=True,
                recipient={"email": "buyer@example.com"},
                bitrefill_client=bitrefill,
            )

            self.assertEqual(first_result["status"], "created")
            self.assertEqual(refreshed["state"], "DELIVERED")
            self.assertEqual(refreshed["redemption"]["value"]["code"], "READY-123")
            self.assertEqual(bitrefill.buy_calls, 1)
            self.assertEqual(bitrefill.refresh_calls, 1)

    def test_order_lookup_rejects_redemption_reveal_for_wrong_recipient(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "recipient": {"email": "buyer@example.com"},
                    "bitrefill": {
                        "orderId": "order_1",
                        "status": "delivered",
                        "redemption": {"value": {"code": "SECRET-CODE"}},
                    },
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            with self.assertRaisesRegex(ValueError, "recipient does not match"):
                lookup_bitrefill_order(
                    store,
                    "quote_1",
                    include_redemption=True,
                    recipient={"email": "attacker@example.com"},
                )

    def test_fulfillment_records_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest(),
                    "recipient": {"email": "approved@example.com"},
                },
            )
            bitrefill = Mock()
            bitrefill.buy_product.side_effect = RuntimeError("provider unavailable")

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "FULFILLMENT_FAILED")
            self.assertEqual(record["metadata"]["fulfillmentError"], "provider unavailable")


if __name__ == "__main__":
    unittest.main()
