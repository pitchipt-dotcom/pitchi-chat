const assert = require("node:assert/strict");
const vm = require("node:vm");
const {scenario} = require("./frontend_models.cjs");
const models = [{id: "qwen", label: "Qwen", available: true, device: "cpu"}];
const healthy = {status: "ok", storage: "ok"};
const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); };
function timers() {
  let next = 0;
  const scheduled = new Map();
  return {
    setTimeout(fn, delay) { const id = ++next; scheduled.set(id, {fn, delay}); return id; },
    clearTimeout(id) { scheduled.delete(id); },
    async fire(fn, delay) {
      const entry = [...scheduled].find(([, timer]) => fn ? timer.fn === fn : timer.delay === delay);
      assert.ok(entry, "expected bounded health retry/timeout timer");
      scheduled.delete(entry[0]); await entry[1].fn(); await flush();
    },
  };
}
const response = (body, status = 200) => ({ok: status >= 200 && status < 300, status, async json() { return body; }});

(async () => {
  for (const displayMode of ["browser", "standalone"]) {
    const clock = timers();
    const app = await scenario(models, "qwen", null, healthy, {...clock, displayMode, onLine: false,
      href: "https://pitch-chat.test:8446/"});
    assert.equal(app.get("offline-notice").hidden, true);
    assert.equal(app.get("send").disabled, false, "navigator.onLine is not HOST reachability");
    assert.equal(vm.runInContext("diagnostics.display_mode", app.context), displayMode);
    assert.equal(vm.runInContext("diagnostics.health_url", app.context), "https://pitch-chat.test:8446/health");
    assert.ok(app.calls.every(call => call.options.cache === "no-store"));
    const originalFetch = app.context.fetch;
    app.context.fetch = async (path, options) => {
      if (path === "/api/resources") throw new TypeError("Failed to fetch");
      if (path === "/api/jobs") return response({detail: "synthetic unavailable"}, 503);
      return originalFetch(path, options);
    };
    await app.context.refreshResources(); await app.context.refreshDurableResources();
    assert.equal(app.get("offline-notice").hidden, true, "peripheral network/HTTP errors must not override healthy HOST");
    assert.equal(app.get("send").disabled, false);
    app.context.fetch = originalFetch;

    app.context.addMessage("assistant", "SYNTHETIC_PRIVATE_CONTENT");
    app.get("tool-output").textContent = "SYNTHETIC_PRIVATE_EVIDENCE";
    app.get("message").value = "SYNTHETIC_PRIVATE_DRAFT";
    app.setOffline(); await app.context.checkHost();
    assert.equal(app.get("offline-notice").hidden, false);
    assert.equal(app.get("send").disabled, true);
    assert.equal(app.get("messages").children.length, 0);
    assert.equal(app.get("tool-output").textContent, "");
    app.setOnline(); await clock.fire(app.context.checkHost);
    assert.equal(app.get("send").disabled, false);
    assert.equal(app.get("offline-notice").hidden, true);
    assert.equal(app.get("message").value, "SYNTHETIC_PRIVATE_DRAFT", "a reconnect must preserve the in-memory unsent draft");
    assert.equal(app.reloads, 0);
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 0);
    await app.windowEvents.online(); await flush();
    assert.equal(app.reloads, 0, "online events must reconnect without a page reload");

    // A server-held Vision answer can finish after earlier messages were drawn.
    vm.runInContext('conversationId = "synthetic-current"', app.context);
    app.context.addMessage("assistant", "SYNTHETICOLDERANSWER");
    app.context.fetch = (path, options) => path === "/api/conversations/synthetic-current"
      ? Promise.resolve(response({messages: [{role: "assistant", content: "SYNTHETICRECOVEREDVISION"}]}))
      : originalFetch(path, options);
    await app.context.hydrateUi();
    assert.match(app.get("messages").textContent, /SYNTHETICRECOVEREDVISION/);
    assert.doesNotMatch(app.get("messages").textContent, /SYNTHETICOLDERANSWER/);
    vm.runInContext('conversationId = null', app.context);
    app.get("messages").replaceChildren();
    app.context.fetch = originalFetch;

    let release;
    app.context.fetch = (path, options) => path === "/api/conversations/synthetic-id" ? new Promise(resolve => { release = resolve; }) : originalFetch(path, options);
    const stale = app.context.api("/api/conversations/synthetic-id");
    const rejected = assert.rejects(stale, error => error.kind === "stale");
    app.setOffline(); await app.context.checkHost();
    release(response({messages: [{role: "assistant", content: "SYNTHETIC_PRIVATE_LATE_RESPONSE"}]}));
    await rejected;
    assert.equal(app.get("messages").children.length, 0);
    assert.doesNotMatch(app.get("connection-diagnostics-output").textContent, /SYNTHETIC_PRIVATE|synthetic-id/,
      "diagnostics must contain neither request/result content nor job/conversation IDs");
    app.context.fetch = originalFetch;
    app.setOnline(); await app.context.checkHost(); await flush();
    app.setHealth({status: "ok", storage: "error"}); await app.context.checkHost();
    assert.equal(app.get("offline-notice").hidden, false, "both health and storage must be ok");
    app.setHealth(healthy); await clock.fire(app.context.checkHost);
    assert.equal(app.get("send").disabled, false);

    const coldClock = timers();
    const cold = await scenario(models, "qwen", "offline", healthy, {...coldClock, displayMode});
    assert.equal(cold.get("send").disabled, true);
    cold.setOnline(); await coldClock.fire(cold.context.checkHost);
    assert.equal(cold.get("chat-model").value, "qwen");
    assert.equal(cold.get("send").disabled, false, "failed cold startup must hydrate on reconnect");
    assert.equal(cold.reloads, 0);

    let modelFail = true;
    const initClock = timers();
    const init = await scenario(models, "qwen", null, healthy, {...initClock, displayMode, respond(path) {
      if (path === "/api/models" && modelFail) throw new TypeError("Failed to fetch");
    }});
    assert.equal(init.get("offline-notice").hidden, true, "UI initialization errors do not mean HOST offline");
    assert.equal(init.get("send").disabled, true);
    modelFail = false; await initClock.fire(init.context.checkHost);
    assert.equal(init.get("send").disabled, false, "partial initialization is retried without reload");
    assert.equal(init.reloads, 0);
    const oldHtml = await scenario(models, "qwen", null, healthy, {displayMode, omitDiagnostics: true});
    assert.equal(oldHtml.get("send").disabled, false, "a fresh JS with old HTML missing diagnostic nodes must still start");
    const invalidModelList = await scenario(models, "qwen", null, healthy, {displayMode, respond(path) {
      if (path === "/api/models") return response({models: null});
    }});
    await invalidModelList.context.checkHost(); await flush();
    assert.equal(invalidModelList.get("offline-notice").hidden, true, "an invalid component schema is a UI error, not HOST offline");
    assert.equal(invalidModelList.get("send").disabled, true);
    console.log(`PASS ${displayMode}: online/offline/cold startup/reconnect/HTTP errors/late-response race/privacy`);
  }

  const timeoutClock = timers();
  let stallHealth = true;
  const stalled = await scenario(models, "qwen", null, healthy, {...timeoutClock, respond(path, options) {
    if (path === "/health" && stallHealth) return new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => { const error = new Error("test timeout"); error.name = "AbortError"; reject(error); });
    });
  }});
  await timeoutClock.fire(null, 5000);
  assert.equal(stalled.get("offline-notice").hidden, false);
  assert.match(stalled.get("connection-diagnostics-output").textContent, /timeout/);
  stallHealth = false; await timeoutClock.fire(stalled.context.checkHost);
  assert.equal(stalled.get("send").disabled, false);
  console.log("PWA connection state checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
