# SingIt Telegram Social Preview — Design

**Date:** 2026-07-18
**Status:** Approved by user in session

## Goal

Replace the obsolete beige SingIt link preview with the current black-and-white circuit-check brand mark supplied by the user. The preview must render predictably as a wide social card in Telegram while preserving the logo artwork exactly.

## Selected direction

- Use `/Users/mp/Downloads/Jul 6, 2026, 05_45_29 PM.png` as the source artwork.
- Keep the black background and white mark unchanged: no redraw, recoloring, text, effects, or generated elements.
- Adapt the square source to a `1200 x 630` social-card canvas by removing only empty black space above and below the mark, with balanced breathing room around the visible artwork.
- Preserve the source aspect ratio during the final resize so the mark is not stretched.

## Metadata

- Publish the new image under a versioned filename: `website/assets/singit-social-preview-v2.png`.
- Set `og:image` and `twitter:image` to the absolute production URL `https://singitai.app/assets/singit-social-preview-v2.png`.
- Add the Open Graph image type, width, height, and alt-text metadata.
- Keep the existing page title and description unless testing reveals an unrelated metadata issue.

The versioned image URL is intentional: it gives link-preview crawlers a new resource instead of reusing the cached legacy image URL.

## Verification

- Confirm the generated asset is exactly `1200 x 630` and remains visually faithful to the supplied source.
- Confirm the local HTML contains the absolute versioned image URL and complete image metadata.
- Push the site change to the production branch and wait for the Cloudflare Pages deployment.
- Confirm the production page and image both return successfully and the live HTML exposes the new metadata.
- Existing Telegram messages are not expected to rewrite themselves; validate with a newly sent link after deployment.

## Out of scope

- Changing the SingIt logo or landing-page design.
- Adding copy to the social image.
- Modifying DNS or the custom-domain configuration.
