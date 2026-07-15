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

Keep pairing and selection as separate operations:

- `Connect iMessage` and `Connect WhatsApp` remain first-time pairing actions.
- Add an authenticated localhost operation that selects a requested channel
  only when an `approval_channel_links` row already exists for the same trusted
  Telegram user.
- Add Telegram commands and persistent keyboard buttons for `Use iMessage` and
  `Use WhatsApp`.
- A successful selection updates only `approval_channel_preferences` and adds
  an audit event. It does not alter encrypted link records.
- Selecting an unlinked or unsupported channel fails closed with a fixed,
  non-sensitive Telegram message instructing the user to connect it first.

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

Gateway:

- `POST /agent/approval-channel/select`
- authenticated with the existing iMessage/approval API token
- body: `telegramUserId`, `channel`
- response identifies only the selected channel and safe Telegram text

Telegram:

- `/use_imessage`
- `/use_whatsapp`
- buttons: `Use iMessage`, `Use WhatsApp`

The existing connect commands remain available for users who have not yet
linked the requested channel.

## Data Flow

1. The Telegram pre-dispatch hook captures the trusted Telegram identity.
2. The selected command sends that trusted ID and the fixed channel value to
   the localhost gateway.
3. The gateway verifies the approval API token and validates the channel.
4. The approval service checks that the same Telegram user owns a link for the
   requested channel.
5. In one SQLite transaction, it upserts the preference and records a
   `channel_selected` audit event.
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
trusted Telegram identity, both commands, both keyboard buttons, and error
handling. The complete gateway and plugin suites must pass before deployment.

Production verification uses the authenticated no-funds approval endpoint:
select iMessage and approve once, then select WhatsApp and approve once. The
temporary test endpoint is removed immediately afterward.
