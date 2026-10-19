// span-viewer service worker.
//
// Purpose: satisfy PWA installability. We deliberately do NOT cache
// HTML/API responses — this app is a live view of real-time and historical
// data, so a stale cached body is worse than an offline error. We only
// pre-cache the icon + manifest so the install prompt is reliable.
//
// style.css is intentionally NOT cached here — base.html renders the link
// with a ?v=<content-hash> bust derived from the file at app startup
// (main.py STATIC_V), so the browser HTTP cache handles freshness without
// the SW pinning a stale copy across deploys.
//
// Bump CACHE_VERSION whenever shipped static assets change. The activate
// handler purges old caches so users don't get pinned to a stale version.

const CACHE_VERSION = 'span-viewer-v4';
const SHELL_ASSETS = [
  '/static/icon.svg',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  // Activate the new SW immediately on next page load instead of waiting for
  // every tab to close. Safe here because we don't cache page HTML.
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL_ASSETS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Pages, API endpoints, and HTMX fragments are always network-first with no
  // fallback to a cached body — we'd rather error than show stale energy
  // numbers.
  const isShell = SHELL_ASSETS.includes(url.pathname);
  if (!isShell) return;

  // Static shell assets: cache-first, refresh in background.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_VERSION);
    const cached = await cache.match(req);
    const fetchAndStore = fetch(req).then((resp) => {
      if (resp.ok) cache.put(req, resp.clone());
      return resp;
    }).catch(() => cached);
    return cached || fetchAndStore;
  })());
});
