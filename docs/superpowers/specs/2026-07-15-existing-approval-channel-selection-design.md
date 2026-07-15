# Existing Approval Channel Selection Design

**Date:** 2026-07-15

## Goal

Let a Telegram wallet user choose iMessage or WhatsApp as the active Sign402
approval channel when that channel is already linked. Channel selection must
not start another pairing, remove either phone link, or expose a linked phone
identity.

## Current Problem

The production account already has both iMessage and WhatsApp links. The
Telegram menu only exposes `Connect iMessage` and `Connect WhatsApp`, so trying
to switch back to iMessage starts a new pairing. The gateway correctly rejects
the duplicate identity as already linked, leaving WhatsApp active even though
the iMessage link still exists.

## Chosen Design

Keep pairing and selection separate internally while preserving the existing
Telegram controls:

- `Connect iMessage` and `Connect WhatsApp` remain the only user-facing actions
  for both selecting and initially linking their respective channels.
- The same existing commands and buttons first call an authenticated localhost
  operation that selects a requested channel only when an
  `approval_channel_links` row already exists for the same trusted Telegram
  user.
- If the requested channel is linked, it becomes active immediately and no
  phone prompt or pairing code is created.
- If the requested channel is not linked, the existing first-time connection
  flow continues unchanged.
- A successful selection updates only `approval_channel_preferences` and adds
  an audit event. It does not alter encrypted link records.
- An unlinked channel continues into pairing; an unsupported channel fails
  closed with a fixed, non-sensitive Telegram message.

The selector uses the existing approval API token boundary. The Telegram user
ID comes from the authenticated Telegram event; command arguments cannot
override it.

## Alternatives Considered

1. Re-pair the same number idempotently. This still asks users to send a code
   for a link that already exists and mixes connection with preference.
2. Update the SQLite preference manually. This is unsuitable for normal users,
   bypasses the application API, and provides no reliable audit trail.
3. Delete one link when selecting the other. This would force unnecessary
   re-pairing and make channel switching destructive.

## Interfaces

Gateway internal operation:

- `POST /agent/approval-channel/select-existing`
- authenticated with the existing iMessage/approval API token
- body: `telegramUserId`, `channel`
- response identifies only whether the channel was selected or still requires
  pairing, plus safe Telegram text when selected

Telegram:

- existing `/connect_imessage` and `/connect_whatsapp` commands
- existing `Connect iMessage` and `Connect WhatsApp` buttons

No additional commands or buttons are added.

## Data Flow

1. The Telegram pre-dispatch hook captures the trusted Telegram identity.
2. The connect command sends that trusted ID and its fixed channel value to the
   localhost selector before prompting for a phone or creating a code.
3. The gateway verifies the approval API token, validates the channel, and
   checks whether that Telegram user owns the requested link.
4. When linked, one SQLite transaction upserts the preference and records a
   `channel_selected` audit event. The command returns immediately.
5. When unlinked, the selector reports `requiresPairing` and the plugin runs
   the existing channel-specific connection flow.
6. Future approvals resolve only the selected channel through the existing
   `_linked_approval_channels` path.

## Failure and Security Behavior

- An unlinked channel is never selected.
- A user cannot select a channel for another Telegram identity.
- Unknown channel values are rejected.
- No phone number, digest, encrypted identity, token, or stack trace is
  returned to Telegram.
- Selecting the current channel is idempotent and succeeds without modifying
  link ownership.
- Existing approval replay and channel-binding checks remain unchanged.

## Tests

Gateway service tests cover:

- selecting a linked inactive channel changes only the preference;
- selecting the already active channel is idempotent;
- selecting an unlinked or unsupported channel fails closed;
- both encrypted channel links remain after switching.

HTTP tests cover authentication and fixed safe responses. Plugin tests cover
trusted Telegram identity, selecting through both existing commands and
buttons, falling back to first-time pairing, and error handling. The complete
gateway and plugin suites must pass before deployment.

Production verification uses the authenticated no-funds approval endpoint:
select iMessage and approve once, then select WhatsApp and approve once. The
temporary test endpoint is removed immediately afterward.
