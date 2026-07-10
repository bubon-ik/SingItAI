# Hermes WhatsApp Approval Design

## Goal

Allow a Telegram Sign402 wallet user who does not use iMessage to select WhatsApp
as their single approval channel. Every payment request must arrive in WhatsApp
as a Meta-approved utility template with explicit **Approve** and **Reject**
buttons. A button decision can resolve only the payment request displayed in
that message.

## Product Decisions

- A user selects exactly one active approval channel: `imessage` or `whatsapp`.
- iMessage keeps the existing Photon integration and behaviour.
- WhatsApp uses Hermes's official `whatsapp_cloud` adapter for inbound Meta
  webhooks. Photon is not used for WhatsApp.
- Every WhatsApp payment notification uses a Meta utility template, even within
  the 24-hour customer-service window. This makes delivery behaviour uniform
  and allows payment confirmation after an inactive period.
- WhatsApp remains approval-only. It never reaches the general Hermes agent or
  wallet chat; only pairing and an approval button decision are accepted.
- A pending approval expires after 10 minutes. Failed delivery, expiry,
  rejection, duplicate events, or an invalid button never execute payment.

## Architecture

```text
Telegram wallet plugin
  -> Sign402 gateway creates payment approval
  -> MetaWhatsAppTemplateNotifier calls Meta Graph API
  -> Meta delivers approved template to user
  -> user taps Approve or Reject
  -> Meta webhook -> Hermes whatsapp_cloud adapter
  -> Sign402 plugin pre-dispatch handler
  -> Sign402 gateway validates and records the decision
  -> purchase flow resumes only after an approved decision
```

Hermes owns the public webhook endpoint and verifies Meta's inbound signature.
The Sign402 gateway calls Meta directly only to send the approved utility
template, because Hermes does not currently support outbound WhatsApp templates.
The same permanent Meta System User token is stored as a secret in both service
environments; it is never stored in the repository or database.

## User Flow

1. In Telegram, the user chooses `Connect WhatsApp` instead of `Connect
   iMessage`.
2. Sign402 creates a short, single-use pairing code and instructs the user to
   send it to the Sign402 WhatsApp Business number.
3. Hermes receives that message through `whatsapp_cloud`; the Sign402 plugin
   consumes it before normal agent dispatch. The gateway links the Meta `wa_id`
   to the authenticated Telegram user and sets `whatsapp` as their active
   approval channel.
4. On purchase, the gateway creates one pending approval and sends the
   `sign402_payment_approval` utility template. Its body contains only safe
   context: merchant, amount, asset/currency, an abbreviated wallet address,
   an approval reference, and the expiry. It has `Approve` and `Reject` quick
   reply buttons whose payload includes the opaque approval ID.
5. Hermes forwards the button event to the Sign402 pre-dispatch handler. The
   handler passes the trusted `wa_id`, button payload, and channel to the
   gateway.
6. The gateway verifies that the approval is pending, unexpired, bound to the
   selected WhatsApp identity, and has not already been decided. It records the
   decision atomically. Only `Approve` resumes the existing purchase state
   machine.

## Data and Interfaces

The existing encrypted approval-channel link store remains the source of truth
for the mapping from Telegram user to phone/channel identity. A new encrypted
preference record stores one active channel per Telegram user. Creating an
iMessage link selects `imessage`; creating a WhatsApp link selects `whatsapp`.
An explicit reconnect operation changes the preference.

The gateway exposes channel-neutral pairing and decision operations. The
Hermes plugin supplies `channel=whatsapp` only for events whose platform is
`whatsapp_cloud`; it supplies `channel=imessage` only for Photon events.

A dedicated `MetaWhatsAppTemplateNotifier` sends only the configured utility
template to the Graph API. Required gateway environment values are:

```env
SIGN402_WHATSAPP_ACCESS_TOKEN=<Meta System User token>
SIGN402_WHATSAPP_PHONE_NUMBER_ID=<Meta Phone Number ID>
SIGN402_WHATSAPP_TEMPLATE_NAME=sign402_payment_approval
SIGN402_WHATSAPP_TEMPLATE_LANGUAGE=en_US
SIGN402_WHATSAPP_GRAPH_API_VERSION=v25.0
```

Hermes retains its own `WHATSAPP_CLOUD_*` configuration, including the App
Secret and webhook Verify Token. During development, Meta's recipient list is
restricted to approved test numbers. In a public beta, Hermes may accept
WhatsApp inbound events broadly only because the Sign402 pre-dispatch handler
consumes every WhatsApp event and drops non-pairing/non-decision text before it
can reach the general agent.

## Failure Handling and Security

- Template delivery failure changes the approval to `delivery_failed`; no funds
  move and Telegram receives a safe failure message.
- A Meta webhook retry, repeated button tap, stale template, or unknown payload
  is idempotently rejected.
- The gateway accepts decisions only from the linked `wa_id`, never from a
  phone number contained in message text.
- The Graph API response and Hermes event logs are redacted of access tokens,
  full wallet secrets, and private payment data.
- The Meta template is utility-only and does not contain seed phrases, private
  keys, one-time login codes, or complete recipient account numbers.
- Existing spend limits, purchase idempotency, and transaction signing checks
  remain mandatory after approval.

## Verification

Automated tests cover:

1. WhatsApp pairing selects the WhatsApp channel and does not create an
   iMessage link.
2. An iMessage-linked user and a WhatsApp-linked user each receive only their
   selected channel's notification.
3. Template payload construction uses safe fields and button payloads bound to
   the pending approval ID.
4. Approve, reject, expired, duplicate, wrong-user, wrong-channel, and failed
   delivery cases all fail closed as appropriate.
5. The Hermes pre-dispatch handler consumes ordinary WhatsApp text and never
   forwards it to the general agent.

Manual production verification uses Meta's test number and one allowlisted
WhatsApp recipient: link from Telegram, initiate a low-value purchase, approve
by button, repeat with reject, and confirm that an expired request cannot be
approved.

## Out of Scope

- General-purpose AI chat in WhatsApp.
- A second simultaneous approval channel for one user.
- Group chats, media approvals, and voice-message decisions.
- Public-beta onboarding before the Meta utility template is approved.
