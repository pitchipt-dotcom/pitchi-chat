"use strict";
const byId = (id) => document.getElementById(id);
const FRONTEND_RELEASE = "pwa-18";
// Transient DOM only; no storage. Future Developer settings can select hidden.
const THINKING_VISIBILITY = "collapsible";
const HOST_UNAVAILABLE = "Pitchi-chat HOSTへ接続できません";
let hostState = "checking";
let connectionGeneration = 0;
let healthCheckPromise = null;
let healthTimer = null;
let hydrationPromise = null;
let needsHydration = true;
let preferredModelId = null;
const diagnostics = {release: FRONTEND_RELEASE, state: hostState, health_url: typeof window !== "undefined"
  ? new URL("/health", window.location.href).href : "/health", health_http_status: null, health_valid: null,
  health_fetch_cache: "no-store", last_request_error: null,
  display_mode: typeof window !== "undefined" && window.matchMedia?.("(display-mode: standalone)").matches ? "standalone" : "browser",
  worker: "none", worker_release: "unknown", shell_source: "unknown", events: []};
function diagnosticPath(path) {
  // Never record queries, conversation/job IDs, input, or response content.
  return path.split("?")[0].replace(/(\/api\/(?:jobs|conversations|chat\/stream\/(?:jobs|by-client))\/)[^/]+/g, "$1:id");
}
function recordDiagnostic(event, fields = {}) {
  diagnostics.state = hostState;
  diagnostics.events.push({event, ...fields});
  diagnostics.events = diagnostics.events.slice(-16);
  const output = byId("connection-diagnostics-output");
  if (output) output.textContent = JSON.stringify(diagnostics, null, 2);
}
class FrontendRequestError extends Error {
  constructor(kind, message, code = null) { super(message); this.kind = kind; this.code = code; }
}
function connectionError(error) { return ["network", "timeout", "stale"].includes(error?.kind); }
let conversationId = null;
let modelState = null;
let isBusy = false;
let toolState = null;
let imageState = null;
let imageJob = null;
let imageSpec = null;
let imageCompilerJob = null;
let imageCompiling = false;
let mediaPreviewJob = null;
const chatImages = globalThis.PitchImages ? globalThis.PitchImages({api,
  getConversation: () => conversationId,
  setConversation: id => { conversationId = id; },
  refreshHistory, refreshConversation: restoreConversationMessages, isBusy: () => isBusy || hostState !== "online"}) : null;
const chatVideos = globalThis.PitchVideos ? globalThis.PitchVideos({api,
  getConversation: () => conversationId, setConversation: id => { conversationId = id; },
  refreshHistory, isBusy: () => isBusy || hostState !== "online"}) : null;
const longVideos = globalThis.PitchLongVideos ? globalThis.PitchLongVideos({api,
  getConversation: () => conversationId, setConversation: id => {conversationId = id;},
  refreshHistory, isBusy: () => isBusy || hostState !== 'online'}) : null;
const chatDeletions = globalThis.PitchDeletions ? globalThis.PitchDeletions({api,
  isBusy: () => isBusy || hostState !== "online",
  onDeleted: async result => {
    const removedConversation = result.targets.some(x => x.kind === 'conversation' && x.id === conversationId);
    // Invalidate pending timeline reads and remove all stale media player URLs.
    if (chatImages) await chatImages.load(null);
    if (chatVideos) await chatVideos.load(null); if(longVideos) await longVideos.load(null);
    byId('tool-artifacts').replaceChildren();
    byId('image-results').replaceChildren(); byId('image-output').textContent = ''; byId('tool-output').textContent = '';
    byId('image-editor').hidden = true;
    imageJob = toolJob = mediaPreviewJob = imageCompilerJob = imageSpec = null;
    if (removedConversation) {
      localStorage.removeItem(PENDING_CHAT_KEY); activeChatJob = null;
      byId('new-chat').click();
    } else if (conversationId) {
      if (chatImages) await chatImages.load(conversationId);
      if (chatVideos) await chatVideos.load(conversationId); if(longVideos) await longVideos.load(conversationId);
    }
    await refreshHistory(); busy(isBusy);
  }}) : null;
globalThis.pitchDeleteItem = (kind,id) => chatDeletions?.item(kind,id);
let imageIsMedia = false;
let toolJob = null;
let loadedModelId = null;
let activeChatJob = null;
let pendingRecovery = false;
const PENDING_CHAT_KEY = "pitch-chat-active-strata-generation";
if (window.matchMedia?.("(max-width: 550px)").matches) {
  byId("management-menu").open = false;
  byId("conversation-menu").open = false;
}
function showHostOffline() {
  if (chatDeletions) chatDeletions.offline();
  if (hostState !== "offline") connectionGeneration++;
  hostState = "offline";
  needsHydration = true;
  preferredModelId = byId("chat-model").value || preferredModelId;
  modelState = toolState = imageState = null;
  loadedModelId = null;
  byId("selected-model-label").textContent = "モデルを再接続中…";
  byId("active-model-label").textContent = "稼働中: 未確認";
  byId("strata-preview").textContent = byId("generation-timing").textContent = byId("evaluation-result").textContent = "";
  byId("strata-preview").hidden = true;
  byId("chat-model").replaceChildren(new Option("HOSTへの接続を待機中", ""));
  byId("tool-selector").replaceChildren(new Option("HOSTへの接続を待機中", ""));
  byId("image-backend").replaceChildren();
  byId("status").textContent = HOST_UNAVAILABLE;
  byId("offline-notice").hidden = false;
  byId("error").textContent = "";
  byId("messages").replaceChildren();
  window.pitchiWork?.offline();
  chatVideos?.offline(); longVideos?.offline();
  byId("conversations").replaceChildren(new Option("HOSTへ接続できません", ""));
  byId("tool-output").textContent = "";
  byId("tool-artifacts").replaceChildren();
  byId("image-output").textContent = "";
  byId("image-results").replaceChildren();
  byId("image-status").textContent = "Pitchi-chat HOSTへ接続できません";
  byId("resource-status").textContent = "HOSTへ接続できません";
  for (const id of ["running-model", "requested-model", "actual-model", "fallback-reason", "cloud-render-status", "gpu-blocking-owner", "running-tool", "gpu-status", "job-status"]) byId(id).textContent = "—";
  byId("waiting-jobs").textContent = "—";
  byId("resource-panel").open = false;
  busy(isBusy);
}
function markHostOnline() {
  const changed = hostState !== "online";
  hostState = "online";
  byId("offline-notice").hidden = true;
  if (byId("error").textContent === HOST_UNAVAILABLE || byId("error").textContent.startsWith("HOSTへのリクエスト")) byId("error").textContent = "";
  if (changed && modelState) updateModelStatus();
  else if (!modelState) byId("status").textContent = "HOSTに接続しました。利用準備中…";
  busy(isBusy);
  if (changed) recordDiagnostic("host_online");
}
function busy(value) {
  isBusy = value;
  for (const id of ["strata-reasoning", "strata-temperature", "strata-max-tokens", "strata-thinking-budget", "strata-streaming", "strata-preflight"]) byId(id).disabled = value || hostState !== "online";
  for (const id of ["send", "new-chat", "conversations", "message", "chat-mode"]) byId(id).disabled = value;
  byId("cancel-generation").hidden = !activeChatJob;
  byId("cancel-generation").disabled = !activeChatJob;
  const disconnected = hostState !== "online";
  byId("chat-model").disabled = value || disconnected || !modelState;
  const gpuModel = selectedModel()?.device === "gpu" && selectedModel()?.connection_mode === "host_owned";
  byId("model-start").disabled = value || disconnected || !gpuModel || !selectedModel()?.available;
  byId("model-stop").disabled = value || disconnected || !gpuModel;
  byId("evidence-query").disabled = value;
  byId("external-retrieval").disabled = value;
  byId("send").disabled = value || disconnected || (byId("chat-mode").value === "video" ? false : byId("chat-mode").value === "work" ? !window.pitchiWork?.available() : !selectedModel()?.available);
  byId("send").textContent = value ? "処理中…" : "送信";
  byId("tool-selector").disabled = value || disconnected || !toolState;
  byId("tool-query").disabled = value;
  const tool = selectedTool();
  byId("tool-run").disabled = value || disconnected || !!toolJob || !tool?.enabled || !["book.search", "tts.generate", "pitch_work.run"].includes(tool?.id);
  byId("tool-run").textContent = tool?.id === "tts.generate" ? "音声jobを作成" : tool?.id === "pitch_work.run" ? "Work jobを作成" : "検索";
  byId("tool-query-label").textContent = tool?.id === "tts.generate" ? "読み上げる文章（local speech job）" : tool?.id === "pitch_work.run" ? "隔離projectへの依頼（Renderer不使用）" : "蔵書検索語（会話履歴には保存しません）";
  byId("tool-query").maxLength = tool?.id === "book.search" ? 500 : 4000;
  byId("tool-project").hidden = byId("tool-project-label").hidden = tool?.id !== "pitch_work.run";
  byId("tool-project").disabled = value || !!toolJob;
  const imageReady = imageState?.backends.some(backend => backend.id === byId("image-backend").value && ["ready", "on_demand"].includes(backend.status));
  const imagePreviewReady = imageState?.backends.some(backend => backend.id === byId("image-backend").value && ["ready", "on_demand", "compile_only"].includes(backend.status));
  const promptMode = byId("image-prompt-mode").value || "compiler";
  byId("image-preview").disabled = value || disconnected || !!imageJob || !imagePreviewReady || (promptMode === "compiler" && !selectedModel()?.available);
  byId("image-generate").disabled = value || disconnected || !!imageJob || !imageReady || !imageState?.generation_enabled || (promptMode === "compiler" && !imageSpec) || (["strata", "qwen35"].includes(promptMode) && byId("image-backend").value === "flux-klein" && !mediaPreviewJob);
  byId("media-positive").disabled = value || !!imageJob;
  byId("media-negative").disabled = value || !!imageJob || byId("image-backend").value === "flux-klein";
  byId("image-prompt-mode").disabled = value || !!imageJob;
  for (const id of ["image-request", "image-backend", "image-aspect", "image-purpose", "image-subject", "image-style", "image-composition", "image-text", "image-negative"]) byId(id).disabled = value || !!imageJob;
}
function newGenerationRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, token => {
    const random = Math.floor(Math.random() * 16);
    return (token === "x" ? random : (random & 3) | 8).toString(16);
  });
}
function selectedTool() {
  return toolState?.tools.find((tool) => tool.id === byId("tool-selector").value);
}
async function refreshTools() {
  const data = await api("/api/tools");
  if (!Array.isArray(data?.tools)) throw new FrontendRequestError("schema", "Tool一覧の形式を読み取れません。");
  toolState = data;
  byId("tool-selector").replaceChildren();
  for (const tool of toolState.tools) {
    if (tool.id === 'image.understand') continue; // Reference selection belongs to the image UI.
    const option = new Option(`${tool.id}${tool.enabled ? "" : "（利用不可）"}`, tool.id);
    option.disabled = !tool.enabled;
    byId("tool-selector").add(option);
  }
  byId("tool-selector").value = toolState.tools.find((tool) => tool.enabled && tool.id !== 'image.understand')?.id || "";
  byId("tool-project").replaceChildren();
  const projects = toolState.tools.find(tool => tool.id === "pitch_work.run")?.projects || [];
  for (const project of projects) byId("tool-project").add(new Option(project, project));
  byId("tool-project").value = projects[0] || "";
  busy(isBusy);
}
function selectedModel() {
  return modelState?.models.find((model) => model.id === byId("chat-model").value);
}
function showModelSelection(selection) {
  if (selection?.fallback_reason) {
    byId("management-menu").open = true;
    byId("resource-panel").open = true;
  }
  byId("cloud-render-status").textContent = "未使用 / 履歴の利用状況は未記録";
  const label = (id) => modelState?.models.find(model => model.id === id)?.label || id || "—";
  byId("requested-model").textContent = label(selection?.requested_model);
  byId("actual-model").textContent = label(selection?.actual_model_used);
  const reasons = {external_gpu_workload: "外部GPU競合 → HOSTがlocal CPUを選択", local_model_failure: "local model失敗 → HOST policy経由fallback"};
  byId("fallback-reason").textContent = reasons[selection?.fallback_reason] || "—";
  byId("gpu-blocking-owner").textContent = selection?.gpu_blocking_owners?.join(" / ") || "—";
}
function updateModelStatus() {
  if (hostState !== "online") { busy(isBusy); return; }
  const model = selectedModel();
  const strata = model?.id === "strata";
  byId("strata-options").hidden = byId("strata-preflight").hidden = byId("strata-preflight-result").hidden = !strata;
  byId("status").textContent = model?.available ? `${model.label}${model.device === "cpu" ? " / CPU-only（GPU未使用・応答に時間がかかる場合があります）" : ""}: 接続先設定済み（疎通未確認）` : "モデル接続先が未設定です";
  updateModelIndicators();
  busy(isBusy);
}
function updateModelIndicators() {
  if (!modelState) return;
  const label = id => modelState.models.find(model => model.id === id)?.label || id;
  const selected = selectedModel();
  byId("selected-model-label").textContent = selected
    ? `選択: ${selected.label}${selected.device === "gpu" && selected.id !== loadedModelId ? "（未起動）" : ""}`
    : "モデルを選択してください";
  byId("active-model-label").textContent = `稼働中: ${loadedModelId ? label(loadedModelId) : "なし"}`;
  for (const model of modelState.models) {
    const option = [...byId("chat-model").children].find(item => item.value === model.id);
    if (option) option.textContent = `${model.label}${model.available ? "" : "（未設定）"}${model.available && model.device === "gpu" ? model.id === loadedModelId ? "（起動済み）" : "（未起動）" : ""}`;
  }
}
async function refreshModels() {
  const previous = byId("chat-model").value || preferredModelId;
  const data = await api("/api/models");
  if (!Array.isArray(data?.models)) throw new FrontendRequestError("schema", "モデル一覧の形式を読み取れません。");
  modelState = data;
  byId("chat-model").replaceChildren();
  for (const model of modelState.models) {
    const option = new Option(`${model.label}${model.available ? "" : "（未設定）"}${model.available && model.device === "gpu" ? model.id === loadedModelId ? "（起動済み）" : "（未起動）" : ""}`, model.id);
    option.disabled = !model.available;
    byId("chat-model").add(option);
  }
  const preferred = modelState.models.find((model) => model.id === previous && model.available)
    || modelState.models.find((model) => model.id === modelState.default_model && model.available)
    || modelState.models.find((model) => model.available);
  byId("chat-model").value = preferred?.id || modelState.default_model;
  updateModelIndicators();
  updateModelStatus();
}
async function api(path, options) {
  const generation = connectionGeneration;
  const readOnly = !options?.method || options.method === "GET";
  const controller = readOnly && !options?.signal ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), path === "/health" ? 5000 : 10000) : null;
  try {
    const response = await fetch(path, {...options, ...(controller ? {signal: controller.signal} : {}), cache: "no-store"});
    if (path === "/health") diagnostics.health_http_status = response.status;
    recordDiagnostic("http", {path: diagnosticPath(path), status: response.status});
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { /* Non-JSON server error. */ }
      const code = detail && typeof detail === "object" && typeof detail.code === "string" ? detail.code : null;
      const message = typeof detail === "string" ? detail : detail && typeof detail.message === "string"
        ? detail.message : `リクエストに失敗しました (${response.status})。`;
      throw new FrontendRequestError("http", message, code);
    }
    const data = await response.json();
    if (path !== "/health" && generation !== connectionGeneration) {
      throw new FrontendRequestError("stale", "接続が変わりました。最新の状態を再取得しています。");
    }
    return data;
  } catch (error) {
    const kind = error.kind || (error.name === "AbortError" ? "timeout" : error.name === "SyntaxError" ? "json" : "network");
    // Error names/classifications only: browser messages can include private URLs.
    diagnostics.last_request_error = {path: diagnosticPath(path), kind,
      error_name: ["TypeError", "AbortError", "SyntaxError"].includes(error.name) ? error.name : "RequestError"};
    recordDiagnostic("request_error", diagnostics.last_request_error);
    if (path !== "/health" && ["network", "timeout"].includes(kind)) queueHealthCheck();
    if (error instanceof FrontendRequestError) throw error;
    throw new FrontendRequestError(kind, kind === "json" ? "応答形式を読み取れません。" : "HOSTへのリクエストが届きません。接続を確認しています。");
  } finally { if (timer !== null) clearTimeout(timer); }
}
function queueHealthCheck() {
  if (healthCheckPromise) return;
  if (healthTimer !== null) clearTimeout(healthTimer);
  healthTimer = setTimeout(checkHost, 0);
}
function checkHost() {
  if (healthCheckPromise) return healthCheckPromise;
  if (healthTimer !== null) clearTimeout(healthTimer);
  healthCheckPromise = (async () => {
    diagnostics.health_http_status = null;
    try {
      const health = await api("/health");
      diagnostics.health_valid = health?.status === "ok" && health?.storage === "ok";
      recordDiagnostic("health", {valid: diagnostics.health_valid});
      if (!diagnostics.health_valid) throw new FrontendRequestError("health_contract", HOST_UNAVAILABLE);
    } catch (error) {
      diagnostics.health_valid = false;
      showHostOffline();
      recordDiagnostic("host_offline", {reason: error.kind || "network"});
    } finally {
      if (diagnostics.health_valid) {
        try {
          markHostOnline();
          if (needsHydration && !isBusy) void hydrateUi().then(resumePendingGeneration);
          else void resumePendingGeneration();
        } catch {
          // A DOM/UI error must never be reported as a failed health request.
          needsHydration = true;
          recordDiagnostic("ui_error", {kind: "initialization"});
        }
      }
      healthCheckPromise = null;
      healthTimer = setTimeout(checkHost, 5000);
    }
  })();
  return healthCheckPromise;
}
function hydrateUi() {
  if (hydrationPromise) return hydrationPromise;
  const generation = connectionGeneration;
  hydrationPromise = (async () => {
    const tasks = [refreshModels, refreshTools, refreshHistory, refreshImageBackends];
    if (chatImages) tasks.push(chatImages.capabilities);
    if (chatVideos) tasks.push(chatVideos.capabilities);
    const results = await Promise.allSettled(tasks.map(task => task()));
    if (generation !== connectionGeneration || hostState !== "online") return;
    needsHydration = results.some(result => result.status === "rejected");
    if (needsHydration) {
      byId("error").textContent = "HOSTには接続しています。一部のUI情報を再取得中です。";
      if (!modelState) byId("status").textContent = "HOSTに接続済み。モデル一覧を再取得中…";
    } else if (byId("error").textContent.startsWith("HOSTには接続しています")) byId("error").textContent = "";
    recordDiagnostic("ui_initialization", {ready: !needsHydration, failed: results.map((r, i) => r.status === "rejected" ? i : null).filter(i => i !== null)});
    // Re-fetch current history from HOST after reconnect; never restore an offline copy.
    const selected = conversationId;
    if (selected && !isBusy) {
      try {
        const data = await api(`/api/conversations/${encodeURIComponent(selected)}`);
        if (selected === conversationId && !isBusy) {
          byId("messages").replaceChildren();
          for (const message of data.messages) addMessage(message.role, message.content);
          if (window.pitchiWork) await window.pitchiWork.restore();
        }
      } catch { needsHydration = true; }
    }
    if (selected && chatImages) {
      try { await chatImages.load(selected); if (chatVideos) await chatVideos.load(selected); if(longVideos) await longVideos.load(selected); } catch { needsHydration = true; }
    }
    busy(isBusy);
  })().finally(() => { hydrationPromise = null; });
  return hydrationPromise;
}
async function copyText(value, button) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
    else {
      const field = document.createElement("textarea");
      field.value = value; field.setAttribute("readonly", "");
      field.style.position = "fixed"; field.style.opacity = "0";
      document.body.append(field); field.select();
      const copied = document.execCommand("copy"); field.remove();
      if (!copied) throw new Error("copy unavailable");
    }
    button.textContent = "コピー済み";
    window.setTimeout(() => { button.textContent = button.dataset.copyLabel; }, 1600);
  } catch {
    button.textContent = "コピー不可";
    window.setTimeout(() => { button.textContent = button.dataset.copyLabel; }, 2000);
  }
}
function copyButton(value, label) {
  const button = document.createElement("button");
  button.type = "button"; button.className = "copy-button";
  button.textContent = "コピー"; button.dataset.copyLabel = "コピー";
  button.setAttribute("aria-label", label);
  button.addEventListener("click", () => copyText(value, button));
  return button;
}
function appendInlineMarkdown(parent, value) {
  const token = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\n]+\*|_[^_\n]+_)/g;
  let cursor = 0, match;
  while ((match = token.exec(value))) {
    if (match.index > cursor) parent.append(document.createTextNode(value.slice(cursor, match.index)));
    let node;
    if (match[1] !== undefined) {
      try {
        const url = new URL(match[2]);
        if (!["http:", "https:"].includes(url.protocol)) throw new Error("unsupported URL");
        node = document.createElement("a"); node.href = url.href; node.target = "_blank";
        node.rel = "noopener noreferrer"; node.textContent = match[1];
      } catch { node = document.createTextNode(match[0]); }
    } else {
      const raw = match[3];
      const strong = raw.startsWith("**") || raw.startsWith("__");
      const code = raw.startsWith("`");
      node = document.createElement(code ? "code" : strong ? "strong" : "em");
      node.textContent = raw.slice(strong ? 2 : 1, -(strong ? 2 : 1));
    }
    parent.append(node); cursor = token.lastIndex;
  }
  if (cursor < value.length) parent.append(document.createTextNode(value.slice(cursor)));
}
function renderMarkdown(root, source) {
  const lines = String(source).replace(/\r\n?/g, "\n").split("\n");
  const blockStart = line => /^(\s*```|\s{0,3}#{1,6}\s|\s*[-*+]\s+|\s*\d+[.)]\s+|\s*>\s?|\s{0,3}(?:---+|\*\*\*+)\s*$)/.test(line);
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^\s*```\s*([^\s`]*)/);
    if (fence) {
      const codeLines = []; i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) codeLines.push(lines[i++]);
      if (i < lines.length) i++;
      const wrapper = document.createElement("div"); wrapper.className = "code-block";
      const toolbar = document.createElement("div"); toolbar.className = "code-block-header";
      const language = document.createElement("span"); language.textContent = fence[1] || "コード";
      toolbar.append(language, copyButton(codeLines.join("\n"), "コードをコピー"));
      const pre = document.createElement("pre"), code = document.createElement("code");
      code.textContent = codeLines.join("\n"); pre.append(code); wrapper.append(toolbar, pre); root.append(wrapper);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const node = document.createElement(`h${Math.min(4, heading[1].length)}`);
      appendInlineMarkdown(node, heading[2]); root.append(node); i++; continue;
    }
    if (/^\s{0,3}(?:---+|\*\*\*+)\s*$/.test(line)) { root.append(document.createElement("hr")); i++; continue; }
    if (/^\s*>/.test(line)) {
      const quote = document.createElement("blockquote");
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        const p = document.createElement("p"); appendInlineMarkdown(p, lines[i++].replace(/^\s*>\s?/, "")); quote.append(p);
      }
      root.append(quote); continue;
    }
    const list = line.match(/^\s{0,3}([-*+]|\d+[.)])\s+/);
    if (list) {
      const ordered = /^\d/.test(list[1]), container = document.createElement(ordered ? "ol" : "ul");
      while (i < lines.length) {
        const item = lines[i].match(/^\s{0,3}([-*+]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const li = document.createElement("li"); appendInlineMarkdown(li, item[2]); container.append(li); i++;
      }
      root.append(container); continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !(paragraph.length && blockStart(lines[i]))) paragraph.push(lines[i++]);
    const p = document.createElement("p");
    paragraph.forEach((part, index) => { if (index) p.append(document.createElement("br")); appendInlineMarkdown(p, part); });
    root.append(p);
  }
}
function addMessage(role, content, reasoning = null) {
  const legacyWorkReceipt = role === "assistant" && /^仕事を受け付けました。run: \d{8}_\d{6}_[a-f0-9]+$/.test(content);
  const article = document.createElement("article");
  article.className = `message ${role === "user" ? "user" : "assistant"}`;
  const label = document.createElement("strong");
  // Historical v0.1 turns have no model attribution; do not invent one on restore.
  label.textContent = role === "user" ? "あなた" : "アシスタント";
  const header = document.createElement("div"); header.className = "message-header";
  header.append(label);
  if (role === "assistant") header.append(copyButton(content, "回答をコピー"));
  const body = document.createElement("div"); body.className = "message-content";
  if (role === "assistant" && !legacyWorkReceipt) renderMarkdown(body, content);
  else {
    const text = document.createElement("p");
    text.textContent = legacyWorkReceipt ? "仕事を受け付けました。進行状況をお知らせします。" : content;
    body.append(text);
  }
  article.append(header, body);
  if (legacyWorkReceipt) {
    const detail = document.createElement("details");
    const summary = document.createElement("summary"); summary.textContent = "技術詳細";
    const original = document.createElement("pre"); original.textContent = content;
    detail.append(summary, original); article.append(detail);
  }
  if (role === "assistant" && typeof reasoning === "string" && reasoning.trim() && THINKING_VISIBILITY === "collapsible") {
    const debug = document.createElement("details");
    debug.className = "thinking-debug";
    debug.open = false;
    const summary = document.createElement("summary");
    summary.textContent = "Thinking / Debug";
    const thinking = document.createElement("pre");
    thinking.textContent = reasoning; // Never HTML/markdown execution.
    debug.append(summary, thinking);
    article.append(debug);
  }
  byId("messages").append(article);
  byId("messages").scrollTop = byId("messages").scrollHeight;
}
function showTiming(metrics) {
  byId("generation-timing").textContent = metrics ? `TTFT ${metrics.time_to_first_token_seconds?.toFixed(2) ?? "—"}秒 / ${metrics.tokens_per_second?.toFixed(1) ?? "—"} tok/s / ${metrics.elapsed_seconds.toFixed(2)}秒${metrics.finish_reason === "length" ? "（出力上限で終了）" : ""}` : "";
}
async function streamChat(payload, clientRequestId, resumedJobId = null) {
  const preview = byId("strata-preview"); preview.textContent = ""; preview.hidden = false;
  clientRequestId ||= newGenerationRequestId();
  try { localStorage.setItem(PENDING_CHAT_KEY, clientRequestId); } catch { /* in-memory recovery still works until this page closes */ }
  let jobId = resumedJobId;
  if (!jobId) {
    let delay = 500;
    while (!jobId) {
      try {
        const accepted = await api("/api/chat/stream", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({...payload, client_request_id: clientRequestId})});
        jobId = accepted.job_id;
      } catch (error) {
        if (!connectionError(error)) {
          try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
          throw error;
        }
        void checkHost();
        await new Promise(resolve => setTimeout(resolve, delay));
        delay = Math.min(3000, Math.round(delay * 1.5));
      }
    }
  }
  activeChatJob = jobId;
  busy(isBusy);
  let delay = 500;
  while (true) {
    let reader;
    try {
      const response = await fetch(`/api/chat/stream/jobs/${encodeURIComponent(jobId)}/events`, {cache: "no-store"});
      if (!response.ok) {
        if (response.status === 404) {
          try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
          activeChatJob = null;
          throw new FrontendRequestError("http", "前回の生成状態を復元できません。");
        }
        throw new TypeError("generation status temporarily unavailable");
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder(); let pending = "", done = null;
      while (!done) {
        const chunk = await reader.read();
        if (chunk.done) break; // subscriber disconnected; reconnect to the same durable job
        pending += decoder.decode(chunk.value, {stream: true}).replace(/\r\n/g, "\n");
        if (pending.length > 1100000) throw new Error("応答が上限を超えました。");
        let end;
        while ((end = pending.indexOf("\n\n")) >= 0) {
          const frame = pending.slice(0, end); pending = pending.slice(end + 2);
          const kind = frame.split("\n").find(x => x.startsWith("event:"))?.slice(6).trim();
          const raw = frame.split("\n").filter(x => x.startsWith("data:")).map(x => x.slice(5).trim()).join("\n");
          if (!raw) continue;
          const data = JSON.parse(raw);
          if (kind === "snapshot") {
            if (typeof data.reply !== "string" || data.reply.length > 1000000) throw new FrontendRequestError("generation", "応答形式が不正です。");
            preview.textContent = data.reply;
          } else if (kind === "error") {
            try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
            activeChatJob = null;
            preview.textContent = ""; preview.hidden = true;
            throw new FrontendRequestError("generation", data.detail || "応答を完了できませんでした。");
          }
          else if (kind === "done") done = data;
        }
      }
      if (done) {
        if (typeof done.reply !== "string" || !done.conversation_id) throw new FrontendRequestError("generation", "完了応答の形式が不正です。");
        preview.textContent = ""; preview.hidden = true;
        activeChatJob = null;
        return done;
      }
    } catch (error) {
      if (error instanceof FrontendRequestError && !connectionError(error)) throw error;
      if (error instanceof SyntaxError || /応答(が|形式|上限)|生成を停止|完了できません/.test(error.message)) throw error;
      // Health checks and UI hydration are read-only; the original POST is never replayed.
      void checkHost();
    } finally {
      if (reader) await reader.cancel().catch(() => {});
    }
    await new Promise(resolve => setTimeout(resolve, delay));
    delay = Math.min(3000, Math.round(delay * 1.5));
  }
}
async function refreshEvaluation() {
  if (!conversationId) throw new Error("評価する会話を選んでください。");
  const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/evaluation`);
  byId("evaluation-result").textContent = JSON.stringify(data, null, 2);
}
byId("evaluation-load").addEventListener("click", async () => {
  try { await refreshEvaluation(); } catch (error) { byId("error").textContent = error.message; }
});
byId("evaluation-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (isBusy || hostState !== "online" || !conversationId) { byId("error").textContent = "評価する会話を選んでください。"; return; }
  try {
    await api(`/api/conversations/${encodeURIComponent(conversationId)}/evaluation`, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({category: byId("evaluation-category").value, score: Number(byId("evaluation-score").value), note: byId("evaluation-note").value})});
    await refreshEvaluation();
  } catch (error) { byId("error").textContent = error.message; }
});
async function strataPreflight() {
  const data = await api("/api/models/strata/preflight");
  const reasons = {external_gpu_workload: "既存GPUサービスとの競合。停止候補を確認し、明示承認後に管理者が停止してください。",
    unknown_gpu_process: "未確認のGPU利用があります。管理者の調査が必要です。", insufficient_ram: "RAM available不足", insufficient_swap_reserve: "Swap空き不足",
    insufficient_vram: "VRAM余裕不足", other_model_loaded: "別モデルが起動中。停止を別途明示してください。", profile_unmanaged: "Strata起動設定が未登録"};
  byId("strata-preflight-result").textContent = `RAM available ${data.ram_available_mib} MiB / 必要 ${data.admission_ram_mib ?? data.required_ram_mib ?? "—"} MiB\nSwap空き ${data.swap_free_mib} MiB / 必要 ${data.min_swap_free_mib ?? "—"} MiB\nVRAM空き ${data.vram_free_mib ?? "—"} MiB\n${data.message || (data.ready_to_start ? "起動前条件を満たしています" : reasons[data.reason] || "起動安全条件を確認できません")}\n${data.ready_to_start ? "" : data.recovery?.instructions || ""}\n停止候補: ${(data.blocking_owners || []).join(", ") || (data.ready_to_start ? "なし" : "管理者が確認してください")}`;
  return data;
}
byId("strata-preflight").addEventListener("click", async () => {
  try { await strataPreflight(); } catch (error) { byId("error").textContent = error.message; }
});
async function refreshHistory() {
  const data = await api("/api/conversations");
  if (!Array.isArray(data?.conversations)) throw new FrontendRequestError("schema", "履歴一覧の形式を読み取れません。");
  byId("conversations").replaceChildren(new Option("会話を選択", ""));
  for (const item of data.conversations) byId("conversations").add(new Option(item.title, item.id));
  byId("conversations").value = conversationId || "";
}
async function restoreConversationMessages(id) {
  conversationId = id;
  byId("messages").replaceChildren();
  try {
    const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
    for (const message of data.messages) addMessage(message.role, message.content);
    if (chatImages) await chatImages.load(id);
    if (chatVideos) await chatVideos.load(id); if(longVideos) await longVideos.load(id);
  } catch (error) {
    if (!/会話が見つかりません/.test(error.message)) throw error;
  }
  byId("conversations").value = id;
}
async function resumePendingGeneration() {
  if (pendingRecovery || isBusy || hostState !== "online") return;
  let clientRequestId;
  try { clientRequestId = localStorage.getItem(PENDING_CHAT_KEY); } catch { return; }
  if (!clientRequestId || !/^[0-9a-f-]{36}$/i.test(clientRequestId)) return;
  pendingRecovery = true;
  try {
    const job = await api(`/api/chat/stream/by-client/${encodeURIComponent(clientRequestId)}`);
    await restoreConversationMessages(job.conversation_id);
    if (job.status === "COMPLETED") {
      try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
      byId("message").value = "";
      return;
    }
    if (!["QUEUED", "RUNNING", "CANCELLING"].includes(job.status)) {
      try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
      byId("message").value = "";
      byId("error").textContent = job.status === "CANCELLED" ? "生成は停止されました。" : "前回の生成は復元できませんでした。質問を確認して再送してください。";
      return;
    }
    byId("chat-model").value = job.model === "vision" ? "vision" : "strata";
    updateModelStatus();
    byId("message").value = job.message || "";
    activeChatJob = job.job_id;
    busy(true);
    byId("error").textContent = "生成中の回答へ再接続しました。送信は再実行していません。";
    const answer = await streamChat(null, clientRequestId, job.job_id);
    showTiming(answer.generation_metrics);
    await restoreConversationMessages(job.conversation_id);
    try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
    byId("message").value = "";
    byId("error").textContent = "";
    await refreshHistory();
  } catch (error) {
    if (error?.kind === "http" && /復元できる生成はありません/.test(error.message)) {
      try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
      byId("message").value = "";
      byId("error").textContent = "再開できる生成はありません。質問を確認して送信してください。";
      return;
    }
    if (!connectionError(error)) byId("error").textContent = error.message;
    else void checkHost();
  } finally {
    activeChatJob = null;
    busy(false);
    pendingRecovery = false;
  }
}
async function refreshResources() {
  try {
    if (hostState !== "online") return;
    const data = await api("/api/resources");
    loadedModelId = data.loaded_model || null;
    updateModelIndicators();
    byId("resource-status").textContent = `${data.mode} / ${data.lifecycle}`;
    byId("running-model").textContent = data.running_model || "—";
    byId("running-tool").textContent = data.running_tool || "—";
    byId("waiting-jobs").textContent = String(data.waiting_jobs);
  } catch (error) {
    byId("resource-status").textContent = "状態を取得できません";
    for (const id of ["running-model", "running-tool", "waiting-jobs"]) byId(id).textContent = "不明";
  } finally { setTimeout(refreshResources, 2000); }
}
async function refreshDurableResources() {
  try {
    if (hostState !== "online") return;
    const [gpu, jobs] = await Promise.all([api("/api/gpu"), api("/api/jobs")]);
    const memory = gpu.memory_used_mib == null ? "未計測" : `${gpu.memory_used_mib} MiB`;
    const classes = gpu.process_classifications;
    const classification = classes ? ` / 背景共有 ${classes.EXTERNAL_SHARED} / 競合 ${classes.EXTERNAL_BLOCKING} / 不明 ${classes.STALE_OR_UNKNOWN}` : "";
    byId("gpu-status").textContent = `${gpu.state} / ${gpu.current_owner || "ownerなし"} / ${memory}${classification}`;
    const recent = jobs.jobs.slice(0, 3);
    byId("job-status").textContent = recent.length ? recent.map(job => `${job.tool}: ${job.status}`).join(" / ") : "jobなし";
  } catch (error) {
    byId("gpu-status").textContent = "状態不明（生成を許可しません）";
    byId("job-status").textContent = "取得できません";
  } finally { setTimeout(refreshDurableResources, 5000); }
}
busy(false);
byId("chat-model").addEventListener("change", updateModelStatus);
byId("chat-mode").addEventListener("change", () => {
  byId("message").maxLength = byId("chat-mode").value === "chat" ? 16000 : 4000;
  byId("pt-options").hidden = !["pt_learning", "clinical"].includes(byId("chat-mode").value);
  byId("external-retrieval").checked = false;
  busy(isBusy);
});
byId("tool-selector").addEventListener("change", () => busy(isBusy));
byId("tool-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = byId("tool-query").value;
  const tool = selectedTool();
  if (isBusy || !query.trim() || byId("tool-run").disabled || !tool?.enabled) return;
  busy(true);
  byId("error").textContent = "";
  byId("tool-output").textContent = "検索中…";
  try {
    byId("tool-artifacts").replaceChildren();
    const argumentsByTool = {"book.search": {query, limit: 5}, "tts.generate": {text: query},
                            "pitch_work.run": {instruction: query, project: byId("tool-project").value}};
    const asynchronous = tool.id !== "book.search";
    const result = await api(asynchronous ? "/api/jobs" : "/api/tools/run", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({tool: tool.id, arguments: argumentsByTool[tool.id]})});
    if (asynchronous) {
      toolJob = result.job_id;
      byId("tool-output").textContent = `job ${toolJob}: ${result.status}`;
      setTimeout(pollToolJob, 1000);
    } else byId("tool-output").textContent = JSON.stringify(result.tool_result.output, null, 2);
  } catch (error) {
    byId("tool-output").textContent = "";
    byId("error").textContent = error.message;
  } finally { busy(false); }
});
async function pollToolJob() {
  if (!toolJob) return;
  if (hostState !== "online") { setTimeout(pollToolJob, 2000); return; }
  const id = toolJob;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    byId("tool-output").textContent = `job ${id}: ${job.status} / run ${job.remote_run_id || "—"}`;
    if (["QUEUED", "RUNNING"].includes(job.status)) { setTimeout(pollToolJob, 2000); return; }
    if (job.status === "SUCCESS") {
      const result = await api(`/api/jobs/${encodeURIComponent(id)}/result`);
      byId("tool-output").textContent += "\n" + JSON.stringify(result.tool_result.output, null, 2);
      for (const artifact of result.tool_result.artifacts || []) {
        if (artifact.kind !== "audio" || !artifact.path?.startsWith(`/api/jobs/${id}/artifacts/`)) continue;
        const audio = document.createElement("audio");
        audio.controls = true; audio.src = artifact.path;
        byId("tool-artifacts").append(audio);
        if (chatDeletions) {
          const menu = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = '︙'; menu.append(summary);
          const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '音声と元ファイルを削除';
          remove.addEventListener('click', () => chatDeletions.item('job',id)); menu.append(remove); byId('tool-artifacts').append(menu);
        }
      }
    } else byId("tool-output").textContent += ` / ${job.error || "HOSTで確認してください"}`;
    toolJob = null;
  } catch (error) {
    byId("error").textContent = error.message;
    if (connectionError(error)) setTimeout(pollToolJob, 2000);
    else toolJob = null;
  }
  finally { busy(isBusy); }
}
byId("new-chat").addEventListener("click", () => {
  byId("conversation-menu").open = false;
  showModelSelection(null);
  conversationId = null;
  if (chatImages) void chatImages.load(null);
  if (chatVideos) void chatVideos.load(null); if(longVideos) void longVideos.load(null);
  showTiming(null); byId("evaluation-result").textContent = "";
  byId("conversations").value = "";
  byId("messages").replaceChildren();
  byId("error").textContent = "";
  byId("message").value = "";
  byId("evidence-query").value = "";
  byId("external-retrieval").checked = false;
  byId("message").focus();
});
byId("conversations").addEventListener("change", async () => {
  const selected = byId("conversations").value;
  if (!selected) { byId("new-chat").click(); return; }
  busy(true);
  byId("conversation-menu").open = false;
  byId("error").textContent = "";
  try {
    const data = await api(`/api/conversations/${selected}`);
    conversationId = selected;
    showModelSelection(null); // Historical turns have no persisted model attribution.
    byId("messages").replaceChildren();
    for (const message of data.messages) addMessage(message.role, message.content);
    if (window.pitchiWork) await window.pitchiWork.restore();
    if (chatImages) await chatImages.load(selected); if (chatVideos) await chatVideos.load(selected); if(longVideos) await longVideos.load(selected);
    byId("message").value = "";
  } catch (error) {
    byId("conversations").value = conversationId || "";
    byId("error").textContent = error.message;
  } finally { busy(false); }
});
byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = byId("message").value;
  if (byId("chat-mode").value === "work") { await window.pitchiWork?.submit(message); return; }
  if (!message.trim() || isBusy || !selectedModel()?.available) return;
  const model = selectedModel().id;
  const mode = byId("chat-mode").value || "chat";
  const ptMode = ["pt_learning", "clinical"].includes(mode);
  const evidenceQuery = ptMode ? byId("evidence-query").value.trim() : "";
  const externalRetrieval = ptMode && !!byId("external-retrieval").checked;
  if (externalRetrieval && !evidenceQuery) {
    byId("error").textContent = "外部検索には患者情報を含まない公開検索語を明示してください。";
    return;
  }
  if (mode === "library" && !toolState?.tools.some(tool => tool.id === "book.search" && tool.enabled)) {
    byId("error").textContent = "book.searchが利用できません。";
    return;
  }
  busy(true);
  byId("error").textContent = "";
  showModelSelection({requested_model: model});
  byId("actual-model").textContent = "処理中…";
  try {
    const payload = {message, conversation_id: conversationId, model, mode,
      ...(ptMode ? {evidence_query: evidenceQuery || null, external_retrieval: externalRetrieval} : {}),
      ...(model === "strata" ? {strata_options: {temperature: Number(byId("strata-temperature").value || "0.2"),
        max_tokens: Number(byId("strata-max-tokens").value || "4096"), reasoning_effort: byId("strata-reasoning").value || "none",
        reasoning_budget_tokens: Number(byId("strata-thinking-budget").value || "512")}} : {})};
    const data = (model === "vision" || model === "strata" && byId("strata-streaming").checked) ? await streamChat(payload, newGenerationRequestId()) : await api("/api/chat", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    showTiming(data.generation_metrics);
    conversationId = data.conversation_id;
    showModelSelection(data.model_selection);
    byId("cloud-render-status").textContent = `${data.cloud_used ? "Cloud送信試行あり" : "Cloud不使用"} / ${data.renderer_used ? "Renderer採用" : "直接出力"}`;
    addMessage("user", message);
    addMessage("assistant", data.reply, data.reasoning_content);
    byId("message").value = "";
    byId("external-retrieval").checked = false; // confirmation is per turn, never inferred from history
    await refreshHistory();
    if (model === "vision" || model === "strata" && byId("strata-streaming").checked) {
      try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
    }
  } catch (error) {
    byId("actual-model").textContent = "未確認（request失敗）";
    byId("error").textContent = error.message;
  }
  finally {
    busy(false);
    if (hostState === "online" && needsHydration) void hydrateUi();
    byId("message").focus();
  }
});
byId("cancel-generation").addEventListener("click", async () => {
  const jobId = activeChatJob;
  if (!jobId) return;
  byId("cancel-generation").disabled = true;
  try {
    await api(`/api/chat/stream/jobs/${encodeURIComponent(jobId)}/cancel`, {method: "POST"});
    try { localStorage.removeItem(PENDING_CHAT_KEY); } catch {}
    activeChatJob = null;
    byId("strata-preview").textContent = "";
    byId("strata-preview").hidden = true;
    byId("error").textContent = "生成を停止しました。質問は履歴へ保存していません。";
  } catch (error) { byId("error").textContent = error.message; }
  finally { busy(isBusy); }
});
if (typeof navigator !== "undefined" && "serviceWorker" in navigator && typeof window !== "undefined" && window.isSecureContext) {
  let hadController = !!navigator.serviceWorker.controller;
  let reloading = false;
  function workerStatus() {
    const controller = navigator.serviceWorker.controller;
    diagnostics.worker = controller?.scriptURL ? new URL(controller.scriptURL).pathname : controller ? "present" : "none";
    controller?.postMessage?.({type: "PITCH_CHAT_SW_DIAGNOSTICS"});
    recordDiagnostic("worker_controller", {present: !!controller});
  }
  navigator.serviceWorker.addEventListener("message", event => {
    if (event.source !== navigator.serviceWorker.controller || event.data?.type !== "PITCH_CHAT_SW_DIAGNOSTICS") return;
    if (event.data.release === "v32") diagnostics.worker_release = event.data.release;
    if (["network", "cache"].includes(event.data.shell_source)) diagnostics.shell_source = event.data.shell_source;
    recordDiagnostic("worker_status");
  });
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    workerStatus();
    if (hadController && !isBusy && !reloading) { reloading = true; window.location.reload(); }
    hadController = true;
  });
  workerStatus();
  navigator.serviceWorker.register("/sw.js", {scope: "/", updateViaCache: "none"})
    .then(registration => { recordDiagnostic("worker_registered"); return registration.update(); })
    .catch(() => {
      recordDiagnostic("worker_update_unavailable");
    });
}
if (typeof window !== "undefined") {
  window.addEventListener("online", checkHost);
  window.addEventListener("offline", checkHost);
  window.addEventListener("pageshow", checkHost);
}
document.addEventListener?.("visibilitychange", () => { if (document.visibilityState === "visible") void checkHost(); });
byId("connection-check")?.addEventListener("click", checkHost);
byId("message").addEventListener("focus", () => {
  window.setTimeout(() => byId("chat-form").scrollIntoView?.({block: "nearest"}), 120);
});

async function refreshImageBackends() {
  const data = await api("/api/images/backends");
  if (!Array.isArray(data?.backends)) throw new FrontendRequestError("schema", "画像backend一覧の形式を読み取れません。");
  const previousBackend = byId("image-backend").value;
  imageState = data;
  byId("image-backend").replaceChildren();
  for (const backend of imageState.backends) {
    const label = {ready: "利用可能", on_demand: "必要時に起動", compile_only: "プロンプト作成のみ"}[backend.status] || "現在利用できません";
    const name = backend.family === "flux2-klein" ? "FLUX.2 Klein 4B Distilled" : backend.id;
    const option = new Option(`${name} / ${label}`, backend.id);
    option.disabled = !["ready", "on_demand", "compile_only"].includes(backend.status);
    byId("image-backend").add(option);
  }
  byId("image-backend").value = imageState.backends.find(backend => backend.id === previousBackend && ["ready", "on_demand", "compile_only"].includes(backend.status))?.id
    || imageState.backends.find(backend => ["ready", "on_demand"].includes(backend.status))?.id
    || imageState.backends.find(backend => backend.status === "compile_only")?.id || "";
  if (imageSpec && imageSpec.backend_preference && imageSpec.backend_preference !== byId("image-backend").value) invalidateImageSpec();
  byId("image-status").textContent = imageState.generation_enabled ? "プロンプト案を確認・編集してから生成できます" : "実生成は無効。利用可能backendのImageSpec作成のみ可能です";
  busy(isBusy);
}
function invalidateImageSpec() {
  imageSpec = null; imageCompilerJob = null; mediaPreviewJob = null; byId("image-editor").hidden = true; busy(isBusy);
  byId("media-editor").hidden = true;
}
byId("image-prompt-mode").addEventListener("input", invalidateImageSpec);
for (const id of ["image-request", "image-backend", "image-aspect"]) byId(id).addEventListener("input", invalidateImageSpec);
async function submitImage(compileOnly) {
  const request = byId("image-request").value;
  if (isBusy || !request.trim() || byId(compileOnly ? "image-preview" : "image-generate").disabled) return;
  busy(true);
  try {
    if (!conversationId) {
      const created = await api('/api/images/conversations', {method:'POST'});
      conversationId = created.conversation_id;
    }
    let job;
    imageCompiling = compileOnly;
    const mode = byId("image-prompt-mode").value || "compiler";
    imageIsMedia = mode !== "compiler";
    if (imageIsMedia) {
      if (!compileOnly && mediaPreviewJob) {
        job = await api(`/api/media/jobs/${encodeURIComponent(mediaPreviewJob)}/render`, {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({positive_prompt: byId("media-positive").value, negative_prompt: byId("media-negative").value})});
      } else {
        job = await api("/api/media/jobs", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({prompt: request, backend: byId("image-backend").value,
            aspect_ratio: byId("image-aspect").value || "1:1", prompt_mode: mode, preview_only: compileOnly, conversation_id: conversationId})});
      }
    } else if (compileOnly) {
      imageSpec = null; imageCompilerJob = null; byId("image-editor").hidden = true;
      job = await api("/api/images/jobs", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({request, backend: byId("image-backend").value, aspect_ratio: byId("image-aspect").value || "1:1",
          model: byId("chat-model").value, compile_only: true, conversation_id: conversationId})});
    } else {
      if (!imageSpec) throw new FrontendRequestError("schema", "先にプロンプト案を作成してください。");
      const spec = {...imageSpec, purpose: byId("image-purpose").value, subject: byId("image-subject").value,
        style: byId("image-style").value, composition: byId("image-composition").value,
        text_requirement: byId("image-text").value,
        negative_constraints: byId("image-negative").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean)};
      job = await api(`/api/images/jobs/${encodeURIComponent(imageCompilerJob)}/render`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(spec)});
    }
    imageJob = job.job_id;
    byId("image-output").textContent = "";
    byId("image-results").replaceChildren();
    byId("image-status").textContent = `job ${imageJob}: ${job.status}`;
    setTimeout(pollImage, 1000);
  } catch (error) {
    byId("image-status").textContent = error.message;
  } finally { busy(false); }
}
async function pollImage() {
  if (!imageJob) return;
  if (hostState !== "online") { setTimeout(pollImage, 2000); return; }
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(imageJob)}`);
    let savedPlanReady = job.status === "SUCCESS";
    byId("image-status").textContent = `${job.status} / ${job.media_phase || job.image_phase || "—"}`;
    if (["QUEUED", "RUNNING"].includes(job.status)) { setTimeout(pollImage, 2000); return; }
    if (job.status === "SUCCESS") {
      const result = await api(`/api/jobs/${encodeURIComponent(imageJob)}/result`);
      if (result.image_spec?.positive_prompt) {
        if (imageCompiling) mediaPreviewJob = imageJob;
        byId("image-output").textContent = `${result.image_spec.positive_prompt}\n\n避けるもの: ${result.image_spec.negative_prompt}\nseed: ${result.image_spec.seed}`;
        byId("image-editor").hidden = true;
        byId("media-editor").hidden = !imageCompiling;
        byId("media-positive").value = result.image_spec.positive_prompt;
        byId("media-negative").value = result.image_spec.negative_prompt || "";
        byId("media-prompt-note").textContent = result.image_spec.backend === "flux-klein" ? "FLUX Distilledは4ステップ・CFG 1。避けたい条件も「生成する内容」に記述し、確認後に画像を生成してください。" : "編集を確認して「画像を生成」を押してください。";
      } else if (result.image_spec) {
        imageSpec = result.image_spec;
        if (imageCompiling) imageCompilerJob = imageJob;
        for (const [id, key] of [["image-purpose", "purpose"], ["image-subject", "subject"], ["image-style", "style"], ["image-composition", "composition"], ["image-text", "text_requirement"]]) byId(id).value = imageSpec[key] || "";
        byId("image-negative").value = (imageSpec.negative_constraints || []).join("\n");
        byId("image-editor").hidden = false;
        byId("image-output").textContent = "プロンプト案を確認・編集してから生成できます。";
      }
      for (const artifact of result.tool_result?.artifacts || []) {
        // Only HOST-generated artifact routes are ever loaded, never tool URLs.
        if (typeof artifact.path !== "string" || !artifact.path.startsWith(`/api/jobs/${imageJob}/artifacts/`)) continue;
        const isVideo = result.image_spec?.media_kind === "video" && artifact.kind === "file";
        const image = document.createElement(isVideo ? "video" : "img");
        if (isVideo) { image.controls = true; image.preload = "metadata"; }
        else image.alt = "生成画像";
        image.src = artifact.path; image.style.maxWidth = "100%";
        byId("image-results").append(image);
        const save = document.createElement("a");
        save.href = artifact.path; save.download = isVideo ? "generated-video.mp4" : "generated-image.png"; save.textContent = isVideo ? "動画を保存" : "画像を保存";
        byId("image-results").append(save);
      }
    } else {
      byId("image-status").textContent += ` / ${job.error || "処理を確認してください"}`;
      if (imageIsMedia && job.status === "ERROR") {
        const saved = await api(`/api/media/jobs/${encodeURIComponent(imageJob)}/plan`);
        savedPlanReady = !!saved.plan?.positive_prompt;
        const failure = saved.events?.findLast(event => event.phase === "FAILED")?.details;
        const resource = failure?.resource_details;
        if (resource?.reason) {
          byId("image-status").textContent += ` / ${globalThis.resourceFailureMessage ? globalThis.resourceFailureMessage(resource) : resource.reason}`;
          if (savedPlanReady) byId("image-status").textContent += "計画は保存済みです。リソース回復後に同じ設定で再試行できます。";
        }
      }
    }
    if (imageIsMedia && savedPlanReady && ["SUCCESS", "ERROR"].includes(job.status) && !imageCompiling) {
      const savedJob = imageJob;
      const retry = document.createElement("button"); retry.type = "button";
      retry.textContent = job.status === "SUCCESS" ? "同じ設定で再生成" : "保存済み計画から再試行";
      retry.addEventListener("click", async () => {
        if (imageJob || isBusy) return;
        try {
          const next = await api(`/api/media/jobs/${encodeURIComponent(savedJob)}/retry`, {method: "POST"});
          imageJob = next.job_id; imageCompiling = false; imageIsMedia = true; busy(isBusy); setTimeout(pollImage, 1000);
        } catch (error) { byId("image-status").textContent = error.message; }
      });
      byId("image-results").append(retry);
    }
    if (conversationId) {
      if (chatImages) await chatImages.load(conversationId);
      if (chatVideos) await chatVideos.load(conversationId); if(longVideos) await longVideos.load(conversationId);
      await refreshHistory();
    }
    imageJob = null;
  } catch (error) {
    byId("image-status").textContent = error.message;
    if (connectionError(error)) setTimeout(pollImage, 2000);
    else imageJob = null;
  }
  finally { busy(isBusy); }
}
byId("image-preview").addEventListener("click", () => submitImage(true));
byId("video-form").addEventListener("submit", event => {
  event.preventDefault();
  if (isBusy || imageJob) return;
  byId("chat-mode").value = "video";
  byId("chat-mode").dispatchEvent(new Event("change"));
  byId("message").value = byId("video-request").value;
  byId("chat-video-prompt-mode").value = byId("video-prompt-mode").value || "direct";
  byId("management-menu").open = false;
  byId("message").focus();
});
byId("image-form").addEventListener("submit", event => { event.preventDefault(); return submitImage(false); });

async function modelLifecycle(action) {
  if (isBusy || byId(action === "start" ? "model-start" : "model-stop").disabled) return;
  busy(true);
  try {
    if (action === "start" && selectedModel().id === "strata") {
      const state = await strataPreflight();
      if (!state.ready_to_start) throw new Error("Strataの起動前条件を満たしていません。既存サービスは自動停止しません。");
      if (!window.confirm("Strataを起動します。RAMとVRAMを大量に使用します。既存サービスの停止は行いません。起動を承認しますか？")) return;
    }
    const result = await api(`/api/models/${encodeURIComponent(selectedModel().id)}/lifecycle`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action})});
    byId("status").textContent = `${result.model}: ${result.state}`;
    byId("management-menu").open = true;
    byId("resource-panel").open = true;
  } catch (error) { byId("error").textContent = error.message; }
  finally { busy(false); }
}
byId("model-start").addEventListener("click", () => modelLifecycle("start"));
byId("model-stop").addEventListener("click", () => modelLifecycle("stop"));
// Start only after the deferred script has installed all DOM handlers. Health
// alone owns connection state; read-only hydration/polling never replays a POST.
recordDiagnostic("startup");
void checkHost();
void refreshResources();
void refreshDurableResources();
