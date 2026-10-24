# Installing the SingIt MCP server

SingIt runs a remote MCP server at `https://app.singitai.app/mcp` (Streamable HTTP, JSON responses
only). Tools: free price/model lookups, plus paid web search, page reading and market data, billed per
call to your SingIt API key's own daily and per-request caps (Base USDC). Details: `sign402-gateway/README.md#mcp-server-remote-for-claude-code-cursor-codex`.

## 1. Get an API key

Create one in the SingIt app at <https://app.singitai.app/app/> (API Keys). It looks like
`sk_singit_…`. Put it in an environment variable, e.g. `SINGIT_API_KEY`.

## 2. Add the server to your MCP client

Send the key as `Authorization: Bearer <key>` (or the `x-api-key` header, for clients that only support
plain API-key headers).

```json
{
  "mcpServers": {
    "singit": {
      "url": "https://app.singitai.app/mcp",
      "headers": { "Authorization": "Bearer ${env:SINGIT_API_KEY}" }
    }
  }
}
```

This repo's root `.mcp.json` has the same block for clients (like cursor.directory) that auto-detect it.

## 3. Verify

Call `initialize`, then `tools/list`; both need the key. `tools/list` shows every tool; a call checks
the key's scopes (`get_price`, `list_price_symbols` and `list_models` need none; `web_search`/`read_page`
need `search`; the rest need `tools`). A missing or revoked key gets `401`, never a silent empty tool list.
