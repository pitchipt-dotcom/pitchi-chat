"use strict";
// Host IDs only. No paths, prompts, media or deletion requests in browser storage.
globalThis.PitchDeletions = function ({api, onDeleted, isBusy}) {
  const el = id => document.getElementById(id);
  let items = [], selected = new Set(), multiple = false, pending = null, busy = false, epoch = 0, operations = [];
  const key = x => `${x.kind}:${x.id}`;
  const status = text => { el('deletion-status').textContent = text; };
  function button(parent, text, action) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = text;
    b.addEventListener('click', action); parent.append(b); return b;
  }
  function resultText(result) {
    return `${result.status === 'COMPLETED' ? 'Host内の削除が完了しました。' : '完全削除は未完了です。残存ファイル・メタデータはアクセスを遮断して保持し、再試行できます。'}\n` +
      `ファイル削除確認: ${result.files_removed || 0}件 / 残存: ${result.remaining_files || 0}件\n` +
      (result.metadata_cleanup_pending ? '関連メタデータの削除が未完了です。再試行してください。\n' : '') +
      (result.sqlite_cleanup_pending ? 'SQLiteの履歴ページ整理が未完了です。再試行してください。\n' : '') +
      (result.failures || []).map(x => `${x.location || ''}: ${x.reason}${x.owners ? ' / '+x.owners.map(o => o.id.slice(0,8)).join(', ') : ''}`).join('\n') + '\n' +
      (result.retained || []).map(x => x.reason).join('\n') + '\n対象外のコピー:\n' + (result.exclusions || []).join('\n');
  }
  function render(data) {
    items = data.items || [];
    operations = data.pending || operations;
    selected = new Set([...selected].filter(k => items.some(x => key(x) === k)));
    const area = el('deletion-items'); area.replaceChildren();
    const filter = el('deletion-filter').value || 'all';
    for (const item of items.filter(x => filter === 'all' || (filter === 'conversation' ? x.kind === filter : x.kind !== 'conversation' && (x.kind === filter || x.media_type === filter)))) {
      const row = document.createElement('article'); row.className = 'deletion-item';
      const box = document.createElement('input'); box.type = 'checkbox'; box.hidden = !multiple;
      box.checked = selected.has(key(item)); box.setAttribute('aria-label', `${item.label}を選択`);
      box.addEventListener('change', () => { box.checked ? selected.add(key(item)) : selected.delete(key(item)); });
      const label = document.createElement('label'); label.append(box, document.createTextNode(
        `${item.kind === 'conversation' ? '会話' : item.kind === 'image' ? '画像' : '成果物'}: ${item.label} / ${item.created_at} / ${item.id.slice(0,8)}`));
      row.append(label);
      const menu = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = '︙'; summary.setAttribute('aria-label','履歴の操作'); menu.append(summary);
      if(item.kind==='long_video' && item.final_job_id)button(menu,'完成動画のみ削除',() => preview([{kind:'job',id:item.final_job_id}]));
      button(menu,item.kind==='long_video'?'完成動画＋専用中間クリップを削除':'履歴と元ファイルを削除',() => preview([item])); row.append(menu); area.append(row);
    }
    const retries = el('deletion-retries'); retries.replaceChildren();
    for (const operation of operations) {
      const p = document.createElement('p'); p.textContent = `完全削除待ち ${operation.operation_id.slice(0,8)} / ${operation.status}`; retries.append(p);
      button(retries,'この削除を再試行',() => { pending = operation; el('deletion-scope').textContent = resultText(operation);
        el('deletion-confirmation').hidden = false; el('deletion-check').checked = false; });
    }
  }
  async function load() {
    const stamp = ++epoch;
    const data = await api('/api/deletions/catalog');
    if (stamp !== epoch) return;
    render(data);
  }
  async function open() {
    pending = null; el('deletion-check').checked = false; el('deletion-confirmation').hidden = true;
    el('deletion-dialog').showModal(); status('削除対象を確認中…');
    try { await load(); status('削除範囲を確認してから実行してください。'); }
    catch (error) { status(error.message); }
  }
  async function preview(targets) {
    if (busy || isBusy()) { status('実行中の処理の完了を待ってください。'); return; }
    busy = true;
    try {
      pending = await api('/api/deletions/preview',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({targets:targets.map(({kind,id}) => ({kind,id}))})});
      const s = pending.scope;
      el('deletion-scope').textContent = `${pending.warning}\n会話${s.conversations}件・画像${s.images}件・ジョブ${s.jobs}件・Hostファイル${s.files}件\n` +
        '会話を選択すると、その会話の添付画像・生成物・最終フレーム・会話本文も削除します。個別成果物の削除では会話本文（画像理解の文章を含む）は残ります。会話に紐付かない旧成果物は個別選択してください。\n' +
        (pending.unsafe_files ? `安全に削除できないファイル${pending.unsafe_files}件があります。部分削除になる場合があります。\n` : '') +
        (pending.retained || []).map(x => x.reason).join('\n') + '\n対象外のコピー:\n' + pending.exclusions.join('\n');
      el('deletion-check').checked = false; el('deletion-confirmation').hidden = false;
      el('deletion-confirmation').scrollIntoView?.({block:'nearest'});
      status('元ファイルも削除されることを確認してください。');
    } catch (error) { status(error.message); }
    finally { busy = false; }
  }
  el('deletion-filter').addEventListener('change',() => render({items}));
  el('deletion-open').addEventListener('click',open);
  el('deletion-close').addEventListener('click',() => { if (!busy) { ++epoch; el('deletion-dialog').close(); } });
  el('deletion-multiple').addEventListener('click',() => { multiple = !multiple;
    el('deletion-multiple').textContent = multiple ? '複数選択を終了' : '複数選択';
    el('deletion-selected').hidden = !multiple; selected.clear(); render({items}); });
  el('deletion-selected').addEventListener('click',() => {
    const targets = items.filter(x => selected.has(key(x)));
    if (!targets.length) { status('削除する対象を選択してください。'); return; }
    void preview(targets);
  });
  el('deletion-cancel').addEventListener('click',() => { if (!busy) { pending = null; el('deletion-confirmation').hidden = true; } });
  el('deletion-execute').addEventListener('click',async () => {
    if (busy || !pending || isBusy()) return;
    if (!el('deletion-check').checked) { status('「元ファイルも削除する」を確認してください。'); return; }
    busy = true; el('deletion-execute').disabled = true;
    try {
      const result = await api(`/api/deletions/${pending.operation_id}/execute`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_original_files:true})});
      el('deletion-confirmation').hidden = true; pending = null; selected.clear();
      await onDeleted(result); await load(); status(resultText(result));
    } catch (error) {
      // Ambiguous completion never causes automatic retry. Catalog exposes saved state.
      status(`${error.message}\n再確認で削除状態を確認してください。自動再送はしません。`);
    } finally { busy = false; el('deletion-execute').disabled = false; }
  });
  el('deletion-refresh').addEventListener('click',() => load().catch(error => status(error.message)));
  el('deletion-dialog').addEventListener('cancel',event => { if (busy) event.preventDefault(); });
  return {open, async item(kind,id) { await open(); await preview([{kind,id}]); },
    offline() { ++epoch; items = []; operations = []; selected.clear(); pending = null; el('deletion-items').replaceChildren();
      el('deletion-retries').replaceChildren(); el('deletion-scope').textContent = ''; el('deletion-confirmation').hidden = true; status('HOSTへ再接続後に再確認してください。'); }};
};
