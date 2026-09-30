// The SingIt app's service worker: what makes the page installable as an app, and what an
// installed app shows when there is no network.
//
// It caches nothing. The page already versions its files (web_api.page_version) and moves an
// open tab to a new release (reloadIfStale in main.js); a second cache here would only let an
// old page outlive a deploy. Everything the app does — the chat, purchases, the wallet — needs
// the network anyway, so offline the app says so instead of showing a browser error.

const OFFLINE = `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#050505">
<title>SingIt — offline</title>
<style>
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center; padding: 24px; box-sizing: border-box;
    background: #050505; color: #f2f2ef; font: 16px/1.6 "Helvetica Neue", system-ui, sans-serif; text-align: center; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 8px; }
  p { color: rgba(242, 242, 239, 0.62); margin: 0 0 24px; }
  button { background: #3ecf8e; color: #050505; border: 0; border-radius: 999px; padding: 12px 24px;
    font-family: inherit; font-size: 15px; font-weight: 600; cursor: pointer; }
</style></head>
<body><main>
  <h1>You are offline</h1>
  <p>SingIt needs the internet to talk to your agent. Nothing was paid or changed.</p>
  <button onclick="location.reload()">Try again</button>
</main></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Only page loads are answered here; API calls, scripts and wallet traffic go to the network
// untouched, as if there were no service worker.
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
