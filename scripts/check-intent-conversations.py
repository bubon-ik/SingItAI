#!/usr/bin/env python3
"""Opt-in live classifier regression check; all Telegram/catalog/wallet I/O is fake.

Run with --live and TYPESAFE_API_KEY in the environment. Never sends a Telegram
message or creates a purchase. Uses synthetic fixtures, not production accounts.
"""
import argparse
import os
from pathlib import Path
import sys
from dataclasses import asdict
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hermes-plugins/sign402-wallet/tests'))
from test_assistant import AssistantTests

CASES = [
    ('us_food_balance', None, ['i need somesing for food in US', 'show me a giftcards', 'what my ballance USDC in base?'], ('balance', 'base')),
    ('ru_acceptance', None, ['Хочу еду в Чехии', 'да, покажи пожалуйста'], ('catalog', 'CZ', 'food')),
    ('en_acceptance', None, ['I want food in US', 'sure, show them'], ('catalog', 'US', 'food')),
    ('decline', None, ['I want food in US', 'no thanks'], ('cancelled',)),
    ('cancel', None, ['show me gift cards', 'never mind'], ('cancelled',)),
    ('country_name', None, ['I need mobile internet', 'Germany'], ('search', 'esim', 'DE')),
    ('all_categories', None, ['I want food in US', 'show all gift card categories instead'], ('catalog', 'US', 'all')),
    ('network_reply', 'network', ['on Solana please'], ('balance', 'solana')),
    ('short_wallet_in_search', 'awaiting-search', ['my balance'], ('balance', 'base')),
    ('merchant_search', 'awaiting-search', ['Wolt'], ('search', 'Wolt', 'US')),
    ('long_merchant_search', 'awaiting-search', ['The Coffee Bean and Tea Leaf'], ('search', 'The Coffee Bean and Tea Leaf', 'US')),
    ('package_interrupt', 'select-package', ['how much USDC do I have on Base?'], ('balance', 'base')),
    ('catalog_country', 'select-product', ['and in Germany?'], ('catalog', 'DE', 'food')),
    ('manual_country', 'awaiting-country', ['Germany'], ('country', 'DE')),
    ('correction_to_balance', None, ['I want food in US', 'no, actually show my Base balance'], ('balance', 'base')),
    ('unsupported_transfer', None, ['I want food in US', 'send all my USDC to another wallet'], ('no_actions',)),
    ('multiple_tasks', None, ['show my balance and order food in US'], ('no_actions',)),
    ('original_esim', None, ['i need internet in Germany'], ('search', 'esim', 'DE')),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow calls to TypeSafe with the configured key')
    args = parser.parse_args()
    if not args.live or not os.environ.get('TYPESAFE_API_KEY'):
        parser.error('--live and TYPESAFE_API_KEY are required')
    failed = []
    for name, stage, messages, expected in CASES:
        case = AssistantTests()
        case.setUp()
        uid = '1045618308'
        decisions = []
        original_classify = case.router.classify
        def classify(*args, **kwargs):
            result = original_classify(*args, **kwargs)
            decisions.append(asdict(result))
            return result
        classifier_patch = patch.object(case.router, 'classify', side_effect=classify)
        classifier_patch.start()
        try:
            case.client.bitrefill_search_result = {'ok': True, 'products': [
                {'productId': 'test-esim', 'name': 'Test eSIM', 'country': 'DE', 'category': 'esim'}]}
            if stage == 'network':
                case.plugin._natural_assistant.remember(uid, case.router.Intent('balance', network='other'), 'network')
            elif stage:
                case.plugin._BITREFILL_SESSIONS[uid] = {'stage': stage, 'country': 'US', 'category': 'food'}
                case.plugin._BITREFILL_USER_COUNTRIES[uid] = 'US'
            for message in messages:
                case.dispatch(message)
                pending = case.plugin._natural_assistant.pending.get(uid)
                if pending and pending[2] == 'confirm-intent':
                    # Confirmation authorizes only fake catalog/read handlers.
                    case.dispatch('Yes')
            kind, *values = expected
            if kind == 'balance':
                assert case.client.calls[-1][0] == 'balance' and case.client.calls[-1][2] == values[0]
            elif kind == 'catalog':
                assert case.client.bitrefill_list_calls[-1][:2] == tuple(values)
            elif kind == 'search':
                assert case.client.bitrefill_search_calls[-1][:2] == tuple(values)
            elif kind == 'country':
                assert case.plugin._BITREFILL_USER_COUNTRIES[uid] == values[0]
            elif kind == 'cancelled':
                assert not case.plugin._natural_assistant.pending and not case.plugin._BITREFILL_SESSIONS
                assert not case.client.bitrefill_list_calls
            elif kind == 'no_actions':
                assert not case.client.calls and not case.client.bitrefill_list_calls and not case.client.bitrefill_search_calls
            assert not case.client.bitrefill_calls and not case.client.withdraw_calls
            print(name + ': PASS', flush=True)
        except Exception as exc:
            failed.append(name)
            # Only synthetic case names and typed state are reported. Never dump
            # provider responses, request headers, credentials or exception text.
            pending = case.plugin._natural_assistant.pending.get(uid)
            print(name + ': FAIL ' + type(exc).__name__ + ' pending=' + str(pending[2] if pending else None), flush=True)
            print('Synthetic case decisions: ' + str(decisions), flush=True)
        finally:
            classifier_patch.stop()
            case.doCleanups()
    print(f'{len(CASES) - len(failed)}/{len(CASES)} live conversation cases passed; fake commerce/wallet I/O', flush=True)
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
