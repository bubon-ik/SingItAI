# SingIt Consumer Commerce Landing — Redesign Design

**Date:** 2026-07-12  
**Status:** Approved in session; awaiting written-spec review

## Goal

Redesign the existing SingIt landing page as the production website for a consumer product. The page must explain within seconds why someone should use SingIt, what they can buy, how the Telegram purchase flow works, and where to start.

The primary conversion is opening the SingIt Telegram bot. The website is informational, but every major narrative path should lead naturally to that action.

## Audience

The primary audience is consumers, not developers, hackathon judges, or infrastructure teams. Visitors may know little about agentic payments, x402, Algorand, or wallets. The page must use familiar shopping language and demonstrate the product through concrete purchase examples.

## Product Positioning

SingIt is a personal shopping agent in Telegram. A user describes what they want, SingIt finds a supported Bitrefill product, presents the item and amount for confirmation, completes the purchase, and returns the result in the conversation.

The central value proposition is:

> Ask for it. SingIt buys it.

Supporting copy:

> Gift cards, mobile top-ups, and travel eSIMs — found and purchased through a simple Telegram conversation.

Security remains an important proof point, but it supports the commerce story rather than replacing it. The page should explain control in plain language: the user sees the product and amount before payment, confirms the purchase, and can review the transaction history.

## Primary Call to Action

The primary CTA label is **Start in Telegram**. It links directly to the production SingIt Telegram bot.

The CTA appears in:

- the desktop and mobile navigation;
- the hero;
- the end of the purchase demonstration;
- the final conversion section.

There is no GitHub CTA in the primary consumer journey. Developer and infrastructure links, if retained, appear only near the footer.

The implementation must use the real Telegram bot URL supplied by the product owner. If that URL is not already present in the repository, implementation must stop and request it instead of inventing a destination.

## Information Architecture

### 1. Navigation

Use a compact floating navigation with the SingIt mark and links to **What you can buy**, **How it works**, and **Safety**. The rightmost action is **Start in Telegram**.

On mobile, secondary links collapse while the brand and Telegram CTA remain immediately visible.

### 2. Hero

The hero uses an editorial split composition:

- left: the value proposition, supporting copy, and primary CTA;
- right: a Telegram conversation paired with a recognizable digital-product result.

The first viewport must show enough of the conversation to communicate the full idea without requiring a scroll. The headline stays within two or three lines on desktop and three or four short lines on mobile.

The hero must not lead with x402 statistics, policy JSON, wallet keys, hardware signing, or hackathon language.

### 3. What You Can Buy

This section appears directly after the hero and explicitly answers what the product supports.

The three supported groups are:

1. **Gift cards** — shopping, gaming, entertainment, and travel.
2. **Mobile top-ups** — prepaid airtime for supported operators.
3. **Data eSIMs** — travel data packages for supported countries and regions.

Each group includes a natural-language Telegram prompt, for example:

- “Buy me a €25 Apple gift card.”
- “Top up my mobile with €10.”
- “Find me an eSIM for Spain.”

Brand examples may include products present in Bitrefill's catalog, but the design must not imply that every brand is available in every country. Include the qualification **Products and brands vary by region.**

Do not advertise bill payments, physical products, prepaid cards, or other categories unless the SingIt agent explicitly supports them at implementation time.

### 4. How It Works

Explain the consumer flow in three steps:

1. **Ask** — message SingIt in Telegram with the item, value, or destination.
2. **Confirm** — review the selected product and exact amount before payment.
3. **Receive** — get the gift-card code, mobile refill, or eSIM details in Telegram.

Technical payment routing is not part of these steps.

### 5. Purchase Demonstration

Show one believable conversation from request to fulfillment. The default example should be a gift-card purchase because it is broadly understandable. The component may cycle through gift-card, mobile-refill, and eSIM examples, but it must remain readable without animation.

The demonstration should show:

- the user's request;
- a short search or match state;
- the selected product and amount;
- explicit confirmation;
- the delivered result.

Do not expose real redemption codes or imitate an unsupported live transaction.

### 6. Safety

Use a compact, plain-language section titled **You stay in control**. It covers:

- the product and amount are visible before purchase;
- the user confirms the purchase;
- the agent does not receive unrestricted spending access;
- the user can review completed purchases.

Hardware approval is out of scope for this version and must not appear in the primary journey.

### 7. Infrastructure Note

A small section near the footer states that purchases are fulfilled through Bitrefill and that SingIt uses secure payment infrastructure. x402, Algorand, Base, Bankr, and other technical details may be linked from a developer-oriented disclosure, but they must not interrupt the consumer story.

Avoid language that falsely implies a commercial partnership, endorsement, or universal product availability.

### 8. Final CTA and Footer

The final CTA uses:

> Your next purchase starts with a message.

The button label is **Start in Telegram**.

The footer retains privacy, data-deletion, contact, and other legally required links. Remove **Built at Berlin Hack** from the production narrative.

## Visual Direction

Use the approved **Commerce First** direction:

- warm cream background with subtle paper grain;
- deep green text and one restrained emerald accent;
- Clash Display for large display typography and Geist for body/UI copy;
- dark ink-like dividers and restrained tinted shadows;
- asymmetric editorial layouts with generous whitespace;
- product tiles and Telegram conversation surfaces instead of abstract infrastructure diagrams;
- no purple/blue AI gradients, generic three-card rows, excessive glass cards, or neon outer glows.

The existing SingIt circuit-check mark remains the primary brand asset.

## Motion and Interaction

Motion intensity is moderate. Use GSAP already present in the project; do not introduce a second animation library.

Approved motion patterns:

- a restrained hero entrance;
- staggered product-category reveals;
- a readable conversation progression;
- subtle product-result transitions;
- tactile hover and pressed states on CTAs.

Animations use transform and opacity only. Content must be visible and understandable when JavaScript is unavailable or `prefers-reduced-motion` is enabled. Scroll-triggered elements must not remain nearly invisible in full-page captures or when users scroll quickly.

## Responsive and Accessibility Requirements

- Use a single-column layout below the mobile breakpoint.
- Prevent horizontal overflow at all supported widths.
- Keep the primary Telegram CTA visible in mobile navigation.
- Use visible keyboard focus states and semantic landmarks.
- Preserve readable contrast for cream, green, and muted text combinations.
- Provide meaningful alternative text for product imagery.
- Maintain touch targets of at least 44 by 44 CSS pixels.
- Do not rely on animation alone to communicate purchase state.

## Technical Constraints

- Keep the existing static stack: `website/index.html`, `website/style.css`, and `website/app.js`.
- Do not migrate frameworks or add a build step.
- Reuse GSAP and ScrollTrigger already loaded by the page.
- Verify all external links and the real Telegram bot destination.
- Keep the site language entirely in English.
- Preserve privacy, data-deletion, and contact links.
- Do not make unrelated gateway or agent changes as part of the landing-page redesign.

## Verification

Before completion, verify:

- desktop at approximately 1280 × 720;
- mobile at approximately 390 × 844;
- no horizontal overflow;
- primary Telegram CTAs all use the real bot URL;
- anchor navigation works;
- motion and purchase-demo states work;
- `prefers-reduced-motion` leaves all content visible;
- page content remains complete if GSAP fails to load;
- product claims match supported SingIt and Bitrefill capabilities;
- the page contains no hackathon-first, hardware-first, or developer-first messaging.

## Out of Scope

- hardware approval and Firefly education;
- a live product catalog embedded in the website;
- account creation or checkout on the website;
- bill payments, physical products, and unsupported Bitrefill categories;
- deployment, analytics, localization, CMS, blog, and developer documentation redesign.
