# SingIt Solana

- This is an independent repository. Do not change the existing Berlin Hack project.
- First milestone: Venice x402 on Solana mainnet, including wallet authentication, balance, approved top-up, and chat.
- Never execute a real top-up without explicit approval of its exact quote. Do not automatically retry an uncertain payment.
- Do not log or commit keys, authentication headers, signed payment payloads, or wallet files.
- Keep wallet and operation state under the ignored `.local/` directory with private permissions.
- Tests use generated unfunded wallets and local fixtures. No test may send a real payment.
- PayAI and the stock merchant are future integrations, not dependencies of the Venice client.
