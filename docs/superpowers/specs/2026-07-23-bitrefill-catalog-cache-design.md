# Bitrefill Catalog Cache Design

## Goal

Make Telegram catalog browsing respond immediately after the first successful load and prevent intermittent upstream delays from making users wait for up to a minute. Keep Bitrefill MCP as the exclusive catalog and purchase route.

## Scope

This change applies only to browse-list results returned by `search-products` for a country catalog. Product details, package pricing, availability checks, invoice creation, payment, and fulfillment remain live requests and are never served from the catalog cache.

## Architecture

`McpBitrefillClient` owns a thread-safe catalog cache keyed by normalized country and the `include_test_products` flag. Each entry contains the complete normalized country catalog, its last successful refresh timestamp, and no secrets or bearer-value data.

The cache has a 10-minute freshness lifetime. Fresh entries return immediately. Stale entries also return immediately while one background refresh updates the entry. Concurrent refreshes for the same key are collapsed into one in-flight request. Refresh failures keep the last successful entry available and are not cached as successful results.

The last successful public catalog snapshot is persisted outside the repository so service restarts do not create a cold cache. Persistence is atomic and contains only public product-list metadata. Invalid, oversized, or incompatible cache files are ignored safely.

## Data Flow

1. Opening the Bitrefill menu or `Browse Catalog` schedules a non-blocking warm-up for the user's selected country.
2. The warm-up asks the gateway for the first catalog page. The gateway loads the complete country catalog through Bitrefill MCP once and stores the normalized snapshot.
3. Selecting `All`, another category, or `Next` asks the gateway for a slice of the same cached country catalog. Category filtering and pagination happen locally.
4. A fresh cache hit returns immediately.
5. A stale cache hit returns the existing products immediately and starts one background refresh.
6. A first-ever cache miss performs one bounded foreground catalog request. Other callers for the same country share that request instead of creating duplicates.

## Latency and Failure Policy

Catalog-only upstream requests use a short timeout instead of the general 60-second commerce timeout. A cold miss therefore fails quickly with the existing retry message instead of leaving the user waiting for a minute. Once any successful snapshot exists, upstream slowness does not delay catalog browsing.

Failures never overwrite a successful snapshot. The cache does not store errors, credentials, recipient data, invoices, prices from product details, purchase responses, or redemption information.

## Telegram Behavior

`Loading catalog…` remains immediate. Warm-up work runs in the background and does not block button processing. Results from stale or superseded Telegram operations remain protected by the existing operation-generation checks.

## Configuration

Defaults:

- fresh lifetime: 600 seconds;
- catalog request timeout: 8 seconds;
- persistent cache path: a service-owned file under the Sign402 data directory;
- bounded maximum cache-file size and product count.

The lifetime, timeout, and path are configurable through non-secret environment settings for operations and testing.

## Testing

Tests must prove:

- a fresh hit makes no new MCP call;
- a stale hit returns immediately and refreshes in the background;
- concurrent cold requests are single-flight;
- refresh failure preserves stale data;
- category and pagination reuse the same country snapshot;
- a persisted snapshot survives client reconstruction;
- malformed or oversized persistence is ignored;
- opening the Telegram catalog schedules warm-up without delaying the reply;
- product details and purchase paths bypass the catalog cache;
- all existing plugin and gateway suites remain green.

Production verification measures the first cold load, a warm category load, pagination, and service health without creating a purchase.
