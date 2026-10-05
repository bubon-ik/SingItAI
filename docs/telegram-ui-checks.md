# Telegram interface refresh — September 18, 2026

The September 19 [conversation update](conversation-ui-checks.md) supersedes the
persistent Home keyboard described below.

This milestone changes the native Telegram bot in the separate Solana repository.
It does not add a Mini App, web frontend or Solana payment execution.

## User-visible changes

- Persistent Home menu: **Chat / Shop / Wallet / Purchases / Settings**. Chat
  appears only when its existing feature flag is enabled.
- Named inline product, denomination and token choices. Older adapters keep a
  named text keyboard, and previously shipped button labels still work.
- Catalogue USD estimates appear only when the provider explicitly labels the
  price as USD. Generic `price` / `payment_price` fields can use other units.
  An estimate is not an executable quote and does not include a promised fee.
- An order review shows the product, denomination, Base network and selected
  payment token before **Request approval** starts the existing quote/purchase
  flow. Gateway policy, caps and external approval channels are unchanged.
- Loading and result messages share an editable operation card when the Hermes
  Telegram adapter supports it. Deleted cards fall back to a fresh message.
- Wallet opens a Base/Solana selector; deposit addresses have their own action.
  Base balance, provider chat credits and daily allowance have separate labels.
  Solana has no withdrawal or purchase action.
- Purchases lists saved orders, with receipts, safe explorer links and an
  explicit one-time code reveal. Revealed code messages remain in chat when the
  user navigates away. Leaving a screen during reveal does not discard the code.
- Delivery email, approval-channel setup and limits are in Settings. Visible
  plugin navigation uses the SingIt name; internal Sign402 identifiers remain.

## Persistence and compatibility

`UserPurchaseStore` retains the latest 100 successful purchase events per user.
The existing `user-purchases.json` remains private and compatible with the
latest-purchase read API. Previous latest-only state is read without a rewrite;
its retained record joins history on the next successful write. Orders that the
old store already overwrote cannot be recovered. The separate operator-assisted
Solana purchase is not imported into a Telegram user's history.

The new authenticated `POST /agent/purchases` endpoint lists six receipts per
page, returns a selected receipt, or explicitly reveals that order. List/detail
responses use an allowlist of receipt fields and never include provider
snapshots, buyer email, redemption data or fulfillment credentials. Fulfillment
capabilities remain encrypted at rest. Clearing a selected order's capability
does not clear the newest order's capability. `/last_purchase` remains available.
Older receipts can lack amount or transaction metadata; the UI does not invent it.
Pending and failed attempts are not a full persisted order ledger in this version.

Inline actions use one-use, expiring, in-memory capabilities bound to user, chat
and message. They cannot approve a payment or supply a different user identity.
The callback handler rechecks the existing SingIt access policy. Restarting the
bot expires old controls; `/start` restores navigation. Main navigation remains
available while a contextual card is open. The Hermes adapter must expose its
PTB application as `_app` and bot as `_bot`; without that support the plugin uses
text keyboards and fresh messages.

## Verification

- 1,245 gateway tests passed.
- 302 plugin tests passed with `python-telegram-bot==22.5`, including actual PTB
  handler and markup construction with mocked transport.
- Coverage includes retained encrypted history across restart, latest-only
  state migration, selected-order reveal, user isolation, token authentication,
  pagination, duplicate receipts, retention bounds, stale/double/forged button
  presses, cancelled review, deleted-message fallback, code delivery during
  navigation and Base/Solana separation.
- Existing payment, spending-policy and approval regression tests passed.
- All commerce, Telegram transport and chain interactions in these tests are
  fixtures. No payment, live bot message, code retrieval or production deployment
  was performed for this interface change.

Reproduce in an isolated development environment with the gateway installed:

```sh
python -m pip install python-telegram-bot==22.5
python -m unittest discover -s sign402-gateway/tests
python -m unittest discover -s hermes-plugins/sign402-wallet/tests
```

CI installs the Telegram test dependency explicitly. The implementation checks
above were completed before deployment, without modifying the running bot.

## Existing VPS bot replacement

On September 18 the owner explicitly requested replacing the existing bot.
Release `6b3c2f575d3ce3c06a08c5712d282688ea50afb1` was deployed in place after
private code/configuration/state backups. The original local Base repository
was not modified. The VPS checkout now tracks the separate Solana repository.

- On the VPS: 1,245 gateway tests, 302 plugin tests using installed PTB 22.6,
  and 46 CDP helper tests passed before switching.
- The gateway and existing Telegram bot restarted successfully; health returned
  HTTP 200 and Telegram `getMe` succeeded. No queued Telegram updates or startup
  tracebacks were observed in the post-restart check.
- Existing Base wallet addresses and encrypted keys, bot configuration and
  stored purchase history were verified unchanged.
- The new purchase-history route returned HTTP 401 without authentication.
- No purchase or code reveal was performed. A user still needs to send `/start`
  and check Home, Settings, Wallet, Purchases and inline navigation in Telegram.
- Solana wallet creation and balance reads are available; purchases and
  withdrawals through the Telegram bot remain on Base.
