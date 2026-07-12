# SingIt Consumer Commerce Landing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current developer-oriented dark-tech SingIt landing with an English consumer-commerce site that explains what people can buy and converts visitors into users of `https://t.me/SingIt0qk_bot`.

**Architecture:** Keep the existing zero-build static site and replace its content, visual system, and progressive-enhancement script in place. `index.html` owns semantic content and all non-JavaScript states, `style.css` owns the responsive cream/green design system, and `app.js` adds optional GSAP motion and the purchase-conversation sequence without hiding baseline content. A small Python standard-library test suite protects copy, CTA, semantic, CSS, and enhancement contracts.

**Tech Stack:** Static HTML5, CSS Grid/Flexbox, vanilla JavaScript, GSAP 3.12.5 + ScrollTrigger already loaded from CDN, Python 3 `unittest`, local `http.server`, in-app browser visual verification.

## Global Constraints

- Keep the existing static stack: `website/index.html`, `website/style.css`, and `website/app.js`.
- Do not migrate frameworks, add a package manager dependency, or add a build step.
- Reuse GSAP and ScrollTrigger already loaded by the page; do not add another animation library.
- Keep all user-facing site language in English.
- Use `https://t.me/SingIt0qk_bot` for every **Start in Telegram** CTA.
- Retain the SingIt circuit-check mark and the existing privacy, data-deletion, and contact links.
- Advertise only gift cards, mobile top-ups, and data eSIMs; state that products and brands vary by region.
- Do not lead with hardware, Firefly, hackathon, x402, Algorand, policy JSON, wallet-key fear, or developer tooling.
- Use warm cream surfaces, deep green text, one restrained emerald accent, Clash Display, and Geist.
- Use transform and opacity only for animation; all content must remain visible without JavaScript, GSAP, or motion.
- Support approximately 1280 × 720 desktop and 390 × 844 mobile without horizontal overflow.

---

## File Map

- `website/index.html` — complete consumer narrative, semantic sections, real Telegram destinations, SEO metadata, and static purchase-demo content.
- `website/style.css` — cream/green tokens, editorial layout, product tiles, Telegram conversation UI, responsive rules, focus states, and reduced-motion behavior.
- `website/app.js` — optional conversation progression, GSAP reveal orchestration, and cleanup-safe responsive ScrollTrigger setup.
- `website/tests/test_landing.py` — standard-library regression tests for copy, destinations, semantics, unsupported claims, CSS contracts, and enhancement safety.

### Shared DOM Contracts

Later tasks depend on these selectors introduced by Task 1:

- `#buy`, `#how`, `#demo`, `#safety` — anchorable narrative sections.
- `.telegram-cta` — every direct bot link.
- `[data-hero-reveal]`, `[data-reveal]`, `[data-batch]` — optional GSAP entry targets.
- `[data-conversation]` containing `[data-message]` items — purchase demonstration.
- `[data-demo-status]` — accessible live status for the optional conversation sequence.
- `.product-tile`, `.step`, `.safety-point` — stable presentation hooks.

---

### Task 1: Consumer Content and Semantic Page Structure

**Files:**
- Create: `website/tests/test_landing.py`
- Modify: `website/index.html`
- Test: `website/tests/test_landing.py`

**Interfaces:**
- Consumes: existing `website/assets/singit-mark.svg`, existing legal/contact destinations, and the global constraints above.
- Produces: all shared DOM contracts listed in the file map; four or more `.telegram-cta` links with exact destination `https://t.me/SingIt0qk_bot`.

- [ ] **Step 1: Write the failing HTML contract tests**

Create `website/tests/test_landing.py` with:

```python
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "app.js").read_text(encoding="utf-8")


class LandingContentTests(unittest.TestCase):
    def test_page_is_consumer_commerce_first(self):
        required = (
            "Ask for it. SingIt buys it.",
            "Gift cards",
            "Mobile top-ups",
            "Data eSIMs",
            "Products and brands vary by region.",
            "Your next purchase starts with a message.",
        )
        for text in required:
            self.assertIn(text, HTML)

    def test_primary_ctas_use_real_telegram_bot(self):
        links = re.findall(
            r'<a[^>]+class="[^"]*telegram-cta[^"]*"[^>]+href="([^"]+)"',
            HTML,
        )
        self.assertGreaterEqual(len(links), 4)
        self.assertEqual(set(links), {"https://t.me/SingIt0qk_bot"})

    def test_required_sections_are_anchorable(self):
        for section_id in ("buy", "how", "demo", "safety"):
            self.assertRegex(HTML, rf'<section[^>]+id="{section_id}"')

    def test_developer_first_copy_is_removed(self):
        forbidden = (
            "Built at Berlin Hack",
            "policy.json",
            "x402 crossed",
            "hardware in the loop",
            "Hand over your keys",
        )
        for text in forbidden:
            self.assertNotIn(text, HTML)

    def test_legal_and_contact_links_remain(self):
        self.assertIn("https://bubon-ik.github.io/SingItAI/", HTML)
        self.assertIn("https://bubon-ik.github.io/SingItAI/data-deletion.html", HTML)
        self.assertIn("mailto:Exkalibur1919@proton.me", HTML)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the HTML tests and verify they fail**

Run:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py' -v
```

Expected: failures for missing consumer copy, missing `#buy`/`#demo`/`#safety`, and missing Telegram bot links.

- [ ] **Step 3: Replace the document body with the approved consumer narrative**

Keep the existing SVG symbol definition and font/CDN includes, update the metadata, then implement this exact semantic structure and copy in `website/index.html`:

```html
<title>SingIt — Your shopping agent in Telegram</title>
<meta name="description" content="Ask SingIt to buy gift cards, top up a mobile, or find a travel eSIM. Review the price and complete the purchase in Telegram.">
<meta property="og:title" content="SingIt — Ask for it. SingIt buys it.">
<meta property="og:description" content="Gift cards, mobile top-ups, and travel eSIMs through a simple Telegram conversation.">
<meta property="og:type" content="website">
```

Use this body structure after the hidden symbol SVG:

```html
<a class="skip-link" href="#main">Skip to content</a>
<div class="paper-grain" aria-hidden="true"></div>

<header class="nav-wrap">
  <div class="nav-shell">
    <a class="brand" href="#top" aria-label="SingIt home">
      <svg class="mark" aria-hidden="true"><use href="#singit-mark"></use></svg>
      <span>SingIt</span>
    </a>
    <nav class="nav-links" aria-label="Primary navigation">
      <a href="#buy">What you can buy</a>
      <a href="#how">How it works</a>
      <a href="#safety">Safety</a>
    </nav>
    <a class="btn btn-primary btn-small telegram-cta" href="https://t.me/SingIt0qk_bot" target="_blank" rel="noopener">
      Start in Telegram <span class="btn-orb" aria-hidden="true">↗</span>
    </a>
  </div>
</header>

<main id="main">
  <section class="hero" id="top">
    <div class="shell hero-grid">
      <div class="hero-copy" data-hero-reveal>
        <p class="eyebrow">Your shopping agent in Telegram</p>
        <h1>Ask for it.<br><em>SingIt buys it.</em></h1>
        <p class="hero-sub">Gift cards, mobile top-ups, and travel eSIMs — found and purchased through a simple Telegram conversation.</p>
        <div class="hero-actions">
          <a class="btn btn-primary telegram-cta" href="https://t.me/SingIt0qk_bot" target="_blank" rel="noopener">Start in Telegram <span class="btn-orb" aria-hidden="true">↗</span></a>
          <a class="text-link" href="#buy">See what you can buy <span aria-hidden="true">↓</span></a>
        </div>
        <p class="availability-note">No new app to learn. Products and brands vary by region.</p>
      </div>
      <div class="hero-demo" data-hero-reveal>
        <div class="phone-shell" aria-label="Example SingIt Telegram purchase">
          <div class="phone-top"><span class="status-dot" aria-hidden="true"></span><div><strong>SingIt</strong><span>bot</span></div></div>
          <div class="message message-user">Buy me a €25 Apple gift card.</div>
          <div class="message message-bot">I found an Apple Gift Card for €25. Review the purchase?</div>
          <div class="purchase-result"><span>Apple Gift Card</span><strong>€25.00</strong><small>Ready to confirm</small></div>
        </div>
      </div>
    </div>
  </section>

  <section class="catalog" id="buy">
    <div class="shell">
      <div class="section-heading" data-reveal><p class="eyebrow">What you can buy</p><h2>Everyday digital essentials, one message away.</h2><p>Tell SingIt what you need. It searches supported Bitrefill products and brings the best match back to your chat.</p></div>
      <div class="product-grid" data-batch>
        <article class="product-tile product-gift"><p class="tile-label">Gift cards</p><h3>Shop, play, stream, travel.</h3><p>Choose from supported shopping, gaming, entertainment, and travel brands.</p><blockquote>“Buy me a €25 Apple gift card.”</blockquote></article>
        <article class="product-tile product-mobile"><p class="tile-label">Mobile top-ups</p><h3>Add airtime without leaving Telegram.</h3><p>Top up prepaid mobile service for supported operators and regions.</p><blockquote>“Top up my mobile with €10.”</blockquote></article>
        <article class="product-tile product-esim"><p class="tile-label">Data eSIMs</p><h3>Land connected.</h3><p>Find a travel data plan for supported countries and regions.</p><blockquote>“Find me an eSIM for Spain.”</blockquote></article>
      </div>
      <p class="region-disclaimer">Products and brands vary by region.</p>
    </div>
  </section>

  <section class="how" id="how">
    <div class="shell">
      <div class="section-heading" data-reveal><p class="eyebrow">How it works</p><h2>From message to purchase in three steps.</h2></div>
      <ol class="steps" data-batch>
        <li class="step"><span>01</span><h3>Ask</h3><p>Message SingIt with the item, value, or destination you need.</p></li>
        <li class="step"><span>02</span><h3>Confirm</h3><p>Review the selected product and exact amount before payment.</p></li>
        <li class="step"><span>03</span><h3>Receive</h3><p>Get the gift-card code, mobile refill, or eSIM details in Telegram.</p></li>
      </ol>
    </div>
  </section>

  <section class="purchase-demo" id="demo">
    <div class="shell demo-grid">
      <div class="demo-copy" data-reveal><p class="eyebrow">See a purchase happen</p><h2>A real shopping flow, without the checkout maze.</h2><p>SingIt keeps the search, review, confirmation, and delivery in one conversation.</p><a class="text-link telegram-cta" href="https://t.me/SingIt0qk_bot" target="_blank" rel="noopener">Try this in Telegram <span aria-hidden="true">↗</span></a></div>
      <div class="conversation-shell" data-conversation aria-label="Gift card purchase conversation">
        <div class="conversation-head"><svg class="mark" aria-hidden="true"><use href="#singit-mark"></use></svg><div><strong>SingIt</strong><span>shopping agent</span></div></div>
        <div class="message message-user" data-message>Buy me a €25 Apple gift card.</div>
        <div class="message message-bot" data-message>I found a €25 Apple Gift Card available in your region.</div>
        <div class="message message-bot message-review" data-message><span>Apple Gift Card</span><strong>€25.00</strong><small>Confirm before purchase</small></div>
        <div class="message message-user message-confirm" data-message>Confirm purchase</div>
        <div class="message message-bot message-success" data-message><strong>Purchase complete</strong><span>Your redemption details are ready in Telegram.</span></div>
        <p class="sr-only" data-demo-status aria-live="polite">Purchase demonstration ready.</p>
      </div>
    </div>
  </section>

  <section class="safety" id="safety">
    <div class="shell safety-grid">
      <div class="section-heading" data-reveal><p class="eyebrow">You stay in control</p><h2>Nothing is purchased behind your back.</h2></div>
      <div class="safety-list" data-batch>
        <article class="safety-point"><span>01</span><h3>See the exact amount</h3><p>Review the product and total before payment.</p></article>
        <article class="safety-point"><span>02</span><h3>Confirm the purchase</h3><p>SingIt waits for your approval before it completes the order.</p></article>
        <article class="safety-point"><span>03</span><h3>Keep a clear record</h3><p>Completed purchases remain available in your account history.</p></article>
      </div>
    </div>
  </section>

  <section class="infrastructure"><div class="shell infrastructure-inner"><p>Purchases are fulfilled through Bitrefill using secure payment infrastructure.</p><p>Products and brands vary by region.</p></div></section>

  <section class="final-cta"><div class="shell shell-narrow"><h2 data-reveal>Your next purchase starts with a message.</h2><p data-reveal>Tell SingIt what you need and continue in Telegram.</p><a class="btn btn-primary btn-large telegram-cta" data-reveal href="https://t.me/SingIt0qk_bot" target="_blank" rel="noopener">Start in Telegram <span class="btn-orb" aria-hidden="true">↗</span></a></div></section>
</main>

<footer class="footer"><div class="shell footer-inner"><a class="brand brand-footer" href="#top"><svg class="mark" aria-hidden="true"><use href="#singit-mark"></use></svg><span>SingIt</span></a><nav class="footer-links" aria-label="Footer navigation"><a href="https://bubon-ik.github.io/SingItAI/" target="_blank" rel="noopener">Privacy policy</a><a href="https://bubon-ik.github.io/SingItAI/data-deletion.html" target="_blank" rel="noopener">Data deletion</a><a href="mailto:Exkalibur1919@proton.me">Contact</a></nav><p class="footer-fine">© 2026 SingIt</p></div></footer>
```

- [ ] **Step 4: Run the HTML tests and verify they pass**

Run:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py' -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit the semantic consumer page**

```bash
git add website/index.html website/tests/test_landing.py
git commit -m "Redesign SingIt landing content for consumers"
```

---

### Task 2: Commerce-First Visual System and Responsive Layout

**Files:**
- Modify: `website/tests/test_landing.py`
- Modify: `website/style.css`
- Test: `website/tests/test_landing.py`

**Interfaces:**
- Consumes: Task 1 selectors and semantic hierarchy.
- Produces: stable single-column mobile collapse, cream/green design tokens, visible focus states, and all visual component styles required by Task 3.

- [ ] **Step 1: Add failing CSS contract tests**

Append to `website/tests/test_landing.py`:

```python
class LandingStyleTests(unittest.TestCase):
    def test_approved_palette_and_fonts_are_declared(self):
        required = (
            "--paper: #f4efe3",
            "--ink: #162018",
            "--accent: #35a96f",
            '"Clash Display"',
            '"Geist"',
        )
        for token in required:
            self.assertIn(token, CSS)

    def test_focus_reduced_motion_and_mobile_contracts_exist(self):
        self.assertIn(":focus-visible", CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce)", CSS)
        self.assertIn("@media (max-width: 720px)", CSS)
        self.assertIn("grid-template-columns: 1fr", CSS)

    def test_site_does_not_use_pure_black_or_ai_gradient(self):
        self.assertNotIn("#000000", CSS.lower())
        self.assertNotIn("#000;", CSS.lower())
        self.assertNotIn("purple", CSS.lower())
```

- [ ] **Step 2: Run the CSS tests and verify they fail**

Run the full suite. Expected: `LandingStyleTests` fails because the approved variables are absent.

- [ ] **Step 3: Replace `website/style.css` with the commerce-first system**

Implement these exact tokens and layout rules; preserve the inline SVG grain technique but tint it like paper:

```css
:root {
  --paper: #f4efe3;
  --paper-deep: #e9e1d1;
  --paper-light: #fbf8f1;
  --ink: #162018;
  --ink-soft: #526057;
  --ink-faint: #7b857e;
  --accent: #35a96f;
  --accent-dark: #176b45;
  --line: rgba(22, 32, 24, 0.14);
  --display: "Clash Display", "Geist", sans-serif;
  --sans: "Geist", sans-serif;
  --ease: cubic-bezier(0.32, 0.72, 0, 1);
}
```

Build the stylesheet in this order so each responsibility stays easy to audit:

1. reset, body, selection, `.sr-only`, `.skip-link`, `.shell`, and typography;
2. fixed `.paper-grain` overlay at `opacity: 0.025`;
3. `.btn`, `.btn-primary`, `.btn-orb`, `.text-link`, and visible `:focus-visible` ring;
4. floating `.nav-shell` with a warm translucent surface and no dark full-width navbar;
5. `.hero-grid` at `grid-template-columns: minmax(0, 1.05fr) minmax(420px, .95fr)` and a maximum 1240px shell;
6. `.phone-shell` and message/result surfaces using nested radii, tinted shadows, and no large scrolling `backdrop-filter`;
7. `.product-grid` at six columns with `.product-gift { grid-column: span 3; }`, `.product-mobile, .product-esim { grid-column: span 3; }`, producing a full 2 × 2 composition where the gift tile spans the full first row on widths below 1100px;
8. `.steps` as three border-top-separated columns rather than three floating cards;
9. `.demo-grid` as an asymmetric 5/7 split and `.conversation-shell` as the only elevated demonstration surface;
10. `.safety-grid` as a 5/7 split with a divided list rather than card boxes;
11. `.infrastructure`, `.final-cta`, and `.footer` using the same paper palette;
12. responsive rules at 960px and 720px that collapse every grid to one column, remove rotations, make hero CTAs full-width, and retain a 44px minimum CTA height.

Use these non-negotiable component declarations:

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body { overflow-x: hidden; background: var(--paper); color: var(--ink); font: 400 1rem/1.6 var(--sans); -webkit-font-smoothing: antialiased; }
.shell { width: min(100% - 64px, 1240px); margin-inline: auto; }
.shell-narrow { max-width: 900px; }
h1, h2 { font-family: var(--display); font-weight: 600; letter-spacing: -0.055em; text-wrap: balance; }
h1 { font-size: clamp(3rem, 6vw, 5.8rem); line-height: .93; }
h2 { font-size: clamp(2.35rem, 4.6vw, 4.8rem); line-height: 1; }
h1 em { color: var(--accent-dark); font-style: normal; }
.skip-link { position: fixed; left: 16px; top: -80px; z-index: 80; background: var(--ink); color: var(--paper); padding: 12px 16px; }
.skip-link:focus { top: 16px; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
.hero { min-height: 100dvh; padding: 160px 0 104px; display: grid; align-items: center; }
.hero-grid { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(420px, .95fr); gap: clamp(48px, 7vw, 112px); align-items: center; }
.catalog, .how, .purchase-demo, .safety { padding: 144px 0 168px; }
.product-grid { display: grid; grid-template-columns: repeat(6, 1fr); grid-auto-flow: dense; gap: 18px; }
.product-gift { grid-column: span 3; grid-row: span 2; }
.product-mobile, .product-esim { grid-column: span 3; }
.steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 40px; list-style: none; }
.step, .safety-point { border-top: 1px solid var(--line); padding-top: 20px; }
.demo-grid, .safety-grid { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: clamp(48px, 8vw, 120px); align-items: start; }
.conversation-shell { border: 1px solid rgba(22,32,24,.12); border-radius: 32px; padding: 8px; background: rgba(255,255,255,.42); box-shadow: 0 40px 100px -62px rgba(42,61,48,.42), inset 0 1px 0 rgba(255,255,255,.7); }
.telegram-cta { min-height: 44px; }
@media (max-width: 960px) {
  .hero-grid, .demo-grid, .safety-grid { grid-template-columns: 1fr; }
  .product-gift, .product-mobile, .product-esim { grid-column: span 6; grid-row: auto; }
}
@media (max-width: 720px) {
  .shell { width: min(100% - 40px, 1240px); }
  .nav-links { display: none; }
  .hero { min-height: auto; padding: 140px 0 88px; }
  .hero-grid, .product-grid, .steps, .demo-grid, .safety-grid { grid-template-columns: 1fr; }
  .product-gift, .product-mobile, .product-esim { grid-column: auto; }
  .hero-actions { display: grid; }
  .hero-actions .btn { width: 100%; justify-content: space-between; }
  .catalog, .how, .purchase-demo, .safety { padding: 104px 0 120px; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
}
```

Add these exact component rules after the structural declarations; do not rename selectors or add a generic card abstraction:

```css
.paper-grain { position: fixed; inset: 0; z-index: 70; pointer-events: none; opacity: .025; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E"); }
.mark { display: block; width: 30px; height: 30px; color: var(--accent-dark); }
.btn { display: inline-flex; align-items: center; gap: 12px; min-height: 48px; padding: 8px 9px 8px 22px; border: 1px solid transparent; border-radius: 999px; font: 600 .95rem/1 var(--sans); text-decoration: none; transition: transform .45s var(--ease), background-color .45s var(--ease); }
.btn:active { transform: scale(.98); }
.btn-primary { background: var(--ink); color: var(--paper-light); }
.btn-primary:hover { background: var(--accent-dark); }
.btn-orb { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: rgba(255,255,255,.14); transition: transform .45s var(--ease); }
.btn:hover .btn-orb { transform: translate(2px,-2px) scale(1.04); }
.btn-small { min-height: 44px; padding-left: 18px; font-size: .86rem; }
.btn-small .btn-orb { width: 28px; height: 28px; }
.btn-large { min-height: 56px; padding-left: 28px; }
.text-link { display: inline-flex; gap: 8px; align-items: center; color: var(--ink); font-weight: 600; text-underline-offset: 5px; }
.nav-wrap { position: fixed; inset: 0 0 auto; z-index: 40; display: flex; justify-content: center; padding: 20px 16px 0; pointer-events: none; }
.nav-shell { pointer-events: auto; display: flex; align-items: center; gap: 30px; width: min(100%, 980px); padding: 9px 9px 9px 18px; border: 1px solid rgba(22,32,24,.11); border-radius: 999px; background: rgba(244,239,227,.82); box-shadow: 0 20px 60px -42px rgba(42,61,48,.46), inset 0 1px 0 rgba(255,255,255,.8); backdrop-filter: blur(18px); }
.brand { display: inline-flex; align-items: center; gap: 9px; color: var(--ink); font: 600 1.25rem/1 var(--display); text-decoration: none; }
.nav-links { display: flex; gap: 24px; margin-left: auto; }
.nav-links a, .footer-links a { color: var(--ink-soft); font-size: .9rem; text-decoration: none; transition: color .35s var(--ease); }
.nav-links a:hover, .footer-links a:hover { color: var(--ink); }
.eyebrow { margin-bottom: 22px; color: var(--accent-dark); font-size: .72rem; font-weight: 600; letter-spacing: .16em; text-transform: uppercase; }
.hero-sub { max-width: 46ch; margin: 28px 0 34px; color: var(--ink-soft); font-size: clamp(1.05rem,1.5vw,1.2rem); }
.hero-actions { display: flex; align-items: center; gap: 22px; }
.availability-note, .region-disclaimer { margin-top: 22px; color: var(--ink-faint); font-size: .78rem; }
.hero-demo { position: relative; }
.phone-shell { width: min(100%, 500px); margin-left: auto; padding: 26px; border: 7px solid rgba(22,32,24,.08); border-radius: 40px; background: var(--ink); color: var(--paper-light); box-shadow: 0 70px 140px -80px rgba(31,69,47,.56), inset 0 1px 0 rgba(255,255,255,.12); transform: rotate(2deg); }
.phone-top, .conversation-head { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.phone-top strong, .phone-top span, .conversation-head strong, .conversation-head span { display: block; }
.phone-top span, .conversation-head span { color: rgba(255,255,255,.52); font-size: .78rem; }
.status-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 5px rgba(53,169,111,.14); }
.message { width: fit-content; max-width: 82%; margin-top: 12px; padding: 13px 16px; border-radius: 18px; font-size: .9rem; line-height: 1.45; }
.message-user { margin-left: auto; border-bottom-right-radius: 5px; background: var(--accent); color: #0c2e1e; }
.message-bot { border-bottom-left-radius: 5px; background: rgba(255,255,255,.09); color: var(--paper-light); }
.purchase-result { display: grid; grid-template-columns: 1fr auto; gap: 7px; margin-top: 16px; padding: 18px; border: 1px solid rgba(255,255,255,.11); border-radius: 20px; background: rgba(255,255,255,.055); }
.purchase-result small { grid-column: 1 / -1; color: rgba(255,255,255,.5); }
.section-heading { display: grid; grid-template-columns: minmax(0,1.1fr) minmax(260px,.55fr); gap: 40px; align-items: end; margin-bottom: 62px; }
.section-heading .eyebrow { grid-column: 1 / -1; margin-bottom: -12px; }
.section-heading > p:last-child { color: var(--ink-soft); }
.product-tile { position: relative; min-height: 250px; overflow: hidden; padding: 34px; border-radius: 30px; background: var(--paper-light); box-shadow: inset 0 0 0 1px rgba(22,32,24,.08); }
.product-gift { min-height: 518px; background: linear-gradient(145deg,#173f2c,#10271d); color: var(--paper-light); }
.product-mobile { background: #dce7d8; }
.product-esim { background: #ead9ca; }
.tile-label { margin-bottom: 52px; color: currentColor; font-size: .7rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; opacity: .64; }
.product-tile h3 { max-width: 13ch; margin-bottom: 12px; font: 600 clamp(1.7rem,2.8vw,2.8rem)/1 var(--display); letter-spacing: -.04em; }
.product-tile > p:not(.tile-label) { max-width: 42ch; opacity: .68; }
.product-tile blockquote { position: absolute; inset: auto 28px 28px; padding-top: 16px; border-top: 1px solid currentColor; font-size: .9rem; opacity: .72; }
.how { background: var(--paper-deep); }
.step span, .safety-point span { color: var(--accent-dark); font-size: .75rem; font-weight: 600; }
.step h3, .safety-point h3 { margin: 24px 0 9px; font: 600 1.5rem/1.1 var(--display); }
.step p, .safety-point p, .demo-copy > p { color: var(--ink-soft); }
.purchase-demo { background: var(--ink); color: var(--paper-light); }
.demo-copy { position: sticky; top: 140px; }
.demo-copy h2 { margin-bottom: 24px; }
.demo-copy .eyebrow { color: #74c99a; }
.demo-copy > p { color: rgba(255,255,255,.58); margin-bottom: 28px; }
.demo-copy .text-link { color: var(--paper-light); }
.conversation-shell { background: #213129; padding: 30px; }
.conversation-head .mark { width: 42px; height: 42px; color: #74c99a; }
.message-review, .message-success { display: grid; gap: 6px; }
.message-review small, .message-success span { color: rgba(255,255,255,.55); }
.message-confirm { font-weight: 600; }
.safety-list { display: grid; }
.safety-point { padding-bottom: 36px; }
.infrastructure { padding: 28px 0; border-block: 1px solid var(--line); }
.infrastructure-inner { display: flex; justify-content: space-between; gap: 24px; color: var(--ink-soft); font-size: .82rem; }
.final-cta { padding: 176px 0 164px; text-align: center; }
.final-cta h2 { max-width: 15ch; margin: 0 auto 26px; }
.final-cta p { margin-bottom: 36px; color: var(--ink-soft); }
.footer { padding: 38px 0; border-top: 1px solid var(--line); }
.footer-inner { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
.footer-links { display: flex; gap: 22px; margin-left: auto; }
.footer-fine { color: var(--ink-faint); font-size: .8rem; }
@media (max-width: 960px) {
  .section-heading { grid-template-columns: 1fr; }
  .section-heading .eyebrow { grid-column: auto; }
  .demo-copy { position: static; }
  .phone-shell { margin-inline: auto; }
}
@media (max-width: 720px) {
  h1 { font-size: clamp(3.1rem,15vw,4.4rem); }
  .nav-shell { gap: 12px; justify-content: space-between; }
  .brand { font-size: 1.1rem; }
  .btn-small { padding-left: 15px; }
  .btn-small .btn-orb { display: none; }
  .phone-shell { padding: 20px; border-width: 5px; border-radius: 30px; transform: none; }
  .product-tile, .product-gift { min-height: 330px; padding: 28px; }
  .product-tile blockquote { inset: auto 24px 24px; }
  .steps { gap: 34px; }
  .conversation-shell { padding: 20px; border-radius: 26px; }
  .infrastructure-inner, .footer-inner { align-items: flex-start; flex-direction: column; }
  .footer-links { margin-left: 0; flex-wrap: wrap; }
}
```

- [ ] **Step 4: Run tests and inspect CSS statically**

Run:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py' -v
git diff --check -- website/style.css website/tests/test_landing.py
```

Expected: 8 tests pass and `git diff --check` prints no output.

- [ ] **Step 5: Commit the visual system**

```bash
git add website/style.css website/tests/test_landing.py
git commit -m "Apply SingIt commerce-first visual system"
```

---

### Task 3: Progressive Conversation and Safe GSAP Motion

**Files:**
- Modify: `website/tests/test_landing.py`
- Modify: `website/app.js`
- Test: `website/tests/test_landing.py`

**Interfaces:**
- Consumes: Task 1 `[data-message]`, `[data-demo-status]`, `[data-reveal]`, `[data-batch]`, and hero selectors; Task 2 transition and reduced-motion CSS.
- Produces: `initConversation()` and `initMotion()` private functions inside one IIFE; static content remains the default DOM state.

- [ ] **Step 1: Add failing enhancement-safety tests**

Append:

```python
class LandingEnhancementTests(unittest.TestCase):
    def test_javascript_is_progressive_and_motion_gated(self):
        required = (
            "prefers-reduced-motion: reduce",
            "typeof gsap === 'undefined'",
            "document.visibilityState",
            "data-message",
            "data-demo-status",
            "ScrollTrigger",
        )
        for text in required:
            self.assertIn(text, JS)

    def test_javascript_never_sets_baseline_content_to_display_none(self):
        self.assertNotIn("style.display = 'none'", JS)
        self.assertNotIn('style.display = "none"', JS)
```

- [ ] **Step 2: Run tests and verify the new contract fails**

Run the full suite. Expected: failure for absent conversation/status selectors in the old script.

- [ ] **Step 3: Replace `website/app.js` with progressive enhancement**

Use this full control flow:

```javascript
(function () {
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function initConversation() {
    var conversation = document.querySelector('[data-conversation]');
    var status = document.querySelector('[data-demo-status]');
    if (!conversation || !status || reduceMotion) return;

    var messages = Array.prototype.slice.call(conversation.querySelectorAll('[data-message]'));
    messages.forEach(function (message, index) {
      message.style.setProperty('--message-index', String(index));
    });
    conversation.classList.add('is-sequenced');
    status.textContent = 'Purchase demonstration ready.';
  }

  function initMotion() {
    if (reduceMotion || typeof gsap === 'undefined' || typeof ScrollTrigger === 'undefined') return;
    gsap.registerPlugin(ScrollTrigger);

    gsap.from('[data-hero-reveal] > *', {
      y: 36,
      opacity: 0,
      duration: 0.9,
      ease: 'power3.out',
      stagger: 0.08,
      clearProps: 'transform,opacity'
    });

    gsap.utils.toArray('[data-reveal]').forEach(function (element) {
      gsap.from(element, {
        y: 32,
        opacity: 0,
        duration: 0.8,
        ease: 'power3.out',
        clearProps: 'transform,opacity',
        scrollTrigger: { trigger: element, start: 'top 90%', once: true }
      });
    });

    gsap.utils.toArray('[data-batch]').forEach(function (group) {
      gsap.from(group.children, {
        y: 30,
        opacity: 0,
        duration: 0.72,
        ease: 'power3.out',
        stagger: 0.08,
        clearProps: 'transform,opacity',
        scrollTrigger: { trigger: group, start: 'top 88%', once: true }
      });
    });
  }

  initConversation();

  if (document.visibilityState === 'visible') {
    initMotion();
  } else {
    document.addEventListener('visibilitychange', function onVisibilityChange() {
      if (document.visibilityState !== 'visible') return;
      document.removeEventListener('visibilitychange', onVisibilityChange);
      initMotion();
    });
  }
})();
```

In `website/style.css`, add the conversation sequence without changing baseline visibility:

```css
.is-sequenced [data-message] {
  animation: message-in .55s var(--ease) both;
  animation-delay: calc(var(--message-index) * 180ms);
}
@keyframes message-in {
  from { opacity: 0; transform: translateY(14px); }
  to { opacity: 1; transform: translateY(0); }
}
```

Because the class is added only after the full static DOM exists, failed JavaScript and reduced-motion users still receive complete content.

- [ ] **Step 4: Run tests and verify the enhancement contract passes**

Run:

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py' -v
git diff --check -- website/app.js website/style.css website/tests/test_landing.py
```

Expected: 10 tests pass; no whitespace errors.

- [ ] **Step 5: Commit the progressive motion layer**

```bash
git add website/app.js website/style.css website/tests/test_landing.py
git commit -m "Add progressive SingIt purchase storytelling"
```

---

### Task 4: Browser Verification, Accessibility, and Final Polish

**Files:**
- Modify if verification finds defects: `website/index.html`
- Modify if verification finds defects: `website/style.css`
- Modify if verification finds defects: `website/app.js`
- Modify if a regression contract is needed: `website/tests/test_landing.py`
- Test: `website/tests/test_landing.py`

**Interfaces:**
- Consumes: the completed static page from Tasks 1–3.
- Produces: verified desktop/mobile page with working anchors, links, responsive layout, and graceful no-motion behavior.

- [ ] **Step 1: Run the complete static suite**

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py' -v
git diff --check -- website/index.html website/style.css website/app.js website/tests/test_landing.py
```

Expected: 10 tests pass and no whitespace errors.

- [ ] **Step 2: Start the local site**

```bash
python3 -m http.server 4173 --directory website
```

Expected: `Serving HTTP on ... port 4173` and `http://localhost:4173/` loads the SingIt page.

- [ ] **Step 3: Verify the 1280 × 720 desktop experience in the in-app browser**

At `http://localhost:4173/`, set viewport to 1280 × 720 and verify:

- the nav, headline, primary CTA, and most of the Telegram example are visible in the first viewport;
- the H1 renders in no more than three lines;
- every navigation anchor scrolls to the matching section;
- product tiles show gift cards, mobile top-ups, and data eSIMs without an empty grid cell;
- the purchase conversation is readable before, during, and after animation;
- all four or more **Start in Telegram** links resolve to `https://t.me/SingIt0qk_bot`;
- no console errors are emitted;
- `document.documentElement.scrollWidth === window.innerWidth`.

- [ ] **Step 4: Verify the 390 × 844 mobile experience**

Set viewport to 390 × 844, reload, and verify:

- the navigation brand and Telegram CTA fit on one row;
- there is no horizontal overflow;
- hero copy remains readable and the H1 uses no more than four short lines;
- the main CTA is at least 44px high and visible without precision tapping;
- all asymmetric grids collapse to a single column;
- message bubbles and product tiles fit without clipped copy;
- focus order follows DOM order.

- [ ] **Step 5: Verify reduced motion and JavaScript failure states**

Use the browser to emulate `prefers-reduced-motion: reduce`, reload, and confirm every section is visible with no delayed or looping animation. Then block or temporarily remove the GSAP CDN scripts in a local inspection and confirm the full page and conversation content still render.

Expected: no content has `opacity: 0`, `visibility: hidden`, or off-screen transforms after load in either state.

- [ ] **Step 6: Convert any visual defect into a regression check before fixing it**

For a source-verifiable defect, first add a focused assertion to `website/tests/test_landing.py`, run it to see the failure, then change the smallest relevant HTML/CSS/JS block. For a viewport-only defect, record the exact viewport and selector in the implementation notes, apply the smallest CSS fix, and repeat Steps 3–5.

- [ ] **Step 7: Run final verification**

```bash
python3 -m unittest discover -s website/tests -p 'test_*.py' -v
git diff --check -- website/index.html website/style.css website/app.js website/tests/test_landing.py
git status --short
```

Expected: all tests pass, no whitespace errors, and only intentional website/test changes are present.

- [ ] **Step 8: Commit final responsive and accessibility polish**

```bash
git add website/index.html website/style.css website/app.js website/tests/test_landing.py
git commit -m "Polish SingIt landing responsiveness and accessibility"
```

## Completion Criteria

- The page immediately explains why consumers use SingIt and what they can buy.
- Gift cards, mobile top-ups, and data eSIMs appear before technical infrastructure.
- The real Telegram bot link is the dominant action throughout the page.
- English copy contains no hackathon-first, hardware-first, or developer-first narrative.
- The approved cream/green Commerce First direction is implemented without generic three-card styling or AI-gradient clichés.
- Desktop, mobile, reduced-motion, and GSAP-failure states are visually verified.
- The complete Python test suite passes.
