/* Renée PWA service worker — minimal offline shell cache. */
const CACHE = "renee-shell-v1";
const SHELL = ["/", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // Never intercept the WebSocket or non-GET — let the network handle them.
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname === "/ws") return;

  // Network-first for the shell so updates land quickly; fall back to cache
  // only when offline.
  event.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp && resp.ok && SHELL.includes(url.pathname)) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});
