// Real Chrome + real DOM/Service Worker on an isolated, synthetic loopback app.
// Uses a disposable profile, never the user's browser or the running HOST.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const {spawn} = require("node:child_process");

const chromePath = process.argv[2];
const root = path.resolve(__dirname, "../frontend");
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "pitch-chat-pwa-test-"));
let phase = "current";
let resourceFailure = false;
let modelFailure = false;
let badHealth = false;
let hostOffline = false;
let posts = 0;
let generationPosts = 0;
let generationSequence = 0;
const mockGenerations = new Map();
const mockGenerationByClient = new Map();
const mockHistory = new Map();
let browser;
let socket;
let browserErrors = "";
const pending = new Map();
let sequence = 0;
const exceptions = [];

const legacyWorker = version => `
const NAME = "pitch-chat-shell-${version}";
self.addEventListener("install", e => e.waitUntil((async () => {
  await (await caches.open(NAME)).addAll(["/", "/static/chat.js"]);
  await self.skipWaiting();
})()));
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.search || url.pathname !== "/static/chat.js") return;
  e.respondWith(caches.match(e.request).then(c => c || fetch(e.request)));
});`;

const server = http.createServer((req, res) => {
  if (hostOffline) { req.socket.destroy(); return; }
  const url = new URL(req.url, "http://localhost");
  if(url.pathname.endsWith("/long-videos")){res.writeHead(200,{"Content-Type":"application/json","Cache-Control":"no-store"});res.end(JSON.stringify({jobs:[]}));return;}
  if (req.method === "POST") posts++;
  const json = (value, status = 200, type = "application/json") => {
    res.writeHead(status, {"Content-Type": type, "Cache-Control": "no-store"});
    res.end(type === "text/event-stream" ? value : JSON.stringify(value));
  };
  if (url.pathname === "/api/chat/stream" && req.method === "POST") {
    let body = ""; req.on("data", chunk => body += chunk); req.on("end", () => {
      const payload = JSON.parse(body); let job = mockGenerationByClient.get(payload.client_request_id);
      if (!job) {
        generationPosts++;
        const conversationId = payload.conversation_id || `strata-conversation-${++generationSequence}`;
        job = {job_id: `strata-job-${generationSequence}`, client_request_id: payload.client_request_id,
          conversation_id: conversationId, message: payload.message, reply: "", revision: 0, status: "RUNNING", answer: null};
        mockGenerations.set(job.job_id, job); mockGenerationByClient.set(job.client_request_id, job);
        setTimeout(() => { if (job.status === "RUNNING") { job.reply = "Strataの"; job.revision++; } }, 350);
        setTimeout(() => {
          if (job.status !== "RUNNING") return;
          job.reply = "Strataのストリーム回答"; job.revision++; job.status = "COMPLETED";
          job.answer = {conversation_id: job.conversation_id, reply: job.reply,
            generation_metrics: {elapsed_seconds: 2, time_to_first_token_seconds: 0.4, tokens_per_second: 12, finish_reason: "stop"},
            model_selection: {requested_model: "strata", actual_model_used: "strata"}};
          const history = mockHistory.get(job.conversation_id) || [];
          history.push({role: "user", content: job.message}, {role: "assistant", content: job.reply});
          mockHistory.set(job.conversation_id, history);
        }, 2200);
      }
      json({job_id: job.job_id, client_request_id: job.client_request_id, conversation_id: job.conversation_id,
        status: job.status, reply: job.reply, revision: job.revision});
    }); return;
  }
  const clientMatch = url.pathname.match(/^\/api\/chat\/stream\/by-client\/([^/]+)$/);
  if (clientMatch) {
    const job = mockGenerationByClient.get(clientMatch[1]);
    if (!job) return json({detail: "復元できる生成はありません。"}, 404);
    return json({...job, message: job.status === "RUNNING" ? job.message : null});
  }
  const eventMatch = url.pathname.match(/^\/api\/chat\/stream\/jobs\/([^/]+)\/events$/);
  if (eventMatch) {
    const job = mockGenerations.get(eventMatch[1]);
    if (!job) return json({detail: "生成が見つかりません。"}, 404);
    let body = `event: snapshot\ndata: ${JSON.stringify({reply: job.reply, revision: job.revision})}\n\n`;
    if (job.status === "COMPLETED") body += `event: done\ndata: ${JSON.stringify(job.answer)}\n\n`;
    if (job.status === "CANCELLED") body += `event: error\ndata: ${JSON.stringify({detail: "生成を停止しました。"})}\n\n`;
    return json(body, 200, "text/event-stream");
  }
  const statusMatch = url.pathname.match(/^\/api\/chat\/stream\/jobs\/([^/]+)$/);
  if (statusMatch) {
    const job = mockGenerations.get(statusMatch[1]);
    return job ? json({...job, message: job.status === "RUNNING" ? job.message : null}) : json({detail: "生成が見つかりません。"}, 404);
  }
  const cancelMatch = url.pathname.match(/^\/api\/chat\/stream\/jobs\/([^/]+)\/cancel$/);
  if (cancelMatch) {
    const job = mockGenerations.get(cancelMatch[1]);
    if (!job) return json({detail: "生成が見つかりません。"}, 404);
    job.status = "CANCELLED"; return json({...job});
  }
  if (/^\/api\/conversations\/[^/]+\/images$/.test(url.pathname)) return json({images: [], jobs: []});
  if (/^\/api\/conversations\/[^/]+\/videos$/.test(url.pathname)) return json({projects: [], clips: [], assemblies: []});
  const conversationMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)$/);
  if (conversationMatch && mockHistory.has(conversationMatch[1])) {
    return json({conversation_id: conversationMatch[1], messages: mockHistory.get(conversationMatch[1])});
  }
  if (url.pathname === "/health" || url.pathname.startsWith("/api/")) {
    if ((resourceFailure && url.pathname === "/api/resources") || (modelFailure && url.pathname === "/api/models")) {
      req.socket.destroy(); return;
    }
    const payloads = {
      "/api/images/vision": {available:false},
      "/api/media/backends": {video: [], default_video_backend: "ltx-video"},
      "/health": {status: badHealth ? "degraded" : "ok", storage: "ok", version: "0.1.0", model: {adapter: "qwen", configured: true}},
      "/api/models": {default_model: "qwen", models: [
        {id: "qwen", label: "Qwen", available: true, device: "cpu"},
        {id: "strata", label: "Strata IQ2_XS", available: true, device: "gpu"}]},
      "/api/tools": {tools: [{id: "book.search", enabled: true}]},
      "/api/conversations": {conversations: [{id: "saved-conversation", title: "保存済み会話"}]},
      "/api/conversations/saved-conversation": {conversation_id: "saved-conversation", messages: [
        {role: "user", content: "保存された質問"}, {role: "assistant", content: "保存された回答"}]},
      "/api/chat": {conversation_id: "synthetic-chat", reply: "こんにちは", reasoning_content: "SYNTHETIC_PRIVATE_THINKING"},
      "/api/resources": {mode: "IDLE", lifecycle: "IDLE", loaded_model: null, waiting_jobs: 0},
      "/api/gpu": {state: "STOPPED", memory_used_mib: 0},
      "/api/jobs": {jobs: []},
      "/api/work/catalog": {enabled: false, projects: []},
      "/api/images/backends": {backends: [], generation_enabled: false},
    };
    return json(payloads[url.pathname] || {detail: "Synthetic test route"}, payloads[url.pathname] ? 200 : 404);
  }
  if (phase.startsWith("legacy")) {
    if (url.pathname === "/sw.js") {
      res.writeHead(200, {"Content-Type": "application/javascript", "Cache-Control": "no-cache"});
      res.end(legacyWorker(phase.endsWith("1") ? "v1" : "v2")); return;
    }
    if (url.pathname === "/") {
      res.writeHead(200, {"Content-Type": "text/html"});
      res.end('<p id="status">legacy</p><script src="/static/chat.js" defer></script>'); return;
    }
    if (url.pathname === "/static/chat.js") {
      res.writeHead(200, {"Content-Type": "application/javascript"});
      res.end('window.legacyShell = true; navigator.serviceWorker.register("/sw.js");'); return;
    }
  }
  const filename = url.pathname === "/" ? "index.html" : url.pathname.startsWith("/static/") ? url.pathname.slice(8) : url.pathname.slice(1);
  const allowed = ["long-videos.js","deletions.js", "chat-videos.js", "chat-images.js", "resource-memory.js", "index.html", "chat.js", "work.js", "style.css", "sw.js", "manifest.webmanifest", "icons/pitch-chat-192.png", "icons/pitch-chat-512.png"];
  if (!allowed.includes(filename)) { res.writeHead(404); res.end(); return; }
  const types = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".webmanifest": "application/manifest+json", ".png": "image/png"};
  res.writeHead(200, {"Content-Type": types[path.extname(filename)], ...(filename === "sw.js" ? {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"} : {})});
  res.end(fs.readFileSync(path.join(root, filename)));
});

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await check()) return; await pause(80); }
  throw new Error(`Timed out: ${label}`);
}
function cdp(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 12000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const reply = await cdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
  if (reply.exceptionDetails) throw new Error(reply.exceptionDetails.exception?.description || reply.exceptionDetails.text);
  return reply.result.value;
}
async function ready() {
  await until(() => evaluate('!!document.getElementById("send") && !document.getElementById("send").disabled'), "usable chat controls");
  await until(() => evaluate('!!navigator.serviceWorker.controller && caches.keys().then(names => names.includes("pitch-chat-shell-v46"))'), "v46 worker installed");
}
async function network(offline) {
  // CDP page network emulation does not also make Service Worker fetch offline.
  // Refuse at the synthetic server too, so both page and worker see HOST loss.
  hostOffline = offline;
  await cdp("Network.emulateNetworkConditions", {offline, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
}
async function cacheIsPrivate() {
  const keys = await evaluate('(async () => { const names=await caches.keys(); return (await Promise.all(names.map(async name => (await (await caches.open(name)).keys()).map(r => new URL(r.url).pathname)))).flat(); })()');
  assert.ok(keys.every(key => key !== "/health" && !key.startsWith("/api/")), "private/health/API data must never enter CacheStorage");
  const bodies = await evaluate('(async () => { const all=[]; for (const name of await caches.keys()) { const cache=await caches.open(name); for (const key of await cache.keys()) all.push(await (await cache.match(key)).text()); } return all.join("\\n"); })()');
  assert.ok(!bodies.includes("SYNTHETIC_PRIVATE_THINKING") && !bodies.includes("SYNTHETIC_PRIVATE_CONTENT"),
    "neither reasoning nor private Chat may enter cached shell bodies");
}

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = spawn(chromePath, ["--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", "--no-default-browser-check",
    "--no-proxy-server", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
  browser.stderr.on("data", chunk => { browserErrors = (browserErrors + chunk.toString()).slice(-2000); });
  await until(() => fs.existsSync(path.join(profile, "DevToolsActivePort")), "headless Chrome startup", 10000);
  const port = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0];
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(targets.find(target => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = event => {
    const reply = JSON.parse(event.data);
    if (reply.method === "Runtime.exceptionThrown") exceptions.push(reply.params.exceptionDetails.text);
    const item = pending.get(reply.id);
    if (!item) return;
    pending.delete(reply.id); clearTimeout(item.timer);
    if (reply.error) item.reject(new Error(reply.error.message)); else item.resolve(reply.result);
  };
  await cdp("Runtime.enable"); await cdp("Page.enable"); await cdp("Network.enable");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  await cdp("Page.navigate", {url: origin}); await ready();
  assert.equal(await evaluate('diagnostics.health_url'), `${origin}/health`);
  assert.equal(await evaluate('diagnostics.display_mode'), "browser");
  assert.equal(await evaluate('document.getElementById("offline-notice").hidden'), true);
  assert.equal(await evaluate('document.getElementById("management-menu").open'), false, "mobile opens on the conversation, not the management panel");
  assert.equal(await evaluate('document.getElementById("conversation-menu").open'), false, "mobile conversation options start collapsed");
  assert.notEqual(await evaluate('getComputedStyle(document.querySelector(".message-content") || document.getElementById("messages")).userSelect'), "none");
  assert.ok(await evaluate('document.getElementById("chat-model").getBoundingClientRect().width > 0'), "model selection remains visible");
  assert.ok(await evaluate('document.getElementById("chat-form").getBoundingClientRect().bottom <= innerHeight + 1'), "composer stays inside the mobile viewport");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 560, deviceScaleFactor: 1, mobile: true});
  await pause(80);
  assert.ok(await evaluate('document.getElementById("chat-form").getBoundingClientRect().bottom <= innerHeight + 1'), "composer remains visible with the reduced keyboard viewport");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  console.log("PASS real Chrome: online startup / same-origin health / DOM initialization");

  const markdown = "## 見出し\n\n本文 **強調** と `inline`\n\n```python\nprint('日本語')\n```";
  await evaluate(`addMessage("assistant", ${JSON.stringify(markdown)}); Object.defineProperty(navigator, "clipboard", {configurable:true, value:{writeText:async value => {window.__copied=value;}}});`);
  assert.equal(await evaluate('document.querySelectorAll(".message.assistant h2").length'), 1);
  assert.equal(await evaluate('document.querySelectorAll(".message.assistant .code-block").length'), 1);
  assert.equal(await evaluate('document.querySelector(".message.assistant .message-content strong").textContent'), "強調");
  await evaluate('Array.from(document.querySelectorAll(".message.assistant .copy-button")).at(-2).click()');
  await until(() => evaluate('window.__copied === ' + JSON.stringify(markdown)), "full Markdown copy preserves source text");
  await evaluate('Array.from(document.querySelectorAll(".message.assistant .copy-button")).at(-1).click()');
  await until(() => evaluate('window.__copied === ' + JSON.stringify("print('日本語')")), "code block copy");
  assert.equal(await evaluate('getComputedStyle(document.querySelector(".message-content")).userSelect'), "text", "answer text remains selectable");
  await evaluate('addMessage("assistant", "<img src=x onerror=alert(1)>");');
  assert.equal(await evaluate('document.querySelectorAll(".message.assistant img").length'), 0, "untrusted markup is rendered as text, not HTML");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await pause(80);
  await evaluate('document.getElementById("management-menu").open = true');
  assert.ok(await evaluate('document.getElementById("messages").getBoundingClientRect().width > 700'), "desktop keeps a wide conversation column");
  assert.ok(await evaluate('document.getElementById("tool-form") !== null'), "desktop retains management tools");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  await evaluate('document.getElementById("management-menu").open = false');
  console.log("PASS real Chrome: mobile/desktop layout, selectable Markdown, full/code copy, and safe rendering");

  await evaluate('document.getElementById("message").value="hello"; document.getElementById("chat-form").requestSubmit()');
  await until(() => evaluate('!!document.querySelector(".thinking-debug")'), "thinking debug response");
  assert.equal(await evaluate('document.querySelectorAll(".message.assistant")[2].querySelector("p").textContent'), "こんにちは");
  assert.equal(await evaluate('document.querySelector(".thinking-debug").open'), false);
  assert.equal(await evaluate('document.querySelector(".thinking-debug pre").getBoundingClientRect().height'), 0,
    "reasoning is not visible while details is closed");
  await evaluate('document.querySelector(".thinking-debug summary").click()');
  assert.equal(await evaluate('document.querySelector(".thinking-debug").open'), true);
  assert.equal(await evaluate('document.querySelector(".thinking-debug pre").textContent'), "SYNTHETIC_PRIVATE_THINKING");
  await evaluate('document.querySelector(".thinking-debug summary").click()');
  await evaluate('document.getElementById("chat-model").value="strata"; document.getElementById("chat-model").dispatchEvent(new Event("change"))');
  assert.match(await evaluate('document.getElementById("selected-model-label").textContent'), /Strata IQ2_XS.*未起動/);
  assert.equal(await evaluate('document.getElementById("strata-options").hidden'), false);
  assert.equal(posts, 1, "switching the selected model does not start it");
  await evaluate('document.getElementById("message").value="ストリーミング確認"; document.getElementById("chat-form").requestSubmit()');
  await until(() => evaluate('document.getElementById("strata-preview").textContent.includes("Strataの")'), "server-side Strata job exposes a partial snapshot");
  await network(true); await evaluate('checkHost()');
  await pause(2500); // The simulated phone is disconnected while the HOST completes its job.
  await network(false); await evaluate('checkHost()');
  await until(() => evaluate('Array.from(document.querySelectorAll(".message.assistant .message-content")).some(node => node.textContent === "Strataのストリーム回答")'), "Strata chat stream reaches the conversation");
  assert.match(await evaluate('document.getElementById("generation-timing").textContent'), /TTFT 0.40/);
  assert.equal(posts, 2, "Strata streaming creates one chat request and no lifecycle request");
  assert.equal(generationPosts, 1, "network interruption does not submit a second Strata generation");
  await until(() => evaluate('!isBusy && !activeChatJob'), 'first generation and hydration fully released controls');
  await evaluate('document.getElementById("message").value="再読み込み後も続ける"; document.getElementById("chat-form").requestSubmit()');
  await until(() => evaluate('document.getElementById("strata-preview").textContent.includes("Strataの")'), "second server-side generation starts");
  await cdp("Page.reload");
  await until(() => evaluate('Array.from(document.querySelectorAll(".message.assistant .message-content")).some(node => node.textContent === "Strataのストリーム回答") && document.querySelectorAll("#messages .message").length >= 4 && !isBusy && !activeChatJob'), "PWA reload restores completed generation and history");
  assert.equal(generationPosts, 2, "PWA reload resumes by client request ID instead of generating again");
  assert.equal(posts, 3, "recovery uses read-only status/event requests, without replaying POST");
  await evaluate('document.getElementById("message").value="画面スリープから戻りました"; document.getElementById("chat-form").requestSubmit()');
  await until(() => evaluate('document.getElementById("strata-preview").textContent.includes("Strataの")'), "third server-side generation starts");
  await cdp("Page.setWebLifecycleState", {state: "frozen"});
  await pause(2500); // Browser timers and fetch subscribers are suspended while the HOST job continues.
  await cdp("Page.setWebLifecycleState", {state: "active"});
  await until(() => evaluate('Array.from(document.querySelectorAll(".message.assistant .message-content")).filter(node => node.textContent === "Strataのストリーム回答").length === 3'), "frozen page resumes the completed HOST generation");
  await until(() => evaluate('!isBusy && !activeChatJob'), 'resumed generation released controls');
  assert.equal(generationPosts, 3, "sleep/resume attaches to the same generation");
  assert.equal(posts, 4, "sleep/resume does not replay a generation POST");
  console.log("PASS real Chrome: network loss, frozen background tab, and PWA reload recover one server-owned generation each");
  await evaluate('document.getElementById("conversation-menu").open=true; const history=document.getElementById("conversations"); history.value="saved-conversation"; history.dispatchEvent(new Event("change"))');
  await until(() => evaluate('document.querySelectorAll("#messages .message").length === 2 && document.getElementById("messages").textContent.includes("保存された回答")'), "saved conversation history restores");
  await evaluate('document.getElementById("chat-model").value="qwen"; document.getElementById("chat-model").dispatchEvent(new Event("change"))');
  assert.equal(await evaluate('document.getElementById("strata-options").hidden'), true, "switching back restores the Qwen chat controls");
  await evaluate('document.getElementById("management-menu").open=true');
  assert.equal(await evaluate('document.getElementById("management-menu").open'), true);
  assert.ok(await evaluate('document.getElementById("tool-form").getBoundingClientRect().width > 0'), "management tools remain reachable when expanded");
  await cacheIsPrivate();
  console.log("PASS real Chrome: final-only Chat body / default-closed Thinking / explicit reveal");

  resourceFailure = true;
  await evaluate('refreshResources()'); await pause(250);
  assert.equal(await evaluate('document.getElementById("offline-notice").hidden'), true);
  assert.equal(await evaluate('diagnostics.health_valid'), true);
  assert.equal(await evaluate('document.getElementById("send").disabled'), false);
  resourceFailure = false;
  console.log("PASS real Chrome: peripheral fetch failure does not falsely mark healthy HOST offline");

  const postsBeforeReconnect = posts;
  await evaluate('window.pageToken="same-page"; addMessage("assistant", "SYNTHETIC_PRIVATE_CONTENT");');
  await network(true); await evaluate('checkHost()');
  assert.equal(await evaluate('document.getElementById("offline-notice").hidden'), false);
  assert.equal(await evaluate('document.getElementById("messages").children.length'), 0);
  assert.equal(await evaluate('document.getElementById("send").disabled'), true);
  await network(false); await evaluate('checkHost()'); await ready();
  assert.equal(await evaluate('window.pageToken'), "same-page", "reconnect must not reload the page");
  assert.equal(posts, postsBeforeReconnect, "recovery must not replay chat/tool mutations");
  console.log("PASS real Chrome: offline clears private output / reconnect restores controls without reload or POST replay");

  await network(true); await cdp("Page.reload");
  await until(() => evaluate('typeof checkHost === "function" && document.getElementById("offline-notice").hidden === false'), "offline shell launch");
  await evaluate('navigator.serviceWorker.controller.postMessage({type:"PITCH_CHAT_SW_DIAGNOSTICS"})');
  await until(() => evaluate('diagnostics.shell_source === "cache"'), "worker reports offline shell cache hit").catch(async error => {
    console.error(await evaluate('JSON.stringify({worker:diagnostics.worker,release:diagnostics.worker_release,source:diagnostics.shell_source,events:diagnostics.events})'));
    throw error;
  });
  await network(false); await evaluate('checkHost()'); await ready();
  console.log("PASS real Chrome: cold offline shell launch then online hydration");

  modelFailure = true; await cdp("Page.reload");
  await until(() => evaluate('typeof diagnostics !== "undefined" && diagnostics.health_valid === true && document.getElementById("error").textContent.includes("一部のUI情報")'), "failed startup hydration");
  assert.equal(await evaluate('document.getElementById("offline-notice").hidden'), true);
  modelFailure = false;
  await ready(); // The scheduled health/hydration retry, no manual reload.
  console.log("PASS real Chrome: initial model-list failure automatically recovers through scheduled retry");

  badHealth = true; await evaluate('checkHost()');
  assert.equal(await evaluate('document.getElementById("offline-notice").hidden'), false);
  badHealth = false; await evaluate('checkHost()'); await ready();
  await cacheIsPrivate();
  const cachedPrivate = await evaluate('(async () => { for(const name of await caches.keys()) for(const request of await (await caches.open(name)).keys()) { const response=await (await caches.open(name)).match(request); if((await response.text()).includes("SYNTHETIC_PRIVATE_CONTENT")) return true; } return false; })()');
  assert.equal(cachedPrivate, false);
  console.log("PASS real Chrome: invalid health contract / cache privacy");

  for (const version of [1, 2]) {
    await evaluate('(async () => { for(const r of await navigator.serviceWorker.getRegistrations()) await r.unregister(); for(const n of await caches.keys()) await caches.delete(n); })()');
    await cdp("Page.navigate", {url: "about:blank"});
    phase = `legacy-${version}`;
    await cdp("Page.navigate", {url: origin});
    await until(() => evaluate(`!!navigator.serviceWorker.controller && caches.keys().then(n=>n.includes("pitch-chat-shell-v${version}"))`), "legacy worker installed");
    phase = "current";
    await cdp("Page.navigate", {url: `${origin}/?shell=pwa-18`});
    await ready();
    assert.equal(await evaluate('FRONTEND_RELEASE'), "pwa-18");
    await until(() => evaluate(`caches.keys().then(n=>!n.includes("pitch-chat-shell-v${version}"))`), "legacy cache deleted").catch(async error => {
      console.error(await evaluate('(async()=>JSON.stringify({caches:await caches.keys(),controller:navigator.serviceWorker.controller?.scriptURL,registrations:(await navigator.serviceWorker.getRegistrations()).map(r=>({active:r.active?.scriptURL,waiting:r.waiting?.scriptURL,installing:r.installing?.scriptURL}))}))()'));
      throw error;
    });
    await cacheIsPrivate();
    console.log(`PASS real Chrome: stale v${version} worker/cache migrated to fresh versioned JS / v46 worker`);
  }
  assert.equal(exceptions.length, 0, "no uncaught DOM/runtime errors");
  console.log("PWA browser checks passed (isolated real Chrome; physical Android standalone remains device verification)");
})().catch(error => {
  console.error(error.stack); if (browser && browser.exitCode !== null) console.error(browserErrors);
  process.exitCode = 1;
}).finally(async () => {
  if (socket?.readyState === WebSocket.OPEN && browser?.exitCode === null) {
    await Promise.race([cdp("Browser.close").catch(() => {}), pause(2000)]);
  }
  socket?.close();
  for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("test ended")); }
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  if (browser && browser.exitCode === null) {
    browser.kill("SIGTERM");
    await Promise.race([new Promise(resolve => browser.once("exit", resolve)), pause(3000)]);
  }
  if (!browser || browser.exitCode !== null) await fs.promises.rm(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 200});
});
