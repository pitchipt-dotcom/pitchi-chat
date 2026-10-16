"use strict";
// Only Host routes are loaded. No image, prompt or result goes into browser storage.
globalThis.PitchImages = function ({api, getConversation, setConversation, refreshHistory, refreshConversation, isBusy}) {
  const el = id => document.getElementById(id);
  let images = [], previewJob = null, active = null, timer = null, generation = 0, posting = false, loadedConversation = null;
  let visionBackend = null;
  async function capabilities() {
    const data = await api('/api/images/vision');
    visionBackend = data.available ? data.backend : null;
    el('chat-image-vision-option').disabled = !visionBackend;
    el('chat-image-vision-option').textContent = visionBackend ? `画像について質問（${data.model}）` : '画像について質問（Visionモデル未接続）';
  }
  const status = text => { el("chat-image-status").textContent = text; };
  const invalidate = () => { previewJob = null; el("chat-image-plan").hidden = true; };
  function selected() {
    const image = images.find(x => x.id === el("chat-image-reference").value);
    el("chat-image-selected").hidden = !image;
    if (image) el("chat-image-selected").src = image.url;
    else el("chat-image-selected").removeAttribute("src");
    invalidate();
  }
  function choose(id) {
    el("chat-image-reference").value = id;
    el("chat-image-mode").value = "direct";
    el("chat-image-controls").open = true;
    selected();
  }
  function timeline(data) {
    const old = el("chat-image-reference").value;
    images = data.images.filter(x => /^[a-f0-9-]{36}$/i.test(x.id) && x.url === `/api/images/attachments/${x.id}/content`);
    el("chat-image-reference").replaceChildren(new Option("未選択", ""));
    let area = el("chat-image-timeline");
    if (!area) { area = document.createElement("section"); area.id = "chat-image-timeline"; el("messages").append(area); }
    area.replaceChildren();
    for (const image of images) {
      const label = `${image.source_kind === "video_frame" ? "動画の最終フレーム" : image.source_kind === "upload" ? "元画像" : "生成画像"} ${image.created_at}${image.parent_id ? "（参照あり）" : ""}`;
      el("chat-image-reference").add(new Option(label, image.id));
      const card = document.createElement("article"), heading = document.createElement("p");
      heading.textContent = label; card.append(heading);
      const img = document.createElement("img"); img.alt = label; img.src = image.url; img.loading = "lazy"; img.style.maxWidth = "100%"; card.append(img);
      const select = document.createElement("button"); select.type = "button"; select.textContent = "この画像を編集対象にする";
      select.addEventListener("click", () => choose(image.id)); card.append(select);
      const save = document.createElement("a"); save.textContent = "画像を保存"; save.href = image.url; save.download = "image.png"; card.append(save);
      const source = data.jobs.find(job => job.job_id === image.source_job_id);
      if (source) {
        const instruction = document.createElement("p");
        instruction.textContent = `${source.request.prompt_mode === "qwen35" ? "Qwen3.5経由（テキスト指示のみ）" : source.request.prompt_mode === "strata" ? "Strata経由（テキスト指示のみ）" : "Direct"}: ${source.request.prompt}`;
        card.append(instruction);
      }
      if (globalThis.pitchDeleteItem) {
        const menu = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = '︙'; menu.append(summary);
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '画像と元ファイルを削除';
        remove.addEventListener('click', () => globalThis.pitchDeleteItem('image',image.id)); menu.append(remove); card.append(menu);
      }
      area.append(card);
    }
    el("chat-image-reference").value = images.some(x => x.id === old) ? old : "";
    el("chat-image-selected").hidden = !images.some(x => x.id === old);
    for (const job of data.jobs) {
      if (job.request.kind === "video") continue;
      if (job.request.kind === 'vision' && job.status === 'SUCCESS') continue;
      const p = document.createElement("p");
      p.textContent = `${job.request.kind === 'vision' ? '画像理解' : job.request.prompt_mode === "qwen35" ? "Qwen3.5（テキスト指示のみ）" : job.request.prompt_mode === "strata" ? "Strata（テキスト指示のみ）" : "Direct"}: ${job.request.prompt} / ${job.status}${job.error ? " / " + job.error : ""}`;
      area.append(p);
      if (job.status === "SUCCESS" && job.request.preview_only) {
        const restore = document.createElement("button"); restore.type = "button"; restore.textContent = "保存済み指示案を確認";
        restore.addEventListener("click", () => restorePreview(job.job_id)); area.append(restore);
      }
    }
  }
  async function restorePreview(jobId) {
    const cid = getConversation();
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/result`);
      if (cid !== getConversation() || active) return;
      if (result.image_spec?.reference_image?.id) {
        el("chat-image-reference").value = result.image_spec.reference_image.id; selected();
      }
      previewJob = jobId; el("chat-image-prompt").value = result.image_spec.positive_prompt;
      el("chat-image-plan").hidden = false; status("指示案を確認してください。この経路は画像を見ず、編集指示の文章だけを最適化します。");
    } catch (error) { status(error.message); }
  }
  async function load(cid) {
    const stamp = ++generation;
    if (cid !== loadedConversation) { invalidate(); loadedConversation = cid; }
    clearTimeout(timer); active = null;
    if (!cid) { images = []; invalidate(); el("chat-image-reference").replaceChildren(new Option("未選択", "")); selected(); return; }
    const data = await api(`/api/conversations/${encodeURIComponent(cid)}/images`);
    if (stamp !== generation || cid !== getConversation()) return;
    timeline(data);
    const pending = data.jobs.find(x => ["QUEUED", "RUNNING"].includes(x.status));
    if (pending) { active = pending.job_id; schedule(); }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(poll, 1500); }
  async function poll() {
    if (!active) return;
    const cid = getConversation(), jobId = active;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (jobId !== active || cid !== getConversation()) return;
      const phase = job.tool === 'image.understand' && job.media_phase === 'COMFY_STARTING' ? 'Visionモデル起動中' : job.media_phase || '';
      status(`${job.status} / ${phase}`);
      if (["QUEUED", "RUNNING"].includes(job.status)) { schedule(); return; }
      active = null;
      if (job.status === "SUCCESS") {
        const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/result`);
        if (cid !== getConversation()) return;
        if (result.image_spec?.positive_prompt && !result.tool_result) await restorePreview(jobId);
        if (result.image_spec?.media_kind === 'vision' && refreshConversation) {
          await refreshConversation(cid);
          status('Qwen3.5-9B-Uncensoredの画像理解結果を会話履歴に保存しました。');
        }
      } else {
        const saved = await api(`/api/media/jobs/${encodeURIComponent(jobId)}/plan`);
        const failure = saved.events?.findLast(x => x.phase === "FAILED")?.details;
        status(`${job.status} / ${globalThis.resourceFailureMessage && failure?.resource_details ? globalThis.resourceFailureMessage(failure.resource_details) : job.error || "結果の確認が必要です。自動再送はしません。"}`);
      }
      if (cid === getConversation()) { await load(cid); await refreshHistory(); }
    } catch (error) { status(error.message); if (active) schedule(); }
  }
  async function ensureConversation() {
    if (!getConversation()) {
      const stamp = generation;
      const data = await api("/api/images/conversations", {method: "POST"});
      if (stamp !== generation || getConversation()) throw new Error("会話が切り替わりました。添付対象を確認してください。");
      setConversation(data.conversation_id);
    }
    return getConversation();
  }
  el("chat-image-upload").addEventListener("change", async () => {
    const file = el("chat-image-upload").files?.[0];
    if (!file) return;
    if (isBusy() || active || posting) { status("処理が完了してから画像を添付してください。"); return; }
    if (file.size > 20 * 1024 * 1024) { status("画像は20MiBまでです。"); return; }
    posting = true;
    try {
      const cid = await ensureConversation();
      const image = await api(`/api/images/attachments?conversation_id=${encodeURIComponent(cid)}`, {method: "POST", headers: {"Content-Type": file.type}, body: file});
      if (cid !== getConversation()) return;
      await load(cid); choose(image.id); await refreshHistory(); status("画像を保存しました。編集指示を入力してください。");
    } catch (error) { status(error.message); }
    finally { posting = false; el("chat-image-upload").value = ""; }
  });
  el("chat-image-reference").addEventListener("change", selected);
  el("chat-image-mode").addEventListener("change", invalidate);
  el("message").addEventListener("input", invalidate);
  el("chat-form").addEventListener("submit", async event => {
    const mode = el("chat-image-mode").value;
    if (el("chat-mode")?.value === "video") return;
    if (mode === "chat") return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (mode === "vision" && !visionBackend) { status("画像理解モデルは未接続です。画像を理解した回答は生成できません。"); return; }
    if (active || isBusy() || posting) { status("実行中の処理の完了を待ってください。"); return; }
    const prompt = el("message").value, reference = el("chat-image-reference").value;
    if (!prompt.trim() || !reference) { status("編集対象と編集指示を指定してください。"); return; }
    posting = true;
    try {
      const cid = getConversation(); invalidate();
      const job = await api("/api/media/jobs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        prompt, kind: mode === 'vision' ? 'vision' : 'image', backend: mode === 'vision' ? visionBackend : "flux-klein",
        prompt_mode: mode === 'vision' ? 'direct' : mode, conversation_id: cid, reference_image_id: reference, preview_only: ["strata", "qwen35"].includes(mode)})});
      if (cid !== getConversation()) return;
      active = job.job_id; status(`${job.status} / ${active}`); schedule();
    } catch (error) { status(error.message); }
    finally { posting = false; }
  }, true);
  el("chat-image-render").addEventListener("click", async () => {
    if (!previewJob || active || isBusy() || posting) return;
    posting = true;
    try {
      const cid = getConversation();
      const job = await api(`/api/media/jobs/${encodeURIComponent(previewJob)}/render`, {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({positive_prompt: el("chat-image-prompt").value, negative_prompt: ""})});
      if (cid !== getConversation()) return;
      active = job.job_id; invalidate(); schedule();
    } catch (error) { status(error.message); }
    finally { posting = false; }
  });
  return {load, invalidate, capabilities};
};
