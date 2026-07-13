# WhatsApp Template Parameter Normalization Design

## Problem

Meta rejects the SingIt approval template request with error `132018` because
the value bound to body variable `{{1}}` contains newline characters. The
approval service currently joins individual context lines with `\n`, which is
valid for iMessage but invalid for a WhatsApp template text parameter. The
failed delivery leaves the Bitrefill order in `USER_REJECTED`; no wallet
funding occurs.

## Design

Normalize only the WhatsApp template body parameter in
`MetaWhatsAppTemplateNotifier`. Preserve the existing context-line list and
iMessage rendering. Collapse whitespace inside each context item, retain the
existing length limits, and join items with ` | ` so the bound value is a
single Meta-compatible line.

Do not change the approved template name, language, variables, buttons,
payment flow, approval IDs, or order-state transitions.

When an approval cannot be delivered, return its existing safe failure text
to Telegram instead of letting the client replace the structured rejection
with `Wallet service returned an invalid response.`

## Data Flow

1. Bitrefill creates structured approval context lines.
2. iMessage continues receiving its existing multiline message.
3. WhatsApp notifier converts the lines into one body parameter separated by
   ` | `.
4. Meta accepts the template request and returns a message ID.
5. If delivery still fails, the nested approval failure text is promoted to
   the top-level Telegram response; wallet funding remains blocked.

## Verification

- Regression test: WhatsApp body variable contains no newline or tab.
- Regression test: multiple context items are separated by ` | `.
- Regression test: an approval delivery failure surfaces its safe Telegram
  message and does not start wallet funding.
- Run the complete gateway, Hermes plugin, and CDP Node test suites.

## Success Criteria

- The exact Bitrefill approval context previously rejected with Meta error
  `132018` is serialized as a single line.
- A failed delivery cannot be displayed as an invalid wallet-service response.
- No payment or approval behavior changes outside WhatsApp delivery formatting
  and error presentation.
