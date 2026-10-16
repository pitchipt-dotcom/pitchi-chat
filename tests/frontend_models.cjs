// Lightweight DOM/fetch mocks; not a real mobile browser integration test.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function scenario(models, defaultModel, tools = null, healthData = {status: "ok", storage: "ok"}, runtime = {}) {
  const elements = new Map();
  const html = fs.readFileSync("frontend/index.html", "utf8");
  const tags = new Map([...html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)].map(match => [match[1], match[0]]));
  if (runtime.omitDiagnostics) { tags.delete("connection-check"); tags.delete("connection-diagnostics-output"); }
  const calls = [];
  const windowEvents = {};
  let transportOffline = tools === "offline";
  const serviceWorkerEvents = {};
  const registrationCalls = [];
  let updateChecks = 0;
  let reloads = 0;
  function element() {
    let text = "";
    const node = {
      value: "", disabled: false, hidden: false, open: true, children: [], listeners: {}, dataset: {}, style: {}, attributes: {},
      addEventListener(name, handler) { this.listeners[name] = handler; },
      setAttribute(name, value) { this.attributes[name] = value; },
      replaceChildren(...children) { text = ""; this.children = children; },
      add(child) { this.children.push(child); },
      append(...children) { text = ""; this.children.push(...children); },
      remove() {}, select() {},
      focus() {},
    };
    Object.defineProperty(node, "textContent", {get() { return text + this.children.map(child => child.textContent || "").join(""); },
      set(value) { text = String(value); this.children = []; }});
    return node;
  }
  const document = {
    getElementById(id) {
      if (!tags.has(id)) return null;
      if (!elements.has(id)) {
        const node = element(); node.hidden = /\shidden\b/.test(tags.get(id)); node.disabled = /\sdisabled\b/.test(tags.get(id));
        elements.set(id, node);
      }
      return elements.get(id);
    },
    body: element(),
    createElement(tag) { const node = element(); node.tagName = tag; return node; },
    createTextNode(value) { const node = element(); node.textContent = value; return node; },
    execCommand() { return true; },
  };
  const context = vm.createContext({
    URL, AbortController, TextDecoder,
    document, Option: function(text, value) { const node = element(); node.textContent = text; node.value = value; return node; },
    setTimeout: runtime.setTimeout || (() => {}), clearTimeout: runtime.clearTimeout || (() => {}),
    navigator: {onLine: runtime.onLine ?? true, serviceWorker: {
      controller: {},
      addEventListener(name, callback) { serviceWorkerEvents[name] = callback; },
      register(url, options) { registrationCalls.push({url, options}); return Promise.resolve({async update() { updateChecks++; }}); },
    }},
    window: {isSecureContext: true, location: {href: runtime.href || "https://pitch-chat.test/", reload() { reloads++; }},
      setTimeout: runtime.setTimeout || (() => {}),
      matchMedia(query) { return {matches: query.includes("max-width: 550px") ? (runtime.width || 1280) <= 550 : query.includes("display-mode") && runtime.displayMode === "standalone"}; },
      addEventListener(name, callback) { windowEvents[name] = callback; }},
    async fetch(path, options) {
      calls.push({path, options});
      if (transportOffline) throw vm.runInContext("new TypeError('Failed to fetch')", context);
      if (runtime.respond) { const response = await runtime.respond(path, options); if (response) return response; }
      let data;
      if (path === "/health") data = healthData;
      else if (path === "/api/images/conversations") data = {conversation_id:"11111111-1111-4111-8111-111111111111"};
      else if (path === "/api/models") data = {models, default_model: defaultModel};
      else if (path === "/api/tools") data = {tools: Array.isArray(tools) ? tools : [{id: "book.search", enabled: true}, {id: "video.generate", enabled: false}]};
      else if (path === "/api/images/backends") data = {backends: [{id: "qwen-image", status: "ready"}], generation_enabled: false};
      else if (path === "/api/images/jobs") data = {job_id: "image-job", status: "QUEUED"};
      else if (path === "/api/tools/run") data = {tool_result: {output: {references: [{title: "Synthetic book", pdf_page: 3}]}}};
      else if (path === "/api/resources") data = {mode: "IDLE", lifecycle: "IDLE", waiting_jobs: 0};
      else if (path === "/api/gpu") data = {state: "STOPPED", current_owner: "external_unmanaged", memory_used_mib: 600,
        process_classifications: {HOST_OWNED: 0, EXTERNAL_SHARED: 2, EXTERNAL_BLOCKING: 1, STALE_OR_UNKNOWN: 0}};
      else if (path === "/api/jobs") data = options?.method === "POST" ? {job_id: "mock-tool-job", status: "QUEUED"} : {jobs: [
        {tool: "video.generate", status: "RECOVERY_REQUIRED"}, {tool: "image.generate", status: "ERROR", error: "synthetic private detail"}]};
      else if (path === "/api/jobs/mock-tool-job") data = {status: "SUCCESS", remote_run_id: "mock-run"};
      else if (path === "/api/jobs/mock-tool-job/result") data = {tool_result: {output: {format: "wav"}, artifacts: [
        {kind: "audio", path: "/api/jobs/mock-tool-job/artifacts/0"},
        {kind: "audio", path: "https://example.org/private.wav"}]}};
      else if (path === "/api/conversations") data = {conversations: []};
      else if (path === "/api/chat") data = {conversation_id: "test-conversation", reply: "<script>not HTML</script>",
        model_selection: {requested_model: "qwen80b", actual_model_used: "qwen", fallback_reason: "external_gpu_workload", gpu_blocking_owners: ["external-image"]}};
      else if (path.endsWith("/lifecycle")) data = {model: "qwen80b", state: "READY"};
      else throw new Error(`Unexpected path: ${path}`);
      return {ok: true, status: 200, async json() { return data; }};
    },
  });
  vm.runInContext(fs.readFileSync("frontend/chat.js", "utf8"), context);
  for (let i = 0; i < 40; i++) await Promise.resolve();
  await context.refreshResources();
  await context.refreshDurableResources();
  return {get: document.getElementById, calls, pollToolJob: context.pollToolJob, context,
    serviceWorkerEvents, registrationCalls, windowEvents, setOnline() { transportOffline = false; },
    setOffline() { transportOffline = true; }, setHealth(value) { healthData = value; },
    get updateChecks() { return updateChecks; }, get reloads() { return reloads; }};
}

module.exports = {scenario};
if (require.main === module) (async () => {
  const models = [{id: "qwen", label: "Qwen", available: true}, {id: "bonsai", label: "BONSAI", available: true}];
  const available = await scenario(models, "bonsai");
  assert.equal(available.calls.find(call => call.path === "/health").options.cache, "no-store");
  assert.equal(available.get("offline-notice").hidden, true);
  assert.equal(available.registrationCalls[0].url, "/sw.js");
  assert.equal(available.registrationCalls[0].options.scope, "/");
  assert.equal(available.registrationCalls[0].options.updateViaCache, "none");
  assert.equal(available.updateChecks, 1, "installed clients should check for a worker update at launch");
  available.serviceWorkerEvents.controllerchange();
  assert.equal(available.reloads, 1, "an update controlling an existing page should load the new app shell");
  assert.ok(available.calls.every(call => call.path.startsWith("/") && call.options?.cache === "no-store"),
    "health and API traffic must be same-origin relative, no-store requests");
  const invalidHealth = await scenario(models, "qwen", null, {status: "degraded", storage: "ok"});
  assert.equal(invalidHealth.get("offline-notice").hidden, false,
    "health must require both HOST and storage status to be ok");
  assert.equal(available.get("chat-model").value, "bonsai");
  assert.equal(available.get("send").disabled, false);
  assert.equal(available.get("image-preview").disabled, false);
  assert.equal(available.get("image-generate").disabled, true);
  available.get("image-request").value = "synthetic image intent";
  await available.get("image-preview").listeners.click();
  const imagePayload = JSON.parse(available.calls.find(call => call.path === "/api/images/jobs").options.body);
  assert.equal(imagePayload.compile_only, true);
  assert.equal(imagePayload.backend, "qwen-image");
  assert.equal("workflow" in imagePayload, false);
  assert.match(available.get("gpu-status").textContent, /external_unmanaged.*600 MiB/);
  assert.match(available.get("gpu-status").textContent, /背景共有 2.*競合 1.*不明 0/);
  assert.match(available.get("job-status").textContent, /video.generate: RECOVERY_REQUIRED/);
  assert.match(available.get("job-status").textContent, /image.generate: ERROR/);
  assert.doesNotMatch(available.get("job-status").textContent, /synthetic private detail/);
  available.get("message").value = "test";
  await available.get("chat-form").listeners.submit({preventDefault() {}});
  const payload = JSON.parse(available.calls.find(call => call.path === "/api/chat").options.body);
  assert.equal(available.get("requested-model").textContent, "qwen80b");
  assert.equal(available.get("actual-model").textContent, "Qwen");
  assert.match(available.get("fallback-reason").textContent, /HOST.*CPU/);
  assert.equal(available.get("gpu-blocking-owner").textContent, "external-image");
  assert.match(available.get("cloud-render-status").textContent, /Cloud不使用.*直接出力/);
  assert.equal(payload.model, "bonsai");
  assert.equal(payload.conversation_id, "11111111-1111-4111-8111-111111111111", "image preview and following chat share the Host conversation");
  assert.equal(payload.mode, "chat");
  assert.equal("external_retrieval" in payload, false);
  const pt = await scenario(models, "qwen");
  pt.get("chat-mode").value = "clinical";
  pt.get("chat-mode").listeners.change();
  assert.equal(pt.get("pt-options").hidden, false);
  pt.get("message").value = "synthetic private case";
  pt.get("evidence-query").value = "synthetic public topic";
  pt.get("external-retrieval").checked = true;
  await pt.get("chat-form").listeners.submit({preventDefault() {}});
  const ptPayload = JSON.parse(pt.calls.find(call => call.path === "/api/chat").options.body);
  assert.equal(ptPayload.mode, "clinical");
  assert.equal(ptPayload.evidence_query, "synthetic public topic");
  assert.equal(ptPayload.external_retrieval, true);
  assert.equal(pt.get("external-retrieval").checked, false);
  const gpu = await scenario([{id: "qwen80b", label: "80B", available: true, device: "gpu", connection_mode: "host_owned"}], "qwen80b");
  assert.equal(gpu.get("model-start").disabled, false);
  await gpu.get("model-start").listeners.click();
  assert.equal(JSON.parse(gpu.calls.find(call => call.path.endsWith("/lifecycle")).options.body).action, "start");
  assert.equal(available.get("messages").children[1].children[0].children[0].textContent, "アシスタント");
  assert.equal(available.get("messages").children[1].children[1].textContent, "<script>not HTML</script>");
  available.get("chat-model").value = "qwen";
  available.get("chat-model").listeners.change();
  available.get("message").value = "continue";
  await available.get("chat-form").listeners.submit({preventDefault() {}});
  const second = JSON.parse(available.calls.filter(call => call.path === "/api/chat")[1].options.body);
  assert.equal(second.model, "qwen");
  assert.equal(second.conversation_id, "test-conversation");
  assert.equal(available.get("tool-selector").children[1].disabled, true);
  available.get("tool-query").value = "synthetic query";
  await available.get("tool-form").listeners.submit({preventDefault() {}});
  const toolPayload = JSON.parse(available.calls.find(call => call.path === "/api/tools/run").options.body);
  assert.equal(toolPayload.tool, "book.search");
  assert.equal(toolPayload.arguments.query, "synthetic query");
  assert.match(available.get("tool-output").textContent, /Synthetic book/);
  available.get("chat-mode").value = "library";
  available.get("message").value = "synthetic library question";
  await available.get("chat-form").listeners.submit({preventDefault() {}});
  assert.equal(JSON.parse(available.calls.filter(call => call.path === "/api/chat").at(-1).options.body).mode, "library");

  const speech = await scenario(models, "qwen", [{id: "tts.generate", enabled: true}]);
  speech.get("tool-query").value = "こんにちは。";
  await speech.get("tool-form").listeners.submit({preventDefault() {}});
  const speechCall = speech.calls.find(call => call.path === "/api/jobs" && call.options?.method === "POST");
  assert.equal(JSON.parse(speechCall.options.body).tool, "tts.generate");
  assert.equal(JSON.parse(speechCall.options.body).arguments.text, "こんにちは。");
  assert.equal(speech.calls.some(call => call.path === "/api/tools/run"), false);
  await speech.pollToolJob();
  assert.equal(speech.get("tool-artifacts").children.length, 1);
  assert.equal(speech.get("tool-artifacts").children[0].src, "/api/jobs/mock-tool-job/artifacts/0");
  const work = await scenario(models, "qwen", [{id: "pitch_work.run", enabled: true, projects: ["demo"]}]);
  work.get("tool-query").value = "unmodified instruction";
  await work.get("tool-form").listeners.submit({preventDefault() {}});
  const workCall = work.calls.find(call => call.path === "/api/jobs" && call.options?.method === "POST");
  const workPayload = JSON.parse(workCall.options.body);
  assert.equal(workPayload.arguments.instruction, "unmodified instruction");
  assert.equal(workPayload.arguments.project, "demo");
  assert.equal(work.get("tool-project").hidden, false);
  const disabledSpeech = await scenario(models, "qwen", [{id: "tts.generate", enabled: false}]);
  disabledSpeech.get("tool-query").value = "must not send";
  await disabledSpeech.get("tool-form").listeners.submit({preventDefault() {}});
  assert.equal(disabledSpeech.calls.some(call => call.path === "/api/jobs" && call.options?.method === "POST"), false);

  const fallback = await scenario([{...models[0], available: false}, models[1]], "qwen");
  assert.equal(fallback.get("chat-model").value, "bonsai");
  assert.equal(fallback.get("chat-model").children[0].disabled, true);
  const unavailable = await scenario(models.map(model => ({...model, available: false})), "qwen");
  assert.equal(unavailable.get("send").disabled, true);
  unavailable.get("message").value = "must not send";
  await unavailable.get("chat-form").listeners.submit({preventDefault() {}});
  assert.equal(unavailable.calls.filter(call => call.path === "/api/chat").length, 0);
  const cpu = await scenario([{...models[0], device: "cpu"}, models[1]], "qwen");
  assert.match(cpu.get("status").textContent, /CPU-only/);
  cpu.get("chat-mode").value = "library";
  cpu.get("chat-mode").listeners.change();
  assert.equal(cpu.get("message").maxLength, 4000);
  const offline = await scenario(models, "qwen", "offline");
  offline.context.addMessage("assistant", "stale synthetic chat");
  offline.get("tool-output").textContent = "stale evidence";
  offline.get("image-output").textContent = "stale API response";
  await assert.rejects(offline.context.api("/api/conversations"), /HOSTへのリクエスト/);
  await offline.context.checkHost();
  assert.equal(offline.get("status").textContent, "Pitchi-chat HOSTへ接続できません");
  assert.equal(offline.get("offline-notice").hidden, false);
  assert.equal(offline.get("messages").children.length, 0);
  assert.equal(offline.get("tool-output").textContent, "");
  assert.equal(offline.get("image-output").textContent, "");
  assert.equal(offline.get("resource-panel").open, false);
  assert.ok(offline.calls.every(call => call.options?.cache === "no-store"));
  offline.setOnline();
  await offline.context.checkHost();
  for (let i = 0; i < 40; i++) await Promise.resolve();
  assert.equal(offline.get("offline-notice").hidden, true, "a successful health response clears offline state");
  assert.equal(offline.get("error").textContent, "", "recovery clears the stale connection error");
  assert.equal(offline.get("send").disabled, false, "recovery re-initializes controls without reloading");
  const thinking = await scenario(models, "qwen");
  thinking.context.addMessage("assistant", "こんにちは", "synthetic private thought");
  const article = thinking.get("messages").children.at(-1);
  assert.equal(article.children[0].children[0].textContent, "アシスタント");
  assert.equal(article.children[1].textContent, "こんにちは");
  const debug = article.children[2];
  assert.equal(debug.tagName, "details");
  assert.equal(debug.open, false, "Thinking is closed by default");
  assert.equal(debug.children[0].textContent, "Thinking / Debug");
  assert.equal(debug.children[1].textContent, "synthetic private thought");
  thinking.context.addMessage("assistant", "plain final");
  assert.equal(thinking.get("messages").children.at(-1).children.length, 2, "No reasoning means no Thinking UI");
  thinking.setOffline(); await thinking.context.checkHost();
  assert.equal(thinking.get("messages").children.length, 0, "Offline clears transient reasoning too");
  const wire = 'event: delta\ndata: {"content":"日本語"}\n\nevent: done\ndata: {"conversation_id":"synthetic-conversation","reply":"日本語の回答","generation_metrics":{"elapsed_seconds":2,"time_to_first_token_seconds":1,"tokens_per_second":3,"finish_reason":"stop"}}\n\n';
  const strata = await scenario([{id:"strata",label:"Strata IQ2_XS",available:true,device:"gpu",connection_mode:"host_owned"}], "strata", null,
    {status:"ok",storage:"ok"}, {respond:async (path) => {
      if (path==="/api/chat/stream") return {ok:true,status:200,json:async()=>({job_id:"synthetic-job"})};
      if (!path.endsWith("/events")) return null;
      const events='event: snapshot\ndata: {"reply":"日本語の回答","revision":2}\n\nevent: done\ndata: {"conversation_id":"synthetic-conversation","reply":"日本語の回答","generation_metrics":{"elapsed_seconds":2,"time_to_first_token_seconds":1,"tokens_per_second":3,"finish_reason":"stop"}}\n\n';
      let i=0; const parts=[events.slice(0,17),events.slice(17)];
      return {ok:true,body:{getReader:()=>({read:async()=>i<parts.length?{value:Buffer.from(parts[i++]),done:false}:{done:true},cancel:async()=>{}})}};
    }});
  strata.get("chat-model").listeners.change();
  assert.equal(strata.calls.filter(x=>x.path.endsWith("/lifecycle")).length,0,"selection must not start/stop services");
  strata.get("strata-streaming").checked=true; strata.get("message").value="こんにちは"; strata.get("chat-mode").value="chat";
  await strata.get("chat-form").listeners.submit({preventDefault(){}});
  assert.equal(strata.get("error").textContent,"");
  assert.equal(strata.get("messages").children.at(-1).children[1].textContent,"日本語の回答");
  assert.equal(strata.get("strata-preview").textContent,"");
  assert.equal(strata.get("strata-preview").hidden,true);
  assert.match(strata.get("generation-timing").textContent,/TTFT 1.00/);
  assert.equal(strata.calls.find(x=>x.path==="/api/chat/stream").options.cache,"no-store");
  assert.ok(strata.calls.some(x=>x.path==="/api/chat/stream/jobs/synthetic-job/events"));
  const broken = await scenario([{id:"strata",label:"Strata",available:true}],"strata",null,{status:"ok",storage:"ok"},
    {respond:async path => {
      if(path==="/api/chat/stream") return {ok:true,status:200,json:async()=>({job_id:"broken-job"})};
      if(!path.endsWith("/events")) return null;
      let sent=false;return {ok:true,body:{getReader:()=>({read:async()=>sent?{done:true}:(sent=true,{value:Buffer.from('event: snapshot\ndata: {"reply":"provisional","revision":1}\n\nevent: error\ndata: {"detail":"model error"}\n\n'),done:false}),cancel:async()=>{}})}};
    }});
  await assert.rejects(broken.context.streamChat({message:"test",model:"strata"}),/model error/);
  assert.equal(broken.get("strata-preview").textContent,"");
  assert.equal(broken.get("messages").children.length,0,"failed stream cannot become an assistant message");

  console.log("Model selector DOM/mock checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
