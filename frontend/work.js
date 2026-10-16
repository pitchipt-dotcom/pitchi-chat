"use strict";
// Presentation only. Every state, judgment and reference comes from Work.
window.pitchiWork = (() => {
  let enabled = false;
  let timer;
  let generation = 0;
  const cards = new Map();
  const explained = new Set();
  function text(parent, tag, value) {
    const node = document.createElement(tag); node.textContent = value; parent.append(node); return node;
  }
  function details(parent, title, value) {
    const node = document.createElement("details"); text(node, "summary", title);
    text(node, "pre", typeof value === "string" ? value : JSON.stringify(value, null, 2)); parent.append(node);
    return node;
  }
  function duplicateSummary(value) {
    if (typeof value !== 'object' || value === null) return String(value ?? '要確認');
    const labels = {new:'未登録', registered_live:'登録済み（本番）', unchanged:'登録済み（隔離元）',
      existing_url:'登録済みURL', old_edition_requires_review:'旧版・要確認', revision_requires_review:'改訂版・要確認',
      held_url_changed:'公式URL変更・要確認', held_metadata_changed:'書誌変更・要確認',
      held_possible_duplicate:'重複候補・要確認', held_live_conflict:'本番書誌との差異・要確認', held_evidence_missing:'根拠不足・要確認'};
    return Object.entries(value).map(([key,count])=>`${labels[key]||'要確認'} ${count}件`).join('、') || '該当資料0件';
  }
  function render(result) {
    let card = cards.get(result.run_id);
    if (!card) { card = document.createElement("article"); cards.set(result.run_id, card); byId("messages").append(card); }
    const openDetails = new Set([...card.querySelectorAll('details')].filter(n => n.open)
      .map(n => n.querySelector('summary').textContent));
    card.replaceChildren(); card.className = "message assistant";
    card.dataset.runId = result.run_id; card.dataset.status = result.status;
    const human = result.human_explanation || {};
    text(card, "strong", human.title || "Pitchi-workからのお知らせ");
    text(card, "p", human.message || "作業状況の説明を取得できません。技術詳細をご確認ください。");
    if(result.reused_result)card.dataset.reusedResult="true";
    if(card.dataset.reusedResult==="true")text(card,"p","保存済みの成功結果を参照しました。今回の新規Host実行・LLM呼出しは0です。");
    if(result.result_details?.kind === "national_coverage") {
      const r=result.result_details;
      text(card,"h3","全国カバレッジの診断結果");
      text(card,"p",`状態：${r.collection.status}。最終更新：${r.collection.updated_at}`);
      details(card,"収集・未取得・retry・索引",r.collection.counts);
      if(r.progress)details(card,"再開前からの進捗",r.progress);
      if(r.preservation)details(card,"既存データの保持検証",r.preservation);
      if(r.log_evidence)details(card,"実際のログ（直近の限定範囲）",r.log_evidence);
      text(card,"p","読み取り専用の診断です。本番変更・ジョブ再開・LLM呼出しは実行していません。");
    } else if(result.result_details) {
      const r=result.result_details;
      text(card,"h3",r.kind === 'guideline_discovery' ? '依頼条件に対するガイドライン探索結果' : '登録状態と検証結果');
      if(r.kind === 'guideline_discovery') {
        const d=r.discovery||{}, c=d.conditions, o=d.objective;
        if(c)text(card,"p",`対象：${c.disease_groups.flat().join('・')|| (c.unresolved_subject?'未確定':'全領域')}。地域：${c.country==='JP'?'日本':'指定なし'}。言語：${c.language_required ? c.language_required+'のみ' : (c.language_preference.join('・')||'指定なし')+'（優先条件）'}。資料：公式診療ガイドライン。`);
        if(o)text(card,"p",`探索処理：${o.processing_success?'正常終了':'取得失敗・確認保留あり'}。依頼目的：${o.goal_met?'確認済み関連資料あり':'未達・確認が必要'}。`);
        text(card,"p",`一覧の発見 ${d.discovered_count??0}件、関連抽出候補 ${d.relevant_candidate_count??0}件、確認済み ${o?.confirmed_count??0}件、未登録 ${o?.unregistered_count??0}件、未確認 ${o?.unconfirmed_count??0}件。`);
        for(const s of d.searched_sources||[])text(card,"p",`${s.id}：${s.index_url}｜${s.skipped_reason?'対象条件外のため未巡回':`${s.requests}リクエスト、${Object.keys(s.pages).length}ページ`}。`);
        text(card,"p",`今回の取得：${d.result_origin==='fresh_official_list_scan'?'公式一覧を新規巡回':d.result_origin==='resumed_official_list_scan'?'中断巡回を再開':'保存済み一覧'}。書誌：保存済み確認済み資料から依頼条件に一致するものだけを照合。`);
        for(const limitation of d.limitations||[])text(card,"p",limitation);
        details(card,"依頼原文と抽出条件",{request:result.original_request,conditions:c});
        if(d.unconfirmed_candidates?.length)details(card,"未確認の関連候補",d.unconfirmed_candidates);
        for(const row of d.registered_matches||[]) {
          const m=row.metadata;
          text(card,"h4",m.title);text(card,"p",`${m.issuer}｜${m.edition}｜登録済み。本番書誌を今回照合しました。最新版の再調査は未実施。`);
          const link=text(card,"a","登録済みの公式資料");link.href=m.official_url;link.target="_blank";link.rel="noopener noreferrer";
        }
        text(card,"p","本番登録・定期巡回の有効化は行っていません。");
      }
      for(const m of r.documents||[]) {
        text(card,"h4",m.title);text(card,"p",`${m.issuer}｜${m.edition}｜${m.revision_year}年`);
        if(r.kind==='guideline_discovery') {
          const row=r.discovery.documents.find(v=>v.id===m.id);
          const versions={latest_listed:'公式一覧に掲載の最新候補',current_published:'現行刊行版',published_with_draft_pending:'刊行版（後継草案あり）',partial_update:'部分改訂'};
          if(row)text(card,"p",`照合：${duplicateSummary({[row.status]:1})}。対象疾患：${m.diseases.join('・')}。版の確認：${versions[row.version_status]||'要確認'}。`);
        }
        const link=text(card,"a","公式資料");link.href=m.official_url;link.target="_blank";link.rel="noopener noreferrer";
      }
      text(card,"p",`変更${r.change_count}件。重複判定：${duplicateSummary(r.duplicate_check)}`);
      details(card,"変更差分",r.diff||"登録前の確認が必要です。");
      if(r.verification)details(card,"検索検証結果",r.verification);
    }
    if(result.saved_plan)details(card,"保存済み本番反映の変更・検証情報",result.saved_plan);
    if (result.artifact) {
      byId("work-context").textContent = "この会話の成果物を選択中";
      try {
        const url = new URL(result.artifact.preview_url);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw Error("Unsafe URL");
        const link = text(card, "a", "開く"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
      } catch { text(card, "p", "成果物を開くリンクはまだ利用できません。"); }
    }
    if (result.host_run_id && result.status === "RUNNING") {
      text(card,"p",`現在の工程：${result.stage}。LLM呼出し：${result.llm_calls}/24回。`);
      details(card,"Host実行run ID",result.host_run_id);
    }
    if (result.status === "RUNNING") {
      const button = text(card, "button", "中止"); button.type = "button";
      button.onclick = async () => { try { render(await api(`/api/work/missions/${result.run_id}/cancel`, {method: "POST"})); }
        catch (error) { byId("error").textContent = "中止の受付を確認できません。状態を確認してから再操作してください。"; details(card, "技術詳細（通信）", error.message); } };
    }
    if (result.approval) {
      const a = result.approval, plan = a.plan;
      text(card, "h3", "本番反映の承認待ち");
      for (const m of plan.documents) {
        text(card, "h4", m.title); text(card, "p", `${m.issuer}｜${m.edition}｜${m.revision_year}年`);
        const link=text(card,"a","公式資料");link.href=m.official_url;link.target="_blank";link.rel="noopener noreferrer";
        details(card,"追加する書誌メタデータ",m);
      }
      text(card,"p",`変更${plan.change_count}ファイル・DB変更${plan.database_changes}件。重複判定：${duplicateSummary(plan.duplicate_check)}`);
      text(card,"pre",Object.keys(plan.files).join("\n"));
      if(plan.kind === "search_quality") {
        text(card,"p",`既知解の支援付き実装。使用モデル：${plan.model}。未知問題の自律改善は未実証です。`);
        details(card,"Host実行run ID",plan.host_run_id);
        const summary=plan.search_summary;
        text(card,"p",`対象検索：${summary.before_after.query}。ガイドライン候補 ${summary.before_after.before_ids.length} → ${summary.before_after.after_ids.length}件。`);
        details(card,"Before / Afterと独立検証",summary);
        details(card,"変更差分",plan.diff);
        if(plan.publication_block_reason)text(card,"p",plan.publication_block_reason);
      }
      text(card,"p",`隔離検証：${plan.isolated_verification.passed ? "PASS" : "FAIL"}（${plan.isolated_verification.checks}項目）`);
      text(card,"p",`バックアップ：${plan.backup.status}`);text(card,"p",`ロールバック：${plan.rollback}`);
      text(card,"p","承認する変更のSHA-256");text(card,"code",a.sha256);
      const label=text(card,"label","内容を確認し、本番反映を承認する");
      const checkbox=document.createElement("input");checkbox.type="checkbox";label.prepend(checkbox);
      const input=document.createElement("input");input.type="text";input.placeholder="上のSHA-256を入力";input.autocomplete="off";card.append(input);
      const button=text(card,"button","本番反映を承認して実行");button.type="button";button.disabled=true;
      const validate=()=>{button.disabled=!a.execution_enabled||!checkbox.checked||input.value.trim()!==a.sha256;};
      checkbox.onchange=validate;input.oninput=validate;
      if(!a.execution_enabled)text(card,"p","本番実行権限は未付与です。変更内容を確認後、管理者による明示承認を待っています。");
      button.onclick=async()=>{
        button.disabled=true;
        try {
          const next=await api(`/api/work/missions/${result.run_id}/approve`,{method:"POST",headers:{"Content-Type":"application/json","X-Pitchi-Human-Approval":a.challenge},body:JSON.stringify({sha256:a.sha256,challenge:a.challenge,confirmation:"本番反映を承認 "+a.sha256})});
          render(next);clearTimeout(timer);++generation;await poll(generation,[result.run_id]);
        } catch(error){text(card,"p","承認の受付を確認できません。再送前に作業状態を確認してください。");details(card,"承認エラー",error.message);}
      };
    }
    if (result.status === "NEEDS_HUMAN" && !result.approval) {
      text(card, "p", "操作『追加指示・人間回答』から、判断や追加の指示を送ってください。回答後も安全確認を行います。");
      // Only explicit question/choices, never an internal reason/error as prose.
      if (human.question) text(card, "p", human.question);
      for (const choice of human.choices || []) {
        const button = text(card, "button", choice); button.type = "button";
        button.onclick = () => {
          byId("work-operation").value = "continue"; byId("message").value = choice;
          byId("message").focus(); // Explicit send still required; never auto-approve.
        };
      }
    }
    if (result.status === "NEEDS_CAPABILITY") {
      const advice = result.capability_advice;
      if (advice) {
        text(card, "p", "機能の追加を依頼するか、暫定的な対応案を確認できます。提案やコマンドは自動実行しません。");
        details(card, "Codexで機能を追加する", advice.options?.codex?.request || "依頼文は保存されていません。");
        const manual = advice.options?.manual;
        const section = details(card, "暫定的な方法を見る", manual || "暫定案は保存されていません。");
        text(section, "p", "提案です。実行前に内容をご確認ください。コマンドの検証状況は上記の記録に示しています。");
        const stop = text(card, "button", "今回はやめる"); stop.type = "button";
        stop.onclick = () => {
          for (const item of card.querySelectorAll('details')) item.open = false;
          stop.disabled = true; stop.textContent = "提案を閉じました（自動実行なし）";
        };
      } else text(card, "p", "詳しい対応案は保存されていません。");
    }
    const original = {...result}; delete original.human_explanation;
    details(card, "技術詳細", original);
    for (const node of card.querySelectorAll('details'))
      node.open = openDetails.has(node.querySelector('summary').textContent);
  }
  async function poll(token, ids) {
    if (token !== generation) return;
    const active = [];
    for (const id of ids) {
      try {
        const result = await api(`/api/work/missions/${id}`);
        if (token !== generation) return;
        render(result);
        if (result.status === "RUNNING") active.push(id);
        else if (!explained.has(id)) {
          explained.add(id);
          api(`/api/work/missions/${id}/explanation`).then(value => {
            if (token === generation && value.status === result.status)
              render({...result, human_explanation: value.human_explanation});
          }).catch(() => {}); // The template is already visible; never block results.
        }
      } catch (error) { byId("error").textContent = "進行状況を取得できません。接続を確認して再度お試しください。"; active.push(id); }
    }
    if (active.length) timer = setTimeout(() => poll(token, active), 2000);
  }
  async function restore() {
    clearTimeout(timer); ++generation; cards.clear(); explained.clear();
    if (!conversationId) return;
    const data = await api(`/api/work/conversations/${conversationId}`);
    byId("work-context").textContent = data.context.artifact_id ? "この会話の成果物を選択中" : "新規依頼";
    await poll(generation, data.runs);
  }
  async function submit(message) {
    if (!enabled || isBusy || !message.trim()) return;
    busy(true); byId("error").textContent = "";
    try {
      const result = await api("/api/work/missions", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({message, conversation_id: conversationId, project: byId("work-project").value,
          operation: byId("work-operation").value})});
      conversationId = result.conversation_id; addMessage("user", message); byId("message").value = "";
      if (result.kind) {
        const card = document.createElement("article"); card.className = "message assistant";
        card.dataset.subjectRunId = result.subject_run_id || "";
        text(card, "p", result.human_explanation.message);
        if (result.kind === "advice" && result.source_result?.capability_advice) {
          details(card, "Codexで機能を追加する", result.source_result.capability_advice.options?.codex?.request);
          details(card, "暫定的な方法を見る", result.source_result.capability_advice.options?.manual);
        }
        if(result.source_result?.tool_task)render({...result.source_result,human_explanation:result.human_explanation});
        details(card, "技術詳細", result); byId("messages").append(card);
        if (result.kind === "advisor") {
          const token = generation;
          const check = async () => {
            if (token !== generation) return;
            try {
              const value = await api(`/api/work/missions/${result.subject_run_id}/advice/${result.advisor.advisor_run_id}`);
              if (token !== generation) return;
              if (value.status === "RUNNING") { setTimeout(check, 2000); return; }
              text(card, "p", value.capability_advice ? "元の仕事について対応案を用意しました。提案は自動実行しません。" : "対応案の分析を完了できませんでした。元の仕事の失敗理由とは別の問題です。");
              if (value.capability_advice) {
                details(card, "Codexで機能を追加する", value.capability_advice.options?.codex?.request);
                details(card, "暫定的な方法を見る", value.capability_advice.options?.manual);
              }
              details(card, "技術詳細（分析）", value);
            } catch { text(card, "p", "分析状態を確認できません。元の仕事は変更していません。"); }
          }; setTimeout(check, 2000);
        }
        await refreshHistory(); return;
      }
      render(result); await refreshHistory(); clearTimeout(timer); ++generation;
      await poll(generation, [result.run_id]);
    } catch (error) { byId("error").textContent = "依頼の受付を確認できません。再送する前に進行状況をご確認ください。";
      const card = document.createElement("article"); byId("messages").append(card); details(card, "技術詳細（受付）", error.message); }
    finally { busy(false); }
  }
  byId("chat-mode").addEventListener("change", () => {
    byId("work-controls").hidden = byId("chat-mode").value !== "work"; busy(isBusy);
  });
  byId("new-chat").addEventListener("click", () => { clearTimeout(timer); ++generation; cards.clear(); explained.clear(); byId("work-context").textContent = "新規依頼"; });
  api("/api/work/catalog").then(data => {
    enabled = data.enabled;
    const labels={"ptotsearch-guidelines":"ガイドライン登録・登録状態確認","ptotsearch-quality":"検索品質改善・成果確認","ptotsearch-national":"全国カバレッジ診断・進捗確認"};
    byId("work-project").replaceChildren(...data.projects.map(id => new Option(labels[id]||id, id)));
    byId("work-context").textContent = enabled ? "新規依頼 / モデル選択・検証はPitchi-workが担当" : "Work接続は無効です";
    busy(isBusy);
  }).catch(error => { byId("work-context").textContent = error.code === "PITCHI_WORK_SOURCE_REAPPROVAL_REQUIRED"
    ? "新しいPitchi-workソースの再承認が必要です。Host管理者に再承認を依頼してください。"
    : "仕事の受付に接続できません。"; });
  return {submit, render, restore, available: () => enabled,
    offline: () => { clearTimeout(timer); ++generation; cards.clear(); explained.clear(); byId("work-context").textContent = "HOSTへの再接続を待機中"; }};
})();
