# SingIt Landing Page — Design

**Date:** 2026-07-12
**Status:** Approved by user in session

## Goal

A single-page product landing for **SingIt** — the consumer-facing brand of the Sign402 / Hermes stack: messaging-based wallet and payment approvals for AI agents (Telegram / WhatsApp / iMessage), hardware-in-the-loop Firefly signing, x402 payments on Algorand.

Audience: builders and the x402 / agentic-commerce crowd, plus hackathon judges and visitors. The page must explain in ~30 seconds that SingIt is the safe way to give an AI agent spending authority, and route visitors to demo / GitHub / Telegram.

Language: English. Delivery: local preview (no deploy yet).

## Structure (one page)

1. **Hero** — keyhole+check logo mark, large serif headline ("Your AI agent can spend. You stay in control." direction), subheadline about messaging-based approvals, CTA.
2. **Problem** — "who gave the agent the wallet keys?" — three bad options (raw keys / approve everything by hand / trust a backend), presented as an elegant list.
3. **How it works** — 4 steps: delegate a limit in chat → approve (Telegram / WhatsApp / iMessage, hardware-in-the-loop Firefly) → agent pays autonomously via x402 on Algorand → every payment carries proof of consent. Includes a chat-approval mockup.
4. **Features** — grid: spending limits & expiry, multi-channel approvals, hardware signing, x402-native, receipts & history.
5. **Built at Berlin Hack** — short block on the stack (x402 · Algorand · Firefly) + links.
6. **Footer** — privacy policy link (existing GitHub Pages), contact email.

## Visual direction: Premium editorial

Strictly follows existing brand assets in `assets/brand/`:

- cream "paper" background (tone from the X header, ~#faf6ec family), subtle blueprint sketch strokes;
- dark ink serif for headlines (SingIt wordmark style);
- deep green for checks/accents, thin gold divider lines;
- feel: "a private bank for AI agents" — trust and control.

## Technical

- One static `website/index.html` + CSS/JS, no build step.
- Brand assets copied/referenced from `assets/brand/`.
- Verified live in the preview browser (desktop + mobile widths).

## Out of scope

Deployment, docs pages, blog, CMS, analytics.
