// The SingIt app's service worker: what makes the page installable as an app, what an
// installed app shows when there is no network, and the notifications the server sends
// (sign402_gateway/web_push.py).
//
// It caches nothing. The page already versions its files (web_api.page_version) and moves an
// open tab to a new release (reloadIfStale in main.js); a second cache here would only let an
// old page outlive a deploy. Everything the app does — the chat, purchases, the wallet — needs
// the network anyway, so offline the app says so instead of showing a browser error.

const OFFLINE = `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#f4f2f0">
<title>SingIt — offline</title>
<style>
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center; padding: 24px; box-sizing: border-box;
    background: #f4f2f0; color: #0c0a08; font: 16px/1.5 Inter, system-ui, sans-serif; text-align: center; }
  h1 { font-size: 24px; font-weight: 400; letter-spacing: -0.02em; margin: 0 0 8px; }
  p { color: #6d6c6b; margin: 0 0 24px; }
  button { background: #3ecf8e; color: #0c0a08; border: 0; border-radius: 6px; padding: 12px 20px;
    font-family: inherit; font-size: 15px; font-weight: 400; cursor: pointer; }
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

// A notification from the server: {title, body, url}, encrypted to this device on the way.
self.addEventListener("push", (event) => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
  } catch {
    message = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(self.registration.showNotification(message.title || "SingIt", {
    body: message.body || "",
    icon: "/assets/icons/icon-192.png",
    data: { url: message.url || "/app/" },
  }));
});

// Tapping it opens the app, or brings an open one forward. Only pages of this app are opened.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const wanted = new URL(event.notification.data?.url || "/app/", self.location.origin);
  const url = wanted.origin === self.location.origin && wanted.pathname.startsWith("/app/") ? wanted.href
    : new URL("/app/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((client) => new URL(client.url).pathname.startsWith("/app/"));
    if (open) return open.focus();
    return self.clients.openWindow(url);
  })());
});
