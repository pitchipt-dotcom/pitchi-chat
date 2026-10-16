// Synthetic DOM/protocol. This does not prove real FLUX editing or phone use.
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const elements = new Map(), timers = [], requests = [];
class Element {
  constructor() { this.value = ''; this.textContent = ''; this.children = []; this.events = {}; this.style = {}; this.hidden = true; }
  addEventListener(name, handler) { this.events[name] = handler; }
  append(child) { this.children.push(child); if (child.id) elements.set(child.id, child); }
  add(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  removeAttribute(name) { delete this[name]; }
}
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const cid = '11111111-1111-4111-8111-111111111111', original = '22222222-2222-4222-8222-222222222222', generated = '33333333-3333-4333-8333-333333333333';
let conversation = cid, complete = false, queued = false, preview = false;
const asset = (id, kind, parent) => ({id, source_kind: kind, parent_id: parent, created_at: 'synthetic time', url: `/api/images/attachments/${id}/content`});
const sandbox = {document: {getElementById: id => elements.get(id) || (id === 'chat-image-timeline' ? null : get(id)), createElement: () => new Element()},
  Option: function(text, value) { this.text = text; this.value = value; },
  setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: () => {}, console};
vm.runInNewContext(fs.readFileSync('frontend/chat-images.js', 'utf8'), sandbox);
const api = async (path, options) => {
  requests.push({path, options});
  if (path.endsWith('/images')) return {images: [asset(original, 'upload'), ...(complete ? [asset(generated, 'generated', original)] : [])], jobs: queued ? [{job_id:'edit-job', status:'RUNNING', request:{prompt:'背景を変えて', prompt_mode:'direct'}}] : []};
  if (path === '/api/media/jobs') { preview = JSON.parse(options.body).preview_only; return {job_id:'edit-job', status:'QUEUED'}; }
  if (path === '/api/jobs/edit-job') return {status: 'SUCCESS'};
  if (path === '/api/jobs/edit-job/result') return {image_spec:{positive_prompt:'Keep the subject, brighten the image.', reference_image:{id:original}}, ...(preview ? {} : {tool_result:{artifacts:[]}})};
  if (path.endsWith('/render')) { complete = true; return {job_id:'edit-job',status:'QUEUED'}; }
  throw new Error(path);
};
(async () => {
  const ui = sandbox.PitchImages({api, getConversation:()=>conversation, setConversation:id=>{conversation=id;}, refreshHistory:async()=>{}, isBusy:()=>false});
  get('chat-image-mode').value = 'chat'; await ui.load(cid);
  assert.equal(get('chat-image-timeline').children[0].children[0].textContent.startsWith('元画像'), true);
  get('chat-image-reference').value=original; await get('chat-image-reference').events.change();
  get('message').value='背景を青に。人物はそのまま。'; get('chat-image-mode').value='direct';
  let blocked=false;
  await get('chat-form').events.submit({preventDefault(){}, stopImmediatePropagation(){blocked=true;}});
  const direct=JSON.parse(requests.find(x=>x.path==='/api/media/jobs').options.body);
  assert.equal(blocked,true); assert.equal(direct.prompt,'背景を青に。人物はそのまま。'); assert.equal(direct.reference_image_id,original); assert.equal(direct.preview_only,false);
  complete=true; await timers.pop()();
  assert.equal(get('chat-image-reference').value,original,'do not auto-select output');
  const output=get('chat-image-timeline').children[1]; output.children[2].events.click();
  assert.equal(get('chat-image-reference').value,generated,'generated image can be selected');
  get('chat-image-mode').value='strata'; await get('chat-form').events.submit({preventDefault(){},stopImmediatePropagation(){}});
  assert.equal(JSON.parse(requests.filter(x=>x.path==='/api/media/jobs').at(-1).options.body).preview_only,true);
  await timers.pop()(); assert.equal(get('chat-image-plan').hidden,false);
  get('chat-image-prompt').value='Human verified editing instruction.'; await get('chat-image-render').events.click();
  assert.equal(JSON.parse(requests.find(x=>x.path.endsWith('/render')).options.body).positive_prompt,'Human verified editing instruction.');
  // Reconnect discovers a server-held job through GET only, without generation replay.
  queued=true; const before=requests.filter(x=>x.options?.method==='POST').length;
  await ui.load(cid); assert.equal(requests.filter(x=>x.options?.method==='POST').length,before);
  conversation=null; await ui.load(null); assert.equal(get('chat-image-plan').hidden,true);
  get('chat-image-mode').value='vision'; await get('chat-form').events.submit({preventDefault(){},stopImmediatePropagation(){}});
  assert.match(get('chat-image-status').textContent,/未接続/);
  console.log('Chat image upload/edit/reference/reconnect DOM checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
