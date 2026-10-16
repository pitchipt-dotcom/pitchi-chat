// Same-origin numeric telemetry and owned idle stop; no swap or shell controls.
(() => {
  const el = id => document.getElementById(id);
  globalThis.resourceFailureMessage = resource => {
    const causes = {insufficient_ram:'RAM不足',insufficient_swap_reserve:'未設定の低swap経路・Swap余裕不足',
      memory_pressure:'継続的なメモリ圧迫',insufficient_cgroup_ram:'cgroupメモリ余裕不足',
      memory_safety_telemetry_unavailable:'安全条件を計測できません',oom_observed:'OOMカウンター増加',
      allocation_failed:'RAM割り当て失敗',gpu_allocation_failed:'GPUメモリ割り当て失敗',
      profile_memory_budget_exceeded:'モデルのVRAM予算超過',insufficient_vram:'VRAM安全余裕不足',
      media_memory_shape_unreviewed:'未検証の同時生成枚数（1枚ずつの生成が必要です）'};
    const value = n => Number.isFinite(n) ? `${Math.round(n)} MiB` : '未計測';
    return `${causes[resource.reason] || 'リソース安全条件を満たしていません'} / RAM利用可能 ${value(resource.ram_available_mib)}（必要 ${value(resource.required_ram_mib)}） / Swap空き ${value(resource.swap_free_mib)}`
      + (Number.isFinite(resource.required_vram_mib) ? ` / モデルVRAM使用 ${value(resource.owned_vram_used_mib)}（予算 ${value(resource.required_vram_mib)}）` : '');
  };
  const show = data => {
    const value = n => Number.isFinite(n) ? `${Math.round(n)} MiB` : '未計測';
    el('memory-status').textContent = `RAM 利用可能 ${value(data.ram_available_mib)} / 使用 ${value(data.ram_used_mib)}\nSwap 使用 ${value(data.swap_used_mib)} / 空き ${value(data.swap_free_mib)}\nVRAM 使用 ${value(data.vram_used_mib)} / 空き ${value(data.vram_free_mib)}\n稼働モデル ${data.loaded_model || 'なし'}${data.loaded_backend ? ' / '+data.loaded_backend : ''} / ${data.state || '不明'}\n起動判定: ${data.ready_to_start ? '条件適合' : '見合わせ'} / 必要RAM ${value(data.required_ram_mib)}\n${data.message || (data.ready_to_start ? 'swap初期化・sudo操作は不要です。' : '状態を確認できません。')}`;
  };
  const fetchJSON = async (url, options={}) => {
    const response = await fetch(url, {...options, cache:'no-store', signal:AbortSignal.timeout(45000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail?.message || '安全条件を満たさないため操作を見合わせました。');
    return data;
  };
  const refresh = async () => {
    const target = el('memory-target').value;
    const backend = el('memory-backend').value;
    const data = await fetchJSON(`/api/resources/memory?target=${encodeURIComponent(target)}&backend=${encodeURIComponent(backend)}`);
    show(data); return data;
  };
  const run = async action => {
    const buttons = ['memory-refresh','memory-release','memory-stop','memory-recover'].map(el);
    buttons.forEach(button => button.disabled=true);
    try { await action(); } catch(error) { el('memory-status').textContent=error.message; }
    finally { buttons.forEach(button => button.disabled=false); }
  };
  el('memory-refresh').addEventListener('click',() => run(refresh));
  el('memory-target').addEventListener('change',() => run(refresh));
  el('memory-backend').addEventListener('change',() => run(refresh));
  const release = async (current, action='stop') => {
    const state=await refresh();
    const target=current ? state.loaded_model : el('memory-target').value;
    if (!['strata','comfyui'].includes(target)) {
      if (current && !state.loaded_model) { el('memory-status').textContent+='\n解放する管理対象モデルはありません。'; return; }
      throw new Error('このモデルは共通停止操作の管理対象ではありません。');
    }
    await fetchJSON('/api/resources/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target,action})});
    await refresh();
  };
  el('memory-release').addEventListener('click',() => run(() => release(true)));
  el('memory-stop').addEventListener('click',() => run(() => release(false)));
  el('memory-recover').addEventListener('click',() => run(() => release(false,'recover')));
})();
