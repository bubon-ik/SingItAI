# SingIt Telegram Social Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy Telegram link-preview image with the user-supplied current SingIt mark and publish complete, cache-busting Open Graph metadata.

**Architecture:** Produce one immutable `1200 x 630` PNG from the approved source by cropping only black background and resizing proportionally. Point the static landing page's Open Graph and Twitter tags at its absolute production URL, then rely on the existing Cloudflare Pages Git deployment.

**Tech Stack:** Static HTML, PNG, macOS `sips`, Git, Cloudflare Pages

## Global Constraints

- Source artwork: `/Users/mp/Downloads/Jul 6, 2026, 05_45_29 PM.png`.
- Do not redraw, recolor, stretch, add text, add effects, or generate elements.
- Output must be exactly `1200 x 630` pixels.
- Published asset name must be `website/assets/singit-social-preview-v2.png`.
- Production image URL must be `https://singitai.app/assets/singit-social-preview-v2.png`.
- Existing page title and description remain unchanged.

---

### Task 1: Produce the wide social image

**Files:**
- Create: `website/assets/singit-social-preview-v2.png`

**Interfaces:**
- Consumes: the approved `1254 x 1254` source PNG.
- Produces: an immutable `1200 x 630` PNG used by the page metadata in Task 2.

- [x] **Step 1: Generate a crop that preserves the artwork**

Run:

```bash
sips --cropToHeightWidth 658 1254 --cropOffset 285 0 \
  '/Users/mp/Downloads/Jul 6, 2026, 05_45_29 PM.png' \
  --out website/assets/singit-social-preview-v2.png
sips --resampleHeightWidth 630 1200 website/assets/singit-social-preview-v2.png
```

Expected: only empty black space above and below the visible mark is removed; the mark itself remains intact and proportional.

- [x] **Step 2: Verify dimensions and inspect the result**

Run:

```bash
sips -g pixelWidth -g pixelHeight website/assets/singit-social-preview-v2.png
```

Expected:

```text
pixelWidth: 1200
pixelHeight: 630
```

Open the result with the workspace image viewer and confirm balanced black spacing around the complete white mark.

### Task 2: Publish complete social metadata

**Files:**
- Modify: `website/index.html:8-12`

**Interfaces:**
- Consumes: `website/assets/singit-social-preview-v2.png` from Task 1.
- Produces: absolute Open Graph and Twitter image metadata discoverable by link-preview crawlers.

- [x] **Step 1: Replace the legacy metadata block**

Replace the existing Open Graph image and Twitter-card declarations with:

```html
<meta property="og:url" content="https://singitai.app/">
<meta property="og:image" content="https://singitai.app/assets/singit-social-preview-v2.png">
<meta property="og:image:secure_url" content="https://singitai.app/assets/singit-social-preview-v2.png">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="SingIt circuit-check mark on a black background">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://singitai.app/assets/singit-social-preview-v2.png">
<meta name="twitter:image:alt" content="SingIt circuit-check mark on a black background">
```

- [x] **Step 2: Verify the local metadata and diff**

Run:

```bash
rg -n 'og:(url|image)|twitter:(card|image)' website/index.html
git diff --check -- website/index.html
git diff -- website/index.html
```

Expected: all image references use the absolute versioned production URL, dimensions are `1200 x 630`, and no whitespace errors are reported.

- [x] **Step 3: Commit the implementation**

Run:

```bash
git add website/index.html website/assets/singit-social-preview-v2.png
git commit -m "Update SingIt social preview"
```

Expected: one commit containing only the HTML metadata change and new PNG.

### Task 3: Deploy and verify production

**Files:**
- No additional file changes.

**Interfaces:**
- Consumes: the committed static-site update on branch `x402Bnkr`.
- Produces: live metadata and image at `https://singitai.app/`.

- [ ] **Step 1: Push the production branch**

Run:

```bash
git push singitai x402Bnkr
```

Expected: the remote branch advances to the implementation commit and triggers the configured Cloudflare Pages deployment.

- [ ] **Step 2: Verify the deployed image and metadata after Cloudflare finishes**

Run:

```bash
curl -fsSI https://singitai.app/assets/singit-social-preview-v2.png
curl -fsS https://singitai.app/ | rg 'og:(url|image)|twitter:(card|image)'
```

Expected: the image returns HTTP `200`, and the live page exposes the absolute `singit-social-preview-v2.png` URL plus `1200 x 630` metadata.

- [ ] **Step 3: Validate Telegram with a new message**

Send `https://singitai.app/` in a new Telegram message after deployment. Expected: the new black-and-white wide preview appears; an already-sent message may retain its original cached preview.
