"use strict";

const CACHE_PREFIX = "pitch-chat-shell-";
// Bump this whenever the app shell changes; older installed PWAs may otherwise
// keep serving a cache-first chat.js forever.
const CACHE_NAME = `${CACHE_PREFIX}v46`;
const SHELL = [
  "/",
  "/static/style.css?v=14",
  "/static/resource-memory.js?v=3",
  "/static/deletions.js?v=2",
  "/static/long-videos.js?v=2",
  "/static/chat.js?v=31",
  "/static/chat-images.js?v=7",
  "/static/chat-videos.js?v=5",
  "/static/work.js?v=9",
  "/manifest.webmanifest",
  "/static/icons/pitch-chat-192.png",
  "/static/icons/pitch-chat-512.png",
];
const SHELL_PATHS = new Set(SHELL);
const shellSources = new Map();
function noteShell(event, source) {
  const client = event.resultingClientId || event.clientId;
  if (!client) return;
  shellSources.set(client, source);
  if (shellSources.size > 32) shellSources.delete(shellSources.keys().next().value);
}
self.addEventListener("message", event => {
  if (event.data?.type !== "PITCH_CHAT_SW_DIAGNOSTICS") return;
  event.source?.postMessage({type: "PITCH_CHAT_SW_DIAGNOSTICS", release: "v46",
    shell_source: shellSources.get(event.source.id) || "unknown"});
});

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await Promise.all(SHELL.map(async path => {
      const request = new Request(new URL(path, self.location.origin), {cache: "reload"});
      const response = await fetch(request);
      if (!response.ok) throw new Error("App shell update failed");
      await cache.put(path, response);
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(name => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
      .map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Do not intercept health/API or non-GET requests at all. The app's API
  // client explicitly sets cache: no-store; these responses never enter SW cache.
  if (url.pathname === "/health" || url.pathname.startsWith("/api/") || request.method !== "GET") return;
  const key = url.pathname + url.search;
  if (!SHELL_PATHS.has(key)) return;

  if (url.pathname === "/") {
    event.respondWith((async () => {
      try {
        const response = await fetch(new Request(request, {cache: "no-store"}));
        if (response.ok && (response.headers.get("content-type") || "").includes("text/html")) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put("/", response.clone());
        }
        noteShell(event, "network");
        return response;
      } catch {
        const cache = await caches.open(CACHE_NAME);
        const shell = await cache.match("/");
        if (shell) { noteShell(event, "cache"); return shell; }
        throw new Error("Pitch-chat HOSTへ接続できません");
      }
    })());
    return;
  }

  // Prefer current assets while online, retaining only the static shell for
  // offline launch. This lets an installed PWA pick up JS/CSS without waiting
  // for the browser's HTTP cache or continuing to serve stale CacheStorage.
  event.respondWith((async () => {
    try {
      const response = await fetch(new Request(request, {cache: "no-store"}));
      if (response.ok) {
        const cache = await caches.open(CACHE_NAME);
        await cache.put(key, response.clone());
      }
      noteShell(event, "network");
      return response;
    } catch (error) {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(key);
      if (cached) { noteShell(event, "cache"); return cached; }
      throw error;
    }
  })());
});
