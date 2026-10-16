"use strict";
// Durable Host jobs and immutable clip branches. Never store private data locally.
globalThis.PitchVideos = function ({api, getConversation, setConversation, refreshHistory, isBusy}) {
  const el = id => document.getElementById(id);
  let stamp = 0, timer = null, posting = false, creating = false, pending = false, clips = [], selected = new Set(), currentCid = null;
  const status = text => { el('chat-video-status').textContent = text; };
  const validId = value => typeof value === 'string' && /^[a-f0-9-]{36}$/i.test(value);
  async function capabilities() {
    const data = await api('/api/media/backends');
    const old = el('chat-video-backend').value;
    el('chat-video-backend').replaceChildren();
    for (const item of data.video || []) {
      const option = new Option(item.id === 'ltx-video' ? 'LTX-Video（標準・約3秒）' : 'MiniMax H3（予備・3.75秒）', item.id);
      option.disabled = !['ready', 'on_demand'].includes(item.status);
      el('chat-video-backend').add(option);
    }
    el('chat-video-backend').value = [...el('chat-video-backend').children].some(x => x.value === old) ? old : data.default_video_backend || 'ltx-video';
  }
  function player(parent, url, title) {
    if (!/^\/api\/jobs\/[a-f0-9-]{36}\/artifacts\/0$/i.test(url)) return;
    const video = document.createElement('video'); video.src = url; video.controls = true;
    video.preload = 'metadata'; video.playsInline = true; video.setAttribute('playsinline', '');
    video.style.maxWidth = '100%'; video.setAttribute('aria-label', title); parent.append(video);
    const save = document.createElement('a'); save.href = url; save.download = 'pitch-chat-video.mp4'; save.textContent = 'MP4を保存'; parent.append(save);
  }
  async function post(path, body) {
    if (posting || pending || isBusy()) { status('実行中の処理の完了を待ってください。'); return; }
    posting = true;
    const cid = getConversation();
    try {
      const job = await api(path, {method:'POST', headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
      if (cid !== getConversation()) return;
      status(`${job.status} / ${job.job_id}`); pending = true; await load(cid); await refreshHistory();
    } catch (error) { status(error.message); }
    finally { posting = false; }
  }
  function button(parent, title, action) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = title;
    b.addEventListener('click', action); parent.append(b);
  }
  function render(data) {
    let area = el('chat-video-timeline');
    if (!area && !(data.clips || []).length && !(data.assemblies || []).length) { clips = []; return; }
    if (!area) { area = document.createElement('section'); area.id = 'chat-video-timeline'; el('messages').append(area); }
    area.replaceChildren(); clips = data.clips || [];
    for (const clip of clips) {
      if (!validId(clip.clip_id) || !validId(clip.project_id)) continue;
      const card = document.createElement('article'); card.className = 'video-card';
      const title = document.createElement('p'); title.textContent = `${clip.backend} / ${clip.prompt_mode} / ${clip.status} / ${clip.start_frame}–${clip.end_frame}f${clip.retry_of_clip_id ? '（再生成）' : ''}`; card.append(title);
      const prompt = document.createElement('p'); prompt.textContent = clip.prompt; card.append(prompt);
      if (clip.status === 'SUCCESS') {
        player(card, clip.video_url, '生成動画');
        const label = document.createElement('label'), box = document.createElement('input'); box.type = 'checkbox'; box.checked = selected.has(clip.clip_id);
        box.addEventListener('change', () => box.checked ? selected.add(clip.clip_id) : selected.delete(clip.clip_id));
        label.append(box, document.createTextNode('このクリップを結合対象にする')); card.append(label);
        button(card, '続きを生成', () => {
          if (!el('message').value.trim()) { status('入力欄に続きの内容を書いてから押してください。'); el('message').focus(); return; }
          return post(`/api/videos/clips/${clip.clip_id}/extend`, {prompt:el('message').value,
            prompt_mode:el('chat-video-prompt-mode').value, backend:el('chat-video-backend').value});
        });
      }
      if (['SUCCESS','ERROR'].includes(clip.status) && clip.settings) button(card, clip.status === 'SUCCESS' ? '同じ設定・seedで再生成' : '保存済み計画で再試行',
        () => post(`/api/media/jobs/${clip.clip_id}/retry`));
      if (clip.error) { const p = document.createElement('p');
        const resource = clip.failure?.resource_details;
        p.textContent = `${resource && globalThis.resourceFailureMessage ? globalThis.resourceFailureMessage(resource) : clip.error} / job ${clip.clip_id}`; card.append(p); }
      if (globalThis.pitchDeleteItem) {
        const menu = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = '︙'; menu.append(summary);
        button(menu,'動画と元ファイルを削除',() => globalThis.pitchDeleteItem('job',clip.clip_id)); card.append(menu);
      }
      area.append(card);
    }
    for (const job of data.assemblies || []) {
      const card = document.createElement('article'), p = document.createElement('p'); p.textContent = `結合 ${job.status}${job.error ? ' / '+job.error : ''}`; card.append(p);
      if (validId(job.job_id) && job.status === 'SUCCESS') player(card, `/api/jobs/${job.job_id}/artifacts/0`, '結合動画');
      if (globalThis.pitchDeleteItem) {
        const menu = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = '︙'; menu.append(summary);
        button(menu,'結合動画を削除',() => globalThis.pitchDeleteItem('job',job.job_id)); card.append(menu);
      }
      area.append(card);
    }
  }
  async function load(cid) {
    const generation = ++stamp; clearTimeout(timer);
    if (cid !== currentCid) { selected = new Set(); currentCid = cid; }
    if (!cid) { clips = []; pending = false; el('chat-video-reference').replaceChildren(new Option('参照なし（T2V）','')); return; }
    const [data, images] = await Promise.all([api(`/api/conversations/${cid}/videos`), api(`/api/conversations/${cid}/images`)]);
    if (stamp !== generation || cid !== getConversation()) return;
    const old = el('chat-video-reference').value;
    el('chat-video-reference').replaceChildren(new Option('参照なし（T2V）',''));
    for (const image of images.images || []) if (validId(image.id)) el('chat-video-reference').add(new Option(
      `${image.source_kind === 'video_frame' ? '動画の最終フレーム' : image.source_kind === 'upload' ? '添付画像' : '生成画像'} ${image.created_at}`,image.id));
    if ((images.images || []).some(x => x.id === old)) el('chat-video-reference').value = old;
    render(data);
    const active = [...(data.clips || []), ...(data.assemblies || [])].find(x => ['QUEUED','RUNNING'].includes(x.status));
    pending = !!active;
    if (active) {
      const jid = active.clip_id || active.job_id;
      const job = await api(`/api/jobs/${jid}`);
      if (stamp !== generation || cid !== getConversation()) return;
      status(`${job.status} / ${job.media_phase || '結合処理'} / job ${jid}`);
      timer = setTimeout(() => load(cid).catch(error => { status(error.message); timer = setTimeout(() => load(cid).catch(() => {}),3000); }),1500);
    } else if (clips.length) status('クリップを保存しました。入力欄から続き・修正を指示できます。再生成は元クリップを残します。');
  }
  el('chat-mode').addEventListener('change', () => {
    el('chat-video-controls').hidden = el('chat-mode').value !== 'video';
    if (el('chat-mode').value === 'video') { el('message').placeholder = '動画の内容、動き、続きの指示を入力'; void load(getConversation()).catch(error => status(error.message)); }
    else { const long=el('long-video-controls');if(long)long.hidden=true; }
    if(el('chat-mode').value !== 'video') el('message').placeholder = 'メッセージを入力してください';
  });
  el('chat-video-refresh').addEventListener('click', () => load(getConversation()).catch(error => status(error.message)));
  el('chat-video-join').addEventListener('click', () => {
    const chosen = clips.filter(x => selected.has(x.clip_id));
    if (!chosen.length || new Set(chosen.map(x => x.project_id)).size !== 1) { status('同じプロジェクトのクリップを選んでください。結合順は表示順です。'); return; }
    return post(`/api/videos/projects/${chosen[0].project_id}/concatenate`,{clip_ids:chosen.map(x => x.clip_id)});
  });
  el('chat-form').addEventListener('submit', async event => {
    if (el('chat-mode').value !== 'video' || el('chat-video-length-mode')?.value === 'long') return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (posting || creating || pending || isBusy() || !el('message').value.trim()) return;
    creating = true;
    try {
      if (!getConversation()) { const generation = stamp; const c = await api('/api/images/conversations',{method:'POST'});
        if (generation !== stamp || getConversation()) throw new Error('会話が切り替わりました。生成対象を確認してください。');
        setConversation(c.conversation_id); }
      return await post('/api/media/jobs',{kind:'video',backend:el('chat-video-backend').value,
        prompt_mode:el('chat-video-prompt-mode').value,prompt:el('message').value,aspect_ratio:'16:9',
        conversation_id:getConversation(),reference_image_id:el('chat-video-reference').value || null});
    } catch (error) { status(error.message); }
    finally { creating = false; }
  },true);
  return {load,capabilities,offline:() => {
    ++stamp; clearTimeout(timer); pending = false; clips = []; selected.clear();
    el('chat-video-reference').replaceChildren(new Option('参照なし（T2V）',''));
    status('HOSTへの再接続を待機中');
  }};
};
