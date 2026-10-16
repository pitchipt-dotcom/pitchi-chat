// Read-only "observe", or explicit opt-in submission of ONE real Chat mission.
// No fixtures/LLM substitutes. Disposable browser; output must stay private.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const origin = process.argv[2];
const request = process.argv[3];
const output = process.argv[4];
const conversation = process.argv[5] || '';
const operation = process.argv[6] || 'create';
const project = process.argv[7] || '';
if(project && !['ptotsearch-quality','ptotsearch-guidelines'].includes(project)) throw Error('Unreviewed project');
if (!origin || !request || !output) throw Error('Usage: node probe_work_chat.cjs CHAT_URL REQUEST PRIVATE_OUTPUT [CONVERSATION_ID] [OPERATION]');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'pitchi-chat-live-'));
const chrome = spawn('/usr/bin/google-chrome', ['--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
  '--disable-background-networking', '--disable-sync', '--disable-extensions', '--no-first-run',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
const pause = ms => new Promise(r => setTimeout(r, ms));
let socket;
const errors = [];
async function main() {
  let port;
  for (let n = 0; n < 100; n++) { try {port = Number(fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]); break;} catch {} await pause(100); }
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((yes, no) => {socket.addEventListener('open', yes, {once: true}); socket.addEventListener('error', no, {once: true});});
  let sequence = 0; const pending = new Map();
  socket.addEventListener('message', event => {
    const value = JSON.parse(event.data);
    if (value.method === 'Runtime.exceptionThrown') errors.push(value.params.exceptionDetails.text);
    if (pending.has(value.id)) { const [yes, no] = pending.get(value.id); pending.delete(value.id); value.error ? no(Error(value.error.message)) : yes(value.result); }
  });
  const call = (method, params = {}) => new Promise((yes, no) => {const id = ++sequence; pending.set(id, [yes, no]); socket.send(JSON.stringify({id, method, params}));});
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || 'Browser evaluation error');
    return result.result.value;
  };
  await call('Runtime.enable'); await call('Page.enable'); await call('Page.navigate', {url: origin});
  for (let n = 0; n < 100; n++) { if (await evaluate('!!window.pitchiWork && window.pitchiWork.available()')) break; await pause(200); }
  const previous = await evaluate(`(async()=>{
    if(${JSON.stringify(conversation)}) { conversationId=${JSON.stringify(conversation)}; await window.pitchiWork.restore(); }
    const ids=[...document.querySelectorAll('[data-run-id]')].map(c=>c.dataset.runId);
    document.getElementById('chat-mode').value='work'; document.getElementById('chat-mode').dispatchEvent(new Event('change'));
    if(${JSON.stringify(operation)}==='observe') return [];
    if(${JSON.stringify(project)}) document.getElementById('work-project').value=${JSON.stringify(project)};
    document.getElementById('work-operation').value=${JSON.stringify(['explain','advisor'].includes(operation) ? 'create' : operation)};
    document.getElementById('message').value=${JSON.stringify(request)};
    document.getElementById('chat-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
    return ids;
  })()`);
  let snapshot;
  if (['explain','advisor'].includes(operation)) {
    for (let n = 0; n < 100; n++) {
      snapshot = await evaluate(`(()=>{const c=[...document.querySelectorAll('[data-subject-run-id]')].at(-1);
        return {subject_run_id:c?.dataset.subjectRunId,text:c?.innerText,
          error:document.getElementById('error').textContent,
          run_ids:[...document.querySelectorAll('[data-run-id]')].map(c=>c.dataset.runId)};})()`);
      if(snapshot.subject_run_id || snapshot.error) break;
      await pause(200);
    }
    if(snapshot.error || !snapshot.subject_run_id) throw Error('Follow-up explanation was not displayed');
    snapshot.before_run_ids=previous;
    snapshot.no_new_run=JSON.stringify(previous)===JSON.stringify(snapshot.run_ids);
    if(!snapshot.no_new_run) throw Error('Explanation created a mission');
    if (operation === 'advisor') {
      const receipt = await evaluate(`JSON.parse([...document.querySelectorAll('[data-subject-run-id]')].at(-1).querySelector('details pre').textContent)`);
      snapshot.advisor_run_id = receipt.advisor?.advisor_run_id;
      if (!snapshot.advisor_run_id) throw Error('Explicit Advisor receipt missing');
      for (let n=0;n<400;n++) {
        snapshot.advisor = await evaluate(`(async()=>await (await fetch('/api/work/missions/${snapshot.subject_run_id}/advice/${snapshot.advisor_run_id}',{cache:'no-store'})).json())()`);
        if(snapshot.advisor.status !== 'RUNNING') break;
        await pause(2000);
      }
      if(snapshot.advisor.subject_run_id !== snapshot.subject_run_id) throw Error('Advisor subject mismatch');
    }
    fs.writeFileSync(output,JSON.stringify(snapshot,null,2),{mode:0o600});
    console.log(JSON.stringify({subject_run_id:snapshot.subject_run_id,no_new_run:snapshot.no_new_run}));
    return;
  }
  for (let n = 0; n < 1200; n++) {
    snapshot = await evaluate(`(()=>{const cards=[...document.querySelectorAll('[data-run-id]')];const c=cards.at(-1);
      const tech=[...(c?.querySelectorAll('details')||[])].find(d=>d.querySelector('summary')?.textContent==='技術詳細');
      let artifact;try{artifact=JSON.parse(tech?.querySelector('pre')?.textContent||'{}').artifact;}catch{}
      return {conversation_id:conversationId,run_id:c?.dataset.runId,status:c?.dataset.status,
        text:c?.innerText,preview_url:artifact?.preview_url,error:document.getElementById('error').textContent};})()`);
    fs.writeFileSync(output, JSON.stringify({...snapshot, browser_errors: errors}, null, 2), {mode: 0o600});
    if (snapshot.error) throw Error(snapshot.error);
    if (snapshot.run_id && !previous.includes(snapshot.run_id) && snapshot.status !== 'RUNNING') break;
    if (n % 10 === 0) console.log(JSON.stringify({run_id: snapshot.run_id, status: snapshot.status}));
    await pause(2000);
  }
  if (!snapshot.run_id || snapshot.status === 'RUNNING') throw Error('Live mission timeout; recorded run continues independently');
  if (operation === 'observe') {
    snapshot.presentation_checks = await evaluate(`(async()=>{
      const checks=[];
      for(const card of document.querySelectorAll('[data-run-id]')) {
        const result=await (await fetch('/api/work/missions/'+card.dataset.runId,{cache:'no-store'})).json();
        const normal=[...card.children].filter(e=>e.tagName!=='DETAILS').map(e=>e.innerText).join('\\n');
        const detail=[...card.querySelectorAll('details')].find(e=>e.querySelector('summary').textContent==='技術詳細');
        const raw=JSON.parse(detail.querySelector('pre').textContent);
        delete result.human_explanation;
        checks.push({run_id:card.dataset.runId,status:card.dataset.status,
          normal_has_no_raw:![card.dataset.runId,result.failure_class,'"validator"','"checks"','E_ENV','E_STOP'].filter(Boolean).some(s=>normal.includes(s)),
          technical_closed:!detail.open,technical_lossless:JSON.stringify(raw)===JSON.stringify(result),normal});
        detail.querySelector('summary').click();
        checks.at(-1).technical_can_open=detail.open;
        detail.querySelector('summary').click();
      }
      return checks;
    })()`);
    if (snapshot.presentation_checks.some(c=>!c.normal_has_no_raw||!c.technical_closed||!c.technical_lossless||!c.technical_can_open))
      throw Error('Human/technical presentation boundary failed');
  }
  if (operation === 'observe') {
    snapshot.search_quality_checks = await evaluate(`(()=>{
      return [...document.querySelectorAll('[data-run-id]')].flatMap(card=>{
        const tech=[...card.querySelectorAll('details')].find(d=>d.querySelector('summary').textContent==='技術詳細');
        const result=JSON.parse(tech.querySelector('pre').textContent);
        const p=result.approval?.plan;if(p?.kind!=='search_quality')return [];
        const titles=[...card.querySelectorAll('details summary')].map(x=>x.textContent);
        return [{host_run_id_present:!!p.host_run_id,before_after_visible:titles.includes('Before / Afterと独立検証'),
          diff_visible:titles.includes('変更差分'),protected_tests_pass:p.search_summary.checks.filter(x=>x.name.startsWith('protected_test:')).length===5 && p.search_summary.checks.every(x=>x.pass),
          current_source_drift_blocked:p.publication_eligible===false && !result.approval.execution_enabled && card.innerText.includes(p.publication_block_reason),
          llm_calls:result.llm_calls,classification:result.classification}];
      });
    })()`);
    if(snapshot.search_quality_checks.some(c=>!c.host_run_id_present||!c.before_after_visible||!c.diff_visible||!c.protected_tests_pass||!c.current_source_drift_blocked))
      throw Error('Search capability display/approval boundary failed');
  }
  if (operation === 'observe' && snapshot.status === 'NEEDS_HUMAN') {
    snapshot.approval_checks = await evaluate(`(()=>{
      const card=[...document.querySelectorAll('[data-run-id]')].at(-1);
      return {official_links:[...card.querySelectorAll('a')].map(a=>a.href),
        checkbox:!!card.querySelector('input[type=checkbox]'),
        hash_input:!!card.querySelector('input[type=text]'),
        buttons:[...card.querySelectorAll('button')].map(b=>({text:b.textContent,disabled:b.disabled}))};})()`);
    if (!snapshot.approval_checks.checkbox || !snapshot.approval_checks.hash_input ||
        !snapshot.approval_checks.buttons.some(b=>b.text.includes('本番反映') && b.disabled))
      throw Error('Approval controls did not remain safely disabled');
  }
  if (snapshot.status === 'NEEDS_CAPABILITY') {
    snapshot.advisor_display = await evaluate(`(()=>{
      const card=[...document.querySelectorAll('[data-run-id]')].at(-1);
      const items=[...card.querySelectorAll('details')];
      const codex=items.find(e=>e.querySelector('summary').textContent.includes('Codex'));
      const manual=items.find(e=>e.querySelector('summary').textContent.includes('暫定'));
      codex?.querySelector('summary').click(); manual?.querySelector('summary').click();
      return {codex_visible:!!codex?.open,codex_request:codex?.querySelector('pre').textContent,
        manual_visible:!!manual?.open,manual:manual?.querySelector('pre').textContent};})()`);
  }
  if (snapshot.preview_url) {
    const response = await fetch(snapshot.preview_url);
    snapshot.preview_http_status = response.status;
    snapshot.preview_title = (await response.text()).match(/<title>([^<]*)<\/title>/)?.[1];
  }
  fs.writeFileSync(output, JSON.stringify({...snapshot, browser_errors: errors}, null, 2), {mode: 0o600});
  console.log(JSON.stringify({run_id: snapshot.run_id, status: snapshot.status, preview_http_status: snapshot.preview_http_status, browser_errors: errors}));
}
main().catch(error => {console.error(error.message); process.exitCode = 1;}).finally(async()=>{
  socket?.close(); chrome.kill('SIGTERM');
  await Promise.race([new Promise(r=>chrome.once('exit',r)),pause(3000)]);
  if (chrome.exitCode === null) chrome.kill('SIGKILL');
  fs.rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});
});
