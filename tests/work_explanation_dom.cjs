// Presentation-only DOM regression; input can be an actual saved Work result.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const result = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; this.dataset = {}; this.open = false; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
  querySelectorAll(tag) { return this.children.flatMap(n => [...(n.tag === tag ? [n] : []), ...n.querySelectorAll(tag)]); }
  querySelector(tag) { return this.querySelectorAll(tag)[0]; }
}
const elements = new Map();
const byId = id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); };
const context = vm.createContext({window: {}, document: {createElement: tag => new Element(tag)}, byId,
  api: async () => ({enabled: false, projects: []}), URL, Option: function(){},
  setTimeout, clearTimeout, conversationId: null, isBusy: false, busy() {}});
vm.runInContext(fs.readFileSync('frontend/work.js', 'utf8'), context);
context.window.pitchiWork.render(result);
const card = byId('messages').children[0];
function visible(n) { return n.tag === 'details' ? '' : n.textContent + n.children.map(visible).join('\n'); }
const normal = visible(card);
for (const forbidden of [result.run_id, result.failure_class, '"checks"', '"validator"', '"missing_capabilities"', 'E_ENV', 'E_STOP']) {
  if (forbidden) assert(!normal.includes(forbidden), 'Internal field appeared in normal presentation');
}
assert(normal.includes(result.human_explanation.message));
const technical = card.children.find(n => n.tag === 'details' && n.children[0].textContent === '技術詳細');
assert(technical && !technical.open, 'Technical detail must be collapsed');
const original = {...result}; delete original.human_explanation;
assert.deepEqual(JSON.parse(technical.children[1].textContent), original);
if (result.status === 'NEEDS_CAPABILITY' && result.capability_advice) {
  const codex = card.children.find(n => n.tag === 'details' && n.children[0].textContent === 'Codexで機能を追加する');
  assert(codex && !codex.open);
  assert.equal(codex.children[1].textContent, result.capability_advice.options.codex.request);
  const manual = card.children.find(n => n.tag === 'details' && n.children[0].textContent === '暫定的な方法を見る');
  assert(manual && !manual.open);
  assert(!normal.includes(result.capability_advice.options.codex.request));
}
assert.equal(card.dataset.status, result.status);
if(result.result_details?.kind === 'national_coverage') {
  const info=result.result_details;
  assert(normal.includes('全国カバレッジの診断結果'));
  assert(normal.includes(info.collection.updated_at));
  assert(card.querySelectorAll('details').some(n=>n.children[0].textContent==='収集・未取得・retry・索引'));
  assert(card.querySelectorAll('details').some(n=>n.children[0].textContent==='既存データの保持検証'));
  assert(card.querySelectorAll('details').some(n=>n.children[0].textContent==='実際のログ（直近の限定範囲）'));
  assert(!normal.includes(info.log_evidence.text));
  assert(!card.querySelectorAll('button').some(n=>n.textContent==='本番反映を承認して実行'));
} else if(result.result_details) {
  const info=result.result_details;
  assert(normal.includes(`変更${info.change_count}件`));
  for(const m of info.documents) {
    assert(normal.includes(m.title));
    assert(card.querySelectorAll('a').some(n=>n.href===m.official_url));
  }
  assert(card.querySelectorAll('details').some(n=>n.children[0].textContent==='検索検証結果'));
  assert(!card.querySelectorAll('button').some(n=>n.textContent==='本番反映を承認して実行'));
  assert(!normal.includes('[object Object]'));
  if(info.kind === 'guideline_discovery') {
    const d=info.discovery;
    assert(normal.includes('依頼条件に対するガイドライン探索結果'));
    assert(normal.includes(`未登録 ${d.objective.unregistered_count}件`));
    if(!d.objective.goal_met)assert(normal.includes('依頼目的：未達・確認が必要'));
    for(const source of d.searched_sources)assert(normal.includes(source.index_url));
    for(const group of d.conditions.disease_groups)for(const term of group)assert(normal.includes(term));
    assert(normal.includes('本番登録・定期巡回の有効化は行っていません'));
  }
}
if(result.approval?.plan.kind==='search_quality') {
  assert(card.querySelectorAll('details').some(n=>n.children[0].textContent==='Before / Afterと独立検証'));
  assert(card.querySelectorAll('details').some(n=>n.children[0].textContent==='変更差分'));
  const publish=card.querySelectorAll('button').find(n=>n.textContent==='本番反映を承認して実行');
  assert(publish.disabled);
}
technical.open = true;
context.window.pitchiWork.render(result);
assert(card.children.find(n => n.tag === 'details' && n.children[0].textContent === '技術詳細').open,
       'A presentation refresh must not collapse a user-opened detail');
if(result.reused_result) {
  const refreshed={...result};delete refreshed.reused_result;
  context.window.pitchiWork.render(refreshed);
  assert(visible(card).includes('今回の新規Host実行・LLM呼出しは0'));
}
console.log('PASS human display, collapsed actions and lossless technical details');
