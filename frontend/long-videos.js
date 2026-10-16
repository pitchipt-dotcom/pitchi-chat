"use strict";
// Parent state is durable on the Host. Reconnection only reads the same jobs.
globalThis.PitchLongVideos = function ({api,getConversation,setConversation,refreshHistory,isBusy}) {
  const el=id=>document.getElementById(id);
  let generation=0,timer=null,posting=false,parents=[],loaded=null,rendered=null;
  const text=value=>{el('long-video-status').textContent=value;};
  const id=value=>typeof value==='string' && /^[a-f0-9-]{36}$/i.test(value);
  const mode=()=>el('chat-mode').value==='video' && el('chat-video-length-mode').value==='long';
  async function post(path,body) {
    if(posting || isBusy()){text('通信・会話処理の完了を待ってください。');return;}
    posting=true;const cid=getConversation();
    try {
      const result=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(cid===getConversation())await load(cid);return result;
    } catch(error){text(`${error.message} 再確認でHostの保存状態を取得できます。自動再送はしません。`);}
    finally{posting=false;}
  }
  function button(root,label,action) {const b=document.createElement('button');b.type='button';b.textContent=label;b.addEventListener('click',action);root.append(b);}
  function render() {
    let area=el('long-video-timeline');
    if(!area && !parents.length)return;
    const snapshot=JSON.stringify(parents);
    if(area && rendered===snapshot)return;
    rendered=snapshot;
    if(!area){area=document.createElement('section');area.id='long-video-timeline';el('messages').append(area);}
    area.replaceChildren();
    for(const parent of parents){
      if(!id(parent.id))continue;
      const card=document.createElement('article');card.className='video-card long-video-card';
      const heading=document.createElement('p');heading.textContent=`長尺 ${parent.status} / クリップ ${parent.current_clip}/${parent.total_clips} / 完了 ${parent.completed_clips}本 / ${parent.completed_seconds.toFixed(2)}秒・目標${parent.target_seconds}秒`;card.append(heading);
      const detail=document.createElement('p');detail.textContent=`残り推定 ${parent.estimated_remaining_seconds==null?'初回計測中':Math.ceil(parent.estimated_remaining_seconds)+'秒'} / 生成試行 ${parent.attempts}/${parent.max_attempts} / 1クリップの再試行上限${parent.retry_limit}`;card.append(detail);
      const description=document.createElement('p');description.textContent=parent.request.prompt;card.append(description);
      const control=action=>post(`/api/long-videos/${parent.id}/control`,{action});
      if(['PLANNING','RUNNING'].includes(parent.status))button(card,'現在のクリップ完了後に一時停止',()=>control('pause'));
      if(['PAUSED','RECOVERY_REQUIRED'].includes(parent.status))button(card,'保存地点から再開・状態照合',()=>control('resume'));
      if(!['COMPLETED','CANCELLED','OUTPUT_DELETED'].includes(parent.status))button(card,'キャンセル（実行中クリップは完了を待つ）',()=>control('cancel'));
      if(parent.status==='FAILED')button(card,'失敗クリップを有限回再試行',()=>control('retry'));
      if(['COMPLETED','OUTPUT_DELETED','FAILED'].includes(parent.status))button(card,'完成済みクリップを再結合',()=>control('reassemble'));
      if(parent.estimate && ['AWAITING_ESTIMATE_APPROVAL','PAUSED'].includes(parent.status) && !parent.video_url){
        const estimate=document.createElement('p');estimate.textContent=`初回実測${parent.estimate.first_clip_seconds.toFixed(2)}秒。合計${parent.estimate.clip_count}本・生成のみ約${Math.ceil(parent.estimate.total_seconds/60)}分、残り約${Math.ceil(parent.estimate.remaining_seconds/60)}分。必要空き容量約${Math.ceil(parent.estimate.required_disk_bytes/1048576)}MiB（構成作成・結合時間は別）。長いI2Vでは人物・画質が変化することがあります。`;card.append(estimate);
        const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';label.append(check,document.createTextNode(parent.target_seconds===180?'180秒の残りの生成時間・容量を確認し、実行を明示承認する':'残りの生成時間・容量を確認して実行する'));card.append(label);
        button(card,'見積もりを承認して残りを生成',()=>{if(!check.checked){text('見積もりの確認欄にチェックしてください。');return;}
          return post(`/api/long-videos/${parent.id}/control`,{action:'approve',estimate_sha256:parent.estimate.sha256,confirm_180_seconds:parent.target_seconds===180});});
      }
      if(parent.error){const error=document.createElement('p');error.textContent=`停止理由: ${parent.error}。不明な処理は再送せず、保存済みクリップを保持しています。`;card.append(error);}
      if(parent.plan){const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='固定設定・シーン構成・各クリップ・検証記録';details.append(summary);
        const pre=document.createElement('pre');pre.textContent=JSON.stringify({plan:parent.plan,clips:parent.members,notice:'フレーム差分は人物同一性や画質の保証ではありません。'},null,2);details.append(pre);card.append(details);}
      // Completed clips stay playable while paused or awaiting budget approval.
      const previews=document.createElement('details'),previewTitle=document.createElement('summary');
      previewTitle.textContent='保存済みクリップを再生・保存';previews.append(previewTitle);
      for(const member of parent.members || []){
        if(member.role!=='clip' || member.status!=='SUCCESS' || !id(member.job_id))continue;
        const label=document.createElement('p');label.textContent=`クリップ ${member.position+1}`;previews.append(label);
        const clip=document.createElement('video');clip.controls=true;clip.playsInline=true;clip.preload='metadata';
        clip.src=`/api/jobs/${member.job_id}/artifacts/0`;clip.style.maxWidth='100%';previews.append(clip);
        const save=document.createElement('a');save.href=clip.src;save.download=`clip-${member.position+1}.mp4`;save.textContent='クリップMP4を保存';previews.append(save);
      }
      if(previews.querySelector('video'))card.append(previews);
      if(parent.video_url===`/api/jobs/${parent.final_job_id}/artifacts/0` && id(parent.final_job_id)){
        const video=document.createElement('video');video.controls=true;video.playsInline=true;video.preload='metadata';video.src=parent.video_url;video.style.maxWidth='100%';card.append(video);
        const save=document.createElement('a');save.href=parent.video_url;save.download='pitch-chat-long-video.mp4';save.textContent='完成MP4を保存';card.append(save);
      }
      if(globalThis.pitchDeleteItem){const menu=document.createElement('details'),summary=document.createElement('summary');summary.textContent='︙ 削除範囲';menu.append(summary);
        if(id(parent.final_job_id))button(menu,'完成動画のみ完全削除',()=>globalThis.pitchDeleteItem('job',parent.final_job_id));
        button(menu,'完成動画＋専用中間クリップを完全削除',()=>globalThis.pitchDeleteItem('long_video',parent.id));card.append(menu);}
      area.append(card);
    }
  }
  async function load(cid) {
    const stamp=++generation;clearTimeout(timer);loaded=cid;
    if(!cid){parents=[];render();return;}
    const data=await api(`/api/conversations/${cid}/long-videos`);
    if(stamp!==generation || cid!==getConversation())return;
    parents=data.jobs || [];render();
    if(parents.some(x=>['PLANNING','RUNNING','PAUSE_REQUESTED','CANCEL_REQUESTED'].includes(x.status)))
      timer=setTimeout(()=>load(cid).catch(error=>{text(error.message);timer=setTimeout(()=>load(cid).catch(()=>{}),3000);}),2000);
  }
  async function start(){
    if(posting || isBusy() || !el('message').value.trim())return;
    if(el('message').value.length>1200){text('共通プロンプトは1200文字以内にしてください。');return;}
    posting=true;
    try {
      if(!getConversation()){
        const stamp=generation;const c=await api('/api/images/conversations',{method:'POST'});
        if(stamp!==generation || getConversation())throw Error('会話が変わりました。対象を確認してください。');setConversation(c.conversation_id);
      }
      const cid=getConversation(),planner=el('long-video-planner').value;
      const manual=planner==='manual'?JSON.parse(el('long-video-manual').value):null;
      await api('/api/long-videos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        conversation_id:cid,prompt:el('message').value,seconds:Number(el('long-video-seconds').value),backend:el('chat-video-backend').value,
        aspect_ratio:el('long-video-aspect').value,planner,manual_story:manual,reference_image_id:el('chat-video-reference').value || null,
        continuity:el('long-video-continuity').value,reset_interval:Number(el('long-video-reset').value),crossfade_frames:Number(el('long-video-fade').value),
        retry_limit:Number(el('long-video-retries').value),max_attempts:Number(el('long-video-budget').value),seed:Number(el('long-video-seed').value)})});
      await load(cid);await refreshHistory();text('構成作成後、最初の1クリップで停止して見積もりを表示します。以後は承認するまで生成しません。');
    }catch(error){text(`${error.message} 保存状態を再確認してください。構成作成に失敗した場合は決定論的分割・手動構成も選べます。`);if(getConversation())void load(getConversation()).catch(()=>{});}
    finally{posting=false;}
  }
  el('chat-mode').addEventListener('change',()=>{el('long-video-controls').hidden=!mode();if(mode())void load(getConversation()).catch(error=>text(error.message));});
  el('chat-video-length-mode').addEventListener('change',()=>{el('long-video-controls').hidden=el('chat-video-length-mode').value!=='long';});
  el('long-video-planner').addEventListener('change',()=>{el('long-video-manual').hidden=el('long-video-planner').value!=='manual';});
  el('long-video-start').addEventListener('click',start);
  el('long-video-refresh').addEventListener('click',()=>load(getConversation()).catch(error=>text(error.message)));
  el('chat-form').addEventListener('submit',event=>{if(!mode())return;event.preventDefault();event.stopImmediatePropagation();void start();},true);
  return {load,offline(){++generation;clearTimeout(timer);parents=[];render();text('Hostへ再接続後、同じ会話の保存済み進捗を取得します。');}};
};
