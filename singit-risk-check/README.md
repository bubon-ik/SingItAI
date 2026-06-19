# SINGIT Risk Check

SINGIT-paid Sign402 risk analysis for x402 payment requirements.

This is a Bankr x402 Cloud service. It exposes one paid endpoint:

```text
POST https://x402.bankr.bot/<your-wallet>/paid-risk-check
```

Bankr service names cannot contain `/`, so the deployable service is `paid-risk-check`. It is the Bankr Cloud version of the product endpoint `POST /paid/risk-check`.

## Payment

The endpoint charges `10 SINGIT` per request on Base.

```json
{
  "currency": "SINGIT",
  "tokenAddress": "0xc2c1e0b7C401e6217193732272444D928646eba3",
  "price": "10"
}
```

`SINGIT` is a custom ERC-20, so Bankr/x402 uses the Permit2 rail. A payer may need a one-time Permit2 approval before their first payment. Later payments are signed x402 payments.

## Request

Analyze raw payment requirements directly:

```json
{
  "paymentRequirements": {
    "scheme": "upto",
    "network": "eip155:8453",
    "asset": "0xc2c1e0b7C401e6217193732272444D928646eba3",
    "maxAmountRequired": "10000000000000000000",
    "payTo": "0x1111111111111111111111111111111111111111",
    "resource": "https://merchant.example/protected",
    "extra": {
      "nonce": "demo-1",
      "assetTransferMethod": "permit2"
    }
  }
}
```

Or ask the service to inspect a URL by fetching its unpaid x402 response:

```json
{
  "url": "https://merchant.example/protected"
}
```

## Response

```json
{
  "ok": true,
  "product": "sign402-risk-check",
  "riskLevel": "low",
  "summary": {
    "network": "eip155:8453",
    "scheme": "upto",
    "asset": {
      "address": "0xc2c1e0b7c401e6217193732272444d928646eba3",
      "symbol": "SINGIT",
      "decimals": 18,
      "transferMethod": "permit2"
    },
    "amount": {
      "atomic": "10000000000000000000",
      "display": "10 SINGIT"
    },
    "receiver": "0x1111111111111111111111111111111111111111",
    "resource": "https://merchant.example/protected"
  },
  "checks": [
    {
      "name": "network_base",
      "status": "pass",
      "message": "Payment is on Base."
    }
  ],
  "recommendation": "Payment looks acceptable for a bounded Sign402 policy, but still require normal approval."
}
```

## Local Test

```bash
npm test
```

## Deploy

From this directory:

```bash
bankr login
bankr x402 deploy paid-risk-check
```

Bankr reads `bankr.x402.json`, bundles `x402/paid-risk-check/index.ts`, and wraps the handler with x402 payment enforcement.
