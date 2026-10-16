const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const handlers = {};
const cacheMap = new Map();
const writes = [];
const networkRequests = [];
const deletions = [];
let claimed = 0;
let skippedWaiting = 0;
let networkFailure = false;
const cache = {
  async match(request) { return cacheMap.get(typeof request === "string" ? request : new URL(request.url).pathname); },
  async put(request, response) {
    const key = typeof request === "string" ? request : new URL(request.url).pathname;
    writes.push(key);
    cacheMap.set(key, response);
  },
};
const sandbox = {
  URL,
  Request,
  self: {
    location: {origin: "https://pitch-chat.test"}, clients: {async claim() { claimed++; }},
    async skipWaiting() { skippedWaiting++; }, addEventListener(name, handler) { handlers[name] = handler; },
  },
  caches: {
    async open() { return cache; }, async match(request) { return cache.match(request); },
    async keys() { return ["pitch-chat-shell-v1", "pitch-chat-shell-v2", "pitch-chat-shell-v3", "pitch-chat-shell-v4",
      "pitch-chat-shell-v5", "pitch-chat-shell-v6", "pitch-chat-shell-v7", "unrelated-app-cache"]; },
    async delete(name) { deletions.push(name); return name.startsWith("pitch-chat-shell-"); },
  },
  async fetch(request) {
    networkRequests.push(request);
    if (networkFailure) throw new TypeError("offline");
    const pathname = new URL(request.url).pathname;
    return {ok: true, status: 200, headers: {get: name => name === "content-type" ?
      (pathname === "/" ? "text/html" : "application/javascript") : null},
      clone() { return this; }, body: `live:${pathname}`};
  },
};
vm.runInNewContext(fs.readFileSync("frontend/sw.js", "utf8"), sandbox);

function fetchEvent(url, method = "GET") {
  let responsePromise;
  const request = new Request(url, {method});
  handlers.fetch({request, clientId: "synthetic-client", respondWith(value) { responsePromise = Promise.resolve(value); }});
  return responsePromise;
}

(async () => {
  assert.match(fs.readFileSync("frontend/sw.js", "utf8"), /CACHE_PREFIX.*v46/,
    "app shell cache version must advance for installed PWA upgrades");
  let install;
  handlers.install({waitUntil(value) { install = value; }});
  await install;
  assert.deepEqual([...cacheMap.keys()].sort(), ["/", "/manifest.webmanifest", "/static/long-videos.js?v=2", "/static/deletions.js?v=2", "/static/chat-videos.js?v=5", "/static/chat-images.js?v=7", "/static/chat.js?v=31", "/static/icons/pitch-chat-192.png",
    "/static/icons/pitch-chat-512.png", "/static/resource-memory.js?v=3", "/static/style.css?v=14", "/static/work.js?v=9"].sort());
  assert.equal(skippedWaiting, 1);
  assert.ok(networkRequests.every(request => request.cache === "reload"), "installation bypasses stale HTTP assets");
  let activation;
  handlers.activate({waitUntil(value) { activation = value; }});
  await activation;
  assert.deepEqual(deletions, ["pitch-chat-shell-v1", "pitch-chat-shell-v2", "pitch-chat-shell-v3", "pitch-chat-shell-v4", "pitch-chat-shell-v5", "pitch-chat-shell-v6", "pitch-chat-shell-v7"]);
  assert.equal(claimed, 1);

  const countBeforeApi = writes.length;
  const apiNetworkBefore = networkRequests.length;
  assert.equal(await fetchEvent("https://pitch-chat.test/api/conversations"), undefined,
    "the service worker must not intercept API requests");
  assert.equal(await fetchEvent("https://pitch-chat.test/health"), undefined,
    "the service worker must not intercept /health");
  assert.equal(await fetchEvent("https://pitch-chat.test/static/chat.js?v=old"), undefined,
    "unrecognized shell versions must not be served from current cache");
  assert.equal(networkRequests.length, apiNetworkBefore);
  const apiResponse = await sandbox.fetch(new Request("https://pitch-chat.test/api/conversations", {cache: "no-store"}));
  assert.match(apiResponse.body, /live:/);
  const postResponse = await sandbox.fetch(new Request("https://pitch-chat.test/api/chat", {method: "POST", cache: "no-store"}));
  assert.match(postResponse.body, /live:/);
  assert.equal(writes.length, countBeforeApi, "API payloads must never be cached");
  assert.ok(writes.every(path => !path.startsWith("/api/")));

  cacheMap.set("/static/chat.js?v=31", {body: "stale:old-installed-js"});
  const refreshedScript = await fetchEvent("https://pitch-chat.test/static/chat.js?v=31");
  assert.equal(refreshedScript.body, "live:/static/chat.js", "installed PWA must use current JS while online");
  assert.equal(cacheMap.get("/static/chat.js?v=31").body, "live:/static/chat.js", "fresh JS replaces stale app-shell cache");
  await fetchEvent("https://pitch-chat.test/static/style.css?v=14");
  assert.ok(writes.length > countBeforeApi, "static app shell may refresh its own cache");
  const unknownResponse = await fetchEvent("https://pitch-chat.test/api/jobs/job-1/result");
  assert.equal(unknownResponse, undefined, "unknown API routes are also outside service worker control");

  cacheMap.set("/static/chat.js?v=31", {body: "stale:old-installed-js"});
  networkFailure = true;
  const shell = await fetchEvent("https://pitch-chat.test/");
  assert.equal(shell.body, "live:/");
  assert.equal(await fetchEvent("https://pitch-chat.test/api/conversations"), undefined,
    "offline API failures are left to the app's no-store fetch handler");
  assert.equal(await fetchEvent("https://pitch-chat.test/static/chat.js?v=31").then(response => response.body),
    "stale:old-installed-js", "offline launch may fall back to static app-shell cache only");
  assert.ok(writes.every(path => !path.startsWith("/api/") && path !== "/health"));
  let workerStatus;
  handlers.message({data: {type: "PITCH_CHAT_SW_DIAGNOSTICS"}, source: {id: "synthetic-client", postMessage(value) { workerStatus = value; }}});
  assert.equal(workerStatus.release, "v46");
  assert.equal(workerStatus.shell_source, "cache");
  process.stdout.write("service worker static-shell/cache-isolation checks passed\n");
})().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
